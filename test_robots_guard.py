import aiohttp, asyncio #type: ignore
from robots_guard import RobotsCache
import unittest 

class TestRobotsGuard(unittest.IsolatedAsyncioTestCase):
    async def _test():
        async with aiohttp.ClientSession() as session:
            cache = RobotsCache(session)

            test_cases = [
                # Google disallows many paths
                ("https://www.google.com/search", False),
                # Google homepage is generally allowed
                ("https://www.google.com/", True),
                # example.com has no robots.txt → everything allowed
                ("https://example.com/anything", True),
            ]

            print("=== robots.txt compliance ===")
            for url, expected_allowed in test_cases:
                allowed = await cache.can_fetch(url)
                status = "PASS" if allowed == expected_allowed else "FAIL"
                print(f"  {status}  {'ALLOW' if allowed else 'BLOCK'}  {url}")

            # Crawl-delay demo
            delay = await cache.crawl_delay("https://www.google.com/")
            print(f"\n  Google crawl-delay: {delay}s")


if __name__ == "__main__":
    unittest.main()