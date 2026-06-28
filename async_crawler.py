"""
async_crawler.py — Queue-worker crawler with structured logging + stats

Changes in this iteration:
  - All print() / self._log() replaced with structlog calls
  - CrawlStats replaces the errors dict — tracks latency, throughput, domains
  - Periodic progress reporter task logs stats every N pages
  - Final summary emitted via CrawlStats.log_summary()
  - Worker logger pre-bound with worker_id so every log line carries it
"""

import os
import asyncio
import signal
import time
from typing import Optional
import aiohttp

from dotenv import load_dotenv
load_dotenv()

from url_normalizer import normalize_url, get_domain
from extract_html import extract_page_data

from ssrf_guard import validate_url_safe, SSRFError
from robots_guard import RobotsCache
from rate_limiter import RateLimiter
from redirect_guard import RedirectError
from resilience import ResilientFetcher, RetryConfig, FetchError, CircuitOpenError
from storage import CrawlStorage
from logger import get_logger
from crawl_stats import CrawlStats
from crawl_quality import CanonicalResolver, ContentDeduplicator

_log = get_logger("crawler")

USER_AGENT = os.getenv("USER_AGENT")


class CrawlState:
    """
    Thread-safe shared state for the worker pool.
    Stats tracking moved to CrawlStats — this holds only structural state.
    """

    def __init__(self, max_pages: int):
        self.max_pages = max_pages
        self.page_data: dict = {}
        self.seen_urls: set[str] = set()
        self.lock = asyncio.Lock()
        self.stats = CrawlStats()
        self.deduplicator = ContentDeduplicator()

    async def try_enqueue(self, normalized_url: str) -> bool:
        async with self.lock:
            if normalized_url in self.seen_urls:
                return False
            if len(self.seen_urls) >= self.max_pages:
                return False
            self.seen_urls.add(normalized_url)
            return True

    async def store_result(self, normalized_url: str, data: dict) -> None:
        async with self.lock:
            self.page_data[normalized_url] = data

    def pages_crawled(self) -> int:
        return len(self.page_data)


class Worker:
    """
    One crawl worker. Each worker binds its worker_id to its logger
    so every log event it emits automatically includes worker_id=N.
    """

    def __init__(
        self,
        worker_id: int,
        queue: asyncio.Queue,
        state: CrawlState,
        session: aiohttp.ClientSession,
        base_domain: str,
        robots_cache: RobotsCache,
        rate_limiter: RateLimiter,
        fetcher: ResilientFetcher,
        allow_cross_domain_redirects: bool = False,
        storage: Optional[CrawlStorage] = None,
        ttl_days: int = 7,
        shutdown_event: asyncio.Event | None = None,
        max_depth: int = 0,   # 0 = unlimited
    ):
        self.id = worker_id
        self.queue = queue
        self.state = state
        self.session = session
        self.base_domain = base_domain
        self.robots_cache = robots_cache
        self.rate_limiter = rate_limiter
        self.fetcher = fetcher
        self.allow_cross_domain_redirects = allow_cross_domain_redirects
        self.storage = storage
        self.ttl_days = ttl_days
        self.shutdown_event = shutdown_event or asyncio.Event()
        self.max_depth = max_depth

        # Pre-bind worker_id — every log call from this worker carries it
        self.log = get_logger("worker", worker_id=worker_id)
        self.canonical_resolver = CanonicalResolver(base_domain=base_domain)

    # ── Guards ────────────────────────────────────────────────────────────

    async def _is_allowed(self, url: str) -> bool:
        try:
            validate_url_safe(url)
        except SSRFError as e:
            self.log.warning("ssrf_blocked", url=url, reason=str(e))
            await self.state.stats.record_error("ssrf")
            return False

        if not await self.robots_cache.can_fetch(url):
            self.log.info("robots_blocked", url=url)
            await self.state.stats.record_error("robots")
            return False

        return True

    async def _apply_rate_limit(self, url: str) -> None:
        domain = get_domain(url)
        if domain not in self.rate_limiter._domain_delays:
            delay = await self.robots_cache.crawl_delay(url)
            if delay is not None:
                self.rate_limiter.set_domain_delay(domain, delay)
                self.log.info(
                    "crawl_delay_set",
                    domain=domain,
                    delay_s=delay,
                    source="robots_txt",
                )
        await self.rate_limiter.wait(domain)

    # ── Link discovery ────────────────────────────────────────────────────

    async def _enqueue_links(self, links: list[str]) -> None:
        enqueued = 0
        for link in links:
            normalized = normalize_url(link)
            if await self.state.try_enqueue(normalized):
                await self.queue.put((link, 0))
                enqueued += 1
            else:
                await self.state.stats.record_skipped_seen()

        if enqueued:
            self.log.debug(
                "links_enqueued",
                count=enqueued,
                queue_size=self.queue.qsize(),
                seen_total=len(self.state.seen_urls),
            )

    # ── Worker loop ───────────────────────────────────────────────────────

    async def run(self) -> None:
        self.log.debug("worker_started")
        while True:
            if self.shutdown_event.is_set():
                self.log.debug("worker_shutdown_requested")
                return

            item = await self.queue.get()

            if item is None:
                self.queue.task_done()
                await self.queue.put(None)
                self.log.debug("worker_stopped")
                return

            if self.shutdown_event.is_set():
                self.queue.task_done()
                self.log.debug("worker_shutdown_mid_queue")
                return

            url, _ = item
            await self._process(url)
            self.queue.task_done()

    async def _process(self, url: str) -> None:
        if get_domain(url) != self.base_domain:
            return

        if not await self._is_allowed(url):
            return

        # TTL check
        if self.storage is not None:
            stale = await self.storage.is_stale(url, ttl_days=self.ttl_days)
            if not stale:
                self.log.info("page_fresh", url=url, ttl_days=self.ttl_days)
                await self.state.stats.record_skipped_fresh()
                return

        await self._apply_rate_limit(url)

        fetch_start = time.monotonic()

        try:
            self.log.debug("fetch_start", url=url)
            html, final_url = await self.fetcher.fetch(
                session=self.session,
                url=url,
                base_domain=self.base_domain,
                allow_cross_domain=self.allow_cross_domain_redirects,
                user_agent=USER_AGENT,
            )
            fetch_ms = (time.monotonic() - fetch_start) * 1000
            extracted = extract_page_data(html, final_url)

            self.log.info(
                "page_crawled",
                url=url,
                final_url=final_url if final_url != url else None,
                duration_ms=round(fetch_ms, 1),
                html_bytes=len(html),
                word_count=extracted.get("word_count", 0),
                internal_links=len(extracted.get("internal_links", [])),
                images=len(extracted.get("images", [])),
                has_json_ld=bool(extracted.get("json_ld")),
            )

        except CircuitOpenError as e:
            self.log.warning(
                "circuit_open",
                url=url,
                domain=get_domain(url),
                reason=str(e),
            )
            await self.state.stats.record_error("circuit_open")
            if self.storage:
                await self.storage.mark_error(url, status="circuit_open")
            return

        except FetchError as e:
            kind = "retried" if e.retryable else "permanent"
            self.log.warning(
                "fetch_failed",
                url=url,
                kind=kind,
                reason=str(e),
                duration_ms=round((time.monotonic() - fetch_start) * 1000, 1),
            )
            await self.state.stats.record_error(kind)
            if self.storage:
                await self.storage.mark_error(url, status=kind)
            return

        except (RedirectError, SSRFError) as e:
            self.log.warning("security_blocked", url=url, reason=str(e))
            if self.storage:
                await self.storage.mark_error(url, status="security_blocked")
            return

        except Exception as e:
            self.log.error("unexpected_error", url=url, exc_info=True)
            await self.state.stats.record_error("other")
            if self.storage:
                await self.storage.mark_error(url, status="error")
            return

        normalized = normalize_url(url)

        # ── Canonical URL resolution ──────────────────────────────────────
        # If the page declares a canonical that differs from what we fetched,
        # store under the canonical URL instead.
        canonical_raw = extracted.get("meta", {}).get("canonical", "")
        canonical = self.canonical_resolver.resolve(url, canonical_raw)

        if canonical and canonical != normalized:
            if canonical in self.state.seen_urls:
                self.log.info(
                    "canonical_already_crawled",
                    fetched_url=url,
                    canonical=canonical,
                )
                return  # canonical already stored, discard this fetch
            self.log.info(
                "canonical_rekeyed",
                fetched_url=url,
                canonical=canonical,
            )
            store_url = canonical
        else:
            store_url = normalized

        # ── Content deduplication ─────────────────────────────────────────
        # Same content_hash at a different URL = duplicate page. Skip it.
        content_hash = extracted.get("content_hash")
        is_dup, original_url = await self.state.deduplicator.check_and_register(
            content_hash, store_url
        )
        if is_dup:
            self.log.info(
                "duplicate_content_skipped",
                url=url,
                store_url=store_url,
                original_url=original_url,
                content_hash=content_hash[:12] + "..." if content_hash else None,
            )
            await self.state.stats.record_skipped_seen()
            return

        await self.state.store_result(store_url, extracted)

        await self.state.stats.record_page_crawled(
            url=url,
            domain=get_domain(url),
            fetch_duration_ms=fetch_ms,
            html_bytes=len(html),
            word_count=extracted.get("word_count", 0),
        )

        if self.storage is not None:
            await self.storage.upsert_page(store_url, extracted, status="ok")

        await self._enqueue_links(extracted.get("internal_links", []))


class AsyncCrawler:
    """Manages the worker pool lifecycle."""

    def __init__(
        self,
        base_url: str,
        max_concurrency: int = 6,
        max_pages: int = 50,
        rate_limit_delay: float = 1.0,
        allow_cross_domain_redirects: bool = False,
        retry_config: RetryConfig | None = None,
        circuit_failure_threshold: int = 5,
        circuit_cooldown: float = 60.0,
        storage: Optional[CrawlStorage] = None,
        ttl_days: int = 7,
        progress_every: int = 10,
        max_depth: int = 0,   # 0 = unlimited
    ):
        self.base_url = base_url
        self.base_domain = get_domain(base_url)
        self.max_concurrency = max_concurrency
        self.max_pages = max_pages
        self.allow_cross_domain_redirects = allow_cross_domain_redirects
        self.retry_config = retry_config
        self.circuit_failure_threshold = circuit_failure_threshold
        self.circuit_cooldown = circuit_cooldown
        self.rate_limit_delay = rate_limit_delay
        self.storage = storage
        self.ttl_days = ttl_days
        self.progress_every = progress_every
        self.max_depth = max_depth
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.session.close()

    async def _progress_reporter(
        self,
        state: CrawlState,
        queue: asyncio.Queue,
        stop_event: asyncio.Event,
    ) -> None:
        """
        Background task that logs progress every `progress_every` seconds.
        Runs until stop_event is set (after queue.join() completes).
        """
        interval = max(5.0, self.progress_every)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            state.stats.log_progress(queue_size=queue.qsize())

    async def crawl(self) -> dict:
        state = CrawlState(max_pages=self.max_pages)

        _log.info(
            "crawl_started",
            base_url=self.base_url,
            base_domain=self.base_domain,
            max_pages=self.max_pages,
            max_depth=self.max_depth if self.max_depth > 0 else "unlimited",
            max_concurrency=self.max_concurrency,
            rate_limit_delay=self.rate_limit_delay,
            ttl_days=self.ttl_days,
        )

        robots_cache = RobotsCache(self.session)
        rate_limiter = RateLimiter(default_delay=self.rate_limit_delay)
        fetcher = ResilientFetcher(
            retry_config=self.retry_config,
            failure_threshold=self.circuit_failure_threshold,
            circuit_cooldown=self.circuit_cooldown,
        )

        # Resumability: seed seen_urls from storage
        if self.storage is not None:
            seen_in_db = await self.storage.get_seen_urls(self.base_domain)
            if seen_in_db:
                async with state.lock:
                    state.seen_urls.update(seen_in_db)
                _log.info(
                    "crawl_resumed",
                    domain=self.base_domain,
                    preloaded_urls=len(seen_in_db),
                )

        # Seed queue with start URL
        queue: asyncio.Queue = asyncio.Queue()
        normalized_start = normalize_url(self.base_url)
        await state.try_enqueue(normalized_start)
        await queue.put((self.base_url, 0))

        # ── Graceful shutdown via SIGINT (Ctrl+C) ────────────────────────
        # When SIGINT fires:
        #   1. shutdown_event is set — workers stop pulling new URLs
        #   2. Any in-flight _process() calls finish their current page
        #   3. queue.join() unblocks once all task_done() calls complete
        #   4. Summary is logged, session closed normally
        # Result: no torn writes, no half-stored pages, clean exit.
        shutdown_event = asyncio.Event()

        loop = asyncio.get_running_loop()

        def _handle_sigint():
            if not shutdown_event.is_set():
                _log.warning(
                    "shutdown_requested",
                    reason="SIGINT received — finishing in-flight pages then stopping",
                )
                shutdown_event.set()
                # Unblock any workers blocked on queue.get()
                for _ in range(self.max_concurrency):
                    try:
                        queue.put_nowait(None)
                    except asyncio.QueueFull:
                        pass

        loop.add_signal_handler(signal.SIGINT, _handle_sigint)

        workers = [
            Worker(
                worker_id=i,
                queue=queue,
                state=state,
                session=self.session,
                base_domain=self.base_domain,
                robots_cache=robots_cache,
                rate_limiter=rate_limiter,
                fetcher=fetcher,
                allow_cross_domain_redirects=self.allow_cross_domain_redirects,
                storage=self.storage,
                ttl_days=self.ttl_days,
                shutdown_event=shutdown_event,
                max_depth=self.max_depth,
            )
            for i in range(self.max_concurrency)
        ]

        worker_tasks = [
            asyncio.create_task(w.run(), name=f"worker-{w.id}")
            for w in workers
        ]

        # Background progress reporter
        stop_progress = asyncio.Event()
        progress_task = asyncio.create_task(
            self._progress_reporter(state, queue, stop_progress)
        )

        await queue.join()

        # Clean exit — remove our SIGINT handler, restore default
        loop.remove_signal_handler(signal.SIGINT)

        if shutdown_event.is_set():
            _log.warning(
                "crawl_interrupted",
                pages_crawled=state.pages_crawled(),
                reason="SIGINT — partial results follow",
            )

        # Stop progress reporter
        stop_progress.set()
        await progress_task

        # Send shutdown sentinel — propagates to all workers
        await queue.put(None)
        await asyncio.gather(*worker_tasks)

        state.stats.log_summary(circuit_states=fetcher.circuit_states())

        return state.page_data


async def crawl_site_async(
    base_url: str,
    max_concurrency: int = 6,
    max_pages: int = 50,
    rate_limit_delay: float = 1.0,
    retry_config: RetryConfig | None = None,
    circuit_failure_threshold: int = 5,
    circuit_cooldown: float = 60.0,
    storage: Optional[CrawlStorage] = None,
    ttl_days: int = 7,
    progress_every: int = 10,
    max_depth: int = 0,
) -> dict:
    async with AsyncCrawler(
        base_url,
        max_concurrency=max_concurrency,
        max_pages=max_pages,
        rate_limit_delay=rate_limit_delay,
        retry_config=retry_config,
        circuit_failure_threshold=circuit_failure_threshold,
        circuit_cooldown=circuit_cooldown,
        storage=storage,
        ttl_days=ttl_days,
        progress_every=progress_every,
        max_depth=max_depth,
    ) as crawler:
        return await crawler.crawl()