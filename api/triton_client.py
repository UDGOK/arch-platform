"""
triton_client.py
================
Production serving layer for all AI API calls.

Provides the same interface as a Triton Inference Server client, built on
httpx async HTTP. When you graduate to self-hosted Triton on DGX, swap the
_TritonHTTPClient backend — all callers stay unchanged.

Features
--------
• Async HTTP with persistent connection pool (httpx.AsyncClient)
• Request batching queue — multiple callers share in-flight NIM requests
• Circuit breaker — opens after N consecutive failures, half-opens after timeout
• Exponential backoff retry with full jitter (AWS-style)
• Per-endpoint rate limiter (token bucket)
• Prometheus-style in-process metrics (counters, latency histogram)
• Sync wrapper so existing synchronous code works without change

Usage
-----
    # Async (FastAPI route handlers)
    client = get_triton_client()
    response = await client.infer_async(endpoint, payload, api_key)

    # Sync (existing orchestrator / nvidia_nim code)
    response = triton_infer(endpoint, payload, api_key)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class TritonConfig:
    # Connection pool
    max_connections:       int   = 20
    max_keepalive:         int   = 10
    keepalive_expiry:      float = 30.0
    connect_timeout:       float = 10.0
    read_timeout:          float = 90.0

    # Retry
    max_retries:           int   = 3
    base_backoff_s:        float = 0.5
    max_backoff_s:         float = 16.0
    retryable_status:      Tuple = (429, 500, 502, 503, 504)

    # Circuit breaker
    cb_failure_threshold:  int   = 5      # open after N consecutive failures
    cb_recovery_timeout_s: float = 30.0   # try half-open after this
    cb_success_threshold:  int   = 2      # close after N successes in half-open

    # Rate limiter (per endpoint)
    rate_limit_rps:        float = 10.0   # requests/sec per endpoint
    rate_limit_burst:      int   = 20     # max burst tokens

    # Batch queue
    batch_window_ms:       int   = 50     # coalesce window
    max_batch_size:        int   = 8


DEFAULT_CONFIG = TritonConfig()


# ---------------------------------------------------------------------------
# Metrics (in-process, no external deps)
# ---------------------------------------------------------------------------

@dataclass
class _Metrics:
    requests_total:   int   = 0
    requests_success: int   = 0
    requests_failed:  int   = 0
    retries_total:    int   = 0
    circuit_opens:    int   = 0
    latency_ms:       Deque = field(default_factory=lambda: deque(maxlen=1000))

    def record(self, success: bool, latency_ms: float) -> None:
        self.requests_total += 1
        if success: self.requests_success += 1
        else:       self.requests_failed  += 1
        self.latency_ms.append(latency_ms)

    def p95_ms(self) -> float:
        if not self.latency_ms: return 0.0
        s = sorted(self.latency_ms)
        return s[int(len(s) * 0.95)]

    def summary(self) -> Dict[str, Any]:
        total = self.requests_total or 1
        return {
            "requests_total":    self.requests_total,
            "success_rate_pct":  round(self.requests_success / total * 100, 1),
            "retries_total":     self.retries_total,
            "circuit_opens":     self.circuit_opens,
            "p95_latency_ms":    round(self.p95_ms(), 1),
        }


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

class CBState(Enum):
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, cfg: TritonConfig) -> None:
        self._cfg          = cfg
        self._state        = CBState.CLOSED
        self._failures     = 0
        self._successes    = 0
        self._opened_at    = 0.0
        self._lock         = asyncio.Lock()

    @property
    def state(self) -> CBState:
        return self._state

    async def allow_request(self) -> bool:
        async with self._lock:
            if self._state == CBState.CLOSED:
                return True
            if self._state == CBState.OPEN:
                if time.monotonic() - self._opened_at >= self._cfg.cb_recovery_timeout_s:
                    self._state = CBState.HALF_OPEN
                    self._successes = 0
                    logger.info("Circuit breaker → HALF_OPEN")
                    return True
                return False
            # HALF_OPEN — allow one probe
            return True

    async def record_success(self) -> None:
        async with self._lock:
            self._failures = 0
            if self._state == CBState.HALF_OPEN:
                self._successes += 1
                if self._successes >= self._cfg.cb_success_threshold:
                    self._state = CBState.CLOSED
                    logger.info("Circuit breaker → CLOSED")

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._failures >= self._cfg.cb_failure_threshold:
                if self._state != CBState.OPEN:
                    self._state     = CBState.OPEN
                    self._opened_at = time.monotonic()
                    logger.warning("Circuit breaker → OPEN (failures=%d)", self._failures)


# ---------------------------------------------------------------------------
# Token Bucket Rate Limiter
# ---------------------------------------------------------------------------

class TokenBucket:
    def __init__(self, rate: float, burst: int) -> None:
        self._rate    = rate
        self._burst   = burst
        self._tokens  = float(burst)
        self._last    = time.monotonic()
        self._lock    = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now    = time.monotonic()
            delta  = now - self._last
            self._tokens = min(self._burst, self._tokens + delta * self._rate)
            self._last   = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
        # Wait for a token
        wait = (1.0 - self._tokens) / self._rate
        await asyncio.sleep(wait)
        async with self._lock:
            self._tokens = max(0.0, self._tokens - 1.0)


# ---------------------------------------------------------------------------
# Core Async Client
# ---------------------------------------------------------------------------

class TritonHTTPClient:
    """
    Async HTTP client with full production resilience.
    Mimics the Triton HTTP client interface for forward compatibility.
    """

    def __init__(self, config: TritonConfig = DEFAULT_CONFIG) -> None:
        self._cfg      = config
        self._cb       = CircuitBreaker(config)
        self._buckets: Dict[str, TokenBucket] = {}
        self._metrics  = _Metrics()
        self._client:  Optional[httpx.AsyncClient] = None
        self._lock     = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            async with self._lock:
                if self._client is None or self._client.is_closed:
                    limits = httpx.Limits(
                        max_connections=self._cfg.max_connections,
                        max_keepalive_connections=self._cfg.max_keepalive,
                        keepalive_expiry=self._cfg.keepalive_expiry,
                    )
                    timeout = httpx.Timeout(
                        connect=self._cfg.connect_timeout,
                        read=self._cfg.read_timeout,
                        write=10.0, pool=5.0,
                    )
                    self._client = httpx.AsyncClient(limits=limits, timeout=timeout)
        return self._client

    def _bucket(self, endpoint: str) -> TokenBucket:
        if endpoint not in self._buckets:
            self._buckets[endpoint] = TokenBucket(
                self._cfg.rate_limit_rps,
                self._cfg.rate_limit_burst,
            )
        return self._buckets[endpoint]

    async def infer_async(
        self,
        endpoint: str,
        payload: Dict[str, Any],
        api_key: str,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        POST payload to endpoint with full resilience stack.
        Returns parsed JSON response.
        Raises RuntimeError on permanent failure.
        """
        if not await self._cb.allow_request():
            raise RuntimeError(
                f"Circuit breaker OPEN for {endpoint} — "
                f"retrying in {self._cfg.cb_recovery_timeout_s:.0f}s"
            )

        await self._bucket(endpoint).acquire()

        headers = {
            "Content-Type":  "application/json",
            "Accept":        "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        if extra_headers:
            headers.update(extra_headers)

        last_exc: Optional[Exception] = None
        for attempt in range(self._cfg.max_retries + 1):
            t0 = time.monotonic()
            try:
                client = await self._get_client()
                resp   = await client.post(endpoint, json=payload, headers=headers)

                if resp.status_code in self._cfg.retryable_status and attempt < self._cfg.max_retries:
                    wait = self._jitter_backoff(attempt)
                    logger.warning("HTTP %d from %s — retry %d in %.2fs",
                                   resp.status_code, endpoint, attempt + 1, wait)
                    self._metrics.retries_total += 1
                    await asyncio.sleep(wait)
                    continue

                if resp.status_code >= 400:
                    err = resp.text[:300]
                    await self._cb.record_failure()
                    self._metrics.record(False, (time.monotonic()-t0)*1000)
                    raise RuntimeError(f"HTTP {resp.status_code} from {endpoint}: {err}")

                data = resp.json()
                await self._cb.record_success()
                self._metrics.record(True, (time.monotonic()-t0)*1000)
                logger.debug("NIM %s → %dms", endpoint.split("/")[-1],
                             int((time.monotonic()-t0)*1000))
                return data

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_exc = exc
                await self._cb.record_failure()
                self._metrics.record(False, (time.monotonic()-t0)*1000)
                if attempt < self._cfg.max_retries:
                    wait = self._jitter_backoff(attempt)
                    logger.warning("%s on attempt %d — retry in %.2fs", exc, attempt, wait)
                    self._metrics.retries_total += 1
                    await asyncio.sleep(wait)
                else:
                    raise TimeoutError(f"Timed out after {self._cfg.max_retries} retries: {exc}") from exc

        raise RuntimeError(f"All retries exhausted for {endpoint}: {last_exc}")

    def _jitter_backoff(self, attempt: int) -> float:
        """Full jitter: sleep = random(0, min(cap, base * 2^attempt))"""
        cap  = self._cfg.max_backoff_s
        base = self._cfg.base_backoff_s
        return random.uniform(0, min(cap, base * (2 ** attempt)))

    def metrics(self) -> Dict[str, Any]:
        return {
            **self._metrics.summary(),
            "circuit_breaker": self._cb.state.value,
        }

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# ---------------------------------------------------------------------------
# Batch Queue
# ---------------------------------------------------------------------------

@dataclass
class _BatchItem:
    endpoint:  str
    payload:   Dict[str, Any]
    api_key:   str
    future:    asyncio.Future


class BatchQueue:
    """
    Coalesces requests within a time window before dispatching.
    Identical (endpoint, api_key) requests in the same window are batched.
    Currently passes through individually (NIM doesn't support batch POST),
    but the queue structure is ready for Triton's /v2/models/{m}/infer batch.
    """

    def __init__(self, client: TritonHTTPClient, cfg: TritonConfig) -> None:
        self._client   = client
        self._cfg      = cfg
        self._queue:   asyncio.Queue = asyncio.Queue()
        self._running  = False

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._worker())

    async def stop(self) -> None:
        self._running = False

    async def submit(
        self, endpoint: str, payload: Dict[str, Any], api_key: str
    ) -> Dict[str, Any]:
        loop   = asyncio.get_event_loop()
        future = loop.create_future()
        await self._queue.put(_BatchItem(endpoint, payload, api_key, future))
        return await future

    async def _worker(self) -> None:
        while self._running:
            batch: List[_BatchItem] = []
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                batch.append(item)
                # Drain remaining items within the window
                deadline = time.monotonic() + self._cfg.batch_window_ms / 1000
                while time.monotonic() < deadline and len(batch) < self._cfg.max_batch_size:
                    try:
                        batch.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
            except asyncio.TimeoutError:
                continue

            # Dispatch batch items concurrently
            tasks = [
                asyncio.create_task(
                    self._dispatch(item)
                ) for item in batch
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _dispatch(self, item: _BatchItem) -> None:
        try:
            result = await self._client.infer_async(
                item.endpoint, item.payload, item.api_key
            )
            if not item.future.done():
                item.future.set_result(result)
        except Exception as exc:
            if not item.future.done():
                item.future.set_exception(exc)


# ---------------------------------------------------------------------------
# Singleton & sync wrapper
# ---------------------------------------------------------------------------

_client_instance: Optional[TritonHTTPClient] = None
_batch_queue:     Optional[BatchQueue]        = None


def get_triton_client() -> TritonHTTPClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = TritonHTTPClient(DEFAULT_CONFIG)
    return _client_instance


def get_batch_queue() -> BatchQueue:
    global _batch_queue
    if _batch_queue is None:
        _batch_queue = BatchQueue(get_triton_client(), DEFAULT_CONFIG)
    return _batch_queue


def triton_infer(
    endpoint: str,
    payload: Dict[str, Any],
    api_key: str,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Synchronous wrapper around the async client.
    Runs in the current thread's event loop if one exists,
    otherwise creates a new one. Safe to call from sync code.
    """
    client = get_triton_client()
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're inside an async context (FastAPI) — use a thread executor
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(
                    lambda: asyncio.run(
                        client.infer_async(endpoint, payload, api_key, extra_headers)
                    )
                )
                return future.result(timeout=95)
        else:
            return loop.run_until_complete(
                client.infer_async(endpoint, payload, api_key, extra_headers)
            )
    except RuntimeError:
        return asyncio.run(
            client.infer_async(endpoint, payload, api_key, extra_headers)
        )
