from typing import Optional
import aiohttp#type:ignore
import os
import asyncio
import time
import signal

from ssrf_guard import validate_url_safe, SSRFError
from crawl_quality import ContentDeduplicator
from crawl_stats import CrawlStats
from storage import CrawlStorage
from resilience import RetryConfig
from robots_guard import RobotsCache
from rate_limiter import RateLimiter
from resilience import ResilientFetcher, CircuitOpenError, FetchError
from logger import get_logger
from crawl_quality import CanonicalResolver
from url_normalizer import get_domain, normalize_url
from extract_html import extract_page_data
from redirect_guard import RedirectError

from dotenv import load_dotenv#type:ignore
load_dotenv()

USER_AGENT = os.getenv("USER_AGENT")

_log = get_logger("crawler")

class CrawlState:
    def __init__(self, max_pages: int):
        self.max_pages=max_pages
        self.page_data=self.page_data
        self.seen_urls:set[str]=set()
        self.lock=asyncio.Lock()
        self.stats=CrawlStats()
        self.deduplicator=ContentDeduplicator()
    
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

class Worker :
    def __init__(
            self,
            worker_id:int,
            queue:asyncio.Queue,
            state:CrawlState,
            session:aiohttp.ClientSession,
            base_domain:str,
            robots_cache:RobotsCache,
            rate_limiter:RateLimiter,
            fetcher:ResilientFetcher,
            allow_cross_domain_redirects: bool=False,
            storage:Optional[CrawlStorage]=None,
            ttl_days:int=7,
            shutdown_event:asyncio.Event | None = None,
            max_depth:int=0,
            ):
        self.id=worker_id
        self.queue=queue
        self.state=state
        self.session=session
        self.base_domain=base_domain
        self.robots_cache=robots_cache
        self.rate_limiter=rate_limiter
        self.fetcher=fetcher
        self.allow_cross_domain_redirects=allow_cross_domain_redirects
        self.storage=storage
        self.ttl_days=ttl_days
        self.shutdown_event=shutdown_event or asyncio.Event()
        self.max_depth=max_depth

        self.log=get_logger("worker", worker_id=worker_id)
        self.canonical_resolver=CanonicalResolver(base_domain=base_domain)

    
    async def _is_allowed(self, url:str) -> bool:
        try:
            validate_url_safe(url)
        except SSRFError as e:
            self.log.warning("ssrf_blocked", url=url, reason=str(e))
            await self.state.stats.record_error("robots")
            return False
        
        return True
    
    async def _apply_rate_limit(self, url: str) -> None:
        domain=get_domain(url)
        if domain not in self.rate_limiter._domain_delays:
            delay = self.robots_cache.crawl_delay
            if delay is not None:
                self.rate_limiter.set_domain_delay(domain, delay)
                self.log.info(
                    "crawl_delay_set",
                    domain=domain,
                    delay_s=delay,
                    source="robots_txt"
                )
        await self.rate_limiter.wait(domain)


    async def _enqueue_links(self, links:list[str]) -> None:
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

    async def _process(self, url:str) -> None:
        if get_domain(url) != self.base_domain:
            return 
        
        if not await self._is_allowed(url):
            return
        
        if self.storage is not None:
            stale = await self.storage.is_stale(url, self.ttl_days)
            if not stale:
                self.log.info("page_is_fresh", url=url, ttl_days=self.ttl_days)
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
            extracted = extract_page_data(html=html, page_url=url)

            self.log.info(
                "page_crawled",
                url=url,
                final_url=final_url if final_url != url else None,
                duration=round(fetch_ms,1),
                html_bytes=len(html),
                word_count=extracted.get('word_count', 0),
                internal_links=len(extracted.get('internal_links', [])),
                images=len(extracted.get('images',[])),
                has_json_Id=bool(extracted.get('json_ld')),
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
                duration_ms=round((time.monotonic() - fetch_start) * 1000, 1)
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
            self.log.error("unexpected_error", url=url, exec_info=True)
            await self.state.stats.record_error("other")
            if self.storage:
                await self.storage.mark_error(url, status="error")
            return
        
        normalized = normalize_url(url)
        
        canonical_raw = extracted.get('meta', {}).get('canonical', "")
        canonical = self.canonical_resolver.resolve(url, canonical_raw)

        if canonical and canonical != normalized:
            if canonical in self.state.seen_urls:
                self.log.info(
                    "canonical_already_crawled",
                    fetched_url=url,
                    canonical=canonical,
                )
                return
            self.log.info(
                "canonical_rekeyed",
                fetched_url=url,
                canonical=canonical,
            )
            store_url=canonical
        else:
            store_url=normalized
            
        content_hash = extracted.get('content_hash')
        is_dup, original_url = await self.state.deduplicator.chech_and_register(content_hash=content_hash, url=store_url)
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
            word_count=extracted.get('word_count',0),
        )

        if self.storage is not None:
            await self.storage.upsert_page(store_url, extracted, status="ok")

        await self._enqueue_links(extracted.get('internal_links', []))

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


class AsyncCrawler:
    def __init__(
            self,
            base_url: str,
            max_concurrency: int=6,
            max_pages: int=10,
            rate_limit_delay: float=1.0,
            allow_cross_domain_redirects: bool=False,
            retry_config: RetryConfig | None = None,
            circuit_failure_threshold: int=5,
            circuit_cooldown: float=60.0,
            storage: Optional[CrawlStorage] = None,
            ttl_days: int=7,
            progress_every: int=10,
            max_depth: int=0,
    ):
        self.base_url=base_url,
        self.base_domain=get_domain(base_url)
        self.max_concurrency=max_concurrency,
        self.max_pages=max_pages,
        self.rate_limit_delay=rate_limit_delay,
        self.allow_cross_domain_redirects=allow_cross_domain_redirects,
        self.retry_config=retry_config,
        self.circuit_failure_threshold=circuit_failure_threshold,
        self.circuit_cooldown=circuit_cooldown,
        self.storage=storage,
        self.ttl_days=ttl_days,
        self.progress_every=progress_every,
        self.max_depth=max_depth,

    async def __aenter__(self):
        self.session=aiohttp.ClientSession(headers={"User-Agent":USER_AGENT})
        return self
    
    async def __aexit__(self, exc_type, exc, tb):
        await self.session.close()
    
    async def _progress_reporter(
        self,
        state: CrawlState,
        queue: asyncio.Queue,
        stop_event: asyncio.Event,
    ) -> None:
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
            max_depth=self.max_depth if self.max_depth else "unlimited",
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

        queue: asyncio.Queue = asyncio.Queue()
        normalize_start = normalize_url(self.base_url)
        await state.try_enqueue(normalize_start)
        await queue.put((self.base_url, 0))

        shutdown_event = asyncio.Event()

        loop = asyncio.Loop()

        def _handle_sigint():
            if not shutdown_event.is_set():
                _log.warning(
                    "shutdown_requested",
                    reason="SIGINT received — finishing in-flight pages then stopping",
                )
                shutdown_event.set()

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
                max_depth=self.max_depth
            )
            for i in range(self.max_concurrency)
        ]

        worker_tasks = [
            asyncio.create_task(w.run(), name=f"worker-{w.id}")
            for w in workers
        ]

        stop_progress = asyncio.Event()
        progress_task = asyncio.create_task(
            self._progress_reporter(state, queue, stop_progress)
        )

        await queue.join()

        loop.remove_signal_handler(signal.SIGNIT)

        if shutdown_event.is_set():
            _log.warning(
                "crawl_interrupted",
                pages_crawled=state.pages_crawled(),
                reason="SIGNIT - partial results follow",
            )

        stop_progress.set()
        await progress_task

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