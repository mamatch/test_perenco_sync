"""Tests for the alert rules and health summary (pipeline/pipeline/observability.py),
previously only ever seen printed at the end of a live run, never asserted on
directly.
"""

from __future__ import annotations

import pytest

from pipeline.audit import ActionRecord, AuditStore
from pipeline.observability import evaluate_alerts, health_summary


@pytest.fixture
def audit(tmp_path):
    store = AuditStore(tmp_path / "audit.sqlite3")
    yield store
    store.close()


def test_evaluate_alerts_surfaces_alerts_recorded_during_the_run(audit):
    with audit.run("mdm_to_cmms") as run_id:
        audit.record_alert(run_id, "mdm_to_cmms", "WARNING", "archive ratio 21.7% exceeds 10% threshold")

    alerts = evaluate_alerts(audit, run_id, settings=None)
    assert any(a.severity == "WARNING" and "archive ratio" in a.message for a in alerts)


def test_evaluate_alerts_flags_high_rejection_rate(audit):
    with audit.run("cmms_to_mdm") as run_id:
        for i in range(6):
            audit.record_action(run_id, ActionRecord("CMMS", "MDM", "SYSTEM", f"S{i}", "UPDATE", "SUCCESS"))
        for i in range(4):
            audit.record_action(run_id, ActionRecord("CMMS", "MDM", "SYSTEM", f"R{i}", "UPDATE", "REJECTED"))
        # 4/10 = 40% > the 20% threshold

    alerts = evaluate_alerts(audit, run_id, settings=None)
    assert any("40%" in a.message or "4/10" in a.message for a in alerts)


def test_evaluate_alerts_silent_when_rejection_rate_is_low(audit):
    with audit.run("cmms_to_mdm") as run_id:
        for i in range(9):
            audit.record_action(run_id, ActionRecord("CMMS", "MDM", "SYSTEM", f"S{i}", "UPDATE", "SUCCESS"))
        audit.record_action(run_id, ActionRecord("CMMS", "MDM", "SYSTEM", "R0", "UPDATE", "REJECTED"))
        # 1/10 = 10%, under the 20% threshold

    alerts = evaluate_alerts(audit, run_id, settings=None)
    assert not any("REJECTED" in a.message or "rejected or failed" in a.message for a in alerts)


def test_evaluate_alerts_iot_info_alerts(audit):
    with audit.run("iot_to_cmms") as run_id:
        audit.record_dq_issue(run_id, "iot_to_cmms", "METER", "OLW-P-301A", "counter_regression", {"day": "2026-01-05"})
        audit.record_dq_issue(run_id, "iot_to_cmms", "IOT_TAG", "GA-OLW.P-777.RUN_HRS", "unresolved_tag", {})

    alerts = evaluate_alerts(audit, run_id, settings=None)
    messages = " ".join(a.message for a in alerts)
    assert "counter-regression" in messages
    assert "could not be mapped" in messages


def test_health_summary_includes_actions_metrics_and_dq_issues(audit):
    with audit.run("mdm_to_cmms") as run_id:
        audit.record_action(run_id, ActionRecord("MDM", "CMMS", "PLATFORM", "JNR", "CREATE", "SUCCESS"))
        audit.record_metric(run_id, "mdm_desired_platforms_active", 7.0)
        audit.record_dq_issue(run_id, "mdm_to_cmms", "PLATFORM", "XYZ", "archive_blocked", "has active descendant")

    summary = health_summary(audit, run_id)
    assert "mdm_to_cmms" in summary
    assert run_id in summary
    assert "CREATE:SUCCESS" in summary
    assert "mdm_desired_platforms_active" in summary
    assert "archive_blocked" in summary
