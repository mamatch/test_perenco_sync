"""Pure logic for the IoT historian -> CMMS meters flow (ARCHITECTURE_.md part A
section 5). No I/O here (no file reads, no HTTP): parsing, tag resolution,
deduplication, unit conversion and daily selection are all plain functions
over plain data, so they can be unit-tested directly. iot_to_cmms.py wires
this to the filesystem, the CMMS client and the audit store.
"""

from __future__ import annotations

import csv as _csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from .timeutil import parse_iso

STRUCTURAL_FAMILIES = {"PLATFORM", "SECTION", "LOC_TAG"}

# Historian units seen (or documented as possible) -> hours multiplier.
# An unrecognised unit is a data-quality rejection, never a silent guess.
UNIT_TO_HOURS = {"h": 1.0, "hr": 1.0, "hrs": 1.0, "hour": 1.0, "hours": 1.0, "min": 1.0 / 60, "minute": 1.0 / 60, "minutes": 1.0 / 60}


@dataclass(frozen=True)
class IotRow:
    tag_id: str
    timestamp_utc: datetime
    value: float
    unit: str
    quality: str
    exported_at_utc: datetime
    source_file: str


@dataclass(frozen=True)
class AssetRef:
    code: str
    family: str | None
    parent_code: str | None


@dataclass
class ResolvedReading:
    asset_code: str
    tag_id: str
    day: date
    timestamp_utc: datetime
    value_hours: float  # after unit conversion, before rounding


def parse_csv_files(paths: Iterable[Path]) -> tuple[list[IotRow], list[dict]]:
    rows: list[IotRow] = []
    errors: list[dict] = []
    for path in sorted(paths):
        with open(path, newline="", encoding="utf-8") as fh:
            for lineno, raw in enumerate(_csv.DictReader(fh), start=2):
                try:
                    rows.append(
                        IotRow(
                            tag_id=raw["tag_id"].strip(),
                            timestamp_utc=parse_iso(raw["timestamp_utc"]),
                            value=float(raw["value"]),
                            unit=raw["unit"].strip().lower(),
                            quality=raw["quality"].strip().upper(),
                            exported_at_utc=parse_iso(raw["exported_at_utc"]),
                            source_file=path.name,
                        )
                    )
                except (KeyError, ValueError) as exc:
                    errors.append({"file": path.name, "line": lineno, "row": raw, "error": str(exc)})
    return rows, errors


def dedupe(rows: list[IotRow]) -> tuple[list[IotRow], list[dict]]:
    """Row identity is (tag_id, timestamp_utc) -- docs/04_mdm_and_iot.md: exports
    overlap by 12h, so the same reading legitimately appears in two files.
    Keeps the copy from the most recently exported file; flags a conflict
    (does not silently pick a winner on value) when duplicates disagree."""
    best: dict[tuple[str, datetime], IotRow] = {}
    conflicts: list[dict] = []
    for row in rows:
        key = (row.tag_id, row.timestamp_utc)
        prev = best.get(key)
        if prev is None:
            best[key] = row
            continue
        if prev.value != row.value or prev.quality != row.quality:
            conflicts.append({"tag_id": row.tag_id, "timestamp_utc": row.timestamp_utc.isoformat(), "values": sorted({prev.value, row.value})})
        if row.exported_at_utc > prev.exported_at_utc:
            best[key] = row
    return list(best.values()), conflicts


def resolve_tag(tag_id: str, assets_by_code: dict[str, AssetRef]) -> tuple[str | None, str]:
    """Deterministic tag -> CMMS asset resolution (docs/04_mdm_and_iot.md +
    ARCHITECTURE_.md part A section 5): tag_id = "<country>-<platform>.<suffix>.RUN_HRS".

    Resolution order:
    1. "<platform>-<suffix>" is a known, non-structural asset code -> that equipment.
    2. exactly one asset under that platform has family "SYS_<suffix>" (the
       historian addresses some machines by system class rather than by an
       individual equipment tag, e.g. a platform's single power-generation
       system) -> that system.
    3. otherwise unresolved -> quarantined, never guessed.

    Returns (asset_code_or_None, reason). reason is "" on success.
    """
    if not tag_id.upper().endswith(".RUN_HRS"):
        return None, "not_a_run_hrs_tag"
    body = tag_id[: -len(".RUN_HRS")]
    parts = body.split(".")
    if len(parts) != 2:
        return None, "malformed_tag_id"
    country_platform, suffix = parts
    if len(country_platform) < 4 or country_platform[2] != "-":
        return None, "malformed_tag_id"
    platform_code = country_platform[3:]

    candidate = f"{platform_code}-{suffix}"
    asset = assets_by_code.get(candidate)
    if asset is not None and asset.family not in STRUCTURAL_FAMILIES:
        return asset.code, ""

    system_family = f"SYS_{suffix}"
    matches = [
        a
        for a in assets_by_code.values()
        if a.family == system_family and _is_under_platform(a, platform_code, assets_by_code)
    ]
    if len(matches) == 1:
        return matches[0].code, ""
    if len(matches) > 1:
        return None, "ambiguous_system_class_tag"
    return None, "unresolved_tag"


def _is_under_platform(asset: AssetRef, platform_code: str, assets_by_code: dict[str, AssetRef]) -> bool:
    code = asset.parent_code
    seen = set()
    while code and code not in seen:
        if code == platform_code:
            return True
        seen.add(code)
        parent = assets_by_code.get(code)
        code = parent.parent_code if parent else None
    return False


def convert_to_hours(row: IotRow) -> tuple[float | None, str]:
    factor = UNIT_TO_HOURS.get(row.unit)
    if factor is None:
        return None, f"unknown_unit:{row.unit}"
    return row.value * factor, ""


def daily_max_timestamp_readings(rows: list[IotRow], asset_code: str) -> dict[date, IotRow]:
    """Business rule: the value for the day is the one at the *maximum
    timestamp*, not the maximum value. GOOD quality only; callers are
    expected to have already filtered to a single tag/asset."""
    by_day: dict[date, IotRow] = {}
    for row in rows:
        if row.quality != "GOOD":
            continue
        day = row.timestamp_utc.date()
        current = by_day.get(day)
        if current is None or row.timestamp_utc > current.timestamp_utc:
            by_day[day] = row
    return by_day
