"""End-to-end tests for pipeline/pipeline/mdm_to_cmms.py::run(), previously
only ever exercised live against the sandbox. CMMS calls are mocked with
requests_mock (unregistered URLs raise, which doubles as a "no CMMS call was
made" assertion); the MDM side is a small real SQLite db built from
tests/mdm_fixture.py, matching what clients/mdm.py actually reads.
"""

from __future__ import annotations

import sqlite3

import pytest

from pipeline.audit import AuditStore
from pipeline.clients.cmms import CmmsClient
from pipeline.clients.mdm import MdmClient
from pipeline.config import Settings
from pipeline.mdm_to_cmms import run

from mdm_fixture import create_mdm_db, insert_org_unit, insert_section_assignment, insert_section_category, insert_system_unit

BASE_URL = "http://cmms.test"
TENANT = "PERENCO"


def _url(path: str) -> str:
    return f"{BASE_URL}/{TENANT}/connector/{path}"


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
def settings():
    return Settings(archive_ratio_threshold=0.5)


def test_empty_mdm_snapshot_aborts_without_calling_the_cmms(cmms, audit, mdm_db, settings, requests_mock):
    # No Asset/Filter or Asset/Post mock registered at all: any HTTP attempt
    # would raise requests_mock.NoMockAddress, which is exactly the
    # assertion -- the empty-snapshot guard must return before touching the CMMS.
    with MdmClient(mdm_db) as mdm:
        run_id = run(cmms, mdm, audit, settings)

    row = audit.conn.execute("SELECT severity, message FROM alerts WHERE run_id=?", (run_id,)).fetchone()
    assert row["severity"] == "CRITICAL"
    assert "zero active platforms" in row["message"]
    assert audit.action_counts(run_id) == {}


def test_new_platform_is_created(cmms, audit, mdm_db, settings, requests_mock):
    conn = sqlite3.connect(mdm_db)
    org_unit = insert_org_unit(conn, name="Tchatamba")
    insert_system_unit(conn, code="JNR", name="Tchatamba", org_unit_id=org_unit, date_start="2020-01-01")
    conn.close()

    requests_mock.post(_url("Asset/Filter"), json=[])  # active tree empty, archived PLATFORM/SECTION empty
    requests_mock.post(_url("Asset/Post"), json=["created"])

    with MdmClient(mdm_db) as mdm:
        run_id = run(cmms, mdm, audit, settings)

    assert audit.action_counts(run_id) == {"CREATE:SUCCESS": 1}
    action = audit.conn.execute("SELECT entity_code, action, status FROM actions WHERE run_id=?", (run_id,)).fetchone()
    assert (action["entity_code"], action["action"], action["status"]) == ("JNR", "CREATE", "SUCCESS")


def test_section_is_rejected_when_its_platform_create_fails(cmms, audit, mdm_db, settings, requests_mock):
    conn = sqlite3.connect(mdm_db)
    org_unit = insert_org_unit(conn, name="Tchatamba")
    unit_id = insert_system_unit(conn, code="JNR", name="Tchatamba", org_unit_id=org_unit, date_start="2020-01-01")
    section_id = insert_section_category(conn, code="PG", name="Power Generation")
    insert_section_assignment(conn, system_unit_id=unit_id, section_category_id=section_id, date_start="2020-01-01")
    conn.close()

    requests_mock.post(_url("Asset/Filter"), json=[])
    requests_mock.post(_url("Asset/Post"), status_code=406, json=["duplicate code"])

    with MdmClient(mdm_db) as mdm:
        run_id = run(cmms, mdm, audit, settings)

    rows = {r["entity_code"]: r["status"] for r in audit.conn.execute("SELECT entity_code, status FROM actions WHERE run_id=?", (run_id,))}
    assert rows["JNR"] == "REJECTED"
    assert rows["JNR_PG"] == "REJECTED"
    section_reason = audit.conn.execute("SELECT reason FROM actions WHERE run_id=? AND entity_code=?", (run_id, "JNR_PG")).fetchone()["reason"]
    assert "parent 'JNR' action failed this run" in section_reason
    # Only ONE Asset/Post call: the section must never be sent once its parent failed
    # (three Asset/Filter calls are expected: active tree + archived PLATFORM + archived SECTION).
    post_calls = [r for r in requests_mock.request_history if r.path.endswith("/asset/post")]
    assert len(post_calls) == 1
