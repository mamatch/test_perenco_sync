"""Date/time helpers shared by every integration.

Centralised here because the active-scope rule (which entities are "in
scope") depends on a single, consistently-applied notion of "now" and of
comparison semantics -- getting this wrong inverts active/inactive
everywhere downstream (see ARCHITECTURE_.md part A section 3 and DECISIONS.md).
"""

from __future__ import annotations

from datetime import date, datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str) -> datetime:
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_active(date_start: date | None, date_end: date | None, as_of: date) -> bool:
    """MDM active-scope predicate (ARCHITECTURE_.md part A section 3, confirmed):

        date_start <= as_of and (date_end is None or date_end > as_of)
    """
    if date_start is not None and date_start > as_of:
        return False
    return date_end is None or date_end > as_of
