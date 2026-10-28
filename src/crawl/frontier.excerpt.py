# Excerpt from app/db/repository.py ("# ..." marks omitted code)

@dataclass(frozen=True, slots=True)
class ClaimPolicy:
    lease_seconds: int
    per_domain_concurrency: int
    default_delay_seconds: float
    max_delay_seconds: float
    scan_factor: int = 8


def effective_delay(crawl_delay: float | None, policy: ClaimPolicy) -> float:
    delay = max(crawl_delay or 0.0, policy.default_delay_seconds)
    return min(delay, max(policy.max_delay_seconds, policy.default_delay_seconds))


def claim_candidates_query(lanes: Collection[CrawlLane] | None) -> TextClause:
    """Due URLs: most urgent lane first, then priority, then discovery-triggered fetches
    before recrawls, then the longest overdue (exactly ``ix_urls_claim``'s order).

    The statuses are literals rather than a parameter so that the partial index's
    predicate is provably implied, including under a generic (prepared) plan.
    """
    statuses = ", ".join(f"'{s}'" for s in _SCHEDULABLE)
    lane_filter = "AND u.lane = ANY(:lanes)" if lanes is not None else ""
    query = text(
        f"""
        SELECT u.id, u.domain
        FROM urls u JOIN crawl_domains d ON d.domain = u.domain
        WHERE u.status IN ({statuses})
          AND u.next_fetch_at <= now()
          {lane_filter}
          AND d.next_fetch_allowed_at <= now()
        ORDER BY u.lane, u.priority DESC, u.discovery_pending DESC, u.next_fetch_at, u.id
        LIMIT :scan
        FOR NO KEY UPDATE OF u SKIP LOCKED
        """  # noqa: S608 (only module constants are interpolated)
    )
    if lanes is not None:
        query = query.bindparams(bindparam("lanes", type_=ARRAY(SmallInteger)))
    return query


# ...

async def claim_batch(
    session: AsyncSession,
    run_id: int,
    limit: int,
    policy: ClaimPolicy,
    lanes: Collection[CrawlLane] | None = None,
) -> list[ClaimedUrl]:
    """Lease up to ``limit`` due URLs while honoring per-domain concurrency and delay.

    ``lanes`` restricts the claim to those lanes (``None``: all). More urgent lanes are
    always claimed first. Must run in its own transaction; the caller commits. The
    domain's ``next_fetch_allowed_at`` is advanced by ``delay * claimed`` so other
    workers, whatever lanes they serve, wait.
    """
    lane_values = _lane_values(lanes)
    if limit <= 0:
        return []
    params: dict[str, Any] = {"scan": limit * policy.scan_factor}
    if lane_values is not None:
        params["lanes"] = lane_values
    candidates = (await session.execute(claim_candidates_query(lanes), params)).all()
    if not candidates:
        return []

    domains = sorted({row.domain for row in candidates})
    domain_rows = (
        await session.execute(
            text(
                """
                SELECT d.domain, d.crawl_delay_seconds,
                       (SELECT count(*) FROM urls a
                        WHERE a.domain = d.domain AND a.status = 'fetching') AS active
                FROM crawl_domains d
                WHERE d.domain = ANY(:domains) AND d.next_fetch_allowed_at <= now()
                ORDER BY d.domain
                FOR UPDATE OF d SKIP LOCKED
                """
            ).bindparams(bindparam("domains", type_=ARRAY(Text))),
            {"domains": domains},
        )
    ).all()

    capacity: dict[str, int] = {}
    delays: dict[str, float] = {}
    for row in domain_rows:
        delay = effective_delay(row.crawl_delay_seconds, policy)
        # A slow domain gets one URL per claim so Scrapy cannot fetch a burst from it.
        per_domain = policy.per_domain_concurrency if delay <= policy.default_delay_seconds else 1
        capacity[row.domain] = max(0, per_domain - int(row.active))
        delays[row.domain] = delay

    chosen: list[int] = []
    per_domain_claimed: dict[str, int] = {}
    for row in candidates:
        if len(chosen) >= limit:
            break
        if capacity.get(row.domain, 0) > 0:
            capacity[row.domain] -= 1
            per_domain_claimed[row.domain] = per_domain_claimed.get(row.domain, 0) + 1
            chosen.append(row.id)
    if not chosen:
        return []

    claimed = (
        await session.execute(
            update(Url)
            .where(Url.id.in_(chosen))
            .values(
                status=UrlStatus.FETCHING,
                lease_run_id=run_id,
                lease_expires_at=func.now() + timedelta(seconds=policy.lease_seconds),
                updated_at=func.now(),
            )
            .returning(
                # ... id, url, kind, lane, recrawl interval, site tier
            )
        )
    ).all()

    for domain, count in per_domain_claimed.items():
        await session.execute(
            update(CrawlDomain)
            .where(CrawlDomain.domain == domain)
            .values(next_fetch_allowed_at=func.now() + timedelta(seconds=delays[domain] * count))
        )

    # ... map the rows to ClaimedUrl, in claim order
