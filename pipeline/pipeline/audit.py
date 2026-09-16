"""Durable run/audit/command store (ARCHITECTURE_.md part A sections 6-8).

Every run gets a run_id. Every planned action is written before execution
(so a crash mid-run still leaves a record of what was intended) and updated
with its outcome. This is what makes replay possible without recomputing the
whole world, and what "every write is traceable to a run" means in practice.

SQLite rather than DuckDB for this store on purpose: it is the transactional,
single-writer, many-small-row workload (one row per action), which is exactly
what SQLite is for, and it needs zero extra infrastructure in the sandbox.
DuckDB remains the right choice for the analytical/reconciliation layer in
production (see ARCHITECTURE_.md section 11); this file is the operational
audit trail, not the analytical warehouse.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    integration TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    source_system TEXT NOT NULL,
    target_system TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_code TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    request_json TEXT,
    result_json TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_run ON actions(run_id);
CREATE INDEX IF NOT EXISTS idx_actions_code ON actions(entity_type, entity_code);

CREATE TABLE IF NOT EXISTS dq_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    integration TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_code TEXT NOT NULL,
    reason TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dq_run ON dq_issues(run_id);

CREATE TABLE IF NOT EXISTS run_metrics (
    run_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    PRIMARY KEY (run_id, metric)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    integration TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- one row per (asset_code, day) actually sent to the CMMS meter: the
-- idempotency/replay boundary for the IoT flow so a re-run does not resend a
-- value already accepted, and so counter-regression can be judged against
-- what we ourselves last sent, not only against the CMMS's own state.
CREATE TABLE IF NOT EXISTS iot_meter_sent (
    asset_code TEXT NOT NULL,
    reading_day TEXT NOT NULL,
    tag_id TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL,
    value INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (asset_code, reading_day)
);
"""


def new_run_id() -> str:
    return f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"


@dataclass
class ActionRecord:
    source_system: str
    target_system: str
    entity_type: str
    entity_code: str
    action: str
    status: str
    reason: str | None = None
    request: dict | None = None
    result: dict | None = None
    retry_count: int = 0


class AuditStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def run(self, integration: str, run_id: str | None = None) -> Iterator[str]:
        """Used as `with audit.run("mdm_to_cmms") as run_id: ...`. Inserts the
        `runs` row up front (status RUNNING) so a crash mid-integration still
        leaves a trace, then always closes it out as either SUCCESS or FAILED
        (with the exception message) when the `with` block exits, and
        re-raises so the caller still sees the failure."""
        run_id = run_id or new_run_id()
        self.conn.execute(
            "INSERT INTO runs (run_id, integration, started_at, status) VALUES (?,?,?,?)",
            (run_id, integration, _now(), "RUNNING"),
        )
        self.conn.commit()
        try:
            yield run_id
        except Exception as exc:  # noqa: BLE001 - we want to record and re-raise
            self.conn.execute(
                "UPDATE runs SET finished_at=?, status=?, notes=? WHERE run_id=?",
                (_now(), "FAILED", str(exc)[:2000], run_id),
            )
            self.conn.commit()
            raise
        else:
            self.conn.execute(
                "UPDATE runs SET finished_at=?, status=? WHERE run_id=?", (_now(), "SUCCESS", run_id)
            )
            self.conn.commit()

    def record_action(self, run_id: str, rec: ActionRecord) -> None:
        self.conn.execute(
            """INSERT INTO actions (run_id, source_system, target_system, entity_type, entity_code, action,
               status, reason, request_json, result_json, retry_count, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                rec.source_system,
                rec.target_system,
                rec.entity_type,
                rec.entity_code,
                rec.action,
                rec.status,
                rec.reason,
                json.dumps(rec.request, ensure_ascii=False) if rec.request is not None else None,
                json.dumps(rec.result, ensure_ascii=False) if rec.result is not None else None,
                rec.retry_count,
                _now(),
            ),
        )
        self.conn.commit()

    def record_dq_issue(self, run_id: str, integration: str, entity_type: str, entity_code: str, reason: str, details: Any = None) -> None:
        self.conn.execute(
            "INSERT INTO dq_issues (run_id, integration, entity_type, entity_code, reason, details, created_at) VALUES (?,?,?,?,?,?,?)",
            (run_id, integration, entity_type, entity_code, reason, json.dumps(details, ensure_ascii=False, default=str) if details is not None else None, _now()),
        )
        self.conn.commit()

    def record_metric(self, run_id: str, metric: str, value: float) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO run_metrics (run_id, metric, value) VALUES (?,?,?)", (run_id, metric, value)
        )
        self.conn.commit()

    def record_alert(self, run_id: str, integration: str, severity: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO alerts (run_id, integration, severity, message, created_at) VALUES (?,?,?,?,?)",
            (run_id, integration, severity, message, _now()),
        )
        self.conn.commit()

    # -- IoT idempotency helpers ---------------------------------------------
    def last_sent_meter(self, asset_code: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM iot_meter_sent WHERE asset_code=? ORDER BY reading_day DESC LIMIT 1", (asset_code,)
        ).fetchone()

    def sent_for_day(self, asset_code: str, reading_day: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM iot_meter_sent WHERE asset_code=? AND reading_day=?", (asset_code, reading_day)
        ).fetchone()

    def record_meter_sent(self, run_id: str, asset_code: str, reading_day: str, tag_id: str, timestamp_utc: str, value: int) -> None:
        self.conn.execute(
            """INSERT INTO iot_meter_sent (asset_code, reading_day, tag_id, timestamp_utc, value, run_id, created_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(asset_code, reading_day) DO UPDATE SET
                 tag_id=excluded.tag_id, timestamp_utc=excluded.timestamp_utc, value=excluded.value,
                 run_id=excluded.run_id, created_at=excluded.created_at""",
            (asset_code, reading_day, tag_id, timestamp_utc, value, run_id, _now()),
        )
        self.conn.commit()

    # -- reporting -------------------------------------------------------------
    def action_counts(self, run_id: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT action || ':' || status AS k, COUNT(*) AS n FROM actions WHERE run_id=? GROUP BY k", (run_id,)
        ).fetchall()
        return {r["k"]: r["n"] for r in rows}

    def dq_issue_counts(self, run_id: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT reason, COUNT(*) AS n FROM dq_issues WHERE run_id=? GROUP BY reason", (run_id,)
        ).fetchall()
        return {r["reason"]: r["n"] for r in rows}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
