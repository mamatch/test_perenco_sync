> ## Candidate solution — start here
>
> This is the original exercise brief, kept as received below. My submission:
>
> - **[`ARCHITECTURE_.md`](ARCHITECTURE_.md)** — Part A, including the clarification-call
>   questions and how each answer shaped the design (section 12).
> - **[`pipeline/`](pipeline/README.md)** — Parts B/C/D: the working, tested sync. Run it with
>   `make sync` (see below) or `cd pipeline && uv run python -m pipeline run-all`.
> - **[`DECISIONS.md`](DECISIONS.md)** — assumptions made against real sandbox data, and the
>   questions the clarification call left open.
>
> `make up && make sync` runs the whole thing end to end against a fresh sandbox.

---

# Data Engineering Architect — technical exercise

**Perenco Data Office · MDM ⇄ CMMS synchronisation**

You are asked to design and build the synchronisation between our master data repository (MDM),
our maintenance system (CMMS, DIMO Maint MX) and an IoT historian, on a sandbox that reproduces the
real systems. We are as interested in your **architecture and reasoning** as in the code.

## How it works

| Step | When | What |
|---|---|---|
| 1. Read | Day 0 | You receive this repository. Read everything in `docs/`, start the sandbox, explore the data. |
| 2. Clarification call | Day 1 (30 min) | Ask us anything about the business, the systems, the constraints. The statement is deliberately incomplete in places: finding the right questions is part of the exercise. |
| 3. Build | Days 2–3 | Deliver your work (see *Deliverables*). |
| 4. Debrief | after | 75 min: 15 min presentation by you; 20 min where we run your pipeline on a fresh sandbox with events of our choosing; 20 min where we ask you to change something in your pipeline live, with us watching; 20 min of questions. |

## The sandbox (Docker)

Prerequisites: Docker with Compose. Python 3.12 + [uv](https://docs.astral.sh/uv/) recommended for your own code.

```bash
docker compose up -d --build         # CMMS mock on :8080, MDM on :8000 (MDM_PORT=8001 docker compose up -d to change)
open http://localhost:8080/docs      # CMMS connector API (Swagger UI)
open http://localhost:8000/admin/    # MDM admin (admin / admin)
ls mdm_data/                         # the MDM SQLite database, readable/writable from your machine
ls iot_historian/exports/            # daily historian CSV exports
docker compose down -v && rm -rf mdm_data   # fresh start
```

Documentation, in reading order:

1. [`docs/01_context.md`](docs/01_context.md) — the systems and our current production architecture
2. [`docs/02_business_rules.md`](docs/02_business_rules.md) — ownership, scope and mapping rules
3. [`docs/03_cmms_api.md`](docs/03_cmms_api.md) — the CMMS connector API and its quirks (`docs/openapi.json`)
4. [`docs/04_mdm_and_iot.md`](docs/04_mdm_and_iot.md) — the MDM data model and the IoT exports
5. [`docs/05_warehouse_hints.md`](docs/05_warehouse_hints.md) — DuckDB or Snowflake

## What we ask you to build

Work in your own repository (or a folder next to this one). Your pipeline may be written in Python
and SQL, with DuckDB or a Snowflake trial account as the warehouse, dbt if you like. Keep it runnable
with one command against a fresh sandbox.

### Part A — Architecture (must have)

A design document (Markdown, 3 to 6 pages, diagrams welcome) covering:

* target architecture and data flows for the three integrations (MDM → CMMS, CMMS → MDM, IoT → CMMS),
  with the orchestration you would use in production and why;
* data ownership and conflict resolution, idempotency, ordering (parents before children when
  creating, children before parents when archiving);
* failure handling: retries, rate limiting, partial failures, poison messages, replay;
* safety rails against destructive runs (what would you refuse to do automatically?);
* security (API keys, secrets, least privilege) and deployment (CI/CD, environments);
* how your design maps onto, or departs from, our current Snowflake / dbt / Celery architecture
  (`docs/01_context.md`) — be candid about trade-offs.

### Part B — Bidirectional synchronisation (must have)

Working code that, against the sandbox:

1. **MDM → CMMS**: creates the missing platforms and sections, archives the ones that
   left the scope, reports the discrepancies it decides not to fix automatically (bodies, orphans
   with children, suspicious cases).
2. **CMMS → MDM**: upserts systems and equipments into `mdm_data/systemref.sqlite3` with their
   class/type, section, platform, criticality and validity, and reports what it rejects.
3. Is **idempotent**: a second run right after the first produces zero writes
   (`GET /_admin/PERENCO/calls` shows `writes`).
4. Survives the API as configured: rate limit, transient 5xx, pagination.

### Part C — Observability (differentiating)

We operate this pipeline every night, and it has bitten us before. Show us how you would know it is
healthy:

* a run/audit model: every run has an id, every action (API call, MDM write, rejected row) is
  traceable to it, with enough context to replay or explain it;
* metrics you would compute per run (volumes, creations, archives, rejects, API error rate,
  duration, freshness) and **alert rules** with thresholds — implement at least the metrics and one
  alert condition (e.g. "the run wants to archive more than N % of the active platforms");
* a health summary at the end of a run (text or table is fine) and a sketch of the dashboard you
  would give to the operations team;
* how you would surface data-quality issues to the business owners (who fixes a system without a
  section? a typo in a criticality label?).

### Part D — Running hours from the IoT historian (differentiating)

The maintenance team wants the running-hours counters of the rotating machines in the CMMS, so that
preventive maintenance can be triggered on hours rather than on calendar. The historian exports are
in `iot_historian/exports/`. Build the flow that pushes them into the CMMS meters
(`Asset/MeterUpdate`), and explain:

* how you link a historian tag to a CMMS asset, and what you do when you cannot;
* how you handle overlapping files, bad quality, units, counter resets, and readings the CMMS
  refuses;
* what cadence you send at, and why.

### Optional stretch (pick at most one, only if you have time)

* dbt models and tests for the transformation layer;
* the same pipeline on a Snowflake trial account with a TASK graph;
* SCD2 history of systems/equipments in the warehouse.

## Deliverables

* Your repository (link or archive) with a README: how to run, what is done, what is not.
* The design document (Part A), including the list of questions you asked us and how the answers
  changed your design.
* Tests where they matter (delta computation, mapping rules, IoT cleaning).
* A short `DECISIONS.md`: the assumptions you made where the statement was ambiguous, and the
  questions you would still want answered.

Timebox: about two working days. **Prioritise**: a complete Part A and B with a thin but honest
Part C beats four half-finished parts. Say what you cut and why.

## Rules of the game

* Your pipeline must only use the connector endpoints with the API key. `/_admin/*` is for you and
  for us to inspect and reset the sandbox, never for the pipeline.
* Do not modify the sandbox code (`mock_gmao`, `systemref_lite`) — if you find a bug, tell us, it is
  worth points.
* Use whatever libraries and AI assistants you like. The debrief is where it counts: we will ask you
  to explain and modify your own code live, and to justify each decision against the data you found
  in the sandbox. Code you cannot explain counts against you.
* The statement is incomplete on purpose. Several situations in the data are not covered by the
  rules above; deciding what to do with them, and telling us, is the job.
* Everything in this repository is fictional data.

## How we evaluate

| Area | Weight | What we look at |
|---|---|---|
| Architecture & reasoning | 30 % | Clarity, trade-offs, fit with our constraints, safety rails, honesty about limits |
| Sync correctness & robustness | 30 % | Rules applied, ordering, idempotency, resilience to the API, data-quality reporting |
| Observability | 15 % | Traceability model, metrics, alert conditions, what you would show operations |
| IoT flow | 15 % | Routing through the MDM, data cleaning, respect of the meter semantics |
| Code & delivery | 10 % | Readability, tests, reproducibility, README, decisions log |

Good luck — and ask questions.
