"""Load (or reload) the exercise master data.

    uv run manage.py seed_masterdata          # load fixtures/masterdata.json (idempotent: same pks)
    uv run manage.py seed_masterdata --reset  # flush everything first
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Load the exercise master data fixture into the SQLite database."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Flush the database before loading")

    def handle(self, *args, **options):
        if options["reset"]:
            call_command("flush", "--no-input")
        call_command("loaddata", "masterdata.json")
        from systemref.models import Equipment, System, SystemUnit

        self.stdout.write(
            self.style.SUCCESS(
                f"Master data loaded: {SystemUnit.objects.count()} system units, "
                f"{System.objects.count()} systems, {Equipment.objects.count()} equipments"
            )
        )
