"""End-to-end tests for pipeline/pipeline/iot_to_cmms.py::run(), in particular
the counter-regression guard (_baseline_value / the quarantine branch in
run()) -- the most business-critical rule in Part D, previously only ever
verified against the real historian exports, never with a synthetic
regression case isolated from the rest of the data.
"""

from __future__ import annotations

import pytest

from pipeline.audit import AuditStore
from pipeline.clients.cmms import CmmsClient
from pipeline.config import Settings
from pipeline.iot_to_cmms import run

BASE_URL = "http://cmms.test"
TENANT = "PERENCO"


def _url(path: str) -> str:
    return f"{BASE_URL}/{TENANT}/connector/{path}"


def _asset(code, family, parent_code=None):
    return {"code": code, "name": code, "family": {"code": family}, "parent": {"code": parent_code} if parent_code else None, "bodies": []}


@pytest.fixture
def cmms():
    return CmmsClient(BASE_URL, TENANT, "test-key", rate_limit_per_minute=0, max_retries=1, timeout=1.0)


@pytest.fixture
def audit(tmp_path):
    store = AuditStore(tmp_path / "audit.sqlite3")
    yield store
    store.close()


@pytest.fixture
def exports_dir(tmp_path):
    d = tmp_path / "exports"
    d.mkdir()
    return d


@pytest.fixture
def settings(exports_dir):
    return Settings(iot_exports_dir=exports_dir)


def _write_csv(path, rows):
    header = "tag_id,timestamp_utc,value,unit,quality,exported_at_utc\n"
    body = "\n".join(",".join(str(v) for v in row) for row in rows)
    path.write_text(header + body + "\n")


def test_counter_regression_is_quarantined_not_sent(cmms, audit, exports_dir, settings, requests_mock):
    requests_mock.post(_url("Asset/Filter"), [{"json": [_asset("OLW-P-301A", "PU_CE")]}, {"json": []}])

    # A prior run already sent 200h for 2026-01-01: the baseline this run must respect.
    with audit.run("iot_to_cmms") as seed_run_id:
        audit.record_meter_sent(seed_run_id, "OLW-P-301A", "2026-01-01", "GA-OLW.P-301A.RUN_HRS", "2026-01-01T00:00:00Z", 200)

    # 2026-01-02's export shows 150h -- a regression, must be quarantined, never sent.
    _write_csv(
        exports_dir / "export.csv",
        [("GA-OLW.P-301A.RUN_HRS", "2026-01-02T00:00:00Z", 150, "h", "GOOD", "2026-01-02T06:00:00Z")],
    )

    run_id = run(cmms, audit, settings)

    action = audit.conn.execute("SELECT action, status, reason FROM actions WHERE run_id=? AND entity_code=?", (run_id, "OLW-P-301A")).fetchone()
    assert (action["action"], action["status"]) == ("UPDATE", "REJECTED")
    assert "counter regression" in action["reason"]

    dq = audit.conn.execute("SELECT reason FROM dq_issues WHERE run_id=? AND entity_code=?", (run_id, "OLW-P-301A")).fetchone()
    assert dq["reason"] == "counter_regression"

    # Never sent to the CMMS: no Asset/MeterUpdate call at all.
    meter_calls = [r for r in requests_mock.request_history if r.path.endswith("/asset/meterupdate")]
    assert meter_calls == []

    # The regression must still show up in the audit-level metric.
    metric = audit.conn.execute("SELECT value FROM run_metrics WHERE run_id=? AND metric=?", (run_id, "iot_counter_regressions")).fetchone()
    assert metric["value"] == 1.0


def test_good_reading_is_sent_and_second_run_is_idempotent(cmms, audit, exports_dir, settings, requests_mock):
    # 2 Asset/Filter calls per run (archived=False, archived=True) x 2 runs.
    active_page = {"json": [_asset("OLW-P-301A", "PU_CE")]}
    empty_page = {"json": []}
    requests_mock.post(_url("Asset/Filter"), [active_page, empty_page, active_page, empty_page])
    requests_mock.post(_url("Asset/MeterUpdate"), json=["ok"])
    # No prior audit history for this asset: _baseline_value() falls back to asking
    # the CMMS directly. No "Running hours" meter yet -> baseline stays None.
    requests_mock.get(_url("Asset/Get"), json={"code": "OLW-P-301A", "meters": []})

    _write_csv(
        exports_dir / "export.csv",
        [("GA-OLW.P-301A.RUN_HRS", "2026-01-01T00:00:00Z", 200, "h", "GOOD", "2026-01-01T06:00:00Z")],
    )

    run_id_1 = run(cmms, audit, settings)
    action = audit.conn.execute("SELECT action, status FROM actions WHERE run_id=? AND entity_code=?", (run_id_1, "OLW-P-301A")).fetchone()
    assert (action["action"], action["status"]) == ("UPDATE", "SUCCESS")
    meter_calls = [r for r in requests_mock.request_history if r.path.endswith("/asset/meterupdate")]
    assert len(meter_calls) == 1

    # Second run, same export: must be a NOOP, no second MeterUpdate call.
    run_id_2 = run(cmms, audit, settings)
    action_2 = audit.conn.execute("SELECT action, status FROM actions WHERE run_id=? AND entity_code=?", (run_id_2, "OLW-P-301A")).fetchone()
    assert (action_2["action"], action_2["status"]) == ("NOOP", "SUCCESS")
    meter_calls_after = [r for r in requests_mock.request_history if r.path.endswith("/asset/meterupdate")]
    assert len(meter_calls_after) == 1  # unchanged


def test_day_with_no_good_reading_is_flagged_and_meter_unchanged(cmms, audit, exports_dir, settings, requests_mock):
    requests_mock.post(_url("Asset/Filter"), [{"json": [_asset("OLW-P-301A", "PU_CE")]}, {"json": []}])
    requests_mock.get(_url("Asset/Get"), json={"code": "OLW-P-301A", "meters": []})

    _write_csv(
        exports_dir / "export.csv",
        [("GA-OLW.P-301A.RUN_HRS", "2026-01-01T00:00:00Z", 200, "h", "BAD", "2026-01-01T06:00:00Z")],
    )

    run_id = run(cmms, audit, settings)

    dq = audit.conn.execute("SELECT reason FROM dq_issues WHERE run_id=? AND entity_code=?", (run_id, "OLW-P-301A")).fetchone()
    assert dq["reason"] == "no_good_reading_for_day"
    meter_calls = [r for r in requests_mock.request_history if r.path.endswith("/asset/meterupdate")]
    assert meter_calls == []
