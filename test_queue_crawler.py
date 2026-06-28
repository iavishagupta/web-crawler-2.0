"""
test_queue_crawler.py — Tests for queue-worker architecture

Focuses on the mechanics the recursive design couldn't easily test:
  - Workers process every URL exactly once
  - Workers don't exceed max_pages
  - Discovered links are enqueued and processed
  - Failed pages don't block the rest of the crawl
  - All workers shut down cleanly
  - Concurrent workers don't double-process URLs

These tests mock the HTTP layer entirely — no network calls.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch, MagicMock
from collections import defaultdict

import sys
sys.path.insert(0, '/home/claude/queue_crawler')

from async_crawler import CrawlState, Worker, AsyncCrawler, crawl_site_async
from resilience import RetryConfig, FetchError


# ── Helpers ──────────────────────────────────────────────────────────────────

def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def make_html(links: list[str], heading: str = "Test") -> str:
    """Generate minimal HTML with the given outgoing links."""
    anchors = "".join(f'<a href="{l}">{l}</a>' for l in links)
    return f"<html><body><h1>{heading}</h1>{anchors}</body></html>"


def make_mock_session():
    return MagicMock()


def patch_fetcher(url_map: dict[str, str]):
    """
    Returns a mock for ResilientFetcher.fetch that serves HTML
    from url_map. Raises FetchError for URLs not in the map.
    """
    async def mock_fetch(session, url, **kwargs):
        # Normalize slightly for matching
        key = url.rstrip("/") or url
        for k, html in url_map.items():
            if url == k or url.rstrip("/") == k.rstrip("/"):
                return html, url
        raise FetchError(f"404: {url}", retryable=False)

    return AsyncMock(side_effect=mock_fetch)


# ── CrawlState tests ──────────────────────────────────────────────────────────

class TestCrawlState(unittest.IsolatedAsyncioTestCase):

    async def test_try_enqueue_new_url(self):
        state = CrawlState(max_pages=10)
        result = await state.try_enqueue("https://example.com/page")
        self.assertTrue(result)
        self.assertIn("https://example.com/page", state.seen_urls)

    async def test_try_enqueue_duplicate(self):
        state = CrawlState(max_pages=10)
        await state.try_enqueue("https://example.com/page")
        result = await state.try_enqueue("https://example.com/page")
        self.assertFalse(result)
        self.assertEqual(len(state.seen_urls), 1)

    async def test_try_enqueue_respects_max_pages(self):
        state = CrawlState(max_pages=2)
        await state.try_enqueue("https://example.com/a")
        await state.try_enqueue("https://example.com/b")
        result = await state.try_enqueue("https://example.com/c")
        self.assertFalse(result)
        self.assertEqual(len(state.seen_urls), 2)

    async def test_store_result(self):
        state = CrawlState(max_pages=10)
        await state.store_result("https://example.com/", {"heading": "Home"})
        self.assertEqual(state.page_data["https://example.com/"]["heading"], "Home")

    async def test_record_error_counts(self):
        state = CrawlState(max_pages=10)
        await state.stats.record_error("ssrf")
        await state.stats.record_error("ssrf")
        await state.stats.record_error("permanent")
        self.assertEqual(state.stats.errors["ssrf"], 2)
        self.assertEqual(state.stats.errors["permanent"], 1)


# ── Worker._enqueue_links tests ───────────────────────────────────────────────

class TestWorkerEnqueue(unittest.IsolatedAsyncioTestCase):

    def _make_worker(self, max_pages=10):
        queue = asyncio.Queue()
        state = CrawlState(max_pages=max_pages)
        session = make_mock_session()

        robots = MagicMock()
        robots.can_fetch = AsyncMock(return_value=True)
        robots.crawl_delay = AsyncMock(return_value=None)

        rate_limiter = MagicMock()
        rate_limiter._domain_delays = {}
        rate_limiter.wait = AsyncMock()
        rate_limiter.set_domain_delay = MagicMock()

        fetcher = MagicMock()

        worker = Worker(
            worker_id=0,
            queue=queue,
            state=state,
            session=session,
            base_domain="example.com",
            robots_cache=robots,
            rate_limiter=rate_limiter,
            fetcher=fetcher,
        )
        return worker, queue, state

    async def test_enqueues_same_domain_links(self):
        worker, queue, state = self._make_worker()
        # Seed state so base URL is already seen
        await state.try_enqueue("https://example.com/")

        await worker._enqueue_links([
            "https://example.com/about",
            "https://example.com/blog",
        ])

        self.assertEqual(queue.qsize(), 2)

    async def test_ignores_external_links(self):
        # Domain filtering now happens in extract_page_data (internal_links field),
        # not in _enqueue_links. _enqueue_links receives already-filtered URLs.
        # This test verifies the extractor filters correctly.
        from extract_html import extract_page_data
        html = '''<html><body>
            <a href="https://other.com/page">External</a>
            <a href="https://cdn.example.org/asset">CDN</a>
            <a href="/internal">Internal</a>
        </body></html>'''
        data = extract_page_data(html, "https://example.com/")
        # internal_links should only contain same-domain URLs
        for link in data["internal_links"]:
            self.assertIn("example.com", link)
        # external_links should contain the off-domain ones
        self.assertTrue(any("other.com" in l for l in data["external_links"]))

    async def test_does_not_enqueue_duplicates(self):
        worker, queue, state = self._make_worker()
        await state.try_enqueue("https://example.com/")

        await worker._enqueue_links(["https://example.com/about"])
        await worker._enqueue_links(["https://example.com/about"])  # same URL

        self.assertEqual(queue.qsize(), 1)

    async def test_respects_max_pages(self):
        worker, queue, state = self._make_worker(max_pages=2)
        await state.try_enqueue("https://example.com/")  # 1 of 2

        await worker._enqueue_links([
            "https://example.com/a",  # 2 of 2 — accepted
            "https://example.com/b",  # 3 of 2 — rejected
        ])

        self.assertEqual(queue.qsize(), 1)


# ── Worker._process tests ─────────────────────────────────────────────────────

class TestWorkerProcess(unittest.IsolatedAsyncioTestCase):

    def _make_worker(self, url_map: dict[str, str], max_pages=10):
        queue = asyncio.Queue()
        state = CrawlState(max_pages=max_pages)
        session = make_mock_session()

        robots = MagicMock()
        robots.can_fetch = AsyncMock(return_value=True)
        robots.crawl_delay = AsyncMock(return_value=None)

        rate_limiter = MagicMock()
        rate_limiter._domain_delays = {}
        rate_limiter.wait = AsyncMock()
        rate_limiter.set_domain_delay = MagicMock()

        fetcher = MagicMock()
        fetcher.fetch = patch_fetcher(url_map)

        worker = Worker(
            worker_id=0,
            queue=queue,
            state=state,
            session=session,
            base_domain="example.com",
            robots_cache=robots,
            rate_limiter=rate_limiter,
            fetcher=fetcher,
        )
        return worker, queue, state

    async def test_stores_crawled_page(self):
        html = make_html([], heading="Home")
        worker, queue, state = self._make_worker({"https://example.com/": html})
        await state.try_enqueue("https://example.com/")

        await worker._process("https://example.com/")

        self.assertEqual(len(state.page_data), 1)

    async def test_enqueues_discovered_links(self):
        html = make_html(["https://example.com/about", "https://example.com/blog"])
        worker, queue, state = self._make_worker({"https://example.com/": html})
        await state.try_enqueue("https://example.com/")

        await worker._process("https://example.com/")

        # Two links should have been enqueued
        self.assertEqual(queue.qsize(), 2)

    async def test_records_permanent_error(self):
        worker, queue, state = self._make_worker({})  # empty map → 404 for all

        # 404 is a permanent FetchError, should be recorded
        await state.try_enqueue("https://example.com/missing")
        await worker._process("https://example.com/missing")

        self.assertEqual(state.stats.errors["permanent"], 1)
        self.assertEqual(len(state.page_data), 0)

    async def test_skips_external_domain(self):
        worker, queue, state = self._make_worker({})
        await state.try_enqueue("https://other.com/page")

        await worker._process("https://other.com/page")

        self.assertEqual(len(state.page_data), 0)
        self.assertEqual(state.stats.errors["other"], 0)


# ── Full crawl integration test ───────────────────────────────────────────────

class TestFullCrawl(unittest.IsolatedAsyncioTestCase):
    """
    End-to-end test of the queue-worker architecture with a mocked
    three-page site:

        /  →  /about, /blog
        /about  →  /
        /blog   →  /blog/post-1
        /blog/post-1  →  (no links)
    """

    def _site_map(self):
        return {
            "https://example.com/": make_html([
                "https://example.com/about",
                "https://example.com/blog",
            ], heading="Home"),
            "https://example.com/about": make_html([
                "https://example.com/",
            ], heading="About"),
            "https://example.com/blog": make_html([
                "https://example.com/blog/post-1",
            ], heading="Blog"),
            "https://example.com/blog/post-1": make_html([], heading="Post 1"),
        }

    async def test_crawls_all_pages(self):
        site = self._site_map()
        fetch_mock = patch_fetcher(site)

        with patch('async_crawler.ResilientFetcher') as MockFetcher, \
             patch('async_crawler.validate_url_safe'), \
             patch('async_crawler.RobotsCache') as MockRobots:

            MockFetcher.return_value.fetch = fetch_mock
            MockFetcher.return_value.circuit_states = MagicMock(return_value={})

            robots_instance = MagicMock()
            robots_instance.can_fetch = AsyncMock(return_value=True)
            robots_instance.crawl_delay = AsyncMock(return_value=None)
            MockRobots.return_value = robots_instance

            with patch('aiohttp.ClientSession'):
                crawler = AsyncCrawler(
                    "https://example.com/",
                    max_concurrency=2,
                    max_pages=10,
                    rate_limit_delay=0.0,
                )
                crawler.session = MagicMock()
                result = await crawler.crawl()

        self.assertEqual(len(result), 4)

    async def test_each_url_processed_exactly_once(self):
        site = self._site_map()
        call_counts: dict[str, int] = defaultdict(int)

        async def counting_fetch(session, url, **kwargs):
            # Strip trailing slash for counting
            call_counts[url.rstrip("/")] += 1
            for k, html in site.items():
                if url.rstrip("/") == k.rstrip("/"):
                    return html, url
            raise FetchError(f"404: {url}", retryable=False)

        fetch_mock = AsyncMock(side_effect=counting_fetch)

        with patch('async_crawler.ResilientFetcher') as MockFetcher, \
             patch('async_crawler.validate_url_safe'), \
             patch('async_crawler.RobotsCache') as MockRobots:

            MockFetcher.return_value.fetch = fetch_mock
            MockFetcher.return_value.circuit_states = MagicMock(return_value={})

            robots_instance = MagicMock()
            robots_instance.can_fetch = AsyncMock(return_value=True)
            robots_instance.crawl_delay = AsyncMock(return_value=None)
            MockRobots.return_value = robots_instance

            with patch('aiohttp.ClientSession'):
                crawler = AsyncCrawler(
                    "https://example.com/",
                    max_concurrency=4,
                    max_pages=20,
                    rate_limit_delay=0.0,
                )
                crawler.session = MagicMock()
                await crawler.crawl()

        for url, count in call_counts.items():
            self.assertEqual(count, 1, f"{url} was fetched {count} times (expected 1)")

    async def test_max_pages_respected(self):
        site = self._site_map()
        fetch_mock = patch_fetcher(site)

        with patch('async_crawler.ResilientFetcher') as MockFetcher, \
             patch('async_crawler.validate_url_safe'), \
             patch('async_crawler.RobotsCache') as MockRobots:

            MockFetcher.return_value.fetch = fetch_mock
            MockFetcher.return_value.circuit_states = MagicMock(return_value={})

            robots_instance = MagicMock()
            robots_instance.can_fetch = AsyncMock(return_value=True)
            robots_instance.crawl_delay = AsyncMock(return_value=None)
            MockRobots.return_value = robots_instance

            with patch('aiohttp.ClientSession'):
                crawler = AsyncCrawler(
                    "https://example.com/",
                    max_concurrency=2,
                    max_pages=2,          # hard cap
                    rate_limit_delay=0.0,
                )
                crawler.session = MagicMock()
                result = await crawler.crawl()

        self.assertLessEqual(len(result), 2)

    async def test_failed_page_does_not_block_crawl(self):
        """A 404 on /about should not prevent /blog from being crawled."""
        site = {
            "https://example.com/": make_html([
                "https://example.com/about",   # will 404
                "https://example.com/blog",    # should succeed
            ]),
            # /about intentionally missing
            "https://example.com/blog": make_html([], heading="Blog"),
        }
        fetch_mock = patch_fetcher(site)

        with patch('async_crawler.ResilientFetcher') as MockFetcher, \
             patch('async_crawler.validate_url_safe'), \
             patch('async_crawler.RobotsCache') as MockRobots:

            MockFetcher.return_value.fetch = fetch_mock
            MockFetcher.return_value.circuit_states = MagicMock(return_value={})

            robots_instance = MagicMock()
            robots_instance.can_fetch = AsyncMock(return_value=True)
            robots_instance.crawl_delay = AsyncMock(return_value=None)
            MockRobots.return_value = robots_instance

            with patch('aiohttp.ClientSession'):
                crawler = AsyncCrawler(
                    "https://example.com/",
                    max_concurrency=2,
                    max_pages=10,
                    rate_limit_delay=0.0,
                )
                crawler.session = MagicMock()
                result = await crawler.crawl()

        # / and /blog should be in results, /about should not
        crawled = set(result.keys())
        self.assertTrue(any("blog" in k for k in crawled), f"blog missing from {crawled}")
        self.assertFalse(any("about" in k for k in crawled), f"about should not be in {crawled}")


if __name__ == "__main__":
    unittest.main(verbosity=2)