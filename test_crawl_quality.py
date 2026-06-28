"""
test_crawl_quality.py — Tests for tracking param stripping,
canonical URL resolution, and content deduplication.
"""

import asyncio
import unittest

import sys
sys.path.insert(0, '/home/claude/queue_crawler')

from url_normalizer import normalize_url, TRACKING_PARAMS
from crawl_quality import CanonicalResolver, ContentDeduplicator


# ── normalize_url ─────────────────────────────────────────────────────────────

class TestNormalizeUrl(unittest.TestCase):

    def test_strips_utm_params(self):
        url = "https://example.com/post?utm_source=twitter&utm_campaign=spring"
        self.assertEqual(normalize_url(url), "https://example.com/post")

    def test_strips_fbclid(self):
        url = "https://example.com/post?fbclid=abc123"
        self.assertEqual(normalize_url(url), "https://example.com/post")

    def test_strips_gclid(self):
        url = "https://example.com/post?gclid=xyz789"
        self.assertEqual(normalize_url(url), "https://example.com/post")

    def test_keeps_real_params(self):
        url = "https://example.com/search?q=python&page=3"
        result = normalize_url(url)
        self.assertIn("q=python", result)
        self.assertIn("page=3", result)

    def test_strips_tracking_keeps_real(self):
        url = "https://example.com/post?utm_source=x&page=2&sort=price"
        result = normalize_url(url)
        self.assertNotIn("utm_source", result)
        self.assertIn("page=2", result)
        self.assertIn("sort=price", result)

    def test_no_trailing_question_mark(self):
        url = "https://example.com/post?utm_source=x&gclid=y"
        result = normalize_url(url)
        self.assertFalse(result.endswith("?"))
        self.assertEqual(result, "https://example.com/post")

    def test_lowercases_host_and_scheme(self):
        url = "HTTPS://Example.COM/post"
        self.assertEqual(normalize_url(url), "https://example.com/post")

    def test_strips_fragment(self):
        url = "https://example.com/post#section"
        self.assertEqual(normalize_url(url), "https://example.com/post")

    def test_no_params_unchanged(self):
        url = "https://example.com/about"
        self.assertEqual(normalize_url(url), "https://example.com/about")

    def test_all_utm_variants_stripped(self):
        utm_variants = [
            "utm_source", "utm_medium", "utm_campaign",
            "utm_term", "utm_content", "utm_id",
        ]
        for param in utm_variants:
            url = f"https://example.com/page?{param}=test"
            result = normalize_url(url)
            self.assertNotIn(param, result, f"{param} should be stripped")
            self.assertEqual(result, "https://example.com/page")

    def test_tracking_params_set_is_frozen(self):
        # TRACKING_PARAMS should be immutable
        self.assertIsInstance(TRACKING_PARAMS, frozenset)

    def test_same_url_different_tracking_normalizes_equal(self):
        url1 = "https://example.com/post?utm_source=twitter"
        url2 = "https://example.com/post?utm_source=facebook"
        # Both should normalize to the same URL — they're the same page
        self.assertEqual(normalize_url(url1), normalize_url(url2))

    def test_different_real_params_stay_different(self):
        url1 = "https://example.com/posts?page=1"
        url2 = "https://example.com/posts?page=2"
        self.assertNotEqual(normalize_url(url1), normalize_url(url2))


# ── CanonicalResolver ─────────────────────────────────────────────────────────

class TestCanonicalResolver(unittest.TestCase):

    def setUp(self):
        self.resolver = CanonicalResolver(base_domain="example.com")

    def test_resolves_relative_canonical(self):
        result = self.resolver.resolve(
            "https://example.com/blog?page=2", "/blog"
        )
        self.assertEqual(result, "https://example.com/blog")

    def test_resolves_absolute_canonical(self):
        result = self.resolver.resolve(
            "https://example.com/blog?page=2",
            "https://example.com/blog"
        )
        self.assertEqual(result, "https://example.com/blog")

    def test_returns_none_for_same_url(self):
        result = self.resolver.resolve(
            "https://example.com/blog",
            "https://example.com/blog"
        )
        self.assertIsNone(result)

    def test_returns_none_for_empty_canonical(self):
        result = self.resolver.resolve("https://example.com/blog", "")
        self.assertIsNone(result)

    def test_returns_none_for_cross_domain(self):
        result = self.resolver.resolve(
            "https://example.com/page",
            "https://cdn.example.org/page"
        )
        self.assertIsNone(result)

    def test_strips_tracking_from_canonical(self):
        # Canonical itself has tracking params — strip them
        result = self.resolver.resolve(
            "https://example.com/post?page=1",
            "https://example.com/post?utm_source=x"
        )
        self.assertEqual(result, "https://example.com/post")

    def test_fetched_url_with_tracking_same_as_canonical(self):
        # Fetched URL had tracking; after normalization same as canonical
        result = self.resolver.resolve(
            "https://example.com/blog?utm_source=x",
            "https://example.com/blog"
        )
        # Both normalize to https://example.com/blog → None (no-op)
        self.assertIsNone(result)

    def test_does_not_follow_javascript_canonical(self):
        result = self.resolver.resolve(
            "https://example.com/page",
            "javascript:void(0)"
        )
        self.assertIsNone(result)


# ── ContentDeduplicator ───────────────────────────────────────────────────────

class TestContentDeduplicator(unittest.IsolatedAsyncioTestCase):

    async def test_first_hash_not_duplicate(self):
        dedup = ContentDeduplicator()
        is_dup, orig = await dedup.check_and_register("hash1", "https://example.com/a")
        self.assertFalse(is_dup)
        self.assertIsNone(orig)

    async def test_same_hash_is_duplicate(self):
        dedup = ContentDeduplicator()
        await dedup.check_and_register("hash1", "https://example.com/a")
        is_dup, orig = await dedup.check_and_register("hash1", "https://example.com/b")
        self.assertTrue(is_dup)
        self.assertEqual(orig, "https://example.com/a")

    async def test_different_hashes_not_duplicates(self):
        dedup = ContentDeduplicator()
        await dedup.check_and_register("hash1", "https://example.com/a")
        is_dup, _ = await dedup.check_and_register("hash2", "https://example.com/b")
        self.assertFalse(is_dup)

    async def test_none_hash_never_deduplicated(self):
        dedup = ContentDeduplicator()
        is_dup, orig = await dedup.check_and_register(None, "https://example.com/a")
        self.assertFalse(is_dup)
        self.assertIsNone(orig)
        # Second None also not deduplicated
        is_dup2, _ = await dedup.check_and_register(None, "https://example.com/b")
        self.assertFalse(is_dup2)

    async def test_empty_hash_never_deduplicated(self):
        dedup = ContentDeduplicator()
        is_dup, _ = await dedup.check_and_register("", "https://example.com/a")
        self.assertFalse(is_dup)

    async def test_stats_counts_unique_hashes(self):
        dedup = ContentDeduplicator()
        await dedup.check_and_register("hash1", "https://example.com/a")
        await dedup.check_and_register("hash2", "https://example.com/b")
        await dedup.check_and_register("hash1", "https://example.com/c")  # dup
        self.assertEqual(dedup.stats()["unique_content_hashes"], 2)

    async def test_concurrent_same_hash_only_one_wins(self):
        """Two workers racing to register the same hash — only one should win."""
        dedup = ContentDeduplicator()

        results = await asyncio.gather(
            dedup.check_and_register("shared_hash", "https://example.com/a"),
            dedup.check_and_register("shared_hash", "https://example.com/b"),
        )

        is_dups = [r[0] for r in results]
        # Exactly one should be a duplicate, one should not
        self.assertEqual(is_dups.count(False), 1)
        self.assertEqual(is_dups.count(True), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)