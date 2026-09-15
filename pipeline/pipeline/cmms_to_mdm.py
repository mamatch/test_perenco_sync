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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .audit import ActionRecord, AuditStore
from .clients.cmms import CmmsClient
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
        resolved_system_id: dict[str, int] = {}

        # -- Systems --------------------------------------------------------
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
            suffix = section.code[len(platform_code) + 1 :] if section.code.startswith(platform_code + "_") else None
            section_category = mdm.section_category_by_code(suffix) if suffix else None
            if section_category is None:
                _reject(
                    audit, run_id, "SYSTEM", code, "unknown_section_category",
                    f"cannot resolve a governed section category from CMMS section code '{section.code}' (suffix={suffix!r})",
                )
                rejected_systems.add(code)
                continue
            class_code = (sys_asset.family or "")[4:]
            system_class = mdm.system_class_by_code(class_code)
            if system_class is None:
                _reject(audit, run_id, "SYSTEM", code, "unknown_system_class", f"governed system class '{class_code}' is not known to the MDM")
                rejected_systems.add(code)
                continue

            system_id, changed = mdm.upsert_system(
                code=code,
                tag=sys_asset.name,
                system_class_id=system_class["id"],
                system_unit_id=system_unit["id"],
                section_id=section_category["id"],
                source=settings.cmms_tenant,
                date_start=_date_only(sys_asset.in_service_date),
                date_end=(today if sys_asset.archived else None),
            )
            resolved_system_id[code] = system_id

            attr_ids: set[int] = set()
            if sys_asset.criticality_code:
                attr_name = CRITICALITY_TO_ATTRIBUTE.get(sys_asset.criticality_code)
                if attr_name:
                    row = mdm.system_attribute_by_name(attr_name)
                    if row:
                        attr_ids.add(row["id"])
                else:
                    audit.record_dq_issue(
                        run_id, "cmms_to_mdm", "SYSTEM", code, "unknown_criticality_code",
                        f"criticality code '{sys_asset.criticality_code}' does not map to a known governed attribute (typo?)",
                    )
            attrs_changed = mdm.set_system_attributes(system_id, attr_ids)

            action = "UPDATE" if (changed or attrs_changed) else "NOOP"
            audit.record_action(run_id, ActionRecord("CMMS", "MDM", "SYSTEM", code, action, "SUCCESS"))

        # -- Equipments -------------------------------------------------------
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

            equipment_id, changed = mdm.upsert_equipment(
                code=code,
                name=eq_asset.name,
                equipment_type_id=equipment_type["id"],
                date_start=_date_only(eq_asset.in_service_date),
                date_end=(today if eq_asset.archived else None),
            )
            system_id = resolved_system_id.get(parent.code)
            assignment_created = False
            if system_id is not None:
                assignment_created = mdm.ensure_system_equipment_assignment(
                    system_id, equipment_id, _date_only(eq_asset.in_service_date) or today
                )
            action = "UPDATE" if (changed or assignment_created) else "NOOP"
            audit.record_action(run_id, ActionRecord("CMMS", "MDM", "EQUIPMENT", code, action, "SUCCESS"))

        # -- Disappeared: MDM knows a System/Equipment that CMMS no longer reports at all ---
        seen_codes = set(assets.keys())
        for row in mdm.conn.execute("SELECT id, code FROM systemref_system WHERE date_end IS NULL").fetchall():
            if row["code"] not in seen_codes:
                if mdm.set_system_date_end(row["id"], today):
                    audit.record_action(run_id, ActionRecord("CMMS", "MDM", "SYSTEM", row["code"], "ARCHIVE", "SUCCESS", "disappeared from the CMMS extraction"))
                    audit.record_dq_issue(run_id, "cmms_to_mdm", "SYSTEM", row["code"], "disappeared_from_cmms", "no longer reported by the CMMS connector (active or archived)")
        for row in mdm.conn.execute("SELECT id, code FROM systemref_equipment WHERE date_end IS NULL").fetchall():
            if row["code"] not in seen_codes:
                if mdm.set_equipment_date_end(row["id"], today):
                    audit.record_action(run_id, ActionRecord("CMMS", "MDM", "EQUIPMENT", row["code"], "ARCHIVE", "SUCCESS", "disappeared from the CMMS extraction"))
                    audit.record_dq_issue(run_id, "cmms_to_mdm", "EQUIPMENT", row["code"], "disappeared_from_cmms", "no longer reported by the CMMS connector (active or archived)")

        mdm.commit()
        return run_id


def _reject(audit: AuditStore, run_id: str, entity_type: str, code: str, reason: str, details: str) -> None:
    audit.record_action(run_id, ActionRecord("CMMS", "MDM", entity_type, code, "REJECTED", "REJECTED", details))
    audit.record_dq_issue(run_id, "cmms_to_mdm", entity_type, code, reason, details)
    logger.info("cmms_to_mdm rejected %s %s: %s (%s)", entity_type, code, details, reason)
