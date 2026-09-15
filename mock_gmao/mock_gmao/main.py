"""FastAPI application mocking the DIMO Maint MX connector API.

Run:  uv run uvicorn mock_gmao.main:app --port 8080

Environment variables (all optional):
  MOCK_DATA_DIR              seed directory                (default: ./data)
  MOCK_STATE_DIR             persisted state directory     (default: ./state)
  MOCK_RATE_LIMIT_PER_MINUTE requests/min/tenant, 0 = off  (default: 50)
  MOCK_CHAOS_RATE            share of connector calls that fail with 500/503, 0 = off (default: 0.03)
  MOCK_CHAOS_SEED            RNG seed for reproducible chaos (default: 1234)
  MOCK_LATENCY_MS            artificial latency per call   (default: 0)
"""

from __future__ import annotations

import os
import random
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from .store import ApiError, TenantStore

DATA_DIR = Path(os.getenv("MOCK_DATA_DIR", Path(__file__).resolve().parents[1] / "data"))
STATE_DIR = Path(os.getenv("MOCK_STATE_DIR", Path(__file__).resolve().parents[1] / "state"))
RATE_LIMIT = int(os.getenv("MOCK_RATE_LIMIT_PER_MINUTE", "50"))
CHAOS_RATE = float(os.getenv("MOCK_CHAOS_RATE", "0.03"))
CHAOS_SEED = int(os.getenv("MOCK_CHAOS_SEED", "1234"))
LATENCY_MS = int(os.getenv("MOCK_LATENCY_MS", "0"))

app = FastAPI(
    title="DIMO Maint MX connector (mock)",
    version="1.0-mock",
    description=(
        "Simplified mock of the DIMO Maint MX CMMS connector API for the data engineering test. "
        "Authenticate with the `X-API-Key` header. Everything under `/_admin` is mock-only tooling."
    ),
)

_stores: dict[str, TenantStore] = {}
_rate_windows: dict[str, deque] = {}
_rate_lock = threading.Lock()
_chaos_rng = random.Random(CHAOS_SEED)


def get_store(tenant: str) -> TenantStore:
    store = _stores.get(tenant)
    if store is None:
        seed = DATA_DIR / f"{tenant}.json"
        if not seed.exists():
            raise ApiError(404, f"Unknown tenant '{tenant}'")
        store = _stores[tenant] = TenantStore(tenant, seed, STATE_DIR)
    return store


def _messages(status: int, messages: list[str], headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=messages, headers=headers or {})


@app.exception_handler(ApiError)
async def _api_error_handler(_request: Request, exc: ApiError):
    return _messages(exc.status_code, exc.messages)


def _rate_limited(store: TenantStore) -> int | None:
    """Sliding one-minute window per tenant. Returns Retry-After seconds when limited."""
    limit = store.rate_limit_override if store.rate_limit_override is not None else RATE_LIMIT
    if limit <= 0:
        return None
    now = time.monotonic()
    with _rate_lock:
        window = _rate_windows.setdefault(store.tenant, deque())
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= limit:
            return max(1, int(60 - (now - window[0])) + 1)
        window.append(now)
    return None


def connector(tenant: str, x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> TenantStore:
    """Dependency shared by every /{tenant}/connector route: auth, rate limit, chaos, latency."""
    store = get_store(tenant)
    store.calls["total"] += 1
    if not x_api_key or x_api_key != store.api_key:
        store.calls["401"] += 1
        raise ApiError(401, "Invalid or missing API key")
    retry_after = _rate_limited(store)
    if retry_after is not None:
        store.calls["429"] += 1
        raise RateLimited(retry_after)
    rate = store.chaos_rate_override if store.chaos_rate_override is not None else CHAOS_RATE
    if rate > 0 and _chaos_rng.random() < rate:
        store.calls["5xx"] += 1
        raise ApiError(_chaos_rng.choice([500, 503]), "Transient server error (simulated)")
    if LATENCY_MS:
        time.sleep(LATENCY_MS / 1000)
    return store


class RateLimited(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after


@app.exception_handler(RateLimited)
async def _rate_limited_handler(_request: Request, exc: RateLimited):
    return _messages(429, ["Rate limit exceeded"], {"Retry-After": str(exc.retry_after)})


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@app.post("/{tenant}/connector/Asset/Filter", tags=["Asset"], summary="Get the list of assets (paginated)")
def asset_filter(store: TenantStore = Depends(connector), body: dict[str, Any] = Body(default_factory=dict)):
    """Filters: archived, assetCode, parentCode, assetFamilyCode, bodyNames[],
    startLastModificationDateTime, endLastModificationDateTime, currentPage (1-based), pageSize (max 1000).
    A page beyond the end returns an empty list. The response does not include the `archived` flag."""
    store.calls["Asset/Filter"] += 1
    return store.filter_assets(body)


@app.get("/{tenant}/connector/Asset/Get", tags=["Asset"], summary="Get one asset by code or interfaceKey")
def asset_get(store: TenantStore = Depends(connector), code: str | None = Query(default=None), interfaceKey: str | None = Query(default=None)):
    store.calls["Asset/Get"] += 1
    return store.get_asset(code, interfaceKey)


@app.post("/{tenant}/connector/Asset/Post", tags=["Asset"], summary="Create an asset")
def asset_post(store: TenantStore = Depends(connector), body: dict[str, Any] = Body(...)):
    """Body: assetCode (required on this tenant), assetName (required), assetFamilyCode, assetParentCode,
    bodyNames[] (required for root assets, inherited from the parent otherwise), interfaceKey, sharedData."""
    store.calls["Asset/Post"] += 1
    return store.post_asset(body)


@app.patch("/{tenant}/connector/Asset/Patch", tags=["Asset"], summary="Update or archive an asset identified by assetCode")
def asset_patch(store: TenantStore = Depends(connector), body: dict[str, Any] = Body(...)):
    """Body: assetCode (required), archived, assetName, parentCode, bodyNames[], assetFamilyCode, interfaceKey.
    Archiving an asset that still has active children is refused (406)."""
    store.calls["Asset/Patch"] += 1
    return store.patch_asset(body)


@app.post("/{tenant}/connector/Asset/MeterUpdate", tags=["Asset"], summary="Record a new meter reading on an asset")
def asset_meter_update(store: TenantStore = Depends(connector), body: dict[str, Any] = Body(...)):
    """Body: assetCode (or assetInterfaceKey), dateTime (ISO 8601, required), value (integer, required),
    name (meter name, default 'Running hours'), unitCode (default 'H'), userName.
    Cumulative meters: the reading must be more recent and not lower than the previous one."""
    store.calls["Asset/MeterUpdate"] += 1
    return store.meter_update(body)


# ---------------------------------------------------------------------------
# ImportExport
# ---------------------------------------------------------------------------

@app.post("/{tenant}/connector/ImportExport/Export", tags=["ImportExport"], summary="Export action (BodyExport, AssetMeterExport)")
def import_export_export(store: TenantStore = Depends(connector), body: dict[str, Any] = Body(...)):
    """Body: action ('BodyExport' | 'AssetMeterExport'), bodyNames[] (optional filter), currentPage, pageSize."""
    store.calls["ImportExport/Export"] += 1
    return store.export(body)


# ---------------------------------------------------------------------------
# Mock administration (no auth, not part of the real API)
# ---------------------------------------------------------------------------

@app.get("/_admin/health", tags=["_admin"])
def health():
    return {"status": "ok", "tenants": sorted(p.stem for p in DATA_DIR.glob("*.json"))}


@app.get("/_admin/{tenant}/state", tags=["_admin"], summary="Dump the full tenant state (including archived flags and meters)")
def admin_state(tenant: str):
    return get_store(tenant).snapshot()


@app.get("/_admin/{tenant}/calls", tags=["_admin"], summary="Call counters since the last reset (use it to check idempotency)")
def admin_calls(tenant: str):
    store = get_store(tenant)
    return dict(store.calls)


@app.post("/_admin/{tenant}/reset", tags=["_admin"], summary="Reload the seed and clear counters/scenarios")
def admin_reset(tenant: str):
    store = get_store(tenant)
    store.reset()
    with _rate_lock:
        _rate_windows.pop(tenant, None)
    return {"reset": tenant}


@app.post("/_admin/{tenant}/scenario/{name}", tags=["_admin"], summary="Mutate the tenant state to simulate an event")
def admin_scenario(tenant: str, name: str):
    """Scenarios: technician_adds_system, technician_archives_system, key_user_renames_section, api_degraded, rate_limit_strict."""
    return {"scenario": name, "result": get_store(tenant).apply_scenario(name)}
