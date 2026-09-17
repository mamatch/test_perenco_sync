"""Thin client for the DIMO Maint MX connector API (docs/03_cmms_api.md).

Responsibilities kept deliberately in this layer, per ARCHITECTURE_.md: Celery
is responsible for when and in which order work runs, not for the detailed API
retry/rate-limit logic -- that belongs here, in the integration worker/client
layer:

* authentication (X-API-Key header, never logged);
* a client-side rate limiter so the pipeline stays under the tenant's global
  50 req/min budget instead of discovering it via 429s;
* retry with exponential backoff + jitter on 429 (respecting Retry-After),
  500 and 503, and on network/timeout errors;
* no retry on 401/404/406 -- these are business/programming errors, not
  transient ones, and must surface to the caller;
* pagination helpers.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from typing import Any, Iterator

import requests

logger = logging.getLogger("pipeline.cmms")


class CmmsError(Exception):
    def __init__(self, status_code: int, messages: list[str]):
        self.status_code = status_code
        self.messages = messages
        super().__init__(f"HTTP {status_code}: {'; '.join(messages)}")


class CmmsAuthError(CmmsError):
    pass


class CmmsNotFound(CmmsError):
    pass


class CmmsValidationError(CmmsError):
    """HTTP 406: business validation error. Never retried."""


class CmmsUnavailable(CmmsError):
    """Retries exhausted on a transient failure (429/5xx/network)."""


class _RateLimiter:
    """Client-side sliding-window limiter, so we stay under the tenant budget
    proactively instead of relying only on reacting to 429 responses."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Call before every HTTP request. Blocks just long enough to keep the
        last 60s of calls under `per_minute` (sliding window, not a fixed
        per-minute bucket that resets on the clock)."""
        if self.per_minute <= 0:
            return
        with self._lock:
            now = time.monotonic()
            # drop timestamps older than the 60s window
            while self._events and now - self._events[0] > 60:
                self._events.popleft()
            if len(self._events) >= self.per_minute:
                # window is full: sleep until the oldest call falls out of it
                sleep_for = 60 - (now - self._events[0]) + 0.05
                time.sleep(max(sleep_for, 0))
                now = time.monotonic()
                while self._events and now - self._events[0] > 60:
                    self._events.popleft()
            self._events.append(time.monotonic())


class CmmsClient:
    def __init__(
        self,
        base_url: str,
        tenant: str,
        api_key: str,
        rate_limit_per_minute: int = 50,
        max_retries: int = 3,
        timeout: float = 10.0,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.tenant = tenant
        self._api_key = api_key
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = session or requests.Session()
        self._limiter = _RateLimiter(rate_limit_per_minute)
        self.calls = 0
        self.retries = 0

    # -- low level ----------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base_url}/{self.tenant}/connector/{path}"

    def _request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> Any:
        url = self._url(path)
        attempt = 0
        while True:  # retry loop: only 429/5xx/network errors loop back here
            attempt += 1
            self._limiter.acquire()  # may sleep to respect the per-minute budget
            try:
                resp = self.session.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    headers={"X-API-Key": self._api_key},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                # connection/timeout errors: treated the same as a transient
                # server failure, retried with backoff up to max_retries.
                if attempt > self.max_retries:
                    raise CmmsUnavailable(0, [f"network error after {attempt} attempts: {exc}"]) from exc
                self._backoff_sleep(attempt)
                self.retries += 1
                continue

            self.calls += 1

            if resp.status_code == 200:
                return resp.json()

            messages = _safe_messages(resp)

            # Business/programming errors: never retried, always surfaced to the caller.
            if resp.status_code == 401:
                raise CmmsAuthError(401, messages)
            if resp.status_code == 404:
                raise CmmsNotFound(404, messages)
            if resp.status_code == 406:
                raise CmmsValidationError(406, messages)

            if resp.status_code == 429:
                # rate limited server-side despite our own limiter (e.g. another
                # process sharing the tenant budget): honour Retry-After if given.
                if attempt > self.max_retries:
                    raise CmmsUnavailable(429, messages)
                retry_after = float(resp.headers.get("Retry-After", 2 * attempt))
                logger.warning("CMMS rate limited, retrying in %.1fs (attempt %d)", retry_after, attempt)
                time.sleep(retry_after)
                self.retries += 1
                continue

            if resp.status_code in (500, 503):
                # transient server failure (the mock injects ~3% of these on
                # purpose) -- retried with exponential backoff + jitter.
                if attempt > self.max_retries:
                    raise CmmsUnavailable(resp.status_code, messages)
                self._backoff_sleep(attempt)
                self.retries += 1
                continue

            # Unexpected status: don't loop forever on something we don't understand.
            raise CmmsError(resp.status_code, messages)

    @staticmethod
    def _backoff_sleep(attempt: int) -> None:
        base = min(2 ** attempt, 30)
        time.sleep(base * (0.5 + random.random() / 2))

    # -- Asset ---------------------------------------------------------------
    def filter_assets_page(self, *, current_page: int = 1, page_size: int = 1000, **filters: Any) -> list[dict]:
        body = {"currentPage": current_page, "pageSize": page_size, **{k: v for k, v in filters.items() if v is not None}}
        return self._request("POST", "Asset/Filter", json=body)

    def iter_assets(self, page_size: int = 1000, **filters: Any) -> Iterator[dict]:
        """Pages through Asset/Filter until a short page (fewer rows than
        page_size) signals the last one. Callers pass archived=True/False
        explicitly since Asset/Filter never returns that field either way."""
        page = 1
        while True:
            rows = self.filter_assets_page(current_page=page, page_size=page_size, **filters)
            if not rows:
                return
            yield from rows
            if len(rows) < page_size:
                return
            page += 1

    def get_asset(self, code: str) -> dict:
        return self._request("GET", "Asset/Get", params={"code": code})

    def post_asset(self, payload: dict) -> list[str]:
        return self._request("POST", "Asset/Post", json=payload)

    def patch_asset(self, payload: dict) -> list[str]:
        return self._request("PATCH", "Asset/Patch", json=payload)

    def meter_update(self, payload: dict) -> list[str]:
        return self._request("POST", "Asset/MeterUpdate", json=payload)

    # -- ImportExport ---------------------------------------------------------
    def iter_export(self, action: str, page_size: int = 1000, **filters: Any) -> Iterator[dict]:
        page = 1
        while True:
            body = {"action": action, "currentPage": page, "pageSize": page_size, **{k: v for k, v in filters.items() if v is not None}}
            rows = self._request("POST", "ImportExport/Export", json=body)
            if not rows:
                return
            yield from rows
            if len(rows) < page_size:
                return
            page += 1


def _safe_messages(resp: requests.Response) -> list[str]:
    try:
        data = resp.json()
        if isinstance(data, list):
            return [str(x) for x in data]
        return [str(data)]
    except ValueError:
        return [resp.text[:500]]
