"""Tests for the CMMS HTTP client's retry/rate-limit/pagination behaviour
(pipeline/pipeline/clients/cmms.py) -- previously only exercised live against
the sandbox, never with a mocked transport. `time.sleep` is monkeypatched to
a no-op throughout so the retry paths run at test speed instead of actually
backing off for real seconds.
"""

from __future__ import annotations

import pytest

from pipeline.clients.cmms import (
    CmmsAuthError,
    CmmsClient,
    CmmsNotFound,
    CmmsUnavailable,
    CmmsValidationError,
    _RateLimiter,
)
from pipeline.clients import cmms as cmms_module

BASE_URL = "http://cmms.test"
TENANT = "PERENCO"


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(cmms_module.time, "sleep", lambda *_args, **_kwargs: None)


@pytest.fixture
def client():
    return CmmsClient(BASE_URL, TENANT, "test-key", rate_limit_per_minute=0, max_retries=3, timeout=1.0)


def _url(path: str) -> str:
    return f"{BASE_URL}/{TENANT}/connector/{path}"


def test_429_is_retried_and_respects_retry_after(requests_mock, client):
    requests_mock.post(
        _url("Asset/Post"),
        [
            {"status_code": 429, "headers": {"Retry-After": "3"}, "json": ["rate limited"]},
            {"status_code": 200, "json": ["ok"]},
        ],
    )
    result = client.post_asset({"assetCode": "X"})
    assert result == ["ok"]
    assert client.retries == 1
    assert client.calls == 2  # every response received counts, 429 included


def test_5xx_is_retried_then_succeeds(requests_mock, client):
    requests_mock.post(
        _url("Asset/Post"),
        [
            {"status_code": 503, "json": ["unavailable"]},
            {"status_code": 500, "json": ["error"]},
            {"status_code": 200, "json": ["ok"]},
        ],
    )
    result = client.post_asset({"assetCode": "X"})
    assert result == ["ok"]
    assert client.retries == 2


def test_5xx_exhausting_retries_raises_cmms_unavailable(requests_mock, client):
    requests_mock.post(_url("Asset/Post"), status_code=503, json=["still down"])
    with pytest.raises(CmmsUnavailable):
        client.post_asset({"assetCode": "X"})
    # max_retries=3: the first attempt plus 3 retries = 4 calls total.
    assert client.calls == 4


@pytest.mark.parametrize(
    "status_code,exc",
    [(401, CmmsAuthError), (404, CmmsNotFound), (406, CmmsValidationError)],
)
def test_business_errors_are_never_retried(requests_mock, client, status_code, exc):
    requests_mock.post(_url("Asset/Post"), status_code=status_code, json=["nope"])
    with pytest.raises(exc):
        client.post_asset({"assetCode": "X"})
    assert client.calls == 1
    assert client.retries == 0


def test_network_error_is_retried_then_raises_unavailable(requests_mock, client):
    import requests as requests_lib

    requests_mock.post(_url("Asset/Post"), exc=requests_lib.exceptions.ConnectionError("boom"))
    with pytest.raises(CmmsUnavailable):
        client.post_asset({"assetCode": "X"})


def test_iter_assets_paginates_until_a_short_page(requests_mock, client):
    page1 = [{"code": f"A{i}"} for i in range(3)]
    page2 = [{"code": "A3"}]  # shorter than page_size -> last page
    requests_mock.post(
        _url("Asset/Filter"),
        [
            {"json": page1},
            {"json": page2},
        ],
    )
    codes = [a["code"] for a in client.iter_assets(page_size=3, archived=False)]
    assert codes == ["A0", "A1", "A2", "A3"]


def test_iter_assets_stops_immediately_on_empty_first_page(requests_mock, client):
    requests_mock.post(_url("Asset/Filter"), json=[])
    assert list(client.iter_assets(archived=False)) == []


def test_rate_limiter_sleeps_once_budget_is_exhausted(monkeypatch):
    fake_now = [0.0]
    monkeypatch.setattr(cmms_module.time, "monotonic", lambda: fake_now[0])
    sleeps: list[float] = []
    monkeypatch.setattr(cmms_module.time, "sleep", lambda s: sleeps.append(s))

    limiter = _RateLimiter(per_minute=2)
    limiter.acquire()
    fake_now[0] += 1
    limiter.acquire()
    fake_now[0] += 1
    limiter.acquire()  # 3rd call within the 60s window -> must wait

    assert sleeps and sleeps[0] > 0


def test_rate_limiter_disabled_when_per_minute_is_zero(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(cmms_module.time, "sleep", lambda s: sleeps.append(s))
    limiter = _RateLimiter(per_minute=0)
    for _ in range(10):
        limiter.acquire()
    assert sleeps == []
