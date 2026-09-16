"""Read-only access to the MDM (systemref-lite) SQLite database.

Builds the MDM-owned desired state (platforms, sections) used by the
MDM -> CMMS integration, and the governed-reference lookups /
current-state reads the CMMS -> MDM integration needs to compute its delta.
Plain sqlite3, not the Django ORM: reading directly against the file is "any
SQLite-capable tool" per docs/04_mdm_and_iot.md, and this client never needs
Django's app registry set up just to read.

Writes are a different story: they go through `clients/mdadmin.py`, which
triggers a Django management command inside MDAdmin's own process (ORM,
transaction, any future model-level validation) rather than writing into
these tables directly from here -- see ARCHITECTURE_.md section 2/11.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..timeutil import is_active


@dataclass(frozen=True)
class DesiredPlatform:
    code: str
    name: str
    body_name: str
    active: bool


@dataclass(frozen=True)
class DesiredSection:
    code: str  # "<unit_code>_<section_code>"
    name: str
    parent_code: str  # platform code
    body_name: str
    active: bool


class MdmClient:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> "MdmClient":
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "MdmClient":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        assert self._conn is not None, "MdmClient used outside a connection"
        return self._conn

    # -- MDM -> CMMS: desired state -----------------------------------------
    def desired_hierarchy(self, as_of: date) -> tuple[list[DesiredPlatform], list[DesiredSection], list[dict]]:
        """Returns (platforms, sections, data_quality_issues)."""
        issues: list[dict] = []
        rows = self.conn.execute(
            """
            SELECT su.id, su.code, su.name, su.date_start, su.date_end, ou.name AS body_name,
                   ou.is_field, ou.is_active AS org_active
            FROM systemref_systemunit su
            JOIN orgref_orgunit ou ON ou.id = su.org_unit_id
            """
        ).fetchall()

        platforms: list[DesiredPlatform] = []
        unit_active: dict[int, bool] = {}
        unit_code: dict[int, str] = {}
        for r in rows:
            active = is_active(_to_date(r["date_start"]), _to_date(r["date_end"]), as_of)
            if active and not (r["is_field"] and r["org_active"]):
                issues.append(
                    {
                        "entity_type": "PLATFORM",
                        "entity_code": r["code"],
                        "reason": "org_unit_not_a_field_or_active_body",
                        "details": f"org_unit '{r['body_name']}' is_field={bool(r['is_field'])} is_active={bool(r['is_active'] if 'is_active' in r.keys() else r['org_active'])}",
                    }
                )
                active = False
            platforms.append(DesiredPlatform(code=r["code"], name=r["name"], body_name=r["body_name"], active=active))
            unit_active[r["id"]] = active
            unit_code[r["id"]] = r["code"]

        srows = self.conn.execute(
            """
            SELECT a.id, a.date_start, a.date_end, sc.code AS section_code, sc.name AS section_name,
                   a.system_unit_id
            FROM systemref_systemunittosectionassignment a
            JOIN systemref_sectioncategory sc ON sc.id = a.section_category_id
            """
        ).fetchall()

        body_by_unit = {r["id"]: r["body_name"] for r in rows}
        sections: list[DesiredSection] = []
        for r in srows:
            unit_id = r["system_unit_id"]
            platform_active = unit_active.get(unit_id, False)
            section_active = platform_active and is_active(
                _to_date(r["date_start"]), _to_date(r["date_end"]), as_of
            )
            sections.append(
                DesiredSection(
                    code=f"{unit_code[unit_id]}_{r['section_code']}",
                    name=r["section_name"],
                    parent_code=unit_code[unit_id],
                    body_name=body_by_unit[unit_id],
                    active=section_active,
                )
            )
        return platforms, sections, issues

    # -- CMMS -> MDM: governed reference lookups -----------------------------
    def system_unit_by_code(self, code: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM systemref_systemunit WHERE code = ?", (code,)).fetchone()

    def section_category_by_code(self, code: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM systemref_sectioncategory WHERE code = ?", (code,)).fetchone()

    def system_class_by_code(self, code: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM systemref_systemclass WHERE code = ?", (code,)).fetchone()

    def equipment_type_by_code(self, code: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM systemref_equipmenttype WHERE code = ?", (code,)).fetchone()

    def system_attribute_by_name(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM systemref_systemattribute WHERE lower(name) = lower(?)", (name,)
        ).fetchone()

    def existing_system_codes(self) -> set[str]:
        return {r["code"] for r in self.conn.execute("SELECT code FROM systemref_system").fetchall()}

    def existing_equipment_codes(self) -> set[str]:
        return {r["code"] for r in self.conn.execute("SELECT code FROM systemref_equipment").fetchall()}

    def system_by_code(self, code: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM systemref_system WHERE code = ?", (code,)).fetchone()

    def equipment_by_code(self, code: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM systemref_equipment WHERE code = ?", (code,)).fetchone()

    def system_attribute_names(self, system_id: int) -> set[str]:
        return {
            r["name"]
            for r in self.conn.execute(
                """SELECT sa.name FROM systemref_systemattributeassignment saa
                   JOIN systemref_systemattribute sa ON sa.id = saa.system_attribute_id
                   WHERE saa.system_id = ?""",
                (system_id,),
            ).fetchall()
        }

    def system_equipment_assignment_exists(self, system_code: str, equipment_code: str) -> bool:
        row = self.conn.execute(
            """SELECT a.id FROM systemref_systemequipmentassignment a
               JOIN systemref_system s ON s.id = a.system_id
               JOIN systemref_equipment e ON e.id = a.equipment_id
               WHERE s.code = ? AND e.code = ?""",
            (system_code, equipment_code),
        ).fetchone()
        return row is not None


def _to_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)
