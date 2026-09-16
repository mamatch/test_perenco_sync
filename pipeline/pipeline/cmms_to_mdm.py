"""Integration 2 -- CMMS -> MDM (ARCHITECTURE_.md part A section 4).

Full reconciliation, not an upsert-only feed: systems and equipments that
appear, change or disappear (archived, or simply no longer reported) in the
CMMS are all reflected into the MDM. Governed reference data (equipment
types, system classes, section categories) is never created by the sync --
an asset that references unknown reference data, an invalid parent, or an
orphan is rejected and surfaced as a data-quality issue instead.

Only two Asset/Filter calls are made regardless of tenant size (one for
archived=False, one for archived=True, both fully paginated): the Filter
response already carries family, parent, bodies, criticality and
inServiceDate, so there is no need for a per-asset Get call.

All delta computation and validation happens here, in pure Python, against
read-only MDM lookups (`clients/mdm.py`). Writes are collected into a
`SyncPlan` and applied in one shot at the end by triggering a Django
management command inside MDAdmin's own process (`clients/mdadmin.py`) --
see ARCHITECTURE_.md section 2/11 for why writes don't happen directly from
here. The plan is one transaction on the Django side: if it fails, every
pending action is recorded FAILED_RETRYABLE, never a partial success.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .audit import ActionRecord, AuditStore
from .clients.cmms import CmmsClient
from .clients.mdadmin import MdAdminCommandError, SyncPlan, apply_plan
from .clients.mdm import MdmClient
from .config import Settings

logger = logging.getLogger("pipeline.cmms_to_mdm")

STRUCTURAL_FAMILIES = {"PLATFORM", "SECTION", "LOC_TAG"}
CRITICALITY_TO_ATTRIBUTE = {"PC": "Production critical", "SCE": "SCE"}


@dataclass
class CmmsAsset:
    code: str
    name: str
    family: str | None
    parent_code: str | None
    bodies: tuple[str, ...]
    criticality_code: str | None
    archived: bool
    in_service_date: str | None


def _pull_all(cmms: CmmsClient) -> dict[str, CmmsAsset]:
    assets: dict[str, CmmsAsset] = {}
    for archived in (False, True):
        for a in cmms.iter_assets(archived=archived):
            assets[a["code"]] = CmmsAsset(
                code=a["code"],
                name=a["name"],
                family=(a.get("family") or {}).get("code"),
                parent_code=(a.get("parent") or {}).get("code"),
                bodies=tuple(b["name"] for b in a.get("bodies") or []),
                criticality_code=(a.get("criticality") or {}).get("code"),
                archived=archived,
                in_service_date=a.get("inServiceDate"),
            )
    return assets


def _date_only(iso_dt: str | None) -> str | None:
    if not iso_dt:
        return None
    return iso_dt[:10]


def run(cmms: CmmsClient, mdm: MdmClient, audit: AuditStore, settings: Settings, run_id: str | None = None) -> str:
    today = datetime.now(timezone.utc).date().isoformat()

    with audit.run("cmms_to_mdm", run_id) as run_id:
        assets = _pull_all(cmms)
        audit.record_metric(run_id, "cmms_assets_seen_total", len(assets))

        systems = {c: a for c, a in assets.items() if (a.family or "").startswith("SYS_")}
        equipments = {c: a for c, a in assets.items() if a.family and a.family not in STRUCTURAL_FAMILIES and not a.family.startswith("SYS_")}
        audit.record_metric(run_id, "cmms_systems_seen", len(systems))
        audit.record_metric(run_id, "cmms_equipments_seen", len(equipments))

        rejected_systems: set[str] = set()
        plan = SyncPlan()
        pending: list[ActionRecord] = []

        # -- Systems --------------------------------------------------------
        # For each CMMS system: walk up to its section then its platform,
        # resolving every piece against MDM's *governed* reference data
        # (system_unit/section_category/system_class). Any link that doesn't
        # resolve is a hard rejection -- this sync never invents governed
        # reference data on the fly, it only reports the gap (DECISIONS.md).
        for code, sys_asset in systems.items():
            section = assets.get(sys_asset.parent_code) if sys_asset.parent_code else None
            if section is None:
                _reject(audit, run_id, "SYSTEM", code, "unknown_parent", f"parent '{sys_asset.parent_code}' not found in CMMS")
                rejected_systems.add(code)
                continue
            if section.family != "SECTION":
                _reject(
                    audit, run_id, "SYSTEM", code, "invalid_system_parent_type",
                    f"parent '{section.code}' has family '{section.family}', expected SECTION",
                )
                rejected_systems.add(code)
                continue
            platform_code = section.parent_code
            if not platform_code:
                _reject(audit, run_id, "SYSTEM", code, "section_without_platform", f"section '{section.code}' has no parent")
                rejected_systems.add(code)
                continue
            system_unit = mdm.system_unit_by_code(platform_code)
            if system_unit is None:
                _reject(audit, run_id, "SYSTEM", code, "unknown_platform_in_mdm", f"platform '{platform_code}' is not a known MDM system unit")
                rejected_systems.add(code)
                continue
            # CMMS section codes are "<platform_code>_<section_suffix>" (e.g. "JNR_PG"
            # under platform "JNR"); strip the platform prefix to get the suffix MDM
            # keys its governed section categories by.
            suffix = section.code[len(platform_code) + 1 :] if section.code.startswith(platform_code + "_") else None
            section_category = mdm.section_category_by_code(suffix) if suffix else None
            if section_category is None:
                _reject(
                    audit, run_id, "SYSTEM", code, "unknown_section_category",
                    f"cannot resolve a governed section category from CMMS section code '{section.code}' (suffix={suffix!r})",
                )
                rejected_systems.add(code)
                continue
            # CMMS system family is "SYS_<class_code>" (e.g. "SYS_PG"); strip
            # the "SYS_" prefix to get the governed system class code.
            class_code = (sys_asset.family or "")[4:]
            system_class = mdm.system_class_by_code(class_code)
            if system_class is None:
                _reject(audit, run_id, "SYSTEM", code, "unknown_system_class", f"governed system class '{class_code}' is not known to the MDM")
                rejected_systems.add(code)
                continue

            # Criticality (PC/SCE) maps to a governed MDM attribute, when recognised.
            attribute_names: set[str] = set()
            if sys_asset.criticality_code:
                attr_name = CRITICALITY_TO_ATTRIBUTE.get(sys_asset.criticality_code)
                if attr_name:
                    attribute_names.add(attr_name)
                else:
                    audit.record_dq_issue(
                        run_id, "cmms_to_mdm", "SYSTEM", code, "unknown_criticality_code",
                        f"criticality code '{sys_asset.criticality_code}' does not map to a known governed attribute (typo?)",
                    )

            date_start = _date_only(sys_asset.in_service_date)
            date_end = today if sys_asset.archived else None  # archived in CMMS -> close the MDM record as of today
            existing = mdm.system_by_code(code)
            current_attrs = mdm.system_attribute_names(existing["id"]) if existing else set()
            # Diff against the current MDM row field by field; only write when
            # something actually differs (keeps NOOP runs genuinely no-op).
            changed = existing is None or (
                existing["tag"] != sys_asset.name
                or existing["section_id"] != section_category["id"]
                or existing["system_class_id"] != system_class["id"]
                or existing["system_unit_id"] != system_unit["id"]
                or existing["date_start"] != date_start
                or existing["date_end"] != date_end
                or current_attrs != attribute_names
            )

            if changed:
                # Don't write yet -- just queue it on the plan. `pending` mirrors
                # the plan so it can be turned into audit rows once we know
                # whether the whole batch actually got applied (see bottom of
                # this function).
                plan.upsert_system(
                    code=code,
                    tag=sys_asset.name,
                    system_class_code=class_code,
                    platform_code=platform_code,
                    section_code=suffix,
                    source=settings.cmms_tenant,
                    date_start=date_start,
                    date_end=date_end,
                    attribute_names=attribute_names,
                )
                pending.append(ActionRecord("CMMS", "MDM", "SYSTEM", code, "UPDATE", "SUCCESS"))
            else:
                audit.record_action(run_id, ActionRecord("CMMS", "MDM", "SYSTEM", code, "NOOP", "SUCCESS"))

        # -- Equipments -------------------------------------------------------
        # Same idea as systems: resolve governed reference data (equipment_type),
        # reject what doesn't resolve, and queue upserts/assignments on the plan.
        for code, eq_asset in equipments.items():
            parent = assets.get(eq_asset.parent_code) if eq_asset.parent_code else None
            if parent is None:
                _reject(audit, run_id, "EQUIPMENT", code, "orphan_equipment", "no parent asset (equipment must belong to a system)")
                continue
            if not (parent.family or "").startswith("SYS_"):
                _reject(audit, run_id, "EQUIPMENT", code, "invalid_equipment_parent_type", f"parent '{parent.code}' has family '{parent.family}', expected a SYS_* system")
                continue
            if parent.code in rejected_systems:
                _reject(audit, run_id, "EQUIPMENT", code, "parent_system_rejected", f"parent system '{parent.code}' was itself rejected this run")
                continue
            equipment_type = mdm.equipment_type_by_code(eq_asset.family or "")
            if equipment_type is None:
                _reject(audit, run_id, "EQUIPMENT", code, "unknown_equipment_type", f"governed equipment type '{eq_asset.family}' is not known to the MDM")
                continue

            date_start = _date_only(eq_asset.in_service_date)
            date_end = today if eq_asset.archived else None
            existing = mdm.equipment_by_code(code)
            changed = existing is None or (
                existing["name"] != eq_asset.name
                or existing["equipment_type_id"] != equipment_type["id"]
                or existing["date_start"] != date_start
                or existing["date_end"] != date_end
            )
            assignment_exists = mdm.system_equipment_assignment_exists(parent.code, code)

            # An equipment can need an upsert, a new system assignment, both, or
            # neither -- `recorded` just makes sure exactly one audit row is
            # written per equipment regardless of which combination applies.
            recorded = False
            if changed:
                plan.upsert_equipment(code=code, name=eq_asset.name, equipment_type_code=eq_asset.family, date_start=date_start, date_end=date_end)
                pending.append(ActionRecord("CMMS", "MDM", "EQUIPMENT", code, "UPDATE", "SUCCESS"))
                recorded = True
            if not assignment_exists:
                plan.assign(system_code=parent.code, equipment_code=code, assignment_date=date_start or today)
                if not recorded:
                    pending.append(ActionRecord("CMMS", "MDM", "EQUIPMENT", code, "UPDATE", "SUCCESS"))
                    recorded = True
            if not recorded:
                audit.record_action(run_id, ActionRecord("CMMS", "MDM", "EQUIPMENT", code, "NOOP", "SUCCESS"))

        # -- Disappeared: MDM knows a System/Equipment that CMMS no longer reports at all ---
        # This is the "full reconciliation, not upsert-only" half of the flow:
        # anything still open (date_end IS NULL) in MDM but absent from this
        # CMMS extraction entirely (not even archived=True) gets closed too.
        seen_codes = set(assets.keys())
        for row in mdm.conn.execute("SELECT code FROM systemref_system WHERE date_end IS NULL").fetchall():
            if row["code"] not in seen_codes:
                plan.close_system(row["code"], today)
                pending.append(ActionRecord("CMMS", "MDM", "SYSTEM", row["code"], "ARCHIVE", "SUCCESS", "disappeared from the CMMS extraction"))
                audit.record_dq_issue(run_id, "cmms_to_mdm", "SYSTEM", row["code"], "disappeared_from_cmms", "no longer reported by the CMMS connector (active or archived)")
        for row in mdm.conn.execute("SELECT code FROM systemref_equipment WHERE date_end IS NULL").fetchall():
            if row["code"] not in seen_codes:
                plan.close_equipment(row["code"], today)
                pending.append(ActionRecord("CMMS", "MDM", "EQUIPMENT", row["code"], "ARCHIVE", "SUCCESS", "disappeared from the CMMS extraction"))
                audit.record_dq_issue(run_id, "cmms_to_mdm", "EQUIPMENT", row["code"], "disappeared_from_cmms", "no longer reported by the CMMS connector (active or archived)")

        # -- Apply the plan inside MDAdmin's own process -----------------------
        # Everything queued above is written in one shot here: either the whole
        # plan lands (and every `pending` record is audited as SUCCESS), or the
        # Django-side transaction fails as a whole (and every `pending` record
        # is instead audited as FAILED_RETRYABLE) -- never a partial write.
        if not plan.is_empty():
            try:
                summary = apply_plan(plan, settings.systemref_lite_dir, settings.mdm_db_path)
                for rec in pending:
                    audit.record_action(run_id, rec)
                audit.record_metric(run_id, "mdadmin_plan_systems_applied", summary.get("systems", 0))
                audit.record_metric(run_id, "mdadmin_plan_equipments_applied", summary.get("equipments", 0))
                audit.record_metric(run_id, "mdadmin_plan_assignments_applied", summary.get("assignments", 0))
                audit.record_metric(run_id, "mdadmin_plan_closures_applied", summary.get("closures", 0))
            except MdAdminCommandError as exc:
                logger.error("apply_sync_plan failed: %s", exc)
                for rec in pending:
                    rec.status = "FAILED_RETRYABLE"
                    rec.reason = f"apply_sync_plan failed: {exc.stderr[:300]}"
                    audit.record_action(run_id, rec)
                audit.record_alert(
                    run_id, "cmms_to_mdm", "CRITICAL",
                    f"MDAdmin apply_sync_plan failed ({exc.returncode}): the whole batch of {len(pending)} MDM write(s) was rolled back.",
                )

        return run_id


def _reject(audit: AuditStore, run_id: str, entity_type: str, code: str, reason: str, details: str) -> None:
    audit.record_action(run_id, ActionRecord("CMMS", "MDM", entity_type, code, "REJECTED", "REJECTED", details))
    audit.record_dq_issue(run_id, "cmms_to_mdm", entity_type, code, reason, details)
    logger.info("cmms_to_mdm rejected %s %s: %s (%s)", entity_type, code, details, reason)
