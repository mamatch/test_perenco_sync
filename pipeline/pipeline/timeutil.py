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


def is_active(date_start: date | None, date_end: date | None, as_of: date, rule: str = "conventional") -> bool:
    """MDM active-scope predicate (ARCHITECTURE_.md part A section 3).

    conventional: date_start <= as_of and (date_end is None or date_end > as_of)
    literal:      date_start <  as_of and (date_end is None or date_end < as_of)
                  (the wording confirmed on the clarification call; the end-date
                  condition is counter-intuitive for a normal validity interval,
                  so it is only used when explicitly selected -- see DECISIONS.md)
    """
    if date_start is not None and rule == "conventional" and date_start > as_of:
        return False
    if date_start is not None and rule == "literal" and date_start >= as_of:
        return False
    if date_end is None:
        return True
    if rule == "conventional":
        return date_end > as_of
    return date_end < as_of
