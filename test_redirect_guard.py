import aiohttp #type: ignore
from redirect_guard import get_html_with_guard, RedirectError, MAX_REDIRECTS, FETCH_TIMEOUT
from ssrf_guard import SSRFError
import unittest

class TestRedirectGuard(unittest.IsolatedAsyncioTestCase):
    async def _test():
        async with aiohttp.ClientSession() as session:

            print("=== Redirect guard tests ===\n")

            # 1. Normal page (no redirect)
            try:
                html, final = await get_html_with_guard(session, "https://example.com/")
                print(f"  PASS  Normal page fetched, {len(html)} bytes, final URL: {final}")
            except Exception as e:
                print(f"  INFO  {e}")

            # 2. SSRF attempt via URL (should be blocked before any request)
            try:
                await get_html_with_guard(session, "http://169.254.169.254/meta-data/")
                print("  FAIL  SSRF URL should have been blocked")
            except (SSRFError, RedirectError) as e:
                print(f"  PASS  SSRF blocked: {e}")

            # 3. Cross-domain redirect enforcement
            print("\n  (Cross-domain redirect test requires a live redirecting server)")
            print("  In production: boot.dev → cdn.boot.dev would be blocked when")
            print("  base_domain='boot.dev' and allow_cross_domain=False")

            print("\n  Stats summary:")
            print(f"    MAX_REDIRECTS = {MAX_REDIRECTS}")
            print(f"    FETCH_TIMEOUT = total={FETCH_TIMEOUT.total}s  connect={FETCH_TIMEOUT.connect}s  read={FETCH_TIMEOUT.sock_read}s")


if __name__ == "__main__":
    unittest.main()