"""Access to the MDM (systemref-lite) SQLite database.

Read side: builds the MDM-owned desired state (platforms, sections) used by
the MDM -> CMMS integration.

Write side: upserts CMMS-owned systems/equipments back into the MDM for the
CMMS -> MDM integration, and reconciles disappeared/archived ones. Uses plain
sqlite3 rather than the Django ORM so the pipeline has no dependency on the
Django app being importable/configured -- the file is "any SQLite-capable
tool" per docs/04_mdm_and_iot.md. Foreign keys are enforced (PRAGMA
foreign_keys=ON) and every write happens inside a single transaction per run
so a mid-run failure cannot leave the MDM half-migrated.
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

    # -- CMMS -> MDM: writes --------------------------------------------------
    def upsert_system(
        self,
        *,
        code: str,
        tag: str,
        system_class_id: int,
        system_unit_id: int,
        section_id: int,
        source: str,
        date_start: str | None,
        date_end: str | None,
    ) -> tuple[int, bool]:
        row = self.system_by_code(code)
        if row is None:
            cur = self.conn.execute(
                """INSERT INTO systemref_system (code, tag, source, date_start, date_end, section_id, system_class_id, system_unit_id)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (code, tag, source, date_start, date_end, section_id, system_class_id, system_unit_id),
            )
            return cur.lastrowid, True
        changed = (
            row["tag"] != tag
            or row["section_id"] != section_id
            or row["system_class_id"] != system_class_id
            or row["system_unit_id"] != system_unit_id
            or row["date_start"] != date_start
            or row["date_end"] != date_end
        )
        if changed:
            self.conn.execute(
                """UPDATE systemref_system SET tag=?, section_id=?, system_class_id=?, system_unit_id=?,
                   date_start=?, date_end=? WHERE id=?""",
                (tag, section_id, system_class_id, system_unit_id, date_start, date_end, row["id"]),
            )
        return row["id"], changed

    def set_system_attributes(self, system_id: int, attribute_ids: set[int]) -> bool:
        current = {
            r["system_attribute_id"]
            for r in self.conn.execute(
                "SELECT system_attribute_id FROM systemref_systemattributeassignment WHERE system_id = ?", (system_id,)
            ).fetchall()
        }
        if current == attribute_ids:
            return False
        to_remove = current - attribute_ids
        to_add = attribute_ids - current
        if to_remove:
            self.conn.executemany(
                "DELETE FROM systemref_systemattributeassignment WHERE system_id=? AND system_attribute_id=?",
                [(system_id, a) for a in to_remove],
            )
        if to_add:
            self.conn.executemany(
                "INSERT INTO systemref_systemattributeassignment (system_id, system_attribute_id) VALUES (?,?)",
                [(system_id, a) for a in to_add],
            )
        return True

    def upsert_equipment(
        self,
        *,
        code: str,
        name: str | None,
        equipment_type_id: int,
        date_start: str | None,
        date_end: str | None,
    ) -> tuple[int, bool]:
        row = self.equipment_by_code(code)
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO systemref_equipment (code, name, date_start, date_end, equipment_type_id) VALUES (?,?,?,?,?)",
                (code, name, date_start, date_end, equipment_type_id),
            )
            return cur.lastrowid, True
        changed = (
            row["name"] != name
            or row["equipment_type_id"] != equipment_type_id
            or row["date_start"] != date_start
            or row["date_end"] != date_end
        )
        if changed:
            self.conn.execute(
                "UPDATE systemref_equipment SET name=?, equipment_type_id=?, date_start=?, date_end=? WHERE id=?",
                (name, equipment_type_id, date_start, date_end, row["id"]),
            )
        return row["id"], changed

    def ensure_system_equipment_assignment(self, system_id: int, equipment_id: int, assignment_date: str) -> bool:
        row = self.conn.execute(
            "SELECT id FROM systemref_systemequipmentassignment WHERE system_id=? AND equipment_id=?",
            (system_id, equipment_id),
        ).fetchone()
        if row:
            return False
        self.conn.execute(
            "INSERT INTO systemref_systemequipmentassignment (system_id, equipment_id, assignment_date) VALUES (?,?,?)",
            (system_id, equipment_id, assignment_date),
        )
        return True

    def set_system_date_end(self, system_id: int, date_end: str | None) -> bool:
        row = self.conn.execute("SELECT date_end FROM systemref_system WHERE id=?", (system_id,)).fetchone()
        if row["date_end"] == date_end:
            return False
        self.conn.execute("UPDATE systemref_system SET date_end=? WHERE id=?", (date_end, system_id))
        return True

    def set_equipment_date_end(self, equipment_id: int, date_end: str | None) -> bool:
        row = self.conn.execute("SELECT date_end FROM systemref_equipment WHERE id=?", (equipment_id,)).fetchone()
        if row["date_end"] == date_end:
            return False
        self.conn.execute("UPDATE systemref_equipment SET date_end=? WHERE id=?", (date_end, equipment_id))
        return True

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()


def _to_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)
