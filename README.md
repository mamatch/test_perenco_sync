# Perenco MDM ⇄ CMMS ⇄ IoT sync — submission

The exercise brief is kept as received in **[`INSTRUCTIONS.md`](INSTRUCTIONS.md)**. This README is
organized around that brief's own [Deliverables list](INSTRUCTIONS.md#deliverables), one section per
bullet.

```bash
make up && make sync   # runs the whole thing end to end against a fresh sandbox
```

---

## 1. Repository — how to run, what is done, what is not

### How to run

```bash
make up                                          # Docker: CMMS mock on :8080, MDM on :8000
make sync                                        # all three integrations, in order
curl -s http://localhost:8080/_admin/PERENCO/calls   # check "writes" — run `make sync` again, unchanged
```

Without Docker: `make local-cmms` and `make local-mdm-seed && make local-mdm` in two other shells,
then `make sync`. All tunable values (CMMS connection, thresholds, paths) live in one place, `.env`
(copy `.env.example` to get one) — see **[`pipeline/README.md`](pipeline/README.md)** for every
option, one-integration-at-a-time commands, and the idempotency proof in full.

### What is done

- **Part A**: **[`ARCHITECTURE_.md`](ARCHITECTURE_.md)** — see section 2 below.
- **Part B**: all three flows, ordering (parent-before-child create, child-before-parent archive),
  the 10% destructive-only archive-ratio safety rail, recursive active-descendant protection, the
  empty-snapshot guard, retry/backoff/rate-limiting against the mock's real quirks (429 with
  `Retry-After`, ~3% 5xx, `Filter` hiding `archived`), and explicit rejection reporting for
  CMMS → MDM instead of silent fixes. MDM writes (CMMS → MDM direction) go through a Django
  management command inside `systemref_lite` (`apply_sync_plan`, additive-only — see
  `DECISIONS.md` #10), not raw SQL from this service; a failed apply rolls back as one transaction
  and every pending action is recorded `FAILED_RETRYABLE`, never a partial write.
- **Part C**: a run/audit SQLite store (`runs`/`actions`/`dq_issues`/`run_metrics`/`alerts`), a
  text health summary + alert rules printed after every run, one implemented alert condition
  (archive ratio > 10%) plus a few more (rejection-rate spike, counter regressions, unresolved IoT
  tags).
- **Part D**: tag resolution (equipment-code convention, with a system-class-shorthand fallback
  confirmed against real seed values), unit conversion, daily maximum-timestamp selection,
  counter-regression quarantine, dedupe on `(tag_id, timestamp_utc)` across overlapping exports.

### What is not done

- No dbt/Snowflake, and Celery orchestration is documented but not stood up here (see
  `ARCHITECTURE_.md` section 2 for why, and how the code already maps onto that target).
- No real dashboard: the health summary is text + the SQLite audit tables are meant to be queried
  directly. `pipeline/README.md` sketches what a production dashboard (Grafana/Metabase on the same
  tables) would show.
- Existing-platform body/site reassignment is reported, not auto-applied.
- No per-worker/shared rate limiting across multiple concurrent workers — this is a single-process
  CLI; `ARCHITECTURE_.md` section 2 describes the production evolution.
- The MDM write dispatch is a `subprocess.run(["uv", "run", "manage.py", ...])` call, not the
  Celery task dispatch production would use — the sandbox-appropriate stand-in (`DECISIONS.md` #10).

Full detail on all of the above: **[`pipeline/README.md`](pipeline/README.md)**.

---

## 2. Design document (Part A)

**[`ARCHITECTURE_.md`](ARCHITECTURE_.md)** — target architecture and data flows for the three
integrations, the orchestration choice and why, ownership/conflict resolution, idempotency,
ordering, failure handling, safety rails, security, deployment, and how the design maps onto
Perenco's current Snowflake/dbt/Celery architecture. Section 12 is the list of questions asked
during the clarification call and how each answer shaped the design, as the deliverable asks;
questions the call left open are in `DECISIONS.md` instead. Cross-referenced throughout to the
actual code, so a claim can be checked in one click rather than taken on faith.

---

## 3. Tests where they matter

65 tests total, no server needed: `cd pipeline && uv run pytest -q` (59) and
`cd systemref_lite && uv run pytest -q` (6).

- **Delta computation** — `pipeline/tests/test_canonical.py` (every `compute_plan()` outcome:
  CREATE/UPDATE/UNARCHIVE/NOOP/BLOCKED, the archive-ratio threshold, recursive active-descendant
  blocking, a same-run parent+child archive edge case caught by testing — see `DECISIONS.md` #2);
  `pipeline/tests/test_mdm_to_cmms_run.py` and `test_cmms_to_mdm_run.py` exercise the same logic
  end to end (empty-snapshot guard, failed-parent propagation, governed-reference rejections, the
  disappeared-asset reconciliation pass, the write-plan apply/rollback).
- **Mapping rules** — `pipeline/tests/test_iot.py::test_resolve_tag_*` (the historian tag → CMMS
  asset convention, direct match and the system-class-shorthand fallback); the governed-reference
  lookups (system class, section category, equipment type) are exercised through
  `test_cmms_to_mdm_run.py`.
- **IoT cleaning** — `pipeline/tests/test_iot.py` (dedupe on overlapping exports, unit conversion,
  daily maximum-timestamp selection) and `pipeline/tests/test_iot_to_cmms_run.py` (the
  counter-regression quarantine end to end, idempotent re-send, no-GOOD-reading handling).
- The MDM write path itself (`apply_sync_plan`, including the all-or-nothing transaction rollback
  on an invalid entry) is covered separately in
  `systemref_lite/systemref/tests/test_apply_sync_plan.py`, since it runs through Django's ORM.
- Also tested: the CMMS client's retry/rate-limit/pagination behaviour
  (`pipeline/tests/test_cmms_client.py`), the audit/observability layer
  (`pipeline/tests/test_audit.py`, `test_observability.py`).

---

## 4. `DECISIONS.md`

**[`DECISIONS.md`](DECISIONS.md)** — the assumptions made where the statement was ambiguous, each
checked against real sandbox data rather than left as a guess, and the questions the clarification
call left open that a real handoff would still need answered.
