"""
main.py — CLI entry point for Crawler

Usage:
    python main.py <URL> [options]

Examples:
    # Basic crawl, dev logging, no DB
    python main.py https://example.com

    # Production crawl with all options
    python main.py https://example.com \
        --workers 10 \
        --max-pages 500 \
        --rate-limit 1.5 \
        --ttl-days 7 \
        --db-dsn "postgresql://user:pass@localhost/crawlerdb" \
        --log-mode production \
        --output report.json

Environment variables (override CLI defaults):
    CRAWLER_DB_DSN        PostgreSQL DSN
    CRAWLER_LOG_MODE      "development" | "production"
    CRAWLER_LOG_LEVEL     "DEBUG" | "INFO" | "WARNING"

All CLI flags also have env var equivalents (CRAWLER_<FLAG>).
"""

import argparse
import asyncio
import json
import os
import sys

import validators  # type: ignore

from logger import configure_logging, get_logger
from async_crawler import crawl_site_async
from storage import CrawlStorage

from dotenv import load_dotenv #type: ignore
load_dotenv()

log = get_logger("main")


#Argument parsing 

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="testcrawler",
        description="Production async web crawler",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    p.add_argument(
        "url",
        help="Base URL to crawl (e.g. https://example.com)",
    )
    p.add_argument(
        "--workers", "-w",
        type=int,
        default=int(os.environ.get("CRAWLER_WORKERS", 6)),
        metavar="N",
        help="Number of concurrent worker coroutines",
    )
    p.add_argument(
        "--max-pages", "-m",
        type=int,
        default=int(os.environ.get("CRAWLER_MAX_PAGES", 50)),
        metavar="N",
        help="Maximum number of pages to crawl",
    )
    p.add_argument(
        "--rate-limit", "-r",
        type=float,
        default=float(os.environ.get("CRAWLER_RATE_LIMIT", 1.0)),
        metavar="SECONDS",
        help="Minimum delay between requests to the same domain",
    )
    p.add_argument(
        "--ttl-days",
        type=int,
        default=int(os.environ.get("CRAWLER_TTL_DAYS", 7)),
        metavar="DAYS",
        help="Recrawl pages older than this many days (requires --db-dsn)",
    )
    p.add_argument(
        "--db-dsn",
        default=os.environ.get("CRAWLER_DB_DSN"),
        metavar="DSN",
        help="PostgreSQL DSN for persistent storage. If omitted, results are in-memory only.",
    )
    p.add_argument(
        "--output", "-o",
        default=os.environ.get("CRAWLER_OUTPUT", "report.json"),
        metavar="FILE",
        help="Path to write JSON report (always written, even with --db-dsn)",
    )
    p.add_argument(
        "--log-mode",
        choices=["development", "production"],
        default=os.environ.get("CRAWLER_LOG_MODE", "development"),
        help="Logging format: pretty console or newline-delimited JSON",
    )
    p.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=os.environ.get("CRAWLER_LOG_LEVEL", "INFO"),
        help="Minimum log level",
    )
    p.add_argument(
        "--progress-every",
        type=int,
        default=int(os.environ.get("CRAWLER_PROGRESS_EVERY", 30)),
        metavar="SECONDS",
        help="Log progress summary every N seconds",
    )

    return p


# Validation 

def validate_url(url: str) -> str:
    """Validate and normalise the base URL. Exits on failure."""
    # Add scheme if missing
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    if not validators.url(url):
        print(f"Error: '{url}' is not a valid URL.", file=sys.stderr)
        sys.exit(1)

    return url


def validate_args(args: argparse.Namespace) -> None:
    if args.workers < 1:
        print("Error: --workers must be at least 1", file=sys.stderr)
        sys.exit(1)
    if args.max_pages < 1:
        print("Error: --max-pages must be at least 1", file=sys.stderr)
        sys.exit(1)
    if args.rate_limit < 0:
        print("Error: --rate-limit must be >= 0", file=sys.stderr)
        sys.exit(1)
    if args.ttl_days < 1:
        print("Error: --ttl-days must be at least 1", file=sys.stderr)
        sys.exit(1)


# Report 

def write_report(page_data: dict, output_path: str) -> None:
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(page_data, f, indent=2, sort_keys=True, ensure_ascii=False)
        log.info("report_written", path=output_path, pages=len(page_data))
    except OSError as e:
        log.error("report_write_failed", path=output_path, reason=str(e))


# Main 

async def run(args: argparse.Namespace) -> int:
    """
    Main async entrypoint. Returns exit code (0 = success, 1 = error).
    """
    url = validate_url(args.url)

    log.info(
        "bootcrawler_starting",
        url=url,
        workers=args.workers,
        max_pages=args.max_pages,
        rate_limit=args.rate_limit,
        ttl_days=args.ttl_days,
        storage="postgres" if args.db_dsn else "none",
        output=args.output,
    )

    # With persistent storage 
    if args.db_dsn:
        try:
            async with CrawlStorage.connect(args.db_dsn) as storage:
                page_data = await crawl_site_async(
                    base_url=url,
                    max_concurrency=args.workers,
                    max_pages=args.max_pages,
                    rate_limit_delay=args.rate_limit,
                    storage=storage,
                    ttl_days=args.ttl_days,
                    progress_every=args.progress_every,
                )
        except Exception as e:
            log.error("storage_connection_failed", dsn=args.db_dsn, reason=str(e))
            log.warning("falling_back_to_memory", reason="DB unavailable")
            page_data = await crawl_site_async(
                base_url=url,
                max_concurrency=args.workers,
                max_pages=args.max_pages,
                rate_limit_delay=args.rate_limit,
                progress_every=args.progress_every,
            )

    # Memory-only (no --db-dsn) 
    else:
        page_data = await crawl_site_async(
            base_url=url,
            max_concurrency=args.workers,
            max_pages=args.max_pages,
            rate_limit_delay=args.rate_limit,
            progress_every=args.progress_every,
        )

    write_report(page_data, args.output)
    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Init logging first — everything after this is structured
    configure_logging(mode=args.log_mode, level=args.log_level)

    validate_args(args)

    try:
        exit_code = asyncio.run(run(args))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        # asyncio.run() surfaces KeyboardInterrupt if SIGINT fires before
        # our handler is registered. Handle it cleanly.
        log.warning("crawl_aborted", reason="KeyboardInterrupt before crawler started")
        sys.exit(130)  # 130 = terminated by SIGINT (unix convention)


if __name__ == "__main__":
    main()