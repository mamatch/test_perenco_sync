import json
import os
import tempfile
from pathlib import Path

import pytest

os.environ["MOCK_STATE_DIR"] = tempfile.mkdtemp()
os.environ["MOCK_RATE_LIMIT_PER_MINUTE"] = "0"
os.environ["MOCK_CHAOS_RATE"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from mock_gmao.main import app  # noqa: E402

T = "PERENCO"
H = {"X-API-Key": "demo-key-global"}

# Codes are taken from the seed so the tests hold for every candidate variant.
SEED = json.loads((Path(__file__).resolve().parents[1] / "data" / f"{T}.json").read_text())
ASSETS = {a["code"]: a for a in SEED["assets"]}
CHILDREN = {}
for _a in SEED["assets"]:
    if _a["parentCode"]:
        CHILDREN.setdefault(_a["parentCode"], []).append(_a)
PLATFORM_WITH_CHILDREN = next(a["code"] for a in SEED["assets"] if a["familyCode"] == "PLATFORM" and not a["archived"] and any(not c["archived"] for c in CHILDREN.get(a["code"], [])))
ARCHIVED_ROOT = next(a["code"] for a in SEED["assets"] if a["parentCode"] is None and a["archived"])
CHILDLESS_SECTION = next(a["code"] for a in SEED["assets"] if a["familyCode"] == "SECTION" and not a["archived"] and a["code"] not in CHILDREN)
METERED = next(a for a in SEED["assets"] if a["meters"] and not a["archived"])
ARCHIVED_EQUIPMENT = next(a["code"] for a in SEED["assets"] if a["archived"] and a["parentCode"] and a["parentCode"].startswith("SYS_"))
BODY = SEED["bodies"][0]["bodyName"]
ORPHAN_BODY = "Pointe-Noire Warehouse"
NEW_SYSTEM = SEED["scenarioTargets"]["new_system_code"]


@pytest.fixture(autouse=True)
def reset():
    with TestClient(app) as c:
        c.post(f"/_admin/{T}/reset")
    yield


@pytest.fixture
def client():
    return TestClient(app)


def test_auth_required(client):
    assert client.post(f"/{T}/connector/Asset/Filter", json={}).status_code == 401
    assert client.post(f"/{T}/connector/Asset/Filter", json={}, headers={"X-API-Key": "nope"}).status_code == 401


def test_filter_mixes_archived_and_hides_flag(client):
    r = client.post(f"/{T}/connector/Asset/Filter", json={}, headers=H)
    assert r.status_code == 200
    rows = r.json()
    codes = {a["code"] for a in rows}
    assert ARCHIVED_ROOT in codes and PLATFORM_WITH_CHILDREN in codes  # archived and active are mixed
    assert "archived" not in rows[0]
    active = client.post(f"/{T}/connector/Asset/Filter", json={"archived": False}, headers=H).json()
    archived = client.post(f"/{T}/connector/Asset/Filter", json={"archived": True}, headers=H).json()
    assert len(active) + len(archived) == len(rows)
    assert ARCHIVED_ROOT in {a["code"] for a in archived}


def test_pagination(client):
    p1 = client.post(f"/{T}/connector/Asset/Filter", json={"currentPage": 1, "pageSize": 30}, headers=H).json()
    p2 = client.post(f"/{T}/connector/Asset/Filter", json={"currentPage": 2, "pageSize": 30}, headers=H).json()
    p9 = client.post(f"/{T}/connector/Asset/Filter", json={"currentPage": 9, "pageSize": 30}, headers=H).json()
    assert len(p1) == 30 and len(p2) == 30 and p9 == []
    assert not ({a["code"] for a in p1} & {a["code"] for a in p2})


def test_get_exposes_archived_and_meters(client):
    r = client.get(f"/{T}/connector/Asset/Get", params={"code": METERED["code"]}, headers=H)
    assert r.status_code == 200
    assert r.json()["archived"] is False
    assert r.json()["meters"][0]["value"] == METERED["meters"][0]["value"]
    assert client.get(f"/{T}/connector/Asset/Get", params={"code": "NOPE"}, headers=H).status_code == 404


def test_post_validation_and_creation(client):
    r = client.post(f"/{T}/connector/Asset/Post", json={"assetName": "x"}, headers=H)
    assert r.status_code == 406 and "assetCode" in r.json()[0]
    r = client.post(f"/{T}/connector/Asset/Post", json={"assetCode": PLATFORM_WITH_CHILDREN, "assetName": "dup"}, headers=H)
    assert r.status_code == 406 and "already exists" in r.json()[0]
    r = client.post(f"/{T}/connector/Asset/Post", json={"assetCode": "NEW", "assetName": "root without body"}, headers=H)
    assert r.status_code == 406 and "body" in r.json()[0].lower()
    r = client.post(
        f"/{T}/connector/Asset/Post",
        json={"assetCode": "NEWPLAT", "assetName": "New platform", "assetFamilyCode": "PLATFORM", "bodyNames": [BODY]},
        headers=H,
    )
    assert r.status_code == 200
    r = client.post(
        f"/{T}/connector/Asset/Post",
        json={"assetCode": "NEWPLAT_PROD", "assetName": "Production", "assetFamilyCode": "SECTION", "assetParentCode": "NEWPLAT"},
        headers=H,
    )
    assert r.status_code == 200
    got = client.get(f"/{T}/connector/Asset/Get", params={"code": "NEWPLAT_PROD"}, headers=H).json()
    assert got["bodies"] == [{"name": BODY}]  # inherited
    assert client.get(f"/_admin/{T}/calls").json()["writes"] == 2


def test_patch_archive_rules(client):
    r = client.patch(f"/{T}/connector/Asset/Patch", json={"assetCode": PLATFORM_WITH_CHILDREN, "archived": True}, headers=H)
    assert r.status_code == 406 and "active child" in r.json()[0]
    r = client.patch(f"/{T}/connector/Asset/Patch", json={"assetCode": CHILDLESS_SECTION, "archived": True}, headers=H)
    assert r.status_code == 200
    assert client.get(f"/{T}/connector/Asset/Get", params={"code": CHILDLESS_SECTION}, headers=H).json()["archived"] is True
    assert client.patch(f"/{T}/connector/Asset/Patch", json={"assetCode": "NOPE", "archived": True}, headers=H).status_code == 404


def test_body_export(client):
    r = client.post(f"/{T}/connector/ImportExport/Export", json={"action": "bodyExport"}, headers=H)
    assert r.status_code == 200
    names = {b["bodyName"] for b in r.json()}
    assert ORPHAN_BODY in names
    assert any(b["archived"] for b in r.json())
    assert client.post(f"/{T}/connector/ImportExport/Export", json={"action": "Foo"}, headers=H).status_code == 406


def test_meter_update_rules(client):
    url = f"/{T}/connector/Asset/MeterUpdate"
    code, base = METERED["code"], METERED["meters"][0]["value"]
    ok = {"assetCode": code, "dateTime": "2026-09-05T00:00:00Z", "value": base + 120, "name": "Running hours", "unitCode": "H"}
    assert client.post(url, json=ok, headers=H).status_code == 200
    # older reading refused
    r = client.post(url, json={**ok, "dateTime": "2026-09-04T00:00:00Z", "value": base + 180}, headers=H)
    assert r.status_code == 406 and "more recent" in r.json()[0]
    # decreasing counter refused
    r = client.post(url, json={**ok, "dateTime": "2026-09-06T00:00:00Z", "value": 5}, headers=H)
    assert r.status_code == 406 and "lower" in r.json()[0]
    # float refused
    r = client.post(url, json={**ok, "dateTime": "2026-09-06T00:00:00Z", "value": base + 180.5}, headers=H)
    assert r.status_code == 406 and "integer" in r.json()[0]
    # unknown asset
    r = client.post(url, json={**ok, "assetCode": "NO-SUCH-ASSET"}, headers=H)
    assert r.status_code == 406 and "not found" in r.json()[0]
    # archived asset
    r = client.post(url, json={**ok, "assetCode": ARCHIVED_EQUIPMENT}, headers=H)
    assert r.status_code == 406 and "archived" in r.json()[0]
    # unit mismatch
    r = client.post(url, json={**ok, "dateTime": "2026-09-06T00:00:00Z", "value": base + 280, "unitCode": "min"}, headers=H)
    assert r.status_code == 406 and "Unit mismatch" in r.json()[0]
    # meter export sees the new value
    rows = client.post(f"/{T}/connector/ImportExport/Export", json={"action": "AssetMeterExport"}, headers=H).json()
    assert next(m for m in rows if m["assetCode"] == code)["value"] == base + 120


def test_scenarios_and_reset(client):
    r = client.post(f"/_admin/{T}/scenario/technician_adds_system")
    assert r.status_code == 200
    assert client.get(f"/{T}/connector/Asset/Get", params={"code": NEW_SYSTEM}, headers=H).status_code == 200
    client.post(f"/_admin/{T}/reset")
    assert client.get(f"/{T}/connector/Asset/Get", params={"code": NEW_SYSTEM}, headers=H).status_code == 404
    assert client.post(f"/_admin/{T}/scenario/nope").status_code == 404


def test_unknown_tenant(client):
    assert client.post("/NOPE/connector/Asset/Filter", json={}, headers=H).status_code == 404
