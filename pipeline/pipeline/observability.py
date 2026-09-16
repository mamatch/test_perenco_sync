"""Run health summary, metrics and alert rules (ARCHITECTURE_.md part A section 8,
README Part C). Kept deliberately simple -- a text table printed at the end of
every run plus a couple of rows in the audit store -- rather than a
standalone dashboarding stack, per the exercise's own guidance to keep Part C
thin but honest.
"""

from __future__ import annotations

from dataclasses import dataclass

from .audit import AuditStore


@dataclass
class Alert:
    severity: str
    message: str


def evaluate_alerts(audit: AuditStore, run_id: str, settings) -> list[Alert]:
    """Two kinds of alerts, deliberately combined here: alerts already written
    to the DB *during* the run (e.g. the archive-ratio breach, raised right
    where it's detected in canonical.py/mdm_to_cmms.py), plus a few more
    computed after the fact just by reading back what the run recorded."""
    alerts: list[Alert] = []
    rows = audit.conn.execute("SELECT severity, message FROM alerts WHERE run_id=?", (run_id,)).fetchall()
    alerts.extend(Alert(r["severity"], r["message"]) for r in rows)

    counts = audit.action_counts(run_id)
    total_actions = sum(counts.values())
    rejected = sum(v for k, v in counts.items() if k.endswith(":REJECTED"))
    failed = sum(v for k, v in counts.items() if k.endswith(":FAILED_RETRYABLE"))
    if total_actions and (rejected + failed) / total_actions > 0.20:  # 20%: arbitrary but documented threshold
        alerts.append(Alert("WARNING", f"{rejected + failed}/{total_actions} actions ({(rejected + failed) / total_actions:.0%}) were rejected or failed this run."))

    dq = audit.dq_issue_counts(run_id)
    if dq.get("counter_regression", 0) > 0:
        alerts.append(Alert("INFO", f"{dq['counter_regression']} IoT counter-regression(s) quarantined -- a meter may need manual correction in the CMMS."))
    if dq.get("unresolved_tag", 0) > 0:
        alerts.append(Alert("INFO", f"{dq['unresolved_tag']} historian reading(s) could not be mapped to a CMMS asset."))

    return alerts


def health_summary(audit: AuditStore, run_id: str) -> str:
    row = audit.conn.execute("SELECT integration, status, started_at, finished_at FROM runs WHERE run_id=?", (run_id,)).fetchone()
    counts = audit.action_counts(run_id)
    dq = audit.dq_issue_counts(run_id)
    metrics = {r["metric"]: r["value"] for r in audit.conn.execute("SELECT metric, value FROM run_metrics WHERE run_id=?", (run_id,))}

    lines = [
        f"=== {row['integration']} :: {run_id} ===",
        f"status={row['status']}  started={row['started_at']}  finished={row['finished_at']}",
        "-- actions --",
    ]
    for k in sorted(counts):
        lines.append(f"  {k:30s} {counts[k]}")
    if metrics:
        lines.append("-- metrics --")
        for k in sorted(metrics):
            lines.append(f"  {k:30s} {metrics[k]}")
    if dq:
        lines.append("-- data quality issues --")
        for k in sorted(dq):
            lines.append(f"  {k:30s} {dq[k]}")
    return "\n".join(lines)
