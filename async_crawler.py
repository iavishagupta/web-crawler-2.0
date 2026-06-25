from crawl import get_domain, normalize_url
from extract_html import extract_page_data

import asyncio
import aiohttp


class AsyncCrawler:
    def __init__(self, BASE_URL: str, max_concurrency: int = 0, max_pages: int = 0):
        self.base_url = BASE_URL
        self.base_domain = get_domain(BASE_URL)

        self.max_concurrency = max_concurrency or 6
        self.max_pages = max_pages or 15

        self.page_data = {}
        self.queued_urls = set()
        self.visited_urls = set()
        self.all_tasks = set()

        self.lock = asyncio.Lock()
        self.semaphore = asyncio.Semaphore(self.max_concurrency)
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.session.close()

    async def should_visit(self, normalized_url: str) -> bool:
        async with self.lock:
            if normalized_url in self.visited_urls:
                return False

            if normalized_url in self.queued_urls:
                return False

            if len(self.visited_urls) + len(self.queued_urls) >= self.max_pages:
                return False

            self.queued_urls.add(normalized_url)
            print(f"{len(self.visited_urls) + len(self.queued_urls)} in queue")
            return True

    async def mark_visited(self, normalized_url: str):
        async with self.lock:
            self.queued_urls.discard(normalized_url)
            self.visited_urls.add(normalized_url)

    async def get_html(self, url: str) -> str:
        async with self.session.get(
            url,
            headers={"User-Agent": "BootCrawler/1.0"}
        ) as res:
            if res.status >= 400:
                raise Exception(f"HTTP Error, exited with code {res.status}")

            content_type = res.headers.get("content-type", "").lower()
            if "text/html" not in content_type:
                raise Exception(f"Returned content is not HTML type: {content_type}")

            return await res.text()

    async def crawl_page(self, current_url: str | None = None):
        if current_url is None:
            current_url = self.base_url

        if get_domain(current_url) != self.base_domain:
            return

        normalized_url = normalize_url(current_url)

        if not await self.should_visit(normalized_url):
            return

        async with self.semaphore:
            try:
                print(f"Crawling: {current_url}")
                html = await self.get_html(current_url)
                extracted_data = extract_page_data(html, current_url)
                print(f"Crawled: {current_url}")
            except Exception as e:
                print(f"Failed to crawl {current_url}: {e}")
                async with self.lock:
                    self.queued_urls.discard(normalized_url)
                return

        async with self.lock:
            self.page_data[normalized_url] = extracted_data

        await self.mark_visited(normalized_url)

        tasks = []

        for link in extracted_data.get("outgoing_links", []):
            if get_domain(link) == self.base_domain:
                task = asyncio.create_task(self.crawl_page(link))
                tasks.append(task)
                self.all_tasks.add(task)
                task.add_done_callback(self.all_tasks.discard)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def crawl(self):
        await self.crawl_page()
        return self.page_data


async def crawl_site_async(
    base_url: str,
    max_concurrency: int = 0,
    max_pages: int = 0
):
    async with AsyncCrawler(base_url, max_concurrency, max_pages) as crawler:
        return await crawler.crawl()