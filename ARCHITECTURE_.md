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

![Architecture](./images/architecture.png)

### Why Celery, not a new orchestrator

MDAdmin already runs a Celery chain in production (`docs/01_context.md`) -- Celery is Perenco's existing mechanism for scheduled/background work, not something this design introduces. Assuming, as this design does, that Celery serves other jobs in MDAdmin beyond this one chain, the lowest-risk move is to repoint the existing chain rather than add a second "how do we schedule background work" mechanism (Airflow) purely for this integration. If that assumption doesn't hold, the calculus changes -- see `DECISIONS.md`.

A Celery `group` of three independent tasks replaces the chain's current five sequential steps:

```text
nightly_sync (Celery group, triggered by Beat)
├── mdm_to_cmms_task    → pipeline.mdm_to_cmms.run()
├── cmms_to_mdm_task    → pipeline.cmms_to_mdm.run()
└── iot_to_cmms_task    → pipeline.iot_to_cmms.run()
```

No edges between them: none has a hard ordering dependency on another (each owns a disjoint slice of state -- platforms/sections, systems/equipments, meters), unlike the old chain's five sequential steps.

On Kubernetes (AKS), three details make this work with the platform instead of against it:

- **Beat's schedule state lives in Redis, not on local disk.** The default file-based scheduler (`celerybeat-schedule`) is lost on every pod restart (ephemeral filesystem); `celery-redbeat` stores it in the existing broker instead, so a rescheduled Beat pod doesn't miss or duplicate the nightly trigger.
- **Workers autoscale 0→N with KEDA**, on its Redis queue-length scaler, rather than an always-on worker deployment sized for a job that only actually runs ~2h a night.
- **The CMMS-calling queue is capped at one concurrent replica.** Celery's per-task `rate_limit` is enforced per worker, not globally across the fleet -- at this volume (50 req/min budget, three tasks a night) capping concurrency to one is simpler and sufficient, without needing a distributed token bucket.

Celery is responsible for **when and in which order** work runs. It should not contain the detailed API retry/rate-limit logic -- that belongs in the sync service's own client layer, as it already does in the `pipeline/` prototype (`clients/cmms.py`).

---

## 3. Integration 1 — MDM → CMMS

### Active-scope rule

An MDM object is active when `date_start <= now()` and `date_end` is null or `> now()`. (An earlier transcription of the clarification call had the end-date condition inverted -- `date_end < now()` -- which would have made an object active *after* its decommissioning date; this was a transcription error, confirmed and corrected, not a real ambiguity.) The comparison timezone must be applied consistently across all entities.

### Destructive-action policy

The 10% archive threshold is a **hard stop for destructive actions only**. Therefore a run may continue with non-destructive CREATE/UPDATE operations while ARCHIVE operations are blocked when the threshold is exceeded.

![Destructive-action policy](./images/destruction_policy.png)

The ratio is evaluated at **two scopes, not one**: globally across the tenant, and per body/site. A single large but legitimate site closure could exceed 10% of the whole tenant while being entirely valid; conversely, data corruption confined to one small body could stay under a global 10% while still being wrong for that body specifically. Either scope breaching its threshold blocks the archive subset it covers; non-destructive work is unaffected either way.

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

![MDM to CMMS](./images/mdm_to_cmms.png)

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

![CMMS to MDM](./images/cmms_to_mdm.png)
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

![IOT to CMMS](./images/iot_to_cmms.png)

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

Concretely, a create call must not treat every "already exists" response the same way: if it comes back for the exact code just submitted, on the first create attempt for that code this run, the client re-reads the asset to confirm it matches the intended state before deciding REJECTED versus SUCCESS/NOOP. A create that fails with "already exists" right after a network timeout usually means the *previous* attempt committed, not that there is a genuine naming conflict; the next run would self-correct once it recomputes desired state either way, but the current run's audit would misreport an idempotent success as a failure without this check.

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

I would **keep the warehouse-centric parts for analytics and history, keep Celery as the orchestrator, and move the operational reconciliation and API integration out of Snowflake into a dedicated Python sync service.**

### What I would keep

- **Celery:** already Perenco's mechanism for scheduled/background work in MDAdmin (assumed, for this design, to serve other jobs there too -- otherwise see the trade-off below). Repointed at three new, independent tasks instead of the current five-step chain.
- **Snowflake + dbt:** moved fully downstream, out of the operational path -- raw/staging models, canonical models, historical data (SCD2), analytics. Fed by Airbyte replicating two Postgres sources (MDM, the sync service's own audit/command store), not by the sync itself.
- **MDM PostgreSQL:** system of record for MDM-owned data, read via a dedicated read replica rather than the primary.
- **Key Vault:** secret management, accessed via managed identity rather than a stored credential.
- **Audit data:** durable, queryable operational history -- now the sync service's own tables (`runs`/`actions`/`dq_issues`/`run_metrics`), independent of whatever triggers a run.

### What I would change

- Replace Snowflake Python UDF-based API calls with a dedicated Python sync service (functional core + I/O shell, the same shape as the `pipeline/` sandbox prototype).
- Repoint the existing Celery chain at this service's three independent task groups, rather than introducing a second orchestration mechanism purely for this integration.
- Use a command/audit store (Postgres) as the boundary between planning and execution, decoupled from Snowflake so the sync never waits on the warehouse.
- Route MDM writes (CMMS → MDM direction) through a Django management command inside MDAdmin's own process rather than writing into MDM's tables directly from the sync service -- preserves any model-level validation/signals MDAdmin's ORM would otherwise bypass, and mirrors the pattern the current Celery import step already uses. **Implemented, not just designed**: `systemref_lite/systemref/management/commands/apply_sync_plan.py` applies the plan `pipeline/pipeline/cmms_to_mdm.py` computes, inside one `transaction.atomic()`; the sandbox dispatches it with a `manage.py` subprocess call (`clients/mdadmin.py`) as the local stand-in for the Celery task dispatch production would use (`DECISIONS.md` #13).

### Trade-offs

**Advantages:** clearer separation between analytics and operational integration, better control of API rate limits and retries, easier local testing, simpler replay, better observability of external-system failures, and no new orchestration technology to introduce or operate.

**Costs:** one more deployable component (the sync service itself) versus keeping everything in Snowflake Tasks/UDFs. Celery on Kubernetes also needs three specific things to work with the platform rather than against it: a Redis-backed Beat schedule (`celery-redbeat`, not the default file-based one, which loses state on pod restart), KEDA-based worker autoscaling (0→N on queue depth, so a job that runs ~2h a night doesn't pay for always-on workers), and a concurrency cap on the CMMS-calling queue (Celery's `rate_limit` is enforced per worker, not globally across the fleet).

**The one assumption this rests on:** that Celery already serves purposes in MDAdmin beyond this one chain. `DECISIONS.md` records the alternative if it doesn't -- Azure Container Apps Jobs on a cron trigger, no broker/worker/Beat infrastructure to operate at all, at the cost of native cross-task dependency management if requirements ever grow past three independent branches.

For the exercise sandbox, I would deliberately keep the implementation simpler and use DuckDB for analytical/reconciliation logic. The architectural boundary remains the same, so the prototype can be evolved toward the production Celery/Snowflake setup without rewriting the business logic.

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
