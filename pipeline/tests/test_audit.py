"""Tests for the run/audit store (pipeline/pipeline/audit.py) -- the
durable record every integration writes through, previously only exercised
indirectly via live runs against the sandbox.
"""

from __future__ import annotations

import pytest

from pipeline.audit import ActionRecord, AuditStore


@pytest.fixture
def audit(tmp_path):
    store = AuditStore(tmp_path / "audit.sqlite3")
    yield store
    store.close()


def test_run_context_manager_records_success(audit):
    with audit.run("mdm_to_cmms") as run_id:
        pass
    row = audit.conn.execute("SELECT status, finished_at FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert row["status"] == "SUCCESS"
    assert row["finished_at"] is not None


def test_run_context_manager_records_failure_and_reraises(audit):
    with pytest.raises(ValueError):
        with audit.run("mdm_to_cmms") as run_id:
            raise ValueError("boom")
    row = audit.conn.execute("SELECT status, notes FROM runs WHERE run_id=?", (run_id,)).fetchone()
    assert row["status"] == "FAILED"
    assert "boom" in row["notes"]


def test_record_action_and_action_counts(audit):
    with audit.run("mdm_to_cmms") as run_id:
        audit.record_action(run_id, ActionRecord("MDM", "CMMS", "PLATFORM", "JNR", "CREATE", "SUCCESS"))
        audit.record_action(run_id, ActionRecord("MDM", "CMMS", "SECTION", "JNR_PG", "CREATE", "SUCCESS"))
        audit.record_action(run_id, ActionRecord("MDM", "CMMS", "SECTION", "JNR_UT", "ARCHIVE", "BLOCKED", "has active descendant"))

    counts = audit.action_counts(run_id)
    assert counts == {"CREATE:SUCCESS": 2, "ARCHIVE:BLOCKED": 1}


def test_record_dq_issue_and_dq_issue_counts(audit):
    with audit.run("cmms_to_mdm") as run_id:
        audit.record_dq_issue(run_id, "cmms_to_mdm", "EQUIPMENT", "OLW-P-777", "orphan_equipment", "no parent asset")
        audit.record_dq_issue(run_id, "cmms_to_mdm", "EQUIPMENT", "OLW-P-999", "orphan_equipment", "no parent asset")
        audit.record_dq_issue(run_id, "cmms_to_mdm", "SYSTEM", "SYS_X", "unknown_system_class", "bad class")

    counts = audit.dq_issue_counts(run_id)
    assert counts == {"orphan_equipment": 2, "unknown_system_class": 1}


def test_record_metric_is_an_upsert(audit):
    with audit.run("mdm_to_cmms") as run_id:
        audit.record_metric(run_id, "mdm_desired_platforms_active", 7.0)
        audit.record_metric(run_id, "mdm_desired_platforms_active", 8.0)  # overwrite, not accumulate

    row = audit.conn.execute(
        "SELECT value FROM run_metrics WHERE run_id=? AND metric=?", (run_id, "mdm_desired_platforms_active")
    ).fetchone()
    assert row["value"] == 8.0


def test_record_alert(audit):
    with audit.run("mdm_to_cmms") as run_id:
        audit.record_alert(run_id, "mdm_to_cmms", "WARNING", "archive ratio exceeded")

    row = audit.conn.execute("SELECT severity, message FROM alerts WHERE run_id=?", (run_id,)).fetchone()
    assert row["severity"] == "WARNING"
    assert row["message"] == "archive ratio exceeded"


def test_iot_meter_idempotency_helpers(audit):
    with audit.run("iot_to_cmms") as run_id:
        assert audit.sent_for_day("OLW-P-301A", "2026-01-01") is None
        assert audit.last_sent_meter("OLW-P-301A") is None

        audit.record_meter_sent(run_id, "OLW-P-301A", "2026-01-01", "GA-OLW.P-301A.RUN_HRS", "2026-01-01T00:00:00Z", 100)
        audit.record_meter_sent(run_id, "OLW-P-301A", "2026-01-02", "GA-OLW.P-301A.RUN_HRS", "2026-01-02T00:00:00Z", 124)

        already = audit.sent_for_day("OLW-P-301A", "2026-01-01")
        assert already["value"] == 100

        latest = audit.last_sent_meter("OLW-P-301A")
        assert latest["reading_day"] == "2026-01-02"
        assert latest["value"] == 124


def test_record_meter_sent_upserts_on_same_day(audit):
    with audit.run("iot_to_cmms") as run_id:
        audit.record_meter_sent(run_id, "OLW-P-301A", "2026-01-01", "TAG", "2026-01-01T00:00:00Z", 100)
        audit.record_meter_sent(run_id, "OLW-P-301A", "2026-01-01", "TAG", "2026-01-01T06:00:00Z", 105)

    row = audit.sent_for_day("OLW-P-301A", "2026-01-01")
    assert row["value"] == 105
    count = audit.conn.execute(
        "SELECT COUNT(*) AS n FROM iot_meter_sent WHERE asset_code=? AND reading_day=?", ("OLW-P-301A", "2026-01-01")
    ).fetchone()["n"]
    assert count == 1
