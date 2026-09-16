"""Applies the CMMS -> MDM write plan by triggering a Django management
command inside MDAdmin's own process (systemref_lite), instead of writing
into MDM's tables directly from this service -- ARCHITECTURE_.md section
2/11. Preserves any model-level validation/signals MDAdmin's ORM would
otherwise bypass. In production this dispatch would be a Celery task handed
to MDAdmin's own workers (DECISIONS.md #12); a subprocess call to `manage.py`
is the sandbox-appropriate stand-in for "triggering execution inside
MDAdmin's process" -- the actual boundary being demonstrated (writes happen
through MDAdmin's ORM, never from outside it) is the same either way.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SyncPlan:
    systems: list[dict] = field(default_factory=list)
    equipments: list[dict] = field(default_factory=list)
    assignments: list[dict] = field(default_factory=list)

    def upsert_system(
        self,
        *,
        code: str,
        tag: str,
        system_class_code: str,
        platform_code: str,
        section_code: str,
        source: str,
        date_start: str | None,
        date_end: str | None,
        attribute_names: set[str],
    ) -> None:
        self.systems.append(
            {
                "op": "upsert",
                "code": code,
                "tag": tag,
                "system_class_code": system_class_code,
                "platform_code": platform_code,
                "section_code": section_code,
                "source": source,
                "date_start": date_start,
                "date_end": date_end,
                "attribute_names": sorted(attribute_names),
            }
        )

    def close_system(self, code: str, date_end: str) -> None:
        self.systems.append({"op": "close", "code": code, "date_end": date_end})

    def upsert_equipment(
        self, *, code: str, name: str | None, equipment_type_code: str, date_start: str | None, date_end: str | None
    ) -> None:
        self.equipments.append(
            {
                "op": "upsert",
                "code": code,
                "name": name,
                "equipment_type_code": equipment_type_code,
                "date_start": date_start,
                "date_end": date_end,
            }
        )

    def close_equipment(self, code: str, date_end: str) -> None:
        self.equipments.append({"op": "close", "code": code, "date_end": date_end})

    def assign(self, *, system_code: str, equipment_code: str, assignment_date: str) -> None:
        self.assignments.append(
            {"system_code": system_code, "equipment_code": equipment_code, "assignment_date": assignment_date}
        )

    def is_empty(self) -> bool:
        return not (self.systems or self.equipments or self.assignments)


class MdAdminCommandError(Exception):
    def __init__(self, returncode: int, stderr: str):
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"apply_sync_plan exited {returncode}: {stderr[:500]}")


def apply_plan(plan: SyncPlan, systemref_lite_dir: Path, mdm_db_path: Path) -> dict:
    """Triggers `manage.py apply_sync_plan` inside systemref_lite. The whole
    plan is one transaction on the Django side, so a non-zero exit means
    none of it was applied -- callers should treat every pending action as
    failed, not partially succeeded."""
    if plan.is_empty():
        return {"systems": 0, "equipments": 0, "assignments": 0, "closures": 0}

    # 1. Serialize the plan to a temp JSON file -- this is the only channel
    #    between this process and the Django one; codes only, never numeric
    #    DB ids, since the two processes never share an id space.
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"systems": plan.systems, "equipments": plan.equipments, "assignments": plan.assignments}, f)
        plan_path = f.name

    try:
        # 2. Run `manage.py apply_sync_plan` as a subprocess, inside
        #    systemref_lite's own uv environment (cwd + SYSTEMREF_DB_PATH make
        #    it point at the same MDM database this pipeline just read from).
        #    The command applies everything in one Django transaction.
        result = subprocess.run(
            ["uv", "run", "manage.py", "apply_sync_plan", "--plan-file", plan_path],
            cwd=systemref_lite_dir,
            env={**os.environ, "SYSTEMREF_DB_PATH": str(mdm_db_path)},
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        Path(plan_path).unlink(missing_ok=True)  # always clean up the temp file, success or failure

    if result.returncode != 0:
        # non-zero exit = the whole transaction was rolled back on the Django
        # side; nothing was partially applied, so the caller can safely mark
        # every pending action as failed rather than re-checking each one.
        raise MdAdminCommandError(result.returncode, result.stderr)

    # 3. The command's last non-empty stdout line is a JSON summary
    #    ({"systems": N, "equipments": N, ...}) -- everything before that may
    #    be Django's own log noise, so we only trust the last line.
    summary_line = next((ln for ln in reversed(result.stdout.splitlines()) if ln.strip()), "{}")
    return json.loads(summary_line)
