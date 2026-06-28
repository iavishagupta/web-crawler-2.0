# WebCrawler

An async web crawler built from scratch in Python. Recursively crawls a website, extracts structured page data, and stores results in PostgreSQL. Built to be correct, safe to deploy, and honest about what it does.

---

## What it does

- Accepts a starting URL and crawls all internal pages up to a configurable limit
- Extracts rich page data: title, all headings, body text, Open Graph tags, JSON-LD structured data, links with anchor text, images
- Stores results in PostgreSQL with JSONB for flexible schema evolution
- Skips pages crawled recently (TTL-based) and resumes interrupted crawls automatically
- Deduplicates by content hash — same page at two URLs is stored once
- Respects `robots.txt`, rate limits per domain, and handles redirects safely
- Retries transient failures with exponential backoff; trips a circuit breaker on persistently failing domains
- Ships structured logs in development (colored console) or production (newline-delimited JSON)

---

## Architecture

```
main.py
  └── crawl_site_async()              ← entry point
        └── AsyncCrawler              ← manages worker pool lifecycle
              ├── CrawlState          ← shared state + stats + deduplicator
              └── Worker × N          ← concurrent coroutines, one per slot
                    ├── ssrf_guard    ← pre-fetch URL validation
                    ├── robots_guard  ← robots.txt compliance
                    ├── rate_limiter  ← per-domain delay
                    ├── ResilientFetcher
                    │     ├── redirect_guard   ← manual redirect following
                    │     ├── retry            ← exponential backoff
                    │     └── circuit_breaker  ← per-domain failure threshold
                    ├── extract_html  ← full page data extraction
                    ├── crawl_quality ← canonical resolution + content dedup
                    └── storage       ← PostgreSQL upsert
```

The crawler uses `asyncio.Queue` with a fixed worker pool. Workers pull URLs, process them, and push discovered links back into the queue. `Queue.join()` handles termination — it blocks until every enqueued item has been fully processed, including pages discovered mid-crawl.

---

## File inventory

| File | Purpose |
|---|---|
| `main.py` | CLI entry point — argparse, env config, logging init |
| `async_crawler.py` | Queue-worker engine, worker pool, SIGINT shutdown |
| `extract_html.py` | Full page extraction — title, headings, body, OG, JSON-LD, links, images |
| `url_normalizer.py` | URL normalization with tracking param stripping |
| `crawl_quality.py` | Canonical URL resolution + content deduplication |
| `ssrf_guard.py` | SSRF protection — blocks private IPs, bad schemes, embedded credentials |
| `robots_guard.py` | Per-domain robots.txt compliance with async caching |
| `rate_limiter.py` | Per-domain rate limiting, respects `Crawl-delay` from robots.txt |
| `redirect_guard.py` | Manual redirect following — SSRF recheck + domain boundary + hop limit |
| `resilience.py` | Retry with exponential backoff + per-domain circuit breaker |
| `storage.py` | PostgreSQL + JSONB — upsert, TTL staleness check, crash resumability |
| `logger.py` | structlog configuration — dev (colored) or production (JSON) mode |
| `crawl_stats.py` | Metrics — latency percentiles, throughput, domain breakdown, summary table |
| `crawl.py` | Deprecated — re-exports from `url_normalizer` for backward compatibility |
| `test_queue_crawler.py` | Tests for queue mechanics, deduplication, worker behavior |
| `test_resilience.py` | Tests for retry logic, circuit breaker state machine, backoff math |
| `test_crawl_quality.py` | Tests for URL normalization, canonical resolution, content dedup |
| `url_normalizer.py` | URL normalization with tracking param stripping (replaces crawl.py) |
| `crawl_quality.py` | Canonical URL resolution + content deduplication via SHA-256 hash |
| `crawl_stats.py` | Crawl metrics — latency percentiles, throughput, domain breakdown, progress + summary |

**17 source files, 3 test files, 4110 lines total, 167 tests.**

---

## Installation

```bash
git clone <repository-url>
cd bootcrawler

pip install aiohttp beautifulsoup4 w3lib structlog asyncpg \
            validators aiohttp apscheduler
```

For PostgreSQL storage, create the database and set the DSN:

```bash
createdb crawlerdb
export CRAWLER_DB_DSN="postgresql://user:password@localhost/crawlerdb"
```

Tables are created automatically on first run.

---

## Usage

```bash
# Basic crawl — memory only, JSON report written to report.json
python main.py https://example.com

# With options
python main.py https://example.com \
  --workers 10 \
  --max-pages 500 \
  --rate-limit 1.5 \
  --output results.json

# With PostgreSQL storage and TTL-based recrawling
python main.py https://example.com \
  --db-dsn "postgresql://user:pass@localhost/crawlerdb" \
  --ttl-days 7

# Production logging (newline-delimited JSON)
python main.py https://example.com --log-mode production

# All options
python main.py --help
```

### CLI options

| Flag | Default | Description |
|---|---|---|
| `url` | required | Base URL to crawl |
| `--workers` | `6` | Concurrent worker coroutines |
| `--max-pages` | `50` | Page limit |
| `--rate-limit` | `1.0` | Seconds between requests to the same domain |
| `--ttl-days` | `7` | Recrawl pages older than N days (requires `--db-dsn`) |
| `--db-dsn` | none | PostgreSQL DSN for persistent storage |
| `--output` | `report.json` | JSON report path |
| `--log-mode` | `development` | `development` (colored) or `production` (JSON) |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `--progress-every` | `30` | Log progress summary every N seconds |

Every flag can also be set via environment variable: `CRAWLER_WORKERS`, `CRAWLER_MAX_PAGES`, `CRAWLER_RATE_LIMIT`, `CRAWLER_TTL_DAYS`, `CRAWLER_DB_DSN`, `CRAWLER_OUTPUT`, `CRAWLER_LOG_MODE`, `CRAWLER_LOG_LEVEL`.

---

## Output

### JSON report (`report.json`)

```json
{
  "https://example.com/": {
    "url": "https://example.com/",
    "crawled_at": "2026-01-15T10:23:41.123Z",
    "title": "Example Domain",
    "headings": [
      { "level": "h1", "text": "Example Domain" }
    ],
    "body_text": "This domain is for use in illustrative examples...",
    "word_count": 42,
    "content_hash": "a3f1d2...",
    "meta": {
      "description": "An example domain",
      "canonical": "https://example.com/",
      "og": { "og:title": "Example Domain" },
      "twitter": {},
      "robots": "",
      "language": "en"
    },
    "json_ld": [],
    "outgoing_links": [
      {
        "url": "https://www.iana.org/domains/example",
        "anchor_text": "More information...",
        "title": "",
        "rel": "",
        "is_external": true
      }
    ],
    "internal_links": [],
    "external_links": ["https://www.iana.org/domains/example"],
    "images": []
  }
}
```

### Crawl summary (logged at end of every run)

```
╔══════════════════════════════════════════════════════════╗
║                    CRAWL SUMMARY                        ║
╠══════════════════════════════════════════════════════════╣
║  Pages crawled      47                                  ║
║  Skipped (fresh)    12     (within TTL window)          ║
║  Skipped (seen)     8      (already queued/visited)     ║
║  Total errors       3      (6.0% error rate)            ║
╠══════════════════════════════════════════════════════════╣
║  Duration           24.3s                               ║
║  Throughput         1.93 pages/sec                      ║
║  Latency p50        142ms                               ║
║  Latency p95        890ms                               ║
╠══════════════════════════════════════════════════════════╣
║  Error breakdown                                        ║
║    permanent             2                              ║
║    robots                1                              ║
╚══════════════════════════════════════════════════════════╝
```

---

## Security

### SSRF protection (`ssrf_guard.py`)

Validates every URL before any HTTP request is made. Blocks:

- Non-HTTP/S schemes (`file://`, `ftp://`, `gopher://`)
- Embedded credentials (`http://user:pass@host`)
- Bare private IPs (`127.x`, `10.x`, `172.16.x`, `192.168.x`, `169.254.x`)
- Hostnames that resolve to private IPs via DNS pre-resolution

The AWS metadata endpoint (`169.254.169.254`) and all RFC 1918 ranges are in the blocklist. SSRF validation also runs on every redirect target — a safe origin URL can redirect to an internal address.

### robots.txt compliance (`robots_guard.py`)

Fetches each domain's `robots.txt` once per crawl run and caches it in memory. Respects `Disallow` directives for the declared user-agent. Treats 401/403 responses as "disallow all". Fails open on network errors (unreachable robots.txt = allowed). Reads `Crawl-delay` and passes it to the rate limiter.

### Rate limiting (`rate_limiter.py`)

Enforces a minimum delay between requests to the same domain. Defaults to 1 second. Overrides with `Crawl-delay` from `robots.txt` when present. Clamped between 0.5s (minimum politeness) and 60s (ignores absurd values). Per-domain locks prevent concurrent workers from racing on the same domain.

### Redirect guard (`redirect_guard.py`)

Follows redirects manually instead of letting `aiohttp` handle them automatically. At each hop: re-runs SSRF validation, checks the redirect doesn't cross the crawl domain boundary, enforces a 5-hop maximum. Prevents SSRF-via-redirect (a server redirecting to `169.254.169.254` after passing the initial URL check).

### DNS rebinding protection (`ssrf_guard.py + redirect_guard.py`)
Standard SSRF protection has a race condition: the hostname is validated against a blocklist at check time, but the actual TCP connection is made moments later — after a new DNS lookup. An attacker who controls a domain can pass the initial check (DNS resolves to a public IP) then immediately flip DNS to point at 169.254.169.254 before the connection is made.
The crawler closes this window with IP pinning. validate_url_safe() resolves the hostname to an IP, validates it, and returns the raw IP URL alongside the original hostname. The actual HTTP request goes directly to that IP — DNS is never consulted again for that hop. The Host header is set manually so the server responds correctly.
This applies on every redirect hop too. redirect_guard.py calls validate_url_safe() on each redirect target and uses the returned IP URL for the next request. A safe origin URL cannot redirect its way to an internal address.
---

## Reliability

### Retry with exponential backoff (`resilience.py`)

Retries on: network errors (connection refused, timeout, DNS failure), HTTP 429 (reads `Retry-After` header), HTTP 5xx.

Never retries: HTTP 4xx (except 429), security errors, wrong content type.

Backoff formula: `min(base × 2^attempt + jitter, max_wait)`. Jitter is randomised ±0.5s to prevent thundering herd — workers that all hit a rate limit simultaneously don't all retry at exactly the same moment.

### Circuit breaker (`resilience.py`)

Per-domain, three-state: CLOSED → OPEN → HALF_OPEN → CLOSED.

Opens after 5 consecutive failures. Stays open for 60 seconds (configurable). Allows one probe request after cooldown. If the probe succeeds, the circuit closes. If it fails, the cooldown resets. Workers skip OPEN circuit domains immediately without consuming retry budget.

### Graceful shutdown

`SIGINT` (Ctrl+C) sets a shutdown event. Workers stop pulling new URLs from the queue, finish their current in-flight page, and exit cleanly. `Queue.join()` waits for all `task_done()` calls before the process exits. No torn writes, no half-stored pages. Partial results are written to the JSON report.

---

## Crawl quality

### URL normalization (`url_normalizer.py`)

Built on `w3lib.canonicalize_url` with an additional tracking param stripping step. 40+ known tracking params are removed: `utm_*`, `fbclid`, `gclid`, `msclkid`, `hsa_*`, `mc_eid`, `mkt_tok`, and more.

`https://example.com/post?utm_source=twitter&page=2` normalizes to `https://example.com/post?page=2`. Two URLs that differ only in tracking params are treated as the same page.

### Canonical URL handling (`crawl_quality.py`)

After fetching a page, the crawler reads its `<link rel="canonical">` tag. If the canonical differs from the fetched URL (and is on the same domain), the result is stored under the canonical URL. If the canonical was already crawled, the fetch is discarded. This prevents paginated URLs (`/post?page=2`, `/post?page=3`) from being stored as separate pages when they all declare the same canonical.

### Content deduplication (`crawl_quality.py`)

Each page's body text is hashed with SHA-256 at extraction time. Before storing, the crawler checks the hash against a registry of seen hashes. If the hash was seen before (at a different URL), the page is a duplicate and is skipped. Handles `www` vs non-`www` variants, syndicated content, and any two routes that serve identical content.

---

## Storage (`storage.py`)

PostgreSQL with a JSONB column for extracted page data. Structured columns handle queries; the JSONB column holds everything the extractor produces without requiring schema migrations when new fields are added.

```sql
CREATE TABLE crawled_pages (
    url          TEXT PRIMARY KEY,
    domain       TEXT NOT NULL,
    last_crawled TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL DEFAULT 'ok',
    content_hash TEXT,
    extracted    JSONB
);
```

Three indexes: domain (for "all pages on example.com" queries), `last_crawled` (for staleness checks), and GIN on `extracted` (for querying inside JSON, e.g. `WHERE extracted @> '{"meta": {"language": "en"}}'`).

**TTL-based recrawling:** `is_stale(url, ttl_days)` returns `True` if the URL was never crawled, was crawled more than `ttl_days` ago, or last crawl errored. Failed URLs are always retried.

**Crash resumability:** At crawl startup, `get_seen_urls(domain)` loads all successfully crawled URLs from the DB into `CrawlState.seen_urls`. If a crawl is interrupted and restarted, already-crawled pages are skipped.

---

## Logging (`logger.py`, `crawl_stats.py`)

Structured logging via `structlog`. Two modes set by `--log-mode` or `CRAWLER_LOG_MODE`:

**Development** — colored human-readable output:
```
2026-01-15T10:23:41Z [info] page_crawled  worker_id=2 url=https://example.com/about duration_ms=142 word_count=312
2026-01-15T10:23:43Z [warning] robots_blocked  worker_id=0 url=https://example.com/admin
```

**Production** — newline-delimited JSON for log aggregators (Datadog, Loki, CloudWatch):
```json
{"timestamp":"2026-01-15T10:23:41Z","level":"info","event":"page_crawled","worker_id":2,"url":"https://example.com/about","duration_ms":142,"word_count":312}
```

Every log event carries structured key-value context — filter by `worker_id`, alert on `duration_ms > 5000`, count by `event` type — without parsing strings.

---

## Running tests

```bash
python -m pytest test_queue_crawler.py test_resilience.py test_crawl_quality.py -v
```

167 tests across three files. All tests mock the HTTP layer — no network calls, no database required.

| Test file | What it covers |
|---|---|
| `test_queue_crawler.py` | Worker pool mechanics, deduplication, Queue.join() termination, max_pages enforcement, failed pages not blocking the crawl |
| `test_resilience.py` | Backoff math, circuit breaker state transitions, retry/no-retry classification by error type |
| `test_crawl_quality.py` | Tracking param stripping, canonical resolution edge cases, content dedup with concurrent workers |
| `test_extract_html.py` | Title, headings, body text, meta (OG/Twitter/canonical/robots/language), JSON-LD, links, images, full extract_page_data integration |
| `test_storage.py` | upsert_page, mark_error, get_page, is_stale TTL logic, is_changed hash comparison, get_seen_urls, schema structure |

---

## Dependencies

```
aiohttp          — async HTTP client
beautifulsoup4   — HTML parsing
w3lib            — URL canonicalization
structlog        — structured logging
asyncpg          — async PostgreSQL driver
validators       — URL validation in CLI
```

---

## What's next

The crawler engine is complete. The planned next phase is a web interface:

- **FastAPI backend** — REST API for job management, SSE endpoint for live crawl progress, APScheduler for recurring crawls
- **React frontend** — submit URLs, watch crawls in real time, browse and search results, download as JSON/CSV, manage schedules

The backend will use `crawl_site_async()` directly — no changes to the crawler engine required.

---

## License

Educational and portfolio use.