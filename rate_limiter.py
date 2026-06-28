import asyncio
import time
from typing import Optional

DEFAULT_DELAY_SECONDS = 1.0
MIN_DELAY_SECONDS = 0.5
MAX_DELAY_SECONDS = 60.0

class RateLimiter:
    def __init__(self, default_delay: float=DEFAULT_DELAY_SECONDS):
        self._default_delay=max(default_delay, MIN_DELAY_SECONDS)
        self._domain_delays: dict[str, float] = {}
        self._last_requests: dict[str, float] = {}
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()

    def set_domain_delay(self, domain: str, delay: Optional[float]) -> None:
        if delay is None:
            self._domain_delays.pop(domain, None)
            return
        
        clamped = max(MIN_DELAY_SECONDS, min(MAX_DELAY_SECONDS, delay))
        self._domain_delays[domain] = clamped
    
    def get_delay(self, domain:str) -> float:
        return self._domain_delays.get(domain, self._default_delay)
    
    async def _get_domain_lock(self, domain: str) -> asyncio.Lock:
        async with self._meta_lock:
            if domain not in self._domain_locks:
                self._domain_locks[domain] = asyncio.Lock()
            return self._domain_locks[domain]
        
    async def wait(self, domain: str) -> None:
        lock = await self._get_domain_lock(domain)

        async with lock:
            delay = self.get_delay(domain)
            now = time.monotonic()
            last = self._last_requests.get(domain, 0.0)
            elapsed = now - last
            remaining = delay - elapsed

            if remaining > 0:
                await asyncio.sleep(remaining)

            self._last_requests[domain] = time.monotonic()

    def stats(self) -> dict :
        return {
            "default_delay" : self._default_delay,
            "domain_overrides" : dict(self._domain_delays),
        }