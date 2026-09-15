"""Integration 1 -- MDM -> CMMS (ARCHITECTURE_.md part A section 3).

Synchronises the MDM-owned hierarchy (platforms, sections) into the CMMS:
creates what entered scope, archives what left it, reports what it refuses
to touch automatically. Safety rails are evaluated before any destructive
write is sent.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .audit import ActionRecord, AuditStore
from .canonical import CurrentAsset, DesiredNode, build_children_index, compute_plan
from .clients.cmms import CmmsClient, CmmsNotFound, CmmsUnavailable, CmmsValidationError
from .clients.mdm import MdmClient
from .config import Settings

logger = logging.getLogger("pipeline.mdm_to_cmms")

STRUCTURAL_FAMILIES = {"PLATFORM", "SECTION"}


def _current_tree(cmms: CmmsClient) -> tuple[dict[str, CurrentAsset], dict[str, list[CurrentAsset]]]:
    """Pulls the whole active tree (all families) plus archived PLATFORM/SECTION
    assets. Needed because:
    - the active tree gives us recursive active-descendant information at any
      depth (System/Equipment included), required by the archive safety rail;
    - Asset/Filter never exposes `archived` (docs/03_cmms_api.md), so the only
      way to know an asset's archived flag is to ask for archived=True and
      archived=False separately and remember which bucket it came from.
    """
    active_all: list[CurrentAsset] = []
    for a in cmms.iter_assets(archived=False):
        active_all.append(
            CurrentAsset(
                code=a["code"],
                name=a["name"],
                family=(a.get("family") or {}).get("code"),
                parent_code=(a.get("parent") or {}).get("code"),
                bodies=tuple(b["name"] for b in a.get("bodies") or []),
                archived=False,
            )
        )

    current_platforms_sections: dict[str, CurrentAsset] = {
        a.code: a for a in active_all if a.family in STRUCTURAL_FAMILIES
    }

    for family in ("PLATFORM", "SECTION"):
        for a in cmms.iter_assets(archived=True, assetFamilyCode=family):
            current_platforms_sections[a["code"]] = CurrentAsset(
                code=a["code"],
                name=a["name"],
                family=family,
                parent_code=(a.get("parent") or {}).get("code"),
                bodies=tuple(b["name"] for b in a.get("bodies") or []),
                archived=True,
            )

    children_by_parent = build_children_index(active_all)
    return current_platforms_sections, children_by_parent


def run(cmms: CmmsClient, mdm: MdmClient, audit: AuditStore, settings: Settings, run_id: str | None = None) -> str:
    as_of = datetime.now(timezone.utc).date()

    with audit.run("mdm_to_cmms", run_id) as run_id:
        platforms, sections, mdm_issues = mdm.desired_hierarchy(as_of)
        for issue in mdm_issues:
            audit.record_dq_issue(run_id, "mdm_to_cmms", issue["entity_type"], issue["entity_code"], issue["reason"], issue["details"])

        active_platform_count = sum(1 for p in platforms if p.active)
        audit.record_metric(run_id, "mdm_desired_platforms_total", len(platforms))
        audit.record_metric(run_id, "mdm_desired_platforms_active", active_platform_count)
        audit.record_metric(run_id, "mdm_desired_sections_active", sum(1 for s in sections if s.active))

        if active_platform_count == 0:
            audit.record_alert(
                run_id,
                "mdm_to_cmms",
                "CRITICAL",
                "MDM extraction returned zero active platforms -- refusing to run to avoid a mass-archive "
                "on a corrupted/empty extraction (docs/02_business_rules.md).",
            )
            logger.error("Empty MDM snapshot: aborting mdm_to_cmms without touching the CMMS")
            return run_id

        desired = [
            DesiredNode(p.code, p.name, "PLATFORM", None, p.body_name, p.active, depth=0) for p in platforms
        ] + [
            DesiredNode(s.code, s.name, "SECTION", s.parent_code, s.body_name, s.active, depth=1) for s in sections
        ]

        current_platforms_sections, children_by_parent = _current_tree(cmms)

        plan, safety = compute_plan(desired, current_platforms_sections, children_by_parent, settings.archive_ratio_threshold)
        for k, v in safety.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                audit.record_metric(run_id, f"safety_{k}", float(v))
        if safety["ratio_breached"]:
            audit.record_alert(
                run_id,
                "mdm_to_cmms",
                "WARNING",
                f"Archive ratio {safety['archive_ratio']:.1%} exceeds the {settings.archive_ratio_threshold:.0%} "
                f"threshold: all {safety['archive_eligible']} destructive action(s) blocked this run.",
            )

        failed_parents: set[str] = set()

        for item in plan:
            if item.action == "NOOP":
                audit.record_action(
                    run_id, ActionRecord("MDM", "CMMS", item.entity_type, item.code, "NOOP", "SUCCESS", item.reason)
                )
                continue

            if item.action == "BLOCKED":
                audit.record_action(
                    run_id, ActionRecord("MDM", "CMMS", item.entity_type, item.code, "ARCHIVE", "BLOCKED", item.reason, item.payload)
                )
                audit.record_dq_issue(run_id, "mdm_to_cmms", item.entity_type, item.code, "archive_blocked", item.reason)
                continue

            if item.parent_code and item.parent_code in failed_parents:
                audit.record_action(
                    run_id,
                    ActionRecord("MDM", "CMMS", item.entity_type, item.code, item.action, "REJECTED", f"parent '{item.parent_code}' action failed this run", item.payload),
                )
                failed_parents.add(item.code)
                continue

            try:
                if item.action == "CREATE":
                    payload = {k: v for k, v in item.payload.items() if v is not None}
                    result = cmms.post_asset(payload)
                elif item.action in ("UPDATE", "UNARCHIVE"):
                    result = cmms.patch_asset(item.payload)
                elif item.action == "ARCHIVE":
                    result = cmms.patch_asset(item.payload)
                else:  # pragma: no cover - defensive
                    continue
                audit.record_action(
                    run_id, ActionRecord("MDM", "CMMS", item.entity_type, item.code, item.action, "SUCCESS", item.reason, item.payload, {"messages": result})
                )
            except CmmsValidationError as exc:
                audit.record_action(
                    run_id, ActionRecord("MDM", "CMMS", item.entity_type, item.code, item.action, "REJECTED", "; ".join(exc.messages), item.payload)
                )
                audit.record_dq_issue(run_id, "mdm_to_cmms", item.entity_type, item.code, "cmms_rejected", exc.messages)
                if item.action in ("CREATE", "UNARCHIVE"):
                    failed_parents.add(item.code)
            except CmmsNotFound as exc:
                audit.record_action(
                    run_id, ActionRecord("MDM", "CMMS", item.entity_type, item.code, item.action, "REJECTED", "; ".join(exc.messages), item.payload)
                )
                failed_parents.add(item.code)
            except CmmsUnavailable as exc:
                audit.record_action(
                    run_id, ActionRecord("MDM", "CMMS", item.entity_type, item.code, item.action, "FAILED_RETRYABLE", "; ".join(exc.messages), item.payload, retry_count=settings.http_max_retries)
                )
                failed_parents.add(item.code)

        mdm.commit()
        return run_id
