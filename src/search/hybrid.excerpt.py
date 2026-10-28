# Excerpt from app/retrieval/hybrid.py ("# ..." marks omitted code)

"""Hybrid retrieval: the vector leg (k-NN over passages) and Reciprocal Rank Fusion.

Vector leg
----------
The query is embedded (``input_type="query"``) and searched with k-NN in the passage
index (``web_passages``). The request's domain and language filters are applied inside
the k-NN query (efficient filtering), so the k nearest passages are all eligible. A
document's vector score is its best passage's similarity; that passage is kept for the
snippet and for the chat tool.

Similarity is the inner product of L2-normalized vectors, i.e. cosine similarity in
[-1, 1]. The k-NN plugin (faiss, ``innerproduct``) reports it as
``_score = 1 + similarity`` for similarity >= 0 and ``1 / (1 - similarity)`` below 0
(``similarity_to_score``); on_disk mode reports the rescored full-precision value.
k-NN always returns neighbours, even for nonsense, so candidates below
``SEARCH_KNN_MIN_SIMILARITY`` are dropped: the k nearest are retrieved and a top-level
``min_score`` removes the weak ones server-side (radial search also works in on_disk
mode, but has no ``k`` bound).

The leg never fails a search: an embedding error or timeout
(``SEARCH_EMBED_TIMEOUT_SECONDS``), a missing passage index or a k-NN error makes it
unavailable, and the search continues with the lexical leg only. Query embeddings are
cached in process (LRU, keyed by the normalized query text).

Fusion
------
``fused(d) = sum over legs of w_leg / (k + rank_leg(d))`` (Reciprocal Rank Fusion,
``SEARCH_RRF_K``, ``SEARCH_LEXICAL_WEIGHT``, ``SEARCH_VECTOR_WEIGHT``), over the whole
candidate window of both legs, then divided by the best fused score so the final score
is in (0, 1]. The lexical rank is the position in the lexical leg's own ranking (BM25
plus the title, passage and freshness bonuses of ``ranking.rank_hits``), so those
features keep the effect they were tuned for. Ties break by the better lexical rank,
then the better vector rank, then document ID. Duplicates (identity URL, content hash)
are then removed keeping the higher-ranked copy.
"""

# ...


class VectorSearcher:
    # ... client, embedder, settings and an LRU of query embeddings

    async def search(self, text: str, filters: SearchFilters) -> VectorLeg:
        """Never raises: failures make the leg unavailable."""
        started = time.perf_counter()
        vector = self.cache.get(text)
        cached = vector is not None
        if vector is None:
            try:
                async with asyncio.timeout(self._settings.embed_timeout_seconds):
                    [vector] = await self._embedder.embed([text], "query")
            except TimeoutError:
                return self._unavailable("embedding_timeout", started)
            except Exception as exc:  # model server down, bad answer: never fail search
                return self._unavailable(f"embedding_failed:{type(exc).__name__}", started)
            self.cache.put(text, vector)
        embedded = time.perf_counter()
        body = build_knn_body(
            vector,
            filters,
            k=self._settings.knn_k,
            min_similarity=self._settings.knn_min_similarity,
            passages=self._passages,
            timeout_seconds=self._settings.timeout_seconds,
        )
        try:
            response: dict[str, Any] = await self._client.search(
                index=self._index,
                body=body,
                params={"request_timeout": self._settings.timeout_seconds + 1.0},
            )
        except NotFoundError:
            return self._unavailable("passage_index_missing", started)
        except Exception as exc:
            return self._unavailable(f"knn_failed:{type(exc).__name__}", started)
        matches, dropped = collapse(
            (response.get("hits") or {}).get("hits", []), self._settings.knn_min_similarity
        )
        done = time.perf_counter()
        return VectorLeg(
            matches=matches,
            embed_ms=(embedded - started) * 1000,
            search_ms=(done - embedded) * 1000,
            cached=cached,
            dropped=dropped,
        )

    def _unavailable(self, reason: str, started: float) -> VectorLeg:
        logger.warning(
            "vector search unavailable; lexical results only",
            extra={"reason": reason, "duration_ms": round((time.perf_counter() - started) * 1000)},
        )
        return VectorLeg(available=False, reason=reason)

# ...

@dataclass(frozen=True, slots=True)
class FusionParams:
    rrf_k: float
    lexical_weight: float
    vector_weight: float


def fuse(
    lexical: list[RankedHit],
    vector: list[PassageMatch],
    vector_sources: dict[str, dict[str, Any]],
    plan: QueryPlan,
    weights: RankingWeights,
    params: FusionParams,
    *,
    now: Any,
) -> list[RankedHit]:
    """Fuse the lexical leg's ranking (``rank_hits`` output) with the vector leg's.

    ``vector_sources`` holds the document fields (``web_documents`` ``_source``) of the
    documents only the vector leg found; one without a source is left out (the passage
    index can briefly lag behind document deletes).
    """
    lexical_rank = {r.hit.doc_id: i for i, r in enumerate(lexical, start=1)}
    vector_rank = {m.document_id: i for i, m in enumerate(vector, start=1)}
    by_id: dict[str, RankedHit] = {r.hit.doc_id: r for r in lexical}
    passages = {m.document_id: m for m in vector}
    max_bm25 = max((r.hit.score for r in lexical), default=0.0)
    for match in vector:
        if match.document_id in by_id or match.document_id not in vector_sources:
            continue
        hit = RawHit(match.document_id, 0.0, vector_sources[match.document_id], {})
        features = compute_features(hit, plan, max_bm25, weights, now)
        # Body coverage from the passage that matched (there is no BM25 highlight).
        coverage = query_terms(match.text)
        features = replace(
            features,
            passage_coverage=(
                sum(1 for t in plan.terms if t in coverage) / len(plan.terms) if plan.terms else 0.0
            ),
        )
        by_id[match.document_id] = RankedHit(hit=hit, score=0.0, features=features)

    raw: dict[str, float] = {}
    for doc_id in by_id:
        score = 0.0
        if doc_id in lexical_rank:
            score += params.lexical_weight / (params.rrf_k + lexical_rank[doc_id])
        if doc_id in vector_rank:
            score += params.vector_weight / (params.rrf_k + vector_rank[doc_id])
        raw[doc_id] = score
    best = max(raw.values(), default=0.0)
    missing = len(by_id) + len(vector) + 1  # sorts after every real rank
    fused: list[RankedHit] = []
    for doc_id, ranked in by_id.items():
        best_passage = passages.get(doc_id)
        normalized = raw[doc_id] / best if best > 0 else 0.0
        features = replace(
            ranked.features,
            lexical_score=ranked.score if doc_id in lexical_rank else None,
            lexical_rank=lexical_rank.get(doc_id),
            vector_rank=vector_rank.get(doc_id),
            vector_similarity=best_passage.similarity if best_passage is not None else None,
            fused_score=raw[doc_id],
        )
        fused.append(
            RankedHit(
                hit=ranked.hit, score=round(normalized, 6), features=features, passage=best_passage
            )
        )
    fused.sort(
        key=lambda r: (
            -(r.features.fused_score or 0.0),
            r.features.lexical_rank or missing,
            r.features.vector_rank or missing,
            r.hit.doc_id,
        )
    )
    return deduplicate(fused)
