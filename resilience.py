import os
from dataclasses import dataclass, field
import aiohttp, asyncio #type:ignore
import random, time
from typing import Optional
from enum import Enum, auto
import urllib.parse as parser 

from redirect_guard import get_html_with_guard, RedirectError
from ssrf_guard import SSRFError

from dotenv import load_dotenv #type: ignore
load_dotenv()

## RETRY LOGIC
@dataclass
class RetryConfig:
    max_attempts: int = 3
    base_wait: float = 1.0
    max_wait: float = 30.0
    jitter: float = 0.5

_RETRYABLE_NETWORK_ERRORS = (
    aiohttp.ClientConnectionError,
    aiohttp.ServerConnectionError,
    aiohttp.ServerTimeoutError,
    aiohttp.ClientOSError,
    asyncio.TimeoutError,
)

_RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
_PERMANENT_HTTP_STATUSES = set(range(400, 500)) - {429}

def _backoff_wait(attempt: int, config: RetryConfig) -> float:
    exponential = 2 ** attempt
    jitter = random.uniform(-config.jitter, config.jitter)
    return min(config.max_wait, max(0.0, config.base_wait * exponential + jitter))

## CB LOGIC
class CircuitState(Enum):
    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()

@dataclass
class CircuitBreaker :
    failure_threshold: int = 5
    cooldown: float = 60.0

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_open(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return False
        if self.state == CircuitState.OPEN:
            now = time.monotonic()
            if now - self.last_failure_time >= self.cooldown :
                self.state = CircuitState.HALF_OPEN
                return False
            return True
        return False
    
    def record_success(self) -> None:
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        if self.failure_count >= self.failure_threshold:
            if self.state != CircuitState.OPEN:
                print(
                    f"[CIRCUIT OPEN] domain tripped after "
                    f"{self.failure_count} failures — "
                    f"backing off for {self.cooldown}s"
                )
            self.state = CircuitState.OPEN

class FetchError(Exception) :
    """
    Raised when a fetch fails permanently (all retries exhausted,
    circuit open, or a non retryable error.)
    """

    def __init__(self, message: str, retryable: bool=False):
        super().__init__(message)
        self.retryable = retryable

class CircuitOpenError(FetchError):
    """Raised immediately when the circuit breaker is open for a domain."""

class ResilientFetcher :
    def __init__(
            self,
            retry_config: Optional[RetryConfig] = None,
            failure_threshold: int = 5,
            circuit_cooldown: float = 60.0,
    ):
        self._retry = retry_config or RetryConfig()
        self._failure_threshold = failure_threshold
        self._circuit_cooldown = circuit_cooldown
        self._circuits: dict[str, CircuitBreaker] = {}
        self._meta_lock = asyncio.Lock()

    async def _get_circuit(self, domain: str) -> CircuitBreaker:
        async with self._meta_lock:
            if domain not in self._circuits:
                self._circuits[domain] = CircuitBreaker(
                    failure_threshold=self._failure_threshold,
                    cooldown=self._circuit_cooldown,
                )
            return self._circuits[domain]
        
    def circuit_states(self) -> dict[str, str]:
        return {
            domain: cb.state.name
            for domain, cb in self._circuits.items()
        }

    async def fetch(
            self,
            session: aiohttp.ClientSession,
            url: str,
            base_domain: Optional[str] = None,
            allow_cross_domain: bool = False,
            user_agent: str = os.getenv("USER_AGENT"),
    ) -> tuple[str, str]:
        domain = parser.urlparse(url).netloc
        circuit = await self._get_circuit(domain)

        async with circuit._lock:
            if circuit.is_open():
                raise CircuitOpenError(
                    f"Circuit OPEN for {domain} — skipping fetch of {url}. "
                    f"Will retry after {circuit.cooldown}s cooldown.",
                    retryable=False,
                )
            
        
        last_error: Exception = RuntimeError("No attempts made")

        for attempt in range(self._retry.max_attempts):
            try:
                html, final_url = await get_html_with_guard(
                    session=session,
                    url=url,
                    base_domain=base_domain,
                    allow_cross_domain=allow_cross_domain,
                    user_agent=user_agent,
                )

                async with circuit._lock:
                    circuit.record_success()
                return html, final_url
            except (RedirectError, SSRFError) as e:
                raise FetchError(str(e), retryable=False) from e
            
            except aiohttp.ClientResponseError as e:
                if e.status in _PERMANENT_HTTP_STATUSES:
                    raise FetchError(
                        f"HTTP {e.status} (permanent): {url}", retryable=False
                    ) from e
                
                if e.status in _RETRYABLE_HTTP_STATUSES:
                    last_error = e
                    async with circuit._lock:
                        circuit.record_failure()

                    retry_after = None
                    if e.status == 429 and hasattr(e, 'headers') and e.headers:
                        try:
                            retry_after = float(e.headers.get("Retry-After", 0))
                        except (ValueError, TypeError):
                            retry_after = None

                    if attempt < self._retry.max_attempts -1 :
                        wait = retry_after or _backoff_wait(attempt, self._retry)
                        print(
                            f"[RETRY] {url} — HTTP {e.status}, "
                            f"attempt {attempt + 1}/{self._retry.max_attempts}, "
                            f"waiting {wait:.1f}s"
                        )
                        await asyncio.sleep(wait)
                    continue

                raise FetchError(f"HTTP {e.status}: {url}", retryable=False) from e
            
            except _RETRYABLE_NETWORK_ERRORS as e:
                last_error = e
                async with circuit._lock:
                    circuit.record_failure()
                
                if attempt < self._retry.max_attempts - 1:
                    wait = _backoff_wait(attempt, self._retry)
                    print(
                        f"[RETRY] {url} - {type(e).__name__}: e, "
                        f"attempt {attempt + 1}/{self._retry.max_attempts},"
                        f"waiting {wait:.1f}s"
                    )
                    await asyncio.sleep(wait)
                continue

            except ValueError as e:
                raise FetchError(str(e), retryable=False) from e
            
        async with circuit._lock:
            circuit.record_failure()

        raise FetchError(
            f"All {self._retry.max_attempts} attempts failed for {url}. "
            f"Last error: {last_error}",
            retryable=True,
        )
                

    

