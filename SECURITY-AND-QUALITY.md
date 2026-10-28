<p align="center">
  <img src="assets/banners/banner-security.png" alt="Security and quality: safe to crawl, honest about limits" width="100%">
</p>

# Security and quality

Websearch is a work in progress. This page covers what the current build does to protect itself and the sites it crawls, how answers stay grounded, how changes are checked, and what it deliberately leaves out. Controls that aren't built are listed as next steps, not as features.

**Contents**

- [Security as built](#security-as-built)
  - [Outbound requests (SSRF)](#outbound-requests-ssrf)
  - [Crawling ethics](#crawling-ethics)
  - [Untrusted content and the language model](#untrusted-content-and-the-language-model)
  - [API surface](#api-surface)
  - [Secrets and logs](#secrets-and-logs)
- [What production would add](#what-production-would-add)
- [Quality](#quality)
  - [How a change is checked](#how-a-change-is-checked)
  - [How we measure relevance](#how-we-measure-relevance)
  - [About figures](#about-figures)
- [Known limits](#known-limits)

---

## Security as built

### Outbound requests (SSRF)

<p align="center"><img src="assets/diagrams/fetch-safety.png" alt="SSRF protection: a URL check with no network access, a pinned connection where every resolved address must be public, and redirects checked again at every hop" width="100%"></p>

| Concern | Control |
| --- | --- |
| Links to internal services | Only http and https. No embedded credentials, no `localhost`, `*.local`, `*.internal`, metadata or single-label hosts. IP literals must be public. |
| DNS rebinding | The host is resolved once at connect time. Every address must be public, and the connection goes to one of those addresses. TLS still verifies the certificate for the host name. |
| Redirects into the network | Never followed automatically. Every hop is resolved and passes both layers and robots.txt again. |
| Cloud metadata endpoints | Link-local ranges are always blocked, even in the test mode that allows private addresses |
| Hostile responses | Body size capped on decoded bytes, which bounds compression bombs. Sitemap decompression capped separately. Content types allow-listed per kind of URL. |
| XML attacks | Entities, DTDs and network access disabled when parsing sitemaps and feeds |
| Ambient credentials | Proxy and `.netrc` environment variables ignored. No cookies stored, no credentials sent. |
| Test-only settings | Allowing private addresses is for local integration tests and is refused in production |

### Crawling ethics

- robots.txt is always enforced. It is not a setting.
- `Crawl-delay` is respected (capped), and politeness is per domain across every worker.
- Login, account and cart URLs are excluded by default.
- Login pages, CAPTCHAs and access controls are never bypassed. 401 and 403 are permanent failures.
- `noindex` and `nofollow` are honored, from the page and from response headers.
- The crawler identifies itself with a configurable user agent, which should carry a contact URL before any real crawl.

### Untrusted content and the language model

| Concern | Control |
| --- | --- |
| Prompt injection from crawled pages | Every source is wrapped in a delimited block with an explicit instruction that its content is data and must not be followed. Delimiters inside page text are escaped. |
| Invented sources | A source registry per request. A citation is valid only if it names a source returned in that request, and, when it carries a URL, only if the URL exactly matches. |
| Invented citations mid-answer | A streaming filter removes markers that name anything unregistered before the text reaches the client |
| HTML in results | Snippets are plain text with highlight offsets. `snippet_html` is escaped, with only `<mark>` added. All crawled text is treated as untrusted. |
| Query injection | Search text is never parsed as query syntax |

### API surface

| Route | Access |
| --- | --- |
| `GET /v1/search`, `POST /v1/chat`, `POST /v1/chat/external` | Open. No authentication or rate limits yet (see [Known limits](#known-limits)). |
| `/v1/crawl/*`, `/v1/stats` | Bearer token, compared in constant time. Disabled entirely when no token is set. The API never starts a crawl. |
| `/v1/answer`, `/v1/fetch` | Stubs that return 501. When built, fetch must use the safe fetcher and robots.txt rules and require authentication, so it can't be an open proxy. |
| Model server | No authentication. It binds to `127.0.0.1` only, and containers reach it through Docker Desktop's host forwarding. |
| Local stack | The Compose file binds every port to `127.0.0.1` and disables the OpenSearch security plugin. It is for development only. |

**Production guard.** With `APP_ENV=production` the app refuses to start unless OpenSearch uses HTTPS, certificate verification and credentials.

### Secrets and logs

- Credentials are secret types, masked in every representation.
- `.env` is git-ignored, and the example file holds placeholders only.
- Logs never contain query strings or search text: URLs are redacted, searches are logged as a hash, and requests by path only.
- Model weights download only from pinned revisions, and the download is refused when it would leave too little free disk. Once cached, the model server starts offline.

---

## What production would add

> Not built. These are the controls a public deployment would need. See [Architecture, Part 2](ARCHITECTURE.md#part-2--beyond-whats-built).

| Threat | Impact | Control |
| --- | --- | --- |
| Abuse of public routes | Search and chat used as free compute, or scraped | Authentication for API clients, per-client rate limits, request size caps |
| The fetch endpoint as an open proxy | Our IPs used to reach arbitrary sites | Authentication, the crawler's safe fetcher and robots.txt rules, per-client limits |
| Index or database exposure | Crawl data read or altered | OpenSearch security plugin with TLS, a least-privilege app user, a private network, replicas |
| Network egress | A bypass of the SSRF checks reaches internal services | Egress restricted at the network level as defence in depth |
| Model server exposure | Unauthenticated access to the GPU | Kept on localhost or a private network, or put behind authentication |
| Poisoned pages | Spam or injected instructions ranked highly | Untrusted framing (built), plus spam signals, reranking and near-duplicate detection |
| Missed operational problems | A lane or the embedding queue silently falls behind | Metrics and alerts on lane lag, failure rates and embedding lag |

---

## Quality

### How a change is checked

<p align="center"><img src="assets/diagrams/quality-gates.png" alt="How a change is checked: format and lint, strict typing, unit tests, integration tests on the real stack, relevance evaluation against a baseline, safe migrations" width="100%"></p>

| Check | What it covers |
| --- | --- |
| Ruff | Formatting and lint, including security and async rules |
| mypy (strict) | The app, the model server and the tests. The ML stack is treated as untyped, so the check gives the same result with or without it installed. |
| Unit tests | No network and no services. Models, the language model and retrieval have in-memory fakes. |
| Integration tests | Opt-in, on real PostgreSQL and OpenSearch with separate test databases and indexes: frontier concurrency, leases, lanes, adaptive recrawl, site budgets under concurrent writes, tier recompute, keyword and hybrid search, the embedding queue and worker, and an end-to-end crawl of a local sample site |
| Model smoke test | Opt-in, against the real models |
| Relevance evaluation | Hand-judged queries against a saved baseline, failing on a regression |
| Migrations | Batched backfills, constraints validated without blocking writes, indexes built concurrently, and `alembic check` to confirm the models match the migrations |

The end-to-end crawl seeds through the API, runs the real crawler as a subprocess, and checks robots.txt, the sitemap, the feed, a redirect, `noindex`, a missing page, a rate limit, search, stats, embedding jobs, and that a recrawl creates no duplicates.

### How we measure relevance

The evaluation runs a dataset of real queries with hand-labeled pages through the search service and reports NDCG@10, MRR@10 and the zero-result rate, overall and per tag. The method is in [Architecture, section 10](ARCHITECTURE.md#10-measuring-relevance). Three rules keep it honest:

1. **Compare like with like.** Every report records the dataset, index contents and settings, and a comparison warns when they differ. A crawl changes the index, so the baseline is saved again after crawling.
2. **Judge what the engine returns.** When a change surfaces unlabeled results, they are judged before the comparison is trusted, because an unlabeled relevant page counts as a miss.
3. **Don't reward today's ranking.** Candidates are pooled from several retrievals, not only the current one.

### About figures

This case study reports capabilities, not numbers. The engine has been run and benchmarked only on a development corpus on one machine, and we don't publish performance, quality or scale figures until they are measured at a size that means something.

---

## Known limits

Most of these are scope we haven't reached yet, not defects. We list them because anyone evaluating this will find them, and because each one marks the next piece of work.

| Limit | Detail | What closes it |
| --- | --- | --- |
| Not tested at web scale | The crawler has run on a development corpus and small sites only | Broad seeds, the open-web crawl scope and a scheduler, measured as it grows |
| No auth on public routes | Search and chat have no authentication or rate limits | Client authentication and per-client limits |
| No reranking in search | The cross-encoder is served but not called by search | Rerank the top results, and the passages the answer layer reads |
| No grounded-answers endpoint | `/v1/answer` and `/v1/fetch` return 501 | Build both on the existing source registry and safe fetcher |
| No JavaScript rendering | Pages that need JavaScript are indexed as served | A headless render pool |
| HTML only | PDFs and other documents are skipped | Extractors for other document types |
| Exact-hash duplicates | No shingling or SimHash | Near-duplicate detection |
| Change detection by exact hash | A page whose boilerplate changes on every fetch looks changed every time. The tier's recrawl floor limits the cost. | Hash the main content only, or near-duplicate comparison |
| Language from markup only | `<html lang>` or `Content-Language`, no content-based detection | A language detector on extracted text |
| First passages only | The tail of a long page is in the keyword index but not the passage index | More passages per page where the cost is justified |
| A fixed similarity floor | Embedding similarities are compressed, so a few nonsense queries still get vector hits | Reranking, or a calibrated per-query threshold |
| Search latency tail | Highlighting the full candidate window dominates slow queries | Compute passage coverage another way, then highlight only the returned page |
| Exact-host domain filter | The `domain` filter doesn't include subdomains | A site-level filter |
| Sites without dated URLs | A site without feeds or sitemap dates can't be measured as publishing | Pin known news sites. A change share can still lift a site to active. |
| No metrics or alerting | Visibility is JSON logs, `/v1/stats` and CLI status | Metrics and alerts |
| Language model tests | The provider adapter is tested with fakes, not against a live server | Contract tests against a live model endpoint |
