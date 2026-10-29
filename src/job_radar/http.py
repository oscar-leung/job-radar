"""Polite HTTP for job boards: per-host concurrency limits, request spacing, and retries
with exponential backoff + jitter (honoring Retry-After).

Workday answers bursts with 429 Too Many Requests. Every Workday tenant shares one limiter
("workday"), because the limit follows our IP, not the tenant.
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)
UA = {"User-Agent": "job-radar/0.1 (personal job search tool)"}
RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass
class HostLimiter:
    """At most `workers` requests in flight, and request starts at least `min_interval`
    seconds apart (plus up to `jitter` seconds), so traffic is spread instead of bursty."""
    workers: int = 4
    min_interval: float = 0.0
    jitter: float = 0.0
    _sem: threading.BoundedSemaphore = field(init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _next: float = 0.0

    def __post_init__(self):
        self._sem = threading.BoundedSemaphore(max(1, self.workers))

    def __enter__(self):
        self._sem.acquire()
        if self.min_interval or self.jitter:
            with self._lock:
                now = time.monotonic()
                start = max(now, self._next)
                self._next = start + self.min_interval + random.random() * self.jitter
            if start > now:
                time.sleep(start - now)
        return self

    def __exit__(self, *exc):
        self._sem.release()
        return False


def _f(name: str, default: str) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return float(default)


LIMITERS: dict[str, HostLimiter] = {
    # ~5 req/s across all Workday tenants, 3 in flight. Tune with env vars if 429s persist.
    "workday": HostLimiter(int(_f("RADAR_WORKDAY_WORKERS", "3")),
                           1.0 / max(_f("RADAR_WORKDAY_RPS", "5"), 0.1), 0.1),
    "edjoin": HostLimiter(2, 0.5, 0.25),
    "icims": HostLimiter(2, 0.5, 0.25),
    "uc": HostLimiter(1, 0.5, 0.25),
    "smartrecruiters": HostLimiter(4, 0.1, 0.1),
}


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0,
                  rand=random.random, retry_after: str | None = None) -> float:
    """Exponential backoff with 'equal jitter': half fixed, half random, so many clients
    retrying at once don't stay in lockstep. A server Retry-After wins if it is longer."""
    exp = min(cap, base * (2 ** attempt))
    delay = exp / 2 + rand() * exp / 2
    if retry_after:
        try:
            delay = max(delay, min(float(retry_after), cap))
        except ValueError:
            pass
    return delay


def request(method: str, url: str, *, limiter: str | None = None, max_attempts: int = 5,
            base: float = 1.0, cap: float = 30.0, sleep=time.sleep, **kw) -> requests.Response:
    """requests.request with retries on 429/5xx/connection errors. Raises after the last try."""
    kw.setdefault("timeout", 30)
    kw["headers"] = {**UA, **(kw.get("headers") or {})}
    lim = LIMITERS.get(limiter) if limiter else None
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            send = getattr(requests, method.lower())   # requests.get / requests.post
            if lim:
                with lim:
                    r = send(url, **kw)
            else:
                r = send(url, **kw)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc, r = e, None
        code = getattr(r, "status_code", 200) if r is not None else None
        if r is not None and code not in RETRY_STATUS:
            r.raise_for_status()
            return r
        if attempt == max_attempts - 1:
            break
        ra = (getattr(r, "headers", None) or {}).get("Retry-After") if r is not None else None
        d = backoff_delay(attempt, base, cap, retry_after=ra)
        log.info("%s %s -> %s; retry %d in %.1fs", method, url[:80],
                 code if r is not None else type(last_exc).__name__, attempt + 1, d)
        sleep(d)
    if r is not None:
        r.raise_for_status()
        return r
    raise last_exc  # type: ignore[misc]
