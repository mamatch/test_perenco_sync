from django.contrib import admin

from .models import OrganisationalStructure, OrgUnit


@admin.register(OrganisationalStructure)
class OrganisationalStructureAdmin(admin.ModelAdmin):
    list_display = ("name", "country_code")


@admin.register(OrgUnit)
class OrgUnitAdmin(admin.ModelAdmin):
    list_display = ("name", "organisational_structure", "is_field", "is_active")
    list_filter = ("organisational_structure", "is_field", "is_active")
