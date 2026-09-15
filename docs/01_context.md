# 1. Context — the systems you are integrating

Perenco is an independent oil and gas operator. Its Data Office runs the integration between the
**master data repository (MDM)** and the **CMMS** (computerised maintenance management system,
*GMAO* in French). This exercise is a simplified but faithful replica of that integration.

## The three systems

| System | Role | What you get in the sandbox |
|---|---|---|
| **MDAdmin / systemref** (MDM) | Django application, PostgreSQL in production. Owns the *reference* hierarchy: sites, platforms, sections. Also stores the systems and equipments imported from the CMMS. | `systemref_lite`: a simplified version of the data model on SQLite, with the Django admin. |
| **DIMO Maint MX** (CMMS) | SaaS, tenant `PERENCO`. Owns the *operational* hierarchy: systems and equipments, their state, criticality, meters. Exposes a REST "connector" API with an API key. | `mock_gmao`: a mock of the connector API with the same shapes, quirks and error codes. |
| **IoT historian** | Time-series platform on the field networks. Records cumulative running-hours counters for rotating machines. Exports a CSV file per day into a landing zone. | `iot_historian/exports/*.csv`. |

## The current production architecture (for information)

You are free to propose something different, but you should understand where we start from.

```
                ┌────────────┐   Airbyte    ┌──────────────────────────────┐
  MDAdmin ──────┤ PostgreSQL ├─────────────▶│ Snowflake  RAW / staging      │
  (Django)      └────────────┘              │   dbt Cloud → ANALYTICS marts │
     ▲                                      │   Snowflake TASK DAG:         │
     │ Celery task                          │   compare MDM vs CMMS,        │
     │ (upsert systems/equipments           │   queue POST/PATCH, call the  │
     │  from Snowflake marts)               │   CMMS via Python UDFs        │
     │                                      └──────────────┬───────────────┘
     │                                                     │ REST (X-API-Key)
     │                                                     ▼
     │                                      ┌──────────────────────────────┐
     └──────────────────────────────────────┤ DIMO Maint MX                │
                                            └──────────────────────────────┘
```

* A Celery `chain` in MDAdmin orchestrates 5 steps: Airbyte (MDM → Snowflake), dbt "master",
  Snowflake task graph (MDM → CMMS deltas + API calls), dbt "gmao" (CMMS → marts), import into
  MDAdmin (Snowflake → PostgreSQL).
* Everything the pipeline does against the CMMS is written to an audit table (`SYNC_AUDIT_LOG`):
  the rows with an API action and no response code form the *queue* consumed by the push step,
  retried on subsequent runs up to a cap.
* Safety rails were added after incidents: refuse to run when the MDM extraction is empty,
  refuse to archive more than 10 % of the active assets in one run, never archive a section that
  still has active children.
* Secrets live in an Azure Key Vault; Snowflake reads them through a UDF.

You do **not** need Snowflake, dbt Cloud, Airbyte or Celery to complete this exercise. You need to
show that you can design and build the equivalent, and explain how it would map onto (or improve)
the architecture above.
