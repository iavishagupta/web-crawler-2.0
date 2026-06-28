import asyncpg #type:ignore
from contextlib import asynccontextmanager
import os, json
from datetime import datetime, timezone, timedelta
from typing import Optional
import urllib.parse as parser

from dotenv import load_dotenv #type: ignore
load_dotenv()

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS crawled_pages(
url             TEXT PRIMARY KEY,
domain          TEXT NOT NULL,
last_crawled    TIMESTAMPTZ NOT NULL,
status          TEXT NOT NULL DEFAULT 'ok',
content_hash    TEXT,
extracted       JSONB
);

--Fast Lookup by domain(e.g. "all pages on example.com")
CREATE INDEX IF NOT EXISTS idx_crawled_pages_domain
    ON crawled_pages (domain);

--Fast staleness check 
CREATE INDEX IF NOT EXISTS idx_crawled_pages_last_crawled
    ON crawled_pages (last_crawled);

-- Full-text search inside extracted JSONB
-- Allows: WHERE extracted @> '{"meta": {"language": "en"}}'
CREATE INDEX IF NOT EXISTS idx_crawled_pages_extracted_gin
    ON crawled_pages USING GIN (extracted);
"""

class CrawlStorage:
    def __init__(self, pool:asyncpg.Pool):
        self._pool = pool

    @classmethod
    @asynccontextmanager
    async def connect(cls, dsn:Optional[str]=None):
        dsn = dsn or os.environ.get("CRAWLER_DB_DSN")
        if not dsn:
            raise ValueError(
                "No DSN provided. Pass dsn= or set CRAWLER_DB_DSN env var."
            )
        pool = await asyncpg.create_pool(
            dsn,
            min_size=2,
            max_size=10,
            timeout=30,
        )

        try:
            storage = cls(pool)
            await storage._init_schema()
            yield storage
        finally:
            await pool.close()

    async def _init_schema(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)

    
    async def upsert_page(
            self, 
            url: str,
            data: dict,
            status: str='ok'
    ) -> None:
        domain = parser.urlparse(url).netloc
        now = datetime.now(timezone.utc)
        content_hash = data.get("content_hash")

        extracted_json = json.dumps(data, ensure_ascii=False)

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO crawled_pages
                    (url, domain, last_crawled, status, content_hash, extracted)
                VALUES
                    ($1, $2, $3, $4, $5, $6::jsonb)
                ON CONFLICT (url) DO UPDATE SET
                    last_crawled = EXCLUDED.last_crawled,
                    status       = EXCLUDED.status,
                    content_hash = EXCLUDED.content_hash,
                    extracted    = EXCLUDED.extracted
                """,
                url, domain, now, status, content_hash, extracted_json,
            )

    async def mark_error(self, url:str, status: str="error") -> None:
        domain = parser.urlparse(url).netloc
        now = datetime.now(timezone.utc)

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO crawled_pages
                    (url, domain, last_crawled, status, content_hash, extracted)
                VALUES
                    ($1, $2, $3, $4, NULL, NULL)
                ON CONFLICT (url) DO UPDATE SET
                    last_crawled = EXCLUDED.last_crawled,
                    status       = EXCLUDED.status
                """,
                url, domain, now, status,
            )

    async def get_page(self, url: str) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT extracted FROM crawled_pages WHERE url = $1",
                url,
            )
        if row is None or row["extracted"] is None:
            return None
        return json.loads(row["extracted"])
    
    async def is_stale(self, url: str, ttl_days:int = 30) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                 SELECT last_crawled, status
                 FROM crawled_pages
                 WHERE url = $1
                """,
                url
            )

        if row is None:
            return True
        
        if row["status"] != "ok":
            return True 
        
        age = datetime.now(timezone.utc) - row['last_crawled']
        return age > timedelta(days=ttl_days)
    
    async def is_changed(self, url:str, new_hash:str) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT content_hash FROM crawled_pages WHERE url = $1",
                url,
            )
        if row is None:
            return True
        return row["content_hash"] != new_hash
    
    async def get_seen_urls(self, domain: str) -> set[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT url FROM crawled_pages
                WHERE domain = $1 AND status = 'ok'
                """,
                domain,
            )

        return {row["url"] for row in rows}
    
    async def get_stale_urls(
            self,
            domain: str,
            ttl_days: int=30,
            limit: int=1000,
    ):
        cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                    SELECT url FROM crawled_pages
                    WHERE domain = $1
                     AND (last_crawled<$2 OR status != 'ok')
                    ORDER BY last_crawled ASC
                    LIMIT $3
                """,
                domain, cutoff, limit
            )

        return [row["url"] for row in rows]
    
    async def domain_stats(self, domain: str) -> dict:
        """
        Summary stats for a domain.
        Useful for monitoring crawl coverage.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*)                        AS total_pages,
                    COUNT(*) FILTER (WHERE status = 'ok')    AS ok_pages,
                    COUNT(*) FILTER (WHERE status != 'ok')   AS error_pages,
                    MIN(last_crawled)               AS first_crawled,
                    MAX(last_crawled)               AS last_crawled,
                    AVG((extracted->>'word_count')::int)
                        FILTER (WHERE extracted ? 'word_count') AS avg_word_count
                FROM crawled_pages
                WHERE domain = $1
                """,
                domain,
            )
        return dict(row)