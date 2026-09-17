"""Tests for the apply_sync_plan management command -- the only place a write
coming from outside this app's process ever lands in governed master data
(see pipeline/pipeline/clients/mdadmin.py and ARCHITECTURE_.md section 4).
Exercises the ORM upsert/close/assign paths and, most importantly, that a
plan with one invalid entry rolls back as a whole (no partial write) --
see DECISIONS.md #11's "all-or-nothing" claim.
"""

from __future__ import annotations

import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from orgref.models import OrganisationalStructure, OrgUnit
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

pytestmark = pytest.mark.django_db


@pytest.fixture
def governed_reference_data():
    """Governed reference data the command must resolve *by code*, never create
    -- the same fixtures pipeline/pipeline/clients/mdm.py's read-only lookups
    would find in the real seed."""
    structure = OrganisationalStructure.objects.create(name="Gabon E&P", country_code="GA")
    org_unit = OrgUnit.objects.create(name="Tchatamba", organisational_structure=structure, is_field=True, is_active=True)
    system_unit = SystemUnit.objects.create(code="JNR", name="Tchatamba", org_unit=org_unit)
    section = SectionCategory.objects.create(code="PG", name="Power Generation")
    system_class = SystemClass.objects.create(code="PG", name="Power Generation")
    equipment_type = EquipmentType.objects.create(code="PU_CE", name="Centrifugal pump")
    SystemAttribute.objects.create(name="Production critical")
    SystemAttribute.objects.create(name="SCE")
    return {
        "system_unit": system_unit,
        "section": section,
        "system_class": system_class,
        "equipment_type": equipment_type,
    }


def _write_plan(tmp_path, plan: dict) -> str:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    return str(path)


def _run(plan_path: str) -> dict:
    call_command("apply_sync_plan", "--plan-file", plan_path)


def test_upsert_creates_a_new_system_with_attributes(tmp_path, governed_reference_data, capsys):
    plan = {
        "systems": [
            {
                "op": "upsert",
                "code": "SYS_JNR_004",
                "tag": "Tchatamba power generation",
                "system_class_code": "PG",
                "platform_code": "JNR",
                "section_code": "PG",
                "source": "PERENCO",
                "date_start": "2020-01-01",
                "date_end": None,
                "attribute_names": ["Production critical"],
            }
        ],
        "equipments": [],
        "assignments": [],
    }
    _run(_write_plan(tmp_path, plan))

    system = System.objects.get(code="SYS_JNR_004")
    assert system.tag == "Tchatamba power generation"
    assert system.system_class.code == "PG"
    assert system.system_unit.code == "JNR"
    assert system.section.code == "PG"
    assert {a.system_attribute.name for a in system.attribute_assignments.all()} == {"Production critical"}

    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary == {"systems": 1, "equipments": 0, "assignments": 0, "closures": 0}


def test_upsert_is_idempotent_and_reconciles_attributes(tmp_path, governed_reference_data):
    base_plan = {
        "systems": [
            {
                "op": "upsert",
                "code": "SYS_JNR_004",
                "tag": "Tchatamba power generation",
                "system_class_code": "PG",
                "platform_code": "JNR",
                "section_code": "PG",
                "source": "PERENCO",
                "date_start": "2020-01-01",
                "date_end": None,
                "attribute_names": ["Production critical"],
            }
        ],
        "equipments": [],
        "assignments": [],
    }
    _run(_write_plan(tmp_path, base_plan))
    assert System.objects.count() == 1

    # Second apply with the SCE attribute instead of Production critical: same
    # row (update_or_create on `code`), attribute set reconciled (old one
    # removed, new one added), not accumulated.
    changed_plan = json.loads(json.dumps(base_plan))
    changed_plan["systems"][0]["attribute_names"] = ["SCE"]
    _run(_write_plan(tmp_path, changed_plan))

    assert System.objects.count() == 1
    system = System.objects.get(code="SYS_JNR_004")
    assert {a.system_attribute.name for a in system.attribute_assignments.all()} == {"SCE"}


def test_close_sets_date_end_only_on_open_rows(tmp_path, governed_reference_data):
    upsert_plan = {
        "systems": [
            {
                "op": "upsert",
                "code": "SYS_JNR_004",
                "tag": "Tchatamba power generation",
                "system_class_code": "PG",
                "platform_code": "JNR",
                "section_code": "PG",
                "source": "PERENCO",
                "date_start": "2020-01-01",
                "date_end": None,
                "attribute_names": [],
            }
        ],
        "equipments": [],
        "assignments": [],
    }
    _run(_write_plan(tmp_path, upsert_plan))

    close_plan = {"systems": [{"op": "close", "code": "SYS_JNR_004", "date_end": "2026-01-01"}], "equipments": [], "assignments": []}
    _run(_write_plan(tmp_path, close_plan))

    system = System.objects.get(code="SYS_JNR_004")
    assert system.date_end == "2026-01-01" or str(system.date_end) == "2026-01-01"

    # Closing again (already closed) must not error and must not move date_end.
    _run(_write_plan(tmp_path, close_plan))
    system.refresh_from_db()
    assert str(system.date_end) == "2026-01-01"


def test_equipment_upsert_and_assignment(tmp_path, governed_reference_data):
    plan = {
        "systems": [
            {
                "op": "upsert",
                "code": "SYS_JNR_004",
                "tag": "Tchatamba power generation",
                "system_class_code": "PG",
                "platform_code": "JNR",
                "section_code": "PG",
                "source": "PERENCO",
                "date_start": "2020-01-01",
                "date_end": None,
                "attribute_names": [],
            }
        ],
        "equipments": [
            {
                "op": "upsert",
                "code": "JNR-PU-101",
                "name": "Cooling pump",
                "equipment_type_code": "PU_CE",
                "date_start": "2020-01-01",
                "date_end": None,
            }
        ],
        "assignments": [{"system_code": "SYS_JNR_004", "equipment_code": "JNR-PU-101", "assignment_date": "2020-01-01"}],
    }
    _run(_write_plan(tmp_path, plan))

    equipment = Equipment.objects.get(code="JNR-PU-101")
    assert equipment.name == "Cooling pump"
    assert equipment.equipment_type.code == "PU_CE"
    assert SystemEquipmentAssignment.objects.filter(system__code="SYS_JNR_004", equipment=equipment).exists()

    # Re-applying the same assignment must not create a duplicate
    # (get_or_create on the (system, equipment) unique_together pair).
    _run(_write_plan(tmp_path, plan))
    assert SystemEquipmentAssignment.objects.filter(system__code="SYS_JNR_004", equipment=equipment).count() == 1


def test_invalid_reference_in_plan_rolls_back_the_whole_batch(tmp_path, governed_reference_data):
    """The concrete claim behind 'a failed apply is all-or-nothing'
    (pipeline/pipeline/cmms_to_mdm.py, ARCHITECTURE_.md section 4): a plan
    where a later entry references an unknown system_class_code must not
    leave the earlier, otherwise-valid entry committed."""
    plan = {
        "systems": [
            {
                "op": "upsert",
                "code": "SYS_JNR_004",
                "tag": "Tchatamba power generation",
                "system_class_code": "PG",
                "platform_code": "JNR",
                "section_code": "PG",
                "source": "PERENCO",
                "date_start": "2020-01-01",
                "date_end": None,
                "attribute_names": [],
            },
            {
                "op": "upsert",
                "code": "SYS_JNR_999",
                "tag": "Unknown class system",
                "system_class_code": "DOES_NOT_EXIST",
                "platform_code": "JNR",
                "section_code": "PG",
                "source": "PERENCO",
                "date_start": "2020-01-01",
                "date_end": None,
                "attribute_names": [],
            },
        ],
        "equipments": [],
        "assignments": [],
    }

    with pytest.raises(SystemClass.DoesNotExist):
        _run(_write_plan(tmp_path, plan))

    # The whole transaction rolled back: not even the first, valid entry survived.
    assert not System.objects.filter(code="SYS_JNR_004").exists()
    assert not System.objects.filter(code="SYS_JNR_999").exists()


def test_missing_plan_file_raises_command_error():
    with pytest.raises(CommandError):
        call_command("apply_sync_plan", "--plan-file", "/does/not/exist.json")
