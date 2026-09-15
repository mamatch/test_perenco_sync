"""Simplified organisation reference (orgref).

In the real MDM an org unit is linked to several organisational structures with
history. Here one org unit belongs to exactly one structure (a country /
subsidiary), which is enough for the exercise.
"""

from django.db import models


class OrganisationalStructure(models.Model):
    """A subsidiary / country level entity, e.g. 'Gabon E&P'."""

    name = models.CharField(max_length=255, unique=True)
    country_code = models.CharField(max_length=2, help_text="ISO 3166-1 alpha-2")

    def __str__(self):
        return self.name


class OrgUnit(models.Model):
    """An operational unit. Field units are the CMMS 'bodies' (sites)."""

    name = models.CharField(max_length=255, unique=True)
    organisational_structure = models.ForeignKey(OrganisationalStructure, on_delete=models.PROTECT, related_name="org_units")
    is_field = models.BooleanField(default=True, help_text="Operational site (True) vs office / analytical unit (False)")
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name
