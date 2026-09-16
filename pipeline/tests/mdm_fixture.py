"""A minimal, hand-built stand-in for the MDM's SQLite schema (systemref_lite),
covering exactly the tables and columns pipeline/pipeline/clients/mdm.py reads
and pipeline/pipeline/cmms_to_mdm.py queries directly. Not a full replica of
systemref_lite/systemref/models.py -- just enough surface for
mdm_to_cmms.py::run() and cmms_to_mdm.py::run() to be exercised without a
Django process, matching the project's "plain sqlite3, no Django ORM needed
to read" design (pipeline/pipeline/clients/mdm.py's own docstring).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE orgref_orgunit (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    is_field INTEGER NOT NULL DEFAULT 1,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE systemref_systemunit (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    org_unit_id INTEGER NOT NULL REFERENCES orgref_orgunit(id),
    date_start TEXT,
    date_end TEXT,
    source TEXT
);

CREATE TABLE systemref_sectioncategory (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL
);

CREATE TABLE systemref_systemunittosectionassignment (
    id INTEGER PRIMARY KEY,
    system_unit_id INTEGER NOT NULL REFERENCES systemref_systemunit(id),
    section_category_id INTEGER NOT NULL REFERENCES systemref_sectioncategory(id),
    date_start TEXT,
    date_end TEXT
);

CREATE TABLE systemref_systemclass (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL
);

CREATE TABLE systemref_equipmenttype (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL
);

CREATE TABLE systemref_system (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    tag TEXT,
    system_class_id INTEGER REFERENCES systemref_systemclass(id),
    system_unit_id INTEGER REFERENCES systemref_systemunit(id),
    section_id INTEGER REFERENCES systemref_sectioncategory(id),
    source TEXT,
    date_start TEXT,
    date_end TEXT
);

CREATE TABLE systemref_equipment (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT,
    equipment_type_id INTEGER REFERENCES systemref_equipmenttype(id),
    date_start TEXT,
    date_end TEXT
);

CREATE TABLE systemref_systemattribute (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE systemref_systemattributeassignment (
    id INTEGER PRIMARY KEY,
    system_id INTEGER NOT NULL REFERENCES systemref_system(id),
    system_attribute_id INTEGER NOT NULL REFERENCES systemref_systemattribute(id)
);

CREATE TABLE systemref_systemequipmentassignment (
    id INTEGER PRIMARY KEY,
    system_id INTEGER NOT NULL REFERENCES systemref_system(id),
    equipment_id INTEGER NOT NULL REFERENCES systemref_equipment(id),
    assignment_date TEXT
);
"""


def create_mdm_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def insert_org_unit(conn, *, name: str, is_field: bool = True, is_active: bool = True) -> int:
    cur = conn.execute("INSERT INTO orgref_orgunit (name, is_field, is_active) VALUES (?,?,?)", (name, is_field, is_active))
    conn.commit()
    return cur.lastrowid


def insert_system_unit(conn, *, code: str, name: str, org_unit_id: int, date_start: str | None, date_end: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO systemref_systemunit (code, name, org_unit_id, date_start, date_end, source) VALUES (?,?,?,?,?,?)",
        (code, name, org_unit_id, date_start, date_end, "MDM"),
    )
    conn.commit()
    return cur.lastrowid


def insert_section_category(conn, *, code: str, name: str) -> int:
    cur = conn.execute("INSERT INTO systemref_sectioncategory (code, name) VALUES (?,?)", (code, name))
    conn.commit()
    return cur.lastrowid


def insert_section_assignment(conn, *, system_unit_id: int, section_category_id: int, date_start: str | None, date_end: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO systemref_systemunittosectionassignment (system_unit_id, section_category_id, date_start, date_end) VALUES (?,?,?,?)",
        (system_unit_id, section_category_id, date_start, date_end),
    )
    conn.commit()
    return cur.lastrowid


def insert_system_class(conn, *, code: str, name: str) -> int:
    cur = conn.execute("INSERT INTO systemref_systemclass (code, name) VALUES (?,?)", (code, name))
    conn.commit()
    return cur.lastrowid


def insert_equipment_type(conn, *, code: str, name: str) -> int:
    cur = conn.execute("INSERT INTO systemref_equipmenttype (code, name) VALUES (?,?)", (code, name))
    conn.commit()
    return cur.lastrowid
