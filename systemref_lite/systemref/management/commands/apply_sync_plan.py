"""Apply a CMMS -> MDM sync plan computed by the external sync service.

    uv run manage.py apply_sync_plan --plan-file plan.json

The plan is produced by pipeline/pipeline/clients/mdadmin.py::SyncPlan and
contains only entries the sync service has already decided need a write --
governed-reference validation and NOOP/UPDATE diffing happen there, in
pipeline/pipeline/cmms_to_mdm.py, not here. This command's only job is to
apply it through the ORM, inside one transaction, so any model-level
validation/signals this app might grow later are never bypassed by a write
coming from outside it (see ARCHITECTURE_.md section 2/11 and DECISIONS.md).
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from systemref.models import (
    Equipment,
    EquipmentType,
    SectionCategory,
    System,
    SystemAttribute,
    SystemAttributeAssignment,
    SystemClass,
    SystemEquipmentAssignment,
    SystemUnit,
)


class Command(BaseCommand):
    help = "Apply a CMMS -> MDM sync plan (JSON) produced by the sync service."

    def add_arguments(self, parser):
        parser.add_argument("--plan-file", required=True)

    def handle(self, *args, **options):
        path = Path(options["plan_file"])
        if not path.exists():
            raise CommandError(f"plan file not found: {path}")
        plan = json.loads(path.read_text())

        applied = {"systems": 0, "equipments": 0, "assignments": 0, "closures": 0}
        with transaction.atomic():
            for action in plan.get("systems", []):
                if action["op"] == "upsert":
                    system, _ = System.objects.update_or_create(
                        code=action["code"],
                        defaults={
                            "tag": action["tag"],
                            "system_class": SystemClass.objects.get(code=action["system_class_code"]),
                            "system_unit": SystemUnit.objects.get(code=action["platform_code"]),
                            "section": SectionCategory.objects.get(code=action["section_code"]),
                            "source": action["source"],
                            "date_start": action["date_start"],
                            "date_end": action["date_end"],
                        },
                    )
                    wanted = set(
                        SystemAttribute.objects.filter(name__in=action.get("attribute_names", [])).values_list("id", flat=True)
                    )
                    current = set(
                        SystemAttributeAssignment.objects.filter(system=system).values_list("system_attribute_id", flat=True)
                    )
                    if current - wanted:
                        SystemAttributeAssignment.objects.filter(
                            system=system, system_attribute_id__in=(current - wanted)
                        ).delete()
                    if wanted - current:
                        SystemAttributeAssignment.objects.bulk_create(
                            [SystemAttributeAssignment(system=system, system_attribute_id=i) for i in (wanted - current)]
                        )
                    applied["systems"] += 1
                elif action["op"] == "close":
                    System.objects.filter(code=action["code"], date_end__isnull=True).update(date_end=action["date_end"])
                    applied["closures"] += 1

            for action in plan.get("equipments", []):
                if action["op"] == "upsert":
                    Equipment.objects.update_or_create(
                        code=action["code"],
                        defaults={
                            "name": action["name"],
                            "equipment_type": EquipmentType.objects.get(code=action["equipment_type_code"]),
                            "date_start": action["date_start"],
                            "date_end": action["date_end"],
                        },
                    )
                    applied["equipments"] += 1
                elif action["op"] == "close":
                    Equipment.objects.filter(code=action["code"], date_end__isnull=True).update(date_end=action["date_end"])
                    applied["closures"] += 1

            for action in plan.get("assignments", []):
                system = System.objects.get(code=action["system_code"])
                equipment = Equipment.objects.get(code=action["equipment_code"])
                _, created = SystemEquipmentAssignment.objects.get_or_create(
                    system=system,
                    equipment=equipment,
                    defaults={"assignment_date": action["assignment_date"]},
                )
                if created:
                    applied["assignments"] += 1

        # Plain, uncolored JSON on the last stdout line: the caller (a
        # subprocess, not a terminal) parses this to know what was applied.
        self.stdout.write(json.dumps(applied))
