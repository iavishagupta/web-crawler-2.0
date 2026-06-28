import urllib.parse as urlparse
from rate_limiter import RateLimiter
import asyncio, time 
import unittest

class TestRateLimiter(unittest.IsolatedAsyncioTestCase):
    async def _test():
        limiter = RateLimiter(default_delay=0.5)
    
        # Simulate hitting example.com three times
        domains = ["example.com", "example.com", "boot.dev", "example.com"]
        print("=== Rate limiter test (0.5s delay per domain) ===")
    
        for domain in domains:
            t0 = time.monotonic()
            await limiter.wait(domain)
            elapsed = time.monotonic() - t0
            print(f"  {domain:<20}  waited {elapsed:.3f}s")
    
        print(f"\n  Stats: {limiter.stats()}")
    
        # Test robots.txt crawl-delay integration
        limiter.set_domain_delay("slow-site.com", 5.0)
        limiter.set_domain_delay("fast-site.com", 0.1)  # will be clamped to MIN
        print(f"\n  slow-site.com delay: {limiter.get_delay('slow-site.com')}s")
        print(f"  fast-site.com delay: {limiter.get_delay('fast-site.com')}s (clamped from 0.1)")
    
    
if __name__ == "__main__":
    unittest.main()