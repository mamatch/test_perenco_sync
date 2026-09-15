"""Simplified system reference (systemref) of the MDM.

Hierarchy used by the CMMS synchronisation:

    OrgUnit (site / CMMS body)
      └─ SystemUnit (platform, living quarters…)         ← MDM is the owner
           └─ SectionCategory via SystemUnitToSectionAssignment (ISO 14224 section)
                └─ System (SYS_… asset in the CMMS)     ← CMMS is the owner
                     └─ Equipment via SystemEquipmentAssignment

Equipment and system codes are the CMMS asset codes. The MDM holds no
cross-reference to other systems (IoT historian, SAP): a link to those systems
has to be derived from the codes themselves.

Every system unit of the MDM is synchronised with the CMMS until it is
decommissioned (``date_end``).

There is deliberately NO uniqueness constraint on System.code or Equipment.code:
codes are typed by CMMS key users, and two tenants may reuse the same code.
"""

from django.db import models

from orgref.models import OrgUnit


class SystemUnit(models.Model):
    code = models.CharField(max_length=64, unique=True, help_text="Also the CMMS asset code of the platform")
    name = models.CharField(max_length=255)
    org_unit = models.ForeignKey(OrgUnit, on_delete=models.PROTECT, related_name="system_units", help_text="Site (CMMS body)")
    date_start = models.DateField(null=True, blank=True)
    date_end = models.DateField(null=True, blank=True, help_text="Decommissioning date")
    source = models.CharField(max_length=64, default="MDM")

    def __str__(self):
        return f"{self.code} - {self.name}"


class SectionCategory(models.Model):
    """ISO 14224 inspired section of a platform (Production, Utilities…)."""

    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=255)

    class Meta:
        verbose_name_plural = "Section categories"

    def __str__(self):
        return f"{self.code} - {self.name}"


class SystemUnitToSectionAssignment(models.Model):
    """Which sections exist on a platform. CMMS asset code = '<unit code>_<section code>'."""

    system_unit = models.ForeignKey(SystemUnit, on_delete=models.CASCADE, related_name="section_assignments")
    section_category = models.ForeignKey(SectionCategory, on_delete=models.CASCADE)
    date_start = models.DateField(null=True, blank=True)
    date_end = models.DateField(null=True, blank=True)

    class Meta:
        unique_together = [("system_unit", "section_category")]

    def __str__(self):
        return f"{self.system_unit.code}_{self.section_category.code}"


class SystemClass(models.Model):
    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=255)

    class Meta:
        verbose_name_plural = "System classes"

    def __str__(self):
        return f"{self.code} - {self.name}"


class System(models.Model):
    code = models.CharField(max_length=64, help_text="CMMS asset code (SYS_…). Not unique across tenants!")
    tag = models.CharField(max_length=255, help_text="Functional name")
    system_class = models.ForeignKey(SystemClass, on_delete=models.PROTECT)
    system_unit = models.ForeignKey(SystemUnit, on_delete=models.PROTECT, related_name="systems", help_text="Platform the system belongs to")
    section = models.ForeignKey(SectionCategory, on_delete=models.PROTECT)
    source = models.CharField(max_length=64, help_text="Origin tenant, e.g. GLOBAL or LATAM")
    date_start = models.DateField(null=True, blank=True)
    date_end = models.DateField(null=True, blank=True)

    def __str__(self):
        return self.code


class SystemAttribute(models.Model):
    """Criticality flags: 'SCE' (safety & environmentally critical), 'Production critical'."""

    name = models.CharField(max_length=255, unique=True)

    def __str__(self):
        return self.name


class SystemAttributeAssignment(models.Model):
    system = models.ForeignKey(System, on_delete=models.CASCADE, related_name="attribute_assignments")
    system_attribute = models.ForeignKey(SystemAttribute, on_delete=models.CASCADE)

    class Meta:
        unique_together = [("system", "system_attribute")]

    def __str__(self):
        return f"{self.system.code} · {self.system_attribute.name}"


class EquipmentType(models.Model):
    """Governed reference data (ISO 14224 class and type). The code is the full CMMS
    family code, e.g. ``PU_CE``. Never created by a sync."""

    code = models.CharField(max_length=16, unique=True, help_text="CMMS family code <class>_<type>")
    name = models.CharField(max_length=255)
    sce = models.BooleanField(default=False, help_text="Safety & environmentally critical by default")

    def __str__(self):
        return f"{self.code} - {self.name}"


class Equipment(models.Model):
    code = models.CharField(max_length=64, help_text="CMMS asset code. Not unique across tenants!")
    name = models.CharField(max_length=255, null=True, blank=True)
    equipment_type = models.ForeignKey(EquipmentType, on_delete=models.PROTECT)
    date_start = models.DateField(null=True, blank=True)
    date_end = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "Equipments"

    def __str__(self):
        return self.name or self.code


class SystemEquipmentAssignment(models.Model):
    system = models.ForeignKey(System, on_delete=models.CASCADE, related_name="equipment_assignments")
    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE, related_name="system_assignments")
    assignment_date = models.DateField()

    class Meta:
        unique_together = [("system", "equipment")]

    def __str__(self):
        return f"{self.system.code} -> {self.equipment.code}"
