import os
import asyncio
import urllib.robotparser
import urllib.parse as urlparse
from typing import Optional
import aiohttp #type: ignore

from dotenv import load_dotenv #type: ignore
load_dotenv()

USER_AGENT =  os.getenv("USER_AGENT")

# How long to wait for robots.txt before giving up (seconds)
ROBOTS_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=10)

# Cache TTL isn't implemented here (keep it simple for now),
# but you'd add time.time() checks in a long-running crawler.


class RobotsCache:
    """
    Async, per-domain cache of RobotFileParser objects.

    Usage:
        cache = RobotsCache(session)
        allowed = await cache.can_fetch("https://example.com/page")
        delay   = await cache.crawl_delay("https://example.com")
    """

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session
        self._cache: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._lock = asyncio.Lock()

    def _robots_url(self, url: str) -> str:
        parsed = urlparse.urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    async def _fetch_robots(self, robots_url: str) -> urllib.robotparser.RobotFileParser:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)

        try:
            async with self._session.get(
                robots_url,
                timeout=ROBOTS_FETCH_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=True,
            ) as res:
                if res.status == 200:
                    text = await res.text(errors="replace")
                    rp.parse(text.splitlines())
                elif res.status == 401 or res.status == 403:
                    # Site explicitly forbids access to robots.txt —
                    # treat the entire site as disallowed
                    rp.parse(["User-agent: *", "Disallow: /"])
                # 404, 5xx, etc. → rp has no rules → everything allowed
        except Exception:
            # Network error, timeout, etc. → fail open
            pass

        return rp

    async def _get_parser(self, url: str) -> urllib.robotparser.RobotFileParser:
        robots_url = self._robots_url(url)

        # Fast path — no lock needed for reads
        if robots_url in self._cache:
            return self._cache[robots_url]

        # Slow path — fetch once, cache result
        async with self._lock:
            # Double-checked locking
            if robots_url in self._cache:
                return self._cache[robots_url]

            rp = await self._fetch_robots(robots_url)
            self._cache[robots_url] = rp
            return rp
   
    async def can_fetch(self, url: str) -> bool:
        """Return True if USER_AGENT is allowed to fetch this URL."""
        rp = await self._get_parser(url)
        return rp.can_fetch(USER_AGENT, url)

    async def crawl_delay(self, url: str) -> Optional[float]:
        """
        Return the Crawl-delay for USER_AGENT, or None if not specified.
        Use this to sleep between requests to a domain.
        """
        rp = await self._get_parser(url)
        delay = rp.crawl_delay(USER_AGENT)
        return float(delay) if delay is not None else None