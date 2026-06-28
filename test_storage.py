import asyncio
import json
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, call

import sys
sys.path.insert(0, '/home/claude/queue_crawler')

from storage import CrawlStorage


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_storage():
    """Return a CrawlStorage with a fully mocked asyncpg pool."""
    pool = MagicMock()
    conn = AsyncMock()

    # pool.acquire() is an async context manager
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acquire_ctx)

    storage = CrawlStorage(pool)
    return storage, conn


def make_row(**kwargs):
    """Return a dict-like object that supports row["key"] access."""
    row = MagicMock()
    row.__getitem__ = lambda self, key: kwargs[key]
    row.get = lambda key, default=None: kwargs.get(key, default)
    return row


# ── upsert_page ───────────────────────────────────────────────────────────────

class TestUpsertPage(unittest.IsolatedAsyncioTestCase):

    async def test_executes_insert_sql(self):
        storage, conn = make_storage()
        data = {"content_hash": "abc123", "title": "Test"}
        await storage.upsert_page("https://example.com/page", data)
        conn.execute.assert_called_once()
        sql, *args = conn.execute.call_args[0]
        self.assertIn("INSERT INTO crawled_pages", sql)
        self.assertIn("ON CONFLICT", sql)

    async def test_passes_correct_url(self):
        storage, conn = make_storage()
        url = "https://example.com/page"
        await storage.upsert_page(url, {"content_hash": "abc"})
        _, url_arg, *_ = conn.execute.call_args[0]
        self.assertEqual(url_arg, url)

    async def test_extracts_domain_from_url(self):
        storage, conn = make_storage()
        await storage.upsert_page("https://example.com/page", {"content_hash": "abc"})
        _, _url, domain, *_ = conn.execute.call_args[0]
        self.assertEqual(domain, "example.com")

    async def test_serializes_data_as_json(self):
        storage, conn = make_storage()
        data = {"title": "Test", "content_hash": "abc", "nested": {"key": "val"}}
        await storage.upsert_page("https://example.com/", data)
        # Last positional arg before named is the JSON string
        args = conn.execute.call_args[0]
        json_arg = args[-1]  # extracted_json is the last arg
        parsed = json.loads(json_arg)
        self.assertEqual(parsed["title"], "Test")
        self.assertEqual(parsed["nested"]["key"], "val")

    async def test_extracts_content_hash_from_data(self):
        storage, conn = make_storage()
        data = {"content_hash": "myhash123"}
        await storage.upsert_page("https://example.com/", data)
        args = conn.execute.call_args[0]
        # content_hash is 5th arg (index 4): url, domain, now, status, hash, json
        self.assertEqual(args[5], "myhash123")

    async def test_default_status_is_ok(self):
        storage, conn = make_storage()
        await storage.upsert_page("https://example.com/", {})
        args = conn.execute.call_args[0]
        status = args[4]
        self.assertEqual(status, "ok")

    async def test_custom_status_passed_through(self):
        storage, conn = make_storage()
        await storage.upsert_page("https://example.com/", {}, status="error")
        args = conn.execute.call_args[0]
        self.assertEqual(args[4], "error")


# ── mark_error ────────────────────────────────────────────────────────────────

class TestMarkError(unittest.IsolatedAsyncioTestCase):

    async def test_executes_insert_sql(self):
        storage, conn = make_storage()
        await storage.mark_error("https://example.com/broken")
        conn.execute.assert_called_once()
        sql = conn.execute.call_args[0][0]
        self.assertIn("INSERT INTO crawled_pages", sql)

    async def test_default_status_is_error(self):
        storage, conn = make_storage()
        await storage.mark_error("https://example.com/broken")
        args = conn.execute.call_args[0]
        self.assertEqual(args[4], "error")

    async def test_custom_status(self):
        storage, conn = make_storage()
        await storage.mark_error("https://example.com/broken", status="permanent")
        args = conn.execute.call_args[0]
        self.assertEqual(args[4], "permanent")


# ── get_page ──────────────────────────────────────────────────────────────────

class TestGetPage(unittest.IsolatedAsyncioTestCase):

    async def test_returns_parsed_data(self):
        storage, conn = make_storage()
        data = {"title": "Test", "word_count": 42}
        conn.fetchrow = AsyncMock(return_value=make_row(extracted=json.dumps(data)))
        result = await storage.get_page("https://example.com/page")
        self.assertEqual(result["title"], "Test")
        self.assertEqual(result["word_count"], 42)

    async def test_returns_none_if_not_found(self):
        storage, conn = make_storage()
        conn.fetchrow = AsyncMock(return_value=None)
        result = await storage.get_page("https://example.com/missing")
        self.assertIsNone(result)

    async def test_returns_none_if_extracted_is_null(self):
        storage, conn = make_storage()
        conn.fetchrow = AsyncMock(return_value=make_row(extracted=None))
        result = await storage.get_page("https://example.com/page")
        self.assertIsNone(result)

    async def test_queries_by_url(self):
        storage, conn = make_storage()
        conn.fetchrow = AsyncMock(return_value=None)
        await storage.get_page("https://example.com/specific")
        _, url_arg = conn.fetchrow.call_args[0]
        self.assertEqual(url_arg, "https://example.com/specific")


# ── is_stale ──────────────────────────────────────────────────────────────────

class TestIsStale(unittest.IsolatedAsyncioTestCase):

    async def test_true_if_url_not_in_db(self):
        storage, conn = make_storage()
        conn.fetchrow = AsyncMock(return_value=None)
        self.assertTrue(await storage.is_stale("https://example.com/new"))

    async def test_true_if_status_not_ok(self):
        storage, conn = make_storage()
        row = make_row(
            last_crawled=datetime.now(timezone.utc),
            status="error"
        )
        conn.fetchrow = AsyncMock(return_value=row)
        self.assertTrue(await storage.is_stale("https://example.com/page"))

    async def test_true_if_crawled_beyond_ttl(self):
        storage, conn = make_storage()
        old_time = datetime.now(timezone.utc) - timedelta(days=10)
        row = make_row(last_crawled=old_time, status="ok")
        conn.fetchrow = AsyncMock(return_value=row)
        self.assertTrue(await storage.is_stale("https://example.com/page", ttl_days=7))

    async def test_false_if_recently_crawled(self):
        storage, conn = make_storage()
        recent = datetime.now(timezone.utc) - timedelta(days=2)
        row = make_row(last_crawled=recent, status="ok")
        conn.fetchrow = AsyncMock(return_value=row)
        self.assertFalse(await storage.is_stale("https://example.com/page", ttl_days=7))

    async def test_exactly_at_ttl_boundary_is_stale(self):
        storage, conn = make_storage()
        # Exactly 7 days + 1 second ago → stale
        exactly_stale = datetime.now(timezone.utc) - timedelta(days=7, seconds=1)
        row = make_row(last_crawled=exactly_stale, status="ok")
        conn.fetchrow = AsyncMock(return_value=row)
        self.assertTrue(await storage.is_stale("https://example.com/page", ttl_days=7))

    async def test_ttl_days_respected(self):
        storage, conn = make_storage()
        # 3 days ago — stale with ttl=1, fresh with ttl=7
        three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)
        row = make_row(last_crawled=three_days_ago, status="ok")
        conn.fetchrow = AsyncMock(return_value=row)
        self.assertTrue(await storage.is_stale("https://example.com/", ttl_days=1))
        self.assertFalse(await storage.is_stale("https://example.com/", ttl_days=7))


# ── is_changed ────────────────────────────────────────────────────────────────

class TestIsChanged(unittest.IsolatedAsyncioTestCase):

    async def test_true_if_url_not_in_db(self):
        storage, conn = make_storage()
        conn.fetchrow = AsyncMock(return_value=None)
        self.assertTrue(await storage.is_changed("https://example.com/new", "newhash"))

    async def test_true_if_hash_differs(self):
        storage, conn = make_storage()
        conn.fetchrow = AsyncMock(return_value=make_row(content_hash="oldhash"))
        self.assertTrue(await storage.is_changed("https://example.com/page", "newhash"))

    async def test_false_if_hash_matches(self):
        storage, conn = make_storage()
        conn.fetchrow = AsyncMock(return_value=make_row(content_hash="samehash"))
        self.assertFalse(await storage.is_changed("https://example.com/page", "samehash"))


# ── get_seen_urls ─────────────────────────────────────────────────────────────

class TestGetSeenUrls(unittest.IsolatedAsyncioTestCase):

    async def test_returns_set_of_urls(self):
        storage, conn = make_storage()
        rows = [make_row(url="https://example.com/a"),
                make_row(url="https://example.com/b")]
        conn.fetch = AsyncMock(return_value=rows)
        result = await storage.get_seen_urls("example.com")
        self.assertIsInstance(result, set)
        self.assertIn("https://example.com/a", result)
        self.assertIn("https://example.com/b", result)

    async def test_returns_empty_set_if_no_rows(self):
        storage, conn = make_storage()
        conn.fetch = AsyncMock(return_value=[])
        result = await storage.get_seen_urls("example.com")
        self.assertEqual(result, set())

    async def test_queries_by_domain(self):
        storage, conn = make_storage()
        conn.fetch = AsyncMock(return_value=[])
        await storage.get_seen_urls("boot.dev")
        _, domain_arg = conn.fetch.call_args[0]
        self.assertEqual(domain_arg, "boot.dev")

    async def test_filters_by_ok_status_in_sql(self):
        storage, conn = make_storage()
        conn.fetch = AsyncMock(return_value=[])
        await storage.get_seen_urls("example.com")
        sql = conn.fetch.call_args[0][0]
        self.assertIn("status = 'ok'", sql)


# ── get_stale_urls ────────────────────────────────────────────────────────────

class TestGetStaleUrls(unittest.IsolatedAsyncioTestCase):

    async def test_returns_list_of_urls(self):
        storage, conn = make_storage()
        rows = [make_row(url="https://example.com/old")]
        conn.fetch = AsyncMock(return_value=rows)
        result = await storage.get_stale_urls("example.com", ttl_days=7)
        self.assertEqual(result, ["https://example.com/old"])

    async def test_returns_empty_list_if_none(self):
        storage, conn = make_storage()
        conn.fetch = AsyncMock(return_value=[])
        result = await storage.get_stale_urls("example.com")
        self.assertEqual(result, [])

    async def test_passes_limit(self):
        storage, conn = make_storage()
        conn.fetch = AsyncMock(return_value=[])
        await storage.get_stale_urls("example.com", ttl_days=7, limit=42)
        args = conn.fetch.call_args[0]
        self.assertIn(42, args)


# ── Schema SQL ────────────────────────────────────────────────────────────────

class TestSchemaSql(unittest.TestCase):
    """Verify the schema SQL contains the expected structural elements."""

    def test_schema_has_table(self):
        from storage import SCHEMA_SQL
        self.assertIn("CREATE TABLE IF NOT EXISTS crawled_pages", SCHEMA_SQL)

    def test_schema_has_primary_key(self):
        from storage import SCHEMA_SQL
        self.assertIn("url", SCHEMA_SQL)
        self.assertIn("PRIMARY KEY", SCHEMA_SQL)

    def test_schema_has_jsonb_column(self):
        from storage import SCHEMA_SQL
        self.assertIn("JSONB", SCHEMA_SQL)

    def test_schema_has_gin_index(self):
        from storage import SCHEMA_SQL
        self.assertIn("GIN", SCHEMA_SQL)

    def test_schema_has_domain_index(self):
        from storage import SCHEMA_SQL
        self.assertIn("idx_crawled_pages_domain", SCHEMA_SQL)


if __name__ == "__main__":
    unittest.main(verbosity=2)