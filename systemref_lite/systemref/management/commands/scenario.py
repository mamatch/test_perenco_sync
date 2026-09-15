"""Mutate the MDM to simulate an event (used during the debrief to test the candidate's pipeline).

    uv run manage.py scenario list
    uv run manage.py scenario decommission_all      # every platform gets date_end = today -> a naive sync would archive everything
    uv run manage.py scenario decommission TCH      # platform TCH gets date_end = today
    uv run manage.py scenario add_section YMB SAFE  # a new section is assigned to a platform
    uv run manage.py scenario reset                 # reload the fixture
"""

from datetime import date

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from systemref.models import SectionCategory, SystemUnit, SystemUnitToSectionAssignment

SCENARIOS = {
    "decommission_all": "Set date_end = today on every system unit (simulates a corrupted MDM extraction: nothing left in scope).",
    "decommission <UNIT_CODE>": "Set date_end = today on a system unit.",
    "add_section <UNIT_CODE> <SECTION_CODE>": "Assign a new section to a system unit, starting today.",
    "reset": "Reload fixtures/masterdata.json (flush first).",
}


class Command(BaseCommand):
    help = "Apply a scenario to the master data."

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("args", nargs="*")

    def handle(self, *args, **options):
        name = options["name"]
        if name == "list":
            for k, v in SCENARIOS.items():
                self.stdout.write(f"{k:45s} {v}")
            return
        if name == "reset":
            call_command("seed_masterdata", "--reset")
            return
        if name == "decommission_all":
            n = SystemUnit.objects.update(date_end=date.today())
            self.stdout.write(self.style.WARNING(f"{n} system unit(s) decommissioned today"))
            return
        if name == "decommission":
            if len(args) != 1:
                raise CommandError("usage: scenario decommission <UNIT_CODE>")
            unit = SystemUnit.objects.get(code=args[0])
            unit.date_end = date.today()
            unit.save(update_fields=["date_end"])
            self.stdout.write(self.style.WARNING(f"{unit.code} decommissioned on {unit.date_end}"))
            return
        if name == "add_section":
            if len(args) != 2:
                raise CommandError("usage: scenario add_section <UNIT_CODE> <SECTION_CODE>")
            unit = SystemUnit.objects.get(code=args[0])
            section = SectionCategory.objects.get(code=args[1])
            obj, created = SystemUnitToSectionAssignment.objects.get_or_create(
                system_unit=unit, section_category=section, defaults={"date_start": date.today()}
            )
            self.stdout.write(self.style.WARNING(f"{obj} {'created' if created else 'already exists'}"))
            return
        raise CommandError(f"Unknown scenario '{name}'. Run: scenario list")
