import time
import asyncio
import statistics
from dataclasses import dataclass, field
from typing import Optional

import structlog#type:ignore

log = structlog.get_logger("crawl_stats")


@dataclass
class CrawlStats:
    """
    Thread-safe crawl metrics accumulator.

    All mutation methods are async and acquire the lock.
    Read methods (for summary) are called after the crawl completes
    so no locking needed there.
    """

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _start_time: float = field(default_factory=time.monotonic)

    pages_crawled: int = 0
    pages_skipped_fresh: int = 0    # skipped because within TTL
    pages_skipped_seen: int = 0     # skipped because already queued/visited

    errors: dict = field(default_factory=lambda: {
        "ssrf":         0,
        "robots":       0,
        "circuit_open": 0,
        "permanent":    0,   # 4xx that won't change
        "retried":      0,   # exhausted all retries
        "redirect":     0,
        "other":        0,
    })

    _fetch_latencies_ms: list[float] = field(default_factory=list)

    _domain_counts: dict = field(default_factory=dict)

    async def record_page_crawled(
        self,
        url: str,
        domain: str,
        fetch_duration_ms: float,
        html_bytes: int,
        word_count: int,
    ) -> None:
        async with self._lock:
            self.pages_crawled += 1
            self._fetch_latencies_ms.append(fetch_duration_ms)
            self._domain_counts[domain] = self._domain_counts.get(domain, 0) + 1

    async def record_skipped_fresh(self) -> None:
        async with self._lock:
            self.pages_skipped_fresh += 1

    async def record_skipped_seen(self) -> None:
        async with self._lock:
            self.pages_skipped_seen += 1

    async def record_error(self, kind: str) -> None:
        async with self._lock:
            if kind in self.errors:
                self.errors[kind] += 1
            else:
                self.errors["other"] += 1

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._start_time

    def pages_per_second(self) -> float:
        elapsed = self.elapsed_seconds()
        return self.pages_crawled / elapsed if elapsed > 0 else 0.0

    def total_errors(self) -> int:
        return sum(self.errors.values())

    def error_rate(self) -> float:
        total = self.pages_crawled + self.total_errors()
        return self.total_errors() / total if total > 0 else 0.0

    def latency_p50_ms(self) -> Optional[float]:
        if not self._fetch_latencies_ms:
            return None
        return statistics.median(self._fetch_latencies_ms)

    def latency_p95_ms(self) -> Optional[float]:
        data = self._fetch_latencies_ms
        if not data:
            return None
        if len(data) < 20:
            return max(data)  
        sorted_data = sorted(data)
        idx = int(len(sorted_data) * 0.95)
        return sorted_data[idx]

    def latency_avg_ms(self) -> Optional[float]:
        if not self._fetch_latencies_ms:
            return None
        return statistics.mean(self._fetch_latencies_ms)

    def top_domains(self, n: int = 5) -> list[tuple[str, int]]:
        return sorted(self._domain_counts.items(), key=lambda x: x[1], reverse=True)[:n]

    def as_dict(self) -> dict:
        """Structured dict for log emission."""
        return {
            "pages_crawled":       self.pages_crawled,
            "pages_skipped_fresh": self.pages_skipped_fresh,
            "pages_skipped_seen":  self.pages_skipped_seen,
            "total_errors":        self.total_errors(),
            "error_rate":          round(self.error_rate(), 4),
            "errors":              dict(self.errors),
            "elapsed_s":           round(self.elapsed_seconds(), 2),
            "pages_per_second":    round(self.pages_per_second(), 3),
            "latency_p50_ms":      round(self.latency_p50_ms(), 1) if self.latency_p50_ms() else None,
            "latency_p95_ms":      round(self.latency_p95_ms(), 1) if self.latency_p95_ms() else None,
            "latency_avg_ms":      round(self.latency_avg_ms(), 1) if self.latency_avg_ms() else None,
            "top_domains":         self.top_domains(),
        }

    # Logging

    def log_progress(self, queue_size: int = 0) -> None:
        """
        Emit a structured progress event. Call periodically during crawl.
        In production this feeds dashboards/alerts.
        In dev it prints a readable line.
        """
        log.info(
            "crawl_progress",
            pages_crawled=self.pages_crawled,
            queue_size=queue_size,
            pages_per_second=round(self.pages_per_second(), 2),
            total_errors=self.total_errors(),
            elapsed_s=round(self.elapsed_seconds(), 1),
        )

    def log_summary(self, circuit_states: dict | None = None) -> None:
        """
        Emit a structured summary event and print a human-readable table.
        Call once at the end of the crawl.
        """
        stats = self.as_dict()

        # Structured log event (for prod) 
        log.info("crawl_complete", **stats, circuit_states=circuit_states or {})

        # Human-readable table (for terminal/dev) 
        elapsed = self.elapsed_seconds()
        p50 = self.latency_p50_ms()
        p95 = self.latency_p95_ms()

        lines = [
            "",
            "╔══════════════════════════════════════════════════════════╗",
            "║                    CRAWL SUMMARY                         ║",
            "╠══════════════════════════════════════════════════════════╣",
            f"║  Pages crawled      {self.pages_crawled:<6}                               ║",
            f"║  Skipped (fresh)    {self.pages_skipped_fresh:<6}  (within TTL window)          ║",
            f"║  Skipped (seen)     {self.pages_skipped_seen:<6}  (already queued/visited)     ║",
            f"║  Total errors       {self.total_errors():<6}  ({self.error_rate()*100:.1f}% error rate)            ║",
            "╠══════════════════════════════════════════════════════════╣",
            f"║  Duration           {elapsed:.1f}s                                 ║",
            f"║  Throughput         {self.pages_per_second():.2f} pages/sec                       ║",
            f"║  Latency p50        {f'{p50:.0f}ms' if p50 else 'n/a':<8}                             ║",
            f"║  Latency p95        {f'{p95:.0f}ms' if p95 else 'n/a':<8}                             ║",
            "╠══════════════════════════════════════════════════════════╣",
            "║  Error breakdown                                         ║",
        ]

        for kind, count in self.errors.items():
            if count > 0:
                lines.append(f"║    {kind:<20} {count:<6}                           ║")

        if self.top_domains():
            lines.append("╠══════════════════════════════════════════════════════════╣")
            lines.append("║  Top domains                                             ║")
            for domain, count in self.top_domains():
                lines.append(f"║    {domain:<30} {count:<6} pages           ║")

        if circuit_states:
            open_circuits = [d for d, s in circuit_states.items() if s != "CLOSED"]
            if open_circuits:
                lines.append("╠══════════════════════════════════════════════════════════╣")
                lines.append("║  Open circuits (domains that tripped)                   ║")
                for domain in open_circuits:
                    lines.append(f"║    {domain:<52} ║")

        lines.append("╚══════════════════════════════════════════════════════════╝")
        lines.append("")

        print("\n".join(lines))


# ── Self-test 
async def _test():
    from logger import configure_logging
    configure_logging(mode="development")

    stats = CrawlStats()

    # Simulate a crawl
    import random
    domains = ["example.com", "boot.dev", "docs.example.com"]
    for i in range(47):
        domain = random.choice(domains)
        await stats.record_page_crawled(
            url=f"https://{domain}/page-{i}",
            domain=domain,
            fetch_duration_ms=random.uniform(80, 800),
            html_bytes=random.randint(5000, 80000),
            word_count=random.randint(100, 2000),
        )
    await stats.record_error("robots")
    await stats.record_error("permanent")
    await stats.record_error("permanent")
    await stats.record_error("circuit_open")
    await stats.record_skipped_fresh()
    await stats.record_skipped_fresh()
    await stats.record_skipped_fresh()

    stats.log_progress(queue_size=12)
    stats.log_summary(circuit_states={"slow-cdn.example.com": "OPEN"})


if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())