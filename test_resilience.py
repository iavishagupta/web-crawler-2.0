"""
test_resilience.py — Unit tests for retry + circuit breaker

Uses unittest.mock to simulate server failures without network calls.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch, MagicMock
import aiohttp

import sys
sys.path.insert(0, '/home/claude/security')

from resilience import (
    ResilientFetcher, RetryConfig, FetchError, CircuitOpenError,
    CircuitBreaker, CircuitState, _backoff_wait,
)


def run(coro):
    return asyncio.run(coro)


class TestBackoff(unittest.TestCase):
    def test_grows_exponentially(self):
        cfg = RetryConfig(base_wait=1.0, max_wait=100.0, jitter=0.0)
        waits = [_backoff_wait(i, cfg) for i in range(4)]
        # 1, 2, 4, 8
        self.assertAlmostEqual(waits[0], 1.0)
        self.assertAlmostEqual(waits[1], 2.0)
        self.assertAlmostEqual(waits[2], 4.0)
        self.assertAlmostEqual(waits[3], 8.0)

    def test_capped_at_max_wait(self):
        cfg = RetryConfig(base_wait=1.0, max_wait=5.0, jitter=0.0)
        wait = _backoff_wait(10, cfg)  # would be 1024 uncapped
        self.assertEqual(wait, 5.0)

    def test_jitter_spreads_values(self):
        cfg = RetryConfig(base_wait=1.0, max_wait=100.0, jitter=0.5)
        waits = {_backoff_wait(0, cfg) for _ in range(20)}
        # With jitter, values shouldn't all be identical
        self.assertGreater(len(waits), 1)


class TestCircuitBreaker(unittest.TestCase):
    def _make_cb(self, threshold=3, cooldown=0.05):
        return CircuitBreaker(failure_threshold=threshold, cooldown=cooldown)

    def test_starts_closed(self):
        cb = self._make_cb()
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertFalse(cb.is_open())

    def test_opens_after_threshold(self):
        cb = self._make_cb(threshold=3)
        for _ in range(3):
            cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)
        self.assertTrue(cb.is_open())

    def test_success_resets_to_closed(self):
        cb = self._make_cb(threshold=2)
        cb.record_failure()
        cb.record_failure()
        self.assertTrue(cb.is_open())
        # Simulate cooldown elapsed
        cb.last_failure_time = 0
        cb.is_open()  # transitions to HALF_OPEN
        cb.record_success()
        self.assertEqual(cb.state, CircuitState.CLOSED)
        self.assertFalse(cb.is_open())

    def test_half_open_after_cooldown(self):
        cb = self._make_cb(threshold=1, cooldown=0.01)
        cb.record_failure()
        self.assertTrue(cb.is_open())
        import time; time.sleep(0.02)
        # After cooldown, is_open() returns False (allows probe)
        self.assertFalse(cb.is_open())
        self.assertEqual(cb.state, CircuitState.HALF_OPEN)

    def test_reopens_if_probe_fails(self):
        cb = self._make_cb(threshold=1, cooldown=0.01)
        cb.record_failure()
        import time; time.sleep(0.02)
        cb.is_open()  # -> HALF_OPEN
        cb.record_failure()
        self.assertEqual(cb.state, CircuitState.OPEN)


class TestResilientFetcher(unittest.TestCase):
    def _fetcher(self, max_attempts=3, threshold=5):
        return ResilientFetcher(
            retry_config=RetryConfig(
                max_attempts=max_attempts,
                base_wait=0.0,   # no sleep in tests
                max_wait=0.0,
                jitter=0.0,
            ),
            failure_threshold=threshold,
            circuit_cooldown=999.0,
        )

    async def _fetch(self, fetcher, mock_fn):
        """Call fetcher.fetch with a mocked get_html_with_guard."""
        with patch('resilience.get_html_with_guard', new=mock_fn):
            session = MagicMock()
            return await fetcher.fetch(session, "https://example.com/page")

    def test_success_on_first_attempt(self):
        fetcher = self._fetcher()
        mock = AsyncMock(return_value=("<html>ok</html>", "https://example.com/page"))
        html, url = run(self._fetch(fetcher, mock))
        self.assertEqual(html, "<html>ok</html>")
        mock.assert_called_once()

    def test_retries_on_503(self):
        fetcher = self._fetcher(max_attempts=3)
        call_count = 0

        async def flaky(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                err = aiohttp.ClientResponseError(
                    MagicMock(), (), status=503, message="Service Unavailable"
                )
                raise err
            return "<html>ok</html>", "https://example.com/page"

        html, _ = run(self._fetch(fetcher, flaky))
        self.assertEqual(html, "<html>ok</html>")
        self.assertEqual(call_count, 3)

    def test_does_not_retry_404(self):
        fetcher = self._fetcher(max_attempts=3)
        call_count = 0

        async def always_404(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise aiohttp.ClientResponseError(
                MagicMock(), (), status=404, message="Not Found"
            )

        with self.assertRaises(FetchError) as ctx:
            run(self._fetch(fetcher, always_404))

        self.assertEqual(call_count, 1)  # no retry
        self.assertFalse(ctx.exception.retryable)

    def test_raises_fetch_error_after_all_retries(self):
        fetcher = self._fetcher(max_attempts=3)

        async def always_503(*args, **kwargs):
            raise aiohttp.ClientResponseError(
                MagicMock(), (), status=503, message="unavailable"
            )

        with self.assertRaises(FetchError) as ctx:
            run(self._fetch(fetcher, always_503))

        self.assertTrue(ctx.exception.retryable)

    def test_circuit_opens_and_rejects(self):
        fetcher = self._fetcher(max_attempts=1, threshold=2)

        async def always_timeout(*args, **kwargs):
            raise asyncio.TimeoutError()

        # Trip the circuit (2 failures)
        for _ in range(2):
            try:
                run(self._fetch(fetcher, always_timeout))
            except FetchError:
                pass

        # Now circuit should be open
        with self.assertRaises(CircuitOpenError):
            run(self._fetch(fetcher, always_timeout))

    def test_does_not_retry_ssrf_error(self):
        from ssrf_guard import SSRFError
        fetcher = self._fetcher(max_attempts=3)
        call_count = 0

        async def ssrf_blocked(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise SSRFError("Private IP")

        # SSRFError is wrapped into FetchError with retryable=False
        with self.assertRaises((FetchError, SSRFError)):
            run(self._fetch(fetcher, ssrf_blocked))

        self.assertEqual(call_count, 1)

    def test_circuit_states_visible(self):
        fetcher = self._fetcher()
        mock = AsyncMock(return_value=("<html/>", "https://example.com/"))
        run(self._fetch(fetcher, mock))
        states = fetcher.circuit_states()
        self.assertIn("example.com", states)
        self.assertEqual(states["example.com"], "CLOSED")


if __name__ == "__main__":
    unittest.main(verbosity=2)