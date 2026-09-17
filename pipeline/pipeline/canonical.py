"""Pure delta-computation core for the MDM -> CMMS integration.

Deliberately free of any I/O (no HTTP, no SQLite) so it can be unit-tested
against plain Python objects: give it a desired state and a current CMMS
state, get back a frozen, ordered list of planned actions plus the safety
verdicts. ARCHITECTURE_.md part A sections 3 and 6 describe exactly the
behaviour implemented here: desired-state reconciliation, CREATE/UPDATE/
ARCHIVE/UNARCHIVE/NOOP/BLOCKED, parent-before-child creation, child-before-
parent archiving, recursive active-descendant protection, and the 10%
archive-ratio hard stop that only blocks destructive actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Literal

Action = Literal["CREATE", "UPDATE", "ARCHIVE", "UNARCHIVE", "NOOP", "BLOCKED"]


@dataclass(frozen=True)
class DesiredNode:
    code: str
    name: str
    family: str  # "PLATFORM" | "SECTION"
    parent_code: str | None
    body_name: str
    active: bool
    depth: int  # 0 = platform, 1 = section


@dataclass(frozen=True)
class CurrentAsset:
    """A row of the live CMMS tree, any family. Used both for the PLATFORM/
    SECTION diff and, unfiltered, for recursive active-descendant checks."""

    code: str
    name: str
    family: str | None
    parent_code: str | None
    bodies: tuple[str, ...]
    archived: bool


@dataclass
class PlannedAction:
    entity_type: str  # "PLATFORM" | "SECTION"
    code: str
    action: Action
    depth: int
    reason: str = ""
    payload: dict = field(default_factory=dict)
    parent_code: str | None = None


def has_active_descendant(code: str, children_by_parent: dict[str, list[CurrentAsset]], excluding: frozenset[str] = frozenset()) -> bool:
    """True if `code` has a live (non-archived) descendant that is not itself
    about to be archived in this same run (`excluding`). Only PLATFORM/SECTION
    codes can ever be in `excluding` -- Systems/Equipment are CMMS-owned and
    this integration never archives them, so they always block when active,
    however deep. `children_by_parent` is built from the active-only tree, so
    a single level of "immediate active, not-excluded child" is enough: any
    child that is itself excluded (i.e. resolved as archive-this-run) was
    only excluded after its own descendants were checked, so nothing further
    down needs re-checking through it."""
    for child in children_by_parent.get(code, []):
        if child.code in excluding:
            continue
        if not child.archived:
            return True
    return False


def build_children_index(all_current_active_tree: Iterable[CurrentAsset]) -> dict[str, list[CurrentAsset]]:
    """All families, so an active System/Equipment blocks archiving its
    Section/Platform ancestors."""
    idx: dict[str, list[CurrentAsset]] = {}
    for a in all_current_active_tree:
        if a.parent_code:
            idx.setdefault(a.parent_code, []).append(a)
    return idx


def compute_plan(
    desired: list[DesiredNode],
    current_platforms_sections: dict[str, CurrentAsset],
    children_by_parent: dict[str, list[CurrentAsset]],
    archive_ratio_threshold: float,
) -> tuple[list[PlannedAction], dict]:
    """Returns (plan, safety_report). `plan` is frozen: everything the caller
    should execute, already ordered (creates/updates/unarchives parent-first,
    archives child-first) and already annotated BLOCKED where a rail refused
    it. archive candidates that would exceed the ratio threshold are also
    turned into BLOCKED entries rather than silently dropped, so the audit
    trail always shows what was *considered*.
    """
    desired_by_code = {d.code: d for d in desired}
    plan: list[PlannedAction] = []
    archive_candidates: list[PlannedAction] = []

    # Pass 1: walk the MDM-desired side. For every platform/section that MDM
    # says should currently exist, compare it to what the CMMS has today and
    # decide CREATE / UNARCHIVE / UPDATE / NOOP. Anything MDM says is *not*
    # active right now is skipped here and picked up in pass 2 instead, from
    # the CMMS side (that's the only way to notice "CMMS still has it, MDM
    # doesn't want it anymore" -> archive candidate).
    for d in desired:
        cur = current_platforms_sections.get(d.code)
        entity_type = d.family
        if not d.active:
            continue  # handled below, from the CMMS side, as a potential archive candidate
        if cur is None:
            # MDM wants it, CMMS has never heard of it -> create it.
            plan.append(
                PlannedAction(
                    entity_type,
                    d.code,
                    "CREATE",
                    d.depth,
                    "in MDM scope, absent from CMMS",
                    {
                        "assetCode": d.code,
                        "assetName": d.name,
                        "assetFamilyCode": entity_type,
                        "assetParentCode": d.parent_code,
                        "bodyNames": [d.body_name] if d.parent_code is None else None,
                    },
                    parent_code=d.parent_code,
                )
            )
            continue

        # It already exists in the CMMS: figure out what (if anything) changed
        # so we only send the fields that actually differ, not a full payload.
        diff: dict = {}
        if cur.name != d.name:
            diff["assetName"] = d.name
        if d.parent_code is not None and cur.parent_code != d.parent_code:
            diff["parentCode"] = d.parent_code
        if cur.archived:
            # It's active again in MDM but still archived in the CMMS -> bring it back
            # (and carry along any other field changes in the same PATCH).
            plan.append(
                PlannedAction(
                    entity_type,
                    d.code,
                    "UNARCHIVE",
                    d.depth,
                    "back in MDM scope, archived in CMMS",
                    {"assetCode": d.code, "archived": False, **diff},
                    parent_code=d.parent_code,
                )
            )
        elif diff:
            plan.append(PlannedAction(entity_type, d.code, "UPDATE", d.depth, "MDM-owned attribute changed", {"assetCode": d.code, **diff}, parent_code=d.parent_code))
        else:
            plan.append(PlannedAction(entity_type, d.code, "NOOP", d.depth, "desired == current", parent_code=d.parent_code))

    # Pass 2: walk the CMMS-current side. Anything active in the CMMS that MDM
    # either never mentions, or mentions but says is no longer active, is a
    # candidate to be archived -- not archived outright yet, it still has to
    # clear the two safety rails below.
    for code, cur in current_platforms_sections.items():
        if cur.archived:
            continue
        d = desired_by_code.get(code)
        if d is not None and d.active:
            continue  # already handled above
        depth = 0 if cur.family == "PLATFORM" else 1
        reason = "outside MDM scope" if d is None else "decommissioned in MDM (out of active window)"
        archive_candidates.append(
            PlannedAction(cur.family or "UNKNOWN", code, "ARCHIVE", depth, reason, {"assetCode": code, "archived": True}, parent_code=cur.parent_code)
        )

    # Safety rail 1: never orphan an active descendant. Resolve deepest-first
    # (sections before platforms) and track which codes were already accepted
    # for archiving this run (`resolved_ok`), so a platform whose only section
    # is *also* being archived right now doesn't get wrongly blocked by its
    # own about-to-disappear child -- see DECISIONS.md #2.
    eligible: list[PlannedAction] = []
    blocked: list[PlannedAction] = []
    resolved_ok: set[str] = set()
    # deepest first: a section's fate must be settled before its platform is checked,
    # so that a platform and its now-childless section can be archived in the same run.
    for cand in sorted(archive_candidates, key=lambda c: -c.depth):
        if has_active_descendant(cand.code, children_by_parent, excluding=frozenset(resolved_ok)):
            cand.action = "BLOCKED"
            cand.reason = "has active descendant(s): archiving would orphan in-scope children"
            blocked.append(cand)
        else:
            resolved_ok.add(cand.code)
            eligible.append(cand)

    # Safety rail 2: never let one bad extraction silently mass-archive the
    # tree. If archiving everything still "eligible" after rail 1 would wipe
    # out more than the configured ratio of the active tree, freeze the whole
    # destructive batch (block it) -- CREATE/UPDATE/UNARCHIVE above are never
    # touched by this, only ARCHIVE is destructive.
    total_active_in_scope = sum(1 for a in current_platforms_sections.values() if not a.archived)
    ratio = (len(eligible) / total_active_in_scope) if total_active_in_scope else 0.0
    ratio_blocked = ratio > archive_ratio_threshold
    if ratio_blocked:
        for cand in eligible:
            cand.action = "BLOCKED"
            cand.reason = f"archive ratio {ratio:.1%} exceeds {archive_ratio_threshold:.0%} threshold: destructive batch frozen, non-destructive work proceeds"
        blocked.extend(eligible)
        eligible = []

    # order: creates/updates/unarchives parent-first (ascending depth), archives child-first (descending depth)
    non_archive = sorted([p for p in plan], key=lambda p: (p.depth, p.code))
    eligible.sort(key=lambda p: (-p.depth, p.code))
    blocked.sort(key=lambda p: (-p.depth, p.code))

    safety_report = {
        "active_in_scope_count": total_active_in_scope,
        "archive_candidates": len(archive_candidates),
        "archive_eligible": len(eligible),
        "archive_blocked_descendant": sum(1 for b in blocked if "active descendant" in b.reason),
        "archive_blocked_ratio": ratio_blocked,
        "archive_ratio": ratio,
        "archive_ratio_threshold": archive_ratio_threshold,
        "ratio_breached": ratio_blocked,
    }
    return non_archive + eligible + blocked, safety_report
