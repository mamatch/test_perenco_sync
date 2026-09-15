from django.contrib import admin

from . import models as m


class ReadableAdmin(admin.ModelAdmin):
    list_per_page = 100


@admin.register(m.SectionCategory, m.SystemClass, m.SystemAttribute)
class SimpleAdmin(ReadableAdmin):
    pass


@admin.register(m.SystemUnit)
class SystemUnitAdmin(ReadableAdmin):
    list_display = ("code", "name", "org_unit", "date_start", "date_end")
    list_filter = ("org_unit",)
    search_fields = ("code", "name")


@admin.register(m.SystemUnitToSectionAssignment)
class SystemUnitToSectionAssignmentAdmin(ReadableAdmin):
    list_display = ("system_unit", "section_category", "date_start", "date_end")
    list_filter = ("section_category",)


@admin.register(m.System)
class SystemAdmin(ReadableAdmin):
    list_display = ("code", "tag", "system_unit", "section", "system_class", "source", "date_start", "date_end")
    list_filter = ("source", "system_unit", "system_class", "section")
    search_fields = ("code", "tag")


@admin.register(m.SystemAttributeAssignment, m.SystemEquipmentAssignment)
class AssignmentAdmin(ReadableAdmin):
    pass


@admin.register(m.EquipmentType)
class EquipmentTypeAdmin(ReadableAdmin):
    list_display = ("code", "name", "sce")
    search_fields = ("code", "name")


@admin.register(m.Equipment)
class EquipmentAdmin(ReadableAdmin):
    list_display = ("code", "name", "equipment_type", "date_start", "date_end")
    search_fields = ("code", "name")
