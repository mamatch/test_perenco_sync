# 4. The MDM (systemref-lite) and the IoT historian exports

## systemref-lite

A Django application with the simplified MDM data model, on SQLite.

* Admin UI: `http://localhost:8000/admin/` (user `admin`, password `admin`) to browse and edit data.
* Database file: `./mdm_data/systemref.sqlite3` on your machine (bind mount). This is the
  application's own database, the equivalent of the PostgreSQL instance behind MDAdmin in production.
  Any SQLite-capable tool can open it.
* Table names follow Django's convention `<app>_<model>` in lowercase, e.g. `systemref_systemunit`,
  `systemref_systemunitattributeassignment`, `orgref_orgunit`.
* Writing back: your pipeline must write systems and equipments into this database, respecting the
  model (foreign keys, unique constraints). How you do it is part of your design. SQLite takes one
  writer at a time; the Django dev server only reads unless someone uses the admin.
* Scenarios for testing: `docker compose exec mdm python manage.py scenario list`.

### Data model (see `systemref_lite/systemref/models.py` for the details)

```
orgref_organisationalstructure  (country / subsidiary)
orgref_orgunit                  (site; is_field, is_active)                 ── CMMS body
systemref_systemunit            (platform; code, org_unit, date_start, date_end)
systemref_systemunittosectionassignment (unit × section category, validity window)
systemref_sectioncategory       (PROD, UTIL, SAFE, …)
systemref_system                (code, tag, system_unit, section, system_class, source, date_start, date_end)
systemref_systemattributeassignment     (criticality flags SCE / Production critical)
systemref_equipmenttype         (governed reference; code = CMMS family code <class>_<type>, sce)
systemref_equipment             (code, equipment_type, date_start, date_end)
systemref_systemequipmentassignment     (which system an equipment belongs to)
systemref_equipmentexternalreference / systemref_systemexternalreference
                                (identifiers of MDM objects in other systems)
```

Pre-loaded content: 8 system units, a handful of systems and equipments from a previous import, the
equipment type reference, and various cross-references. Explore the admin.

## IoT historian exports

Folder `iot_historian/exports/`, one file per day, `runhours_YYYY-MM-DD.csv`, exported at 01:30 UTC,
covering the previous 36 hours (so two consecutive files overlap by 12 hours). Rows are not sorted.

| Column | Meaning |
|---|---|
| `tag_id` | Historian tag, e.g. `GA-OBA.K-101A.RUN_HRS`. The part before the last dot identifies the machine in the historian. |
| `timestamp_utc` | Hourly sample time (ISO 8601, UTC). |
| `value` | Cumulative counter value at that time. |
| `unit` | `h` for hours; other units exist. |
| `quality` | `GOOD` or `BAD` (sensor / network issue). |
| `exported_at_utc` | When the file was produced. |

The historian knows nothing about the CMMS: its tags follow the instrumentation naming of each field,
and one export mixes every field of the group. On the CMMS side, the running hours of an asset are the
meter named `Running hours` (unit `H`), updated with `Asset/MeterUpdate`. Finding out how to get from
a tag to an asset is part of the exercise.
