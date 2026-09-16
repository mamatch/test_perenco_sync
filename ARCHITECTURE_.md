# Part A — Target Architecture

## 1. Objective and design principles

The goal is to maintain a coherent operational asset hierarchy between the MDM and the CMMS, while feeding CMMS running-hour meters from the IoT historian.

The three integrations have explicit data ownership:

- **MDM → CMMS:** MDM is the source of truth for sites, platforms and sections.
- **CMMS → MDM:** CMMS is the source of truth for systems and equipments.
- **IoT → CMMS:** the historian is the source of truth for cumulative running hours.

The design is based on six principles:

1. **Ownership-driven synchronization:** only the owning system can authoritatively change a mastered attribute.
2. **Desired-state reconciliation:** compute a delta before issuing any write; unchanged entities produce `NOOP`.
3. **Idempotent writes:** a replay of the same run is safe and produces no additional writes.
4. **Safety before execution:** destructive actions are planned first and executed only after guardrails pass.
5. **Isolation of failures:** a bad entity or transient API failure must not invalidate unrelated work.
6. **Full auditability:** every write is associated with a `run_id` and a business outcome.
7. **Explicit data-quality outcomes:** invalid parentage, unknown reference data and unresolved IoT assets are rejected/quarantined rather than silently repaired.
8. **No fabricated industrial measurements:** missing or suspicious historian data is surfaced as a data-quality issue instead of being inferred.

---

## 2. Target production architecture

```text
                                 ┌──────────────────────┐
                                 │        MDAdmin        │
                                 │   MDM / PostgreSQL    │
                                 │ sites / platforms /   │
                                 │ sections / reference  │
                                 └──────────┬───────────┘
                                            │
                                      extract / CDC
                                            │
                                            ▼
┌──────────────────────┐             ┌──────────────────────┐
│   IoT historian      │             │ Snowflake / staging  │
│ daily running-hours  │────────────▶│ raw + normalized     │
│ CSV / landing zone   │             │ + reconciliation     │
└──────────┬───────────┘             └──────────┬───────────┘
           │                                    │
           │                                    │ desired/current state
           │                                    ▼
           │                         ┌──────────────────────┐
           │                         │ Reconciliation layer │
           │                         │ Python / dbt logic   │
           │                         │ delta + validation   │
           │                         └──────────┬───────────┘
           │                                    │
           │                              action plan
           │                                    ▼
           │                         ┌──────────────────────┐
           │                         │ Command / audit     │
           │                         │ store               │
           │                         └──────────┬───────────┘
           │                                    │
           │                              dispatch
           │                                    ▼
           │                         ┌──────────────────────┐
           │                         │ Python integration   │
           │                         │ workers              │
           │                         │ retry/rate limit     │
           │                         └───────┬─────┬────────┘
           │                                 │     │
           │                          REST   │     │ DB/domain adapter
           │                                 ▼     ▼
           │                         ┌──────────┐ ┌──────────┐
           └────────────────────────▶│  CMMS    │ │   MDM    │
                                     │ REST API │ │ adapter  │
                                     └──────────┘ └──────────┘

                         Airflow orchestrates the batch,
                    reconciliation tasks and operational checks.
```

### Why Airflow in production

The workload is a nightly batch with explicit dependencies, retries, backfills and a need for operational visibility. Airflow is therefore a better orchestration boundary than embedding the complete workflow in database tasks.

A single DAG would contain three independent task groups:

```text
nightly_sync
├── mdm_to_cmms
│   ├── extract_mdm
│   ├── extract_cmms
│   ├── compute_delta
│   ├── safety_checks
│   └── execute_commands
│
├── cmms_to_mdm
│   ├── extract_cmms
│   ├── validate_reference_data
│   ├── compute_delta
│   └── write_mdm
│
└── iot_to_cmms
    ├── discover_new_exports
    ├── normalize_and_deduplicate
    ├── resolve_assets
    ├── compute_daily_readings
    └── update_meters
```

Airflow is responsible for **when and in which order** work runs. It should not contain the detailed API retry/rate-limit logic. That belongs in the integration worker/client layer.

For higher operational volumes, API actions can be queued and consumed by workers with a shared rate limiter. This avoids having several parallel tasks independently violating the CMMS global limit of 50 requests/minute.

---

## 3. Integration 1 — MDM → CMMS

### Active-scope rule

An MDM object is active when `date_start <= now()` and `date_end` is null or `> now()`. (An earlier transcription of the clarification call had the end-date condition inverted -- `date_end < now()` -- which would have made an object active *after* its decommissioning date; this was a transcription error, confirmed and corrected, not a real ambiguity.) The comparison timezone must be applied consistently across all entities.

### Destructive-action policy

The 10% archive threshold is a **hard stop for destructive actions only**. Therefore a run may continue with non-destructive CREATE/UPDATE operations while ARCHIVE operations are blocked when the threshold is exceeded.

```text
planned actions
      │
      ├── CREATE / UPDATE ────────► eligible for execution
      │
      └── ARCHIVE ──► archive ratio > 10% ?
                           │
                      yes  │  no
                           ▼    ▼
                        BLOCK  EXECUTE
```

The archive safety check is evaluated before any destructive write is sent to the CMMS.


### Responsibility

Synchronize the MDM-owned hierarchy:

```text
Site/Body
  └─ Platform
      └─ Section
```

Sites/bodies already exist in the CMMS and are not created by the connector.

### Flow

```text
MDM snapshot
    │
    ▼
Validate snapshot
    │
    ▼
Build desired state
    │
    ├───────────────┐
    ▼               ▼
Current CMMS     MDM state
    │               │
    └──────┬────────┘
           ▼
      Delta planner
           │
    ┌──────┼───────────┐
    ▼      ▼           ▼
 CREATE  UPDATE     ARCHIVE
    │      │           │
    └──────┼───────────┘
           ▼
      safety checks
           │
           ▼
        executor
```

### Delta rules

For each entity, compare a canonical representation rather than raw database rows.

- **CREATE:** desired entity exists, current CMMS entity does not.
- **UPDATE:** same business identity exists but an MDM-owned attribute differs.
- **UNARCHIVE:** desired entity is active but the CMMS entity is archived.
- **ARCHIVE candidate:** entity exists in CMMS but is outside the active MDM scope.
- **NOOP:** desired and current canonical states are equal.
- **BLOCKED:** an otherwise valid archive cannot be executed safely, for example because active children remain.

### Ordering

Creation follows parent-to-child order:

```text
Platform → Section
```

Any dependent child must only be created once its parent exists.

Archiving follows the reverse order:

```text
Section → Platform
```

and more generally, children must be archived before their parents. A parent with active children is never archived automatically.

### Safety rails

Before any destructive action:

1. the MDM snapshot must be considered valid;
2. an empty source snapshot must not trigger bulk archiving;
3. the archive ratio must stay below the configured 10% threshold;
4. the candidate must have **no active descendant at any depth**;
5. the planned destructive set is frozen before execution.

Active-child detection is recursive. For example, an active Equipment blocks archiving its parent System, which in turn blocks archiving its parent Section and Platform.

Destructive actions are therefore **planned first, validated second, executed last**. A safety failure blocks the destructive subset rather than non-destructive CREATE/UPDATE work, in line with the clarification call.

---

## 4. Integration 2 — CMMS → MDM

This flow is a **reconciliation**, not an upsert-only feed. Disappeared/archived Systems and Equipments in the CMMS are reconciled into the MDM as well as newly observed and changed assets.


### Responsibility

The CMMS owns operational Systems and Equipments. The MDM must reflect them with their:

- platform and section relationship;
- classification;
- criticality;
- validity dates.

### Flow

```text
CMMS assets
    │
    ▼
Normalize into canonical model
    │
    ▼
Resolve platform / section / system parents
    │
    ▼
Validate governed reference data
    │
    ├───────────────┐
    │ valid         │ invalid
    ▼               ▼
Delta planner     REJECTED
    │               │
    ▼               └── audit + data-quality feedback
MDM writes
```

### Reference-data rule


The MDM owns Equipment Types, System Classes and Section Categories. Therefore a CMMS asset referencing an unknown Equipment Type is rejected; the pipeline never creates the missing reference data.

This prevents operational data-quality errors from silently becoming new master data.

### Conflict resolution

Ownership is attribute-level, not just entity-level. For example:

- names of MDM-owned entities follow the MDM;
- Systems and Equipments are mastered by the CMMS;
- governed classifications are mastered by the MDM.

A canonical model should be used so that comparisons ignore technical metadata (`id`, timestamps generated by the database, etc.) and focus on business attributes.

Validation must also enforce the hierarchy, not only parent existence: an Equipment must resolve to a System, and a System must resolve to a valid Section/Platform path. Records with an unknown parent, invalid parent type, missing required parent, or unknown governed reference data are rejected and surfaced as data-quality issues.

Observed data cases in the sandbox include an orphan Equipment, an invalid System parent, and unknown Equipment Type codes. These are treated as explicit rejection/quarantine scenarios rather than automatically creating missing master data.

---

## 5. Integration 3 — IoT → CMMS

The historian is authoritative for cumulative running hours.

```text
Daily CSV exports
      │
      ▼
Parse + normalize
      │
      ▼
Deduplicate on (tag_id, timestamp_utc)
      │
      ▼
Identify RUN_HRS measurements
      │
      ▼
Resolve historian tag → CMMS asset
      │
      ▼
Filter to GOOD measurements
      │
      ▼
Group by asset + UTC day
      │
      ▼
Select row with MAX timestamp
      │
      ▼
Detect counter regression
      │
      ├── normal ─────────────► MeterUpdate
      └── regression ─────────► quarantine/audit, no automatic update
```

### Mapping

The clarified mapping is deterministic: the historian `tag_id` is derived from the country code and the equipment code, with the equipment identifier represented in the tag convention, followed by `.RUN_HRS`. The transformation must be implemented as a small, unit-tested parsing function and validated against real CSV examples.

### Daily value

The confirmed business rule is **the value associated with the maximum timestamp for the day**, not the maximum numeric value.

### Duplicate exports

The exports overlap in time, so the stable row identity is `(tag_id, timestamp_utc)`. File-level checkpointing can optimize processing, but it is not the correctness mechanism; row-level deduplication must make replay safe.

### Counter resets and missing GOOD readings

The business leaves these cases to engineering judgment. The proposed policy is conservative:

- if the cumulative counter decreases for a RUN_HRS series, classify it as a **counter-reset anomaly**, do not infer a new cumulative value and do not send that suspicious point automatically;
- if a day has no `GOOD` reading, do not fabricate or carry forward a value; produce a data-quality event and leave the CMMS meter unchanged.

This favours data integrity over silent interpolation and isolates one bad machine/day from the rest of the batch.

Non-running-hours tags such as pressure measurements must never enter the MeterUpdate flow simply because they are present in the historian export.

## 6. Idempotency and state management

The central rule is: **compute desired state, compare with current state, write only the delta**.

This guarantees the required zero-write second run:

```text
Run 1:
  desired != current
  → writes

Run 2:
  desired == current
  → NOOP everywhere
```

Each execution has a `run_id`. Planned actions are stored with their status and outcome. For operations where the external API may time out after the server has committed the write, the client must re-read or use a business key before creating again; blindly retrying a non-idempotent POST is unsafe.

A durable command/audit store also provides replayability: failed commands can be retried without recomputing the entire world, provided the reconciliation state is still valid. The command record should carry the business key and intended state so the executor can re-read the target when a timeout occurs after an unknown write outcome.

---

## 7. Failure handling

### Retryable failures

Retry:

- HTTP 429: respect `Retry-After`;
- HTTP 500 / 503;
- network timeouts / connection errors.

Use exponential backoff with jitter and a maximum retry count. The global CMMS limit must be respected across all workers, not independently per task instance.

### Non-retryable failures

Do not automatically retry:

- 401 authentication failures;
- 404 caused by a genuine missing asset, until the discrepancy is understood;
- 406 business validation errors.

The body of a 406 response is recorded in the audit and exposed as a data-quality/integration issue.

### Partial failures

A batch is not transactional across CMMS, MDM and the warehouse. Therefore the design accepts partial success:

```text
100 planned writes
70 SUCCESS
20 REJECTED
10 FAILED_RETRYABLE
```

The run remains replayable. The next run or a targeted replay retries only what is still outstanding.

### Poison messages

An action that repeatedly fails for a deterministic reason (for example an invalid parent or unknown reference code) should stop consuming retry capacity after the configured retry cap and move to a **dead-letter / rejected state** with the exact reason.

Poison messages must not block unrelated commands.

### Replay

Replay operates on durable commands/audit records, not on mutable in-memory state. A replay uses the same idempotent executor and safety checks as a normal run.

---

## 8. Observability and audit

Every run gets a `run_id` and every write records at least:

```text
run_id
source_system
target_system
entity_type
entity_code
action
request/result status
timestamp
retry_count
error / validation reason
```

Operational metrics should include:

- source and target record counts;
- CREATE / UPDATE / ARCHIVE / NOOP / BLOCKED / REJECTED counts;
- API calls, 429s and 5xxs;
- retry counts;
- run duration;
- source freshness;
- archive ratio.

A useful alert is a destructive-action anomaly such as archive ratio exceeding the configured threshold.

---

## 9. Security

### Secrets

The CMMS API key is a secret. In production it should be stored in Azure Key Vault (or an equivalent managed secret store) and injected at runtime; it must never live in source code, Git history, notebooks or logs.

### Least privilege

Separate identities/permissions should be used for:

- read access to source data;
- write access to MDM mastered entities;
- CMMS connector access;
- warehouse access;
- orchestration.

The API credential should only have the connector permissions required by this integration.

### Logging

Never log API keys, authorization headers or full sensitive payloads. Audit records should contain enough information to explain an action without becoming a secret store.

---

## 10. Deployment and CI/CD

I would use three environments:

```text
DEV → TEST → PROD
```

The same code and configuration structure should be promoted through environments; only external endpoints, credentials, thresholds and schedules differ.

CI should run:

```text
lint / format
unit tests
integration tests against mock CMMS
reconciliation idempotency test
safety-rail tests
container build
```

Deployment should be automated through the existing GitLab CI/CD or equivalent pipeline. The production deployment should use immutable/containerized artifacts and environment-managed secrets/configuration.

---

## 11. Position versus the current Snowflake / dbt / Celery architecture

The current architecture is a valid batch-oriented design: Airbyte ingests the MDM, Snowflake/dbt performs reconciliation, Snowflake Tasks orchestrate the delta flow, Python UDFs call the CMMS and Celery completes the MDM-side import.

I would **keep the warehouse-centric parts for data transformation, history and reconciliation**, but move the operational API integration out of Snowflake into a dedicated Python integration component.

### What I would keep

- **Snowflake + dbt:** raw/staging models, canonical models, historical data, reconciliation logic where SQL is the right abstraction.
- **MDM PostgreSQL:** system of record for MDM-owned data.
- **Key Vault:** secret management.
- **Audit data:** durable, queryable operational history.

### What I would change

- Replace Snowflake Python UDF-based API calls with a Python integration worker.
- Use Airflow as the production orchestrator for scheduling, dependency management, retries, backfills and operational visibility.
- Use a command/audit store as the boundary between planning and execution.

### Trade-offs

**Advantages:** clearer separation between analytics and operational integration, better control of API rate limits and retries, easier local testing, simpler replay and better observability of external-system failures.

**Costs:** one more deployable component and some additional infrastructure compared with keeping everything in Snowflake Tasks/UDFs.

For the exercise sandbox, I would deliberately keep the implementation simpler and use DuckDB for analytical/reconciliation logic. The architectural boundary remains the same, so the prototype can be evolved toward the production Snowflake/Airflow setup without rewriting the business logic.

---

## 12. Questions asked during the clarification call, and how the answers shaped the design

The statement is deliberately incomplete in several places; these are the questions I brought to the call, the answer confirmed, and the concrete effect each answer had on the design below. (Questions the call did **not** fully resolve, and assumptions made in their absence, are in `DECISIONS.md`.)

1. **Q: The statement doesn't define "active" for a platform/section. Given `date_start`/`date_end` on `SystemUnit`, what exact rule puts an entity in scope?**
   A: `date_start <= now()` and (`date_end` is null or `date_end > now()`), one consistent timezone. (An earlier transcription of the call had the end-date condition inverted -- confirmed as a transcription error, not the real rule.)
   Impact: this single predicate gates every CREATE/ARCHIVE decision in Integration 1 -- a sign error here would silently invert which platforms are in scope, so it's centralised in one function (`is_active()`) and boundary-tested rather than inlined at each call site.

2. **Q: Is the 10% archive-ratio guardrail a hard stop on the whole run, or only on destructive (archive) actions?**
   A: destructive actions only; CREATE/UPDATE work continues.
   Impact: shaped the safety-rail architecture directly -- the plan is computed in full first, then only the ARCHIVE subset is filtered by the ratio check, so a batch that's mostly legitimate creates/updates is never held hostage by a handful of stale archive candidates.

3. **Q: "Never archive something that still has active children" -- is that check one level deep (immediate children), or does it need to look further down the hierarchy?**
   A: recursive, any depth.
   Impact: required pulling the *full* active asset tree (every family, not just PLATFORM/SECTION) so an active Equipment several hops down a Section still blocks archiving the Platform above it -- and, found by testing rather than by the call, required resolving candidates deepest-first so a platform and its only section can still be archived together in the same run (see `DECISIONS.md` #3).

4. **Q: Is the CMMS → MDM direction an upsert-only feed, or does it need to reconcile Systems/Equipments that disappeared or got archived in the CMMS?**
   A: full reconciliation.
   Impact: added the "disappeared from the CMMS" pass that decommissions (`date_end`) any MDM System/Equipment no longer reported by the CMMS at all -- a plain upsert loop would have left stale rows open forever.

5. **Q: The historian tag convention (`<country>-<platform>.<suffix>.RUN_HRS`) is given as one example, not a formal grammar -- what exactly determines the target asset?**
   A: a deterministic tag-to-equipment-code convention based on country code + equipment code.
   Impact: became the primary branch of the tag-resolution function (`<platform>-<suffix>` as a direct CMMS asset code). The one case that convention alone doesn't cover -- a platform's single aggregate system addressed by class shorthand instead of an individual equipment tag -- wasn't something the call anticipated either; it was found by matching real historian values against a pre-existing CMMS meter (`DECISIONS.md` #8), which is exactly the kind of gap this call format is meant to surface early but didn't catch here.

6. **Q: When several readings exist for the same asset/day, is the one to keep the maximum *value*, or the one at the latest *timestamp*?**
   A: the reading at the maximum timestamp.
   Impact: directly shaped `daily_max_timestamp_readings()` -- the intuitive-but-wrong implementation (max value) would have silently accepted a spurious high outlier over the actual latest sensor reading.

7. **Q: What should happen when a counter appears to decrease, or no GOOD reading exists for a day -- repair it automatically, or leave it to engineering judgement?**
   A: engineering judgement; the business did not mandate an automatic fix.
   Impact: led to the conservative quarantine policy (never infer a new baseline, never fabricate a value), which then held up against two independent real cases found in the sandbox: an inflated CMMS seed value that would otherwise have masked genuine data, and a real counter reset that needs a human to acknowledge before the baseline can move again.

These answers are recorded again, alongside the sandbox evidence for each, in `DECISIONS.md`.
