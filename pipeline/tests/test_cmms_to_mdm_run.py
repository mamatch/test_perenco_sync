"""End-to-end tests for pipeline/pipeline/cmms_to_mdm.py::run(). The write
path (clients/mdadmin.py::apply_plan, which shells out to systemref_lite's
Django management command) is monkeypatched to a fast in-memory fake here --
the real subprocess/ORM behaviour is covered by
systemref_lite/systemref/tests/test_apply_sync_plan.py and was verified live
against the sandbox. This file is about the orchestration
around it: governed-reference rejection, the plan/pending bookkeeping, and
the disappeared-asset reconciliation pass.
"""

from __future__ import annotations

import sqlite3

import pytest

from pipeline.audit import AuditStore
from pipeline.clients import mdadmin as mdadmin_module
from pipeline.clients.cmms import CmmsClient
from pipeline.clients.mdm import MdmClient
from pipeline.cmms_to_mdm import run
from pipeline.config import Settings

from mdm_fixture import create_mdm_db, insert_equipment_type, insert_org_unit, insert_section_category, insert_system_class, insert_system_unit

BASE_URL = "http://cmms.test"
TENANT = "PERENCO"


def _url(path: str) -> str:
    return f"{BASE_URL}/{TENANT}/connector/{path}"


def _asset(code, family, parent_code=None, criticality=None, in_service_date="2020-01-01T00:00:00Z"):
    return {
        "code": code,
        "name": code,
        "family": {"code": family},
        "parent": {"code": parent_code} if parent_code else None,
        "bodies": [],
        "criticality": {"code": criticality} if criticality else None,
        "inServiceDate": in_service_date,
    }


@pytest.fixture
def cmms():
    return CmmsClient(BASE_URL, TENANT, "test-key", rate_limit_per_minute=0, max_retries=1, timeout=1.0)


@pytest.fixture
def audit(tmp_path):
    store = AuditStore(tmp_path / "audit.sqlite3")
    yield store
    store.close()


@pytest.fixture
def mdm_db(tmp_path):
    path = tmp_path / "mdm.sqlite3"
    create_mdm_db(path)
    return path


@pytest.fixture
def settings(tmp_path):
    return Settings(systemref_lite_dir=tmp_path)


@pytest.fixture
def fake_apply_plan(monkeypatch):
    """Records the SyncPlan it was called with instead of shelling out to Django."""
    calls: list = []

    def _fake(plan, systemref_lite_dir, mdm_db_path):
        calls.append(plan)
        return {
            "systems": len(plan.systems),
            "equipments": len(plan.equipments),
            "assignments": len(plan.assignments),
            "closures": sum(1 for s in plan.systems + plan.equipments if s["op"] == "close"),
        }

    monkeypatch.setattr("pipeline.cmms_to_mdm.apply_plan", _fake)
    return calls


def test_system_with_unknown_parent_is_rejected(cmms, audit, mdm_db, settings, fake_apply_plan, requests_mock):
    requests_mock.post(_url("Asset/Filter"), [{"json": [_asset("SYS_JNR_004", "SYS_PG", parent_code="MISSING_SECTION")]}, {"json": []}])

    with MdmClient(mdm_db) as mdm:
        run_id = run(cmms, mdm, audit, settings)

    row = audit.conn.execute("SELECT status FROM actions WHERE run_id=? AND entity_code=?", (run_id, "SYS_JNR_004")).fetchone()
    assert row["status"] == "REJECTED"
    dq = audit.conn.execute("SELECT reason FROM dq_issues WHERE run_id=? AND entity_code=?", (run_id, "SYS_JNR_004")).fetchone()
    assert dq["reason"] == "unknown_parent"
    assert fake_apply_plan == []  # nothing valid to apply


def test_system_with_unknown_system_class_is_rejected(cmms, audit, mdm_db, settings, fake_apply_plan, requests_mock):
    conn = sqlite3.connect(mdm_db)
    org_unit = insert_org_unit(conn, name="Tchatamba")
    insert_system_unit(conn, code="JNR", name="Tchatamba", org_unit_id=org_unit, date_start="2020-01-01")
    insert_section_category(conn, code="PG", name="Power Generation")
    conn.close()

    requests_mock.post(
        _url("Asset/Filter"),
        [
            {
                "json": [
                    _asset("JNR_PG", "SECTION", parent_code="JNR"),
                    _asset("SYS_JNR_004", "SYS_UNKNOWN", parent_code="JNR_PG"),
                ]
            },
            {"json": []},
        ],
    )

    with MdmClient(mdm_db) as mdm:
        run_id = run(cmms, mdm, audit, settings)

    dq = audit.conn.execute("SELECT reason FROM dq_issues WHERE run_id=? AND entity_code=?", (run_id, "SYS_JNR_004")).fetchone()
    assert dq["reason"] == "unknown_system_class"


def test_valid_system_and_equipment_are_queued_and_applied(cmms, audit, mdm_db, settings, fake_apply_plan, requests_mock):
    conn = sqlite3.connect(mdm_db)
    org_unit = insert_org_unit(conn, name="Tchatamba")
    insert_system_unit(conn, code="JNR", name="Tchatamba", org_unit_id=org_unit, date_start="2020-01-01")
    insert_section_category(conn, code="PG", name="Power Generation")
    insert_system_class(conn, code="PG", name="Power Generation")
    insert_equipment_type(conn, code="PU_CE", name="Centrifugal pump")
    conn.close()

    requests_mock.post(
        _url("Asset/Filter"),
        [
            {
                "json": [
                    _asset("JNR_PG", "SECTION", parent_code="JNR"),
                    _asset("SYS_JNR_004", "SYS_PG", parent_code="JNR_PG", criticality="PC"),
                    _asset("JNR-PU-101", "PU_CE", parent_code="SYS_JNR_004"),
                ]
            },
            {"json": []},
        ],
    )

    with MdmClient(mdm_db) as mdm:
        run_id = run(cmms, mdm, audit, settings)

    assert len(fake_apply_plan) == 1
    plan = fake_apply_plan[0]
    assert [s["code"] for s in plan.systems] == ["SYS_JNR_004"]
    assert plan.systems[0]["attribute_names"] == ["Production critical"]
    assert [e["code"] for e in plan.equipments] == ["JNR-PU-101"]
    assert plan.assignments == [{"system_code": "SYS_JNR_004", "equipment_code": "JNR-PU-101", "assignment_date": "2020-01-01"}]

    statuses = {r["entity_code"]: r["status"] for r in audit.conn.execute("SELECT entity_code, status FROM actions WHERE run_id=?", (run_id,))}
    assert statuses["SYS_JNR_004"] == "SUCCESS"
    assert statuses["JNR-PU-101"] == "SUCCESS"


def test_apply_plan_failure_marks_pending_actions_failed_retryable(cmms, audit, mdm_db, settings, monkeypatch, requests_mock):
    conn = sqlite3.connect(mdm_db)
    org_unit = insert_org_unit(conn, name="Tchatamba")
    insert_system_unit(conn, code="JNR", name="Tchatamba", org_unit_id=org_unit, date_start="2020-01-01")
    insert_section_category(conn, code="PG", name="Power Generation")
    insert_system_class(conn, code="PG", name="Power Generation")
    conn.close()

    requests_mock.post(
        _url("Asset/Filter"),
        [
            {"json": [_asset("JNR_PG", "SECTION", parent_code="JNR"), _asset("SYS_JNR_004", "SYS_PG", parent_code="JNR_PG")]},
            {"json": []},
        ],
    )

    def _boom(plan, systemref_lite_dir, mdm_db_path):
        raise mdadmin_module.MdAdminCommandError(1, "IntegrityError: boom")

    monkeypatch.setattr("pipeline.cmms_to_mdm.apply_plan", _boom)

    with MdmClient(mdm_db) as mdm:
        run_id = run(cmms, mdm, audit, settings)

    row = audit.conn.execute("SELECT status, reason FROM actions WHERE run_id=? AND entity_code=?", (run_id, "SYS_JNR_004")).fetchone()
    assert row["status"] == "FAILED_RETRYABLE"
    assert "apply_sync_plan failed" in row["reason"]

    alert = audit.conn.execute("SELECT severity, message FROM alerts WHERE run_id=?", (run_id,)).fetchone()
    assert alert["severity"] == "CRITICAL"
    assert "rolled back" in alert["message"]


def test_system_disappeared_from_cmms_is_closed(cmms, audit, mdm_db, settings, fake_apply_plan, requests_mock):
    conn = sqlite3.connect(mdm_db)
    conn.execute("INSERT INTO systemref_system (code, date_end) VALUES (?, NULL)", ("SYS_OLD_001",))
    conn.commit()
    conn.close()

    requests_mock.post(_url("Asset/Filter"), json=[])  # CMMS reports nothing at all, not even archived

    with MdmClient(mdm_db) as mdm:
        run_id = run(cmms, mdm, audit, settings)

    assert len(fake_apply_plan) == 1
    plan = fake_apply_plan[0]
    assert [s["code"] for s in plan.systems if s["op"] == "close"] == ["SYS_OLD_001"]

    row = audit.conn.execute("SELECT action, status, reason FROM actions WHERE run_id=? AND entity_code=?", (run_id, "SYS_OLD_001")).fetchone()
    assert (row["action"], row["status"]) == ("ARCHIVE", "SUCCESS")
    assert "disappeared" in row["reason"]
