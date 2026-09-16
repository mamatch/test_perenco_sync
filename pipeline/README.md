# Perenco MDM ⇄ CMMS ⇄ IoT sync -- Part B/C/D implementation

Implements ARCHITECTURE_.md (Part A) against the sandbox: MDM -> CMMS, CMMS -> MDM,
IoT historian -> CMMS meters, plus a run/audit store and a health summary. See
`../DECISIONS.md` for the assumptions and the sandbox observations behind them.

## Run it

From the repository root, with the sandbox up (`make up`, or `make local-cmms` /
`make local-mdm-seed` + `make local-mdm` without Docker):

```bash
cd systemref_lite && uv sync && cd ..   # once: cmms_to_mdm dispatches into this venv
cd pipeline
uv sync
uv run python -m pipeline run-all          # all three integrations, in order
uv run python -m pipeline run mdm-to-cmms  # or one at a time
uv run python -m pipeline run cmms-to-mdm
uv run python -m pipeline run iot-to-cmms
uv run pytest -q                           # unit tests, no server needed
```

Configuration is environment variables, all optional (defaults match the sandbox --
see `pipeline/config.py`): `CMMS_BASE_URL`, `CMMS_TENANT`, `CMMS_API_KEY`,
`SYSTEMREF_DB_PATH`, `SYSTEMREF_LITE_DIR`, `IOT_EXPORTS_DIR`, `AUDIT_DB_PATH`,
`ARCHIVE_RATIO_THRESHOLD` (default `0.10`), `CMMS_RATE_LIMIT_PER_MINUTE` (default `50`).
The MDM active-scope rule (`date_start <= as_of and (date_end is null or date_end > as_of)`)
is fixed, not configurable -- see DECISIONS.md #1.

**Idempotency proof**, exactly as the exercise asks for it:

```bash
curl -s -X POST http://localhost:8080/_admin/PERENCO/reset
uv run python -m pipeline run-all   # first run: writes happen
curl -s http://localhost:8080/_admin/PERENCO/calls   # note "writes"
uv run python -m pipeline run-all   # second run
curl -s http://localhost:8080/_admin/PERENCO/calls   # "writes" unchanged
```

The default 10% archive-ratio threshold blocks every archive candidate on this small
sandbox (see DECISIONS.md #4 for why that's correct, not a bug). To see archives
actually execute end to end: `ARCHIVE_RATIO_THRESHOLD=0.5 uv run python -m pipeline run mdm-to-cmms`.

## What is done

- **Part A**: `../ARCHITECTURE_.md` (not touched by this implementation).
- **Part B**: all three flows, ordering (parent-before-child create, child-before-parent
  archive), the 10% destructive-only safety rail, recursive active-descendant
  protection, the empty-snapshot guard, retry/backoff/rate-limiting against the mock's
  real quirks (429 with `Retry-After`, ~3% 5xx, `Filter` hiding `archived`), and explicit
  rejection reporting for CMMS -> MDM instead of silent fixes. MDM writes (CMMS -> MDM
  direction) go through a Django management command inside `systemref_lite`
  (`apply_sync_plan`, additive-only -- see DECISIONS.md #13), not raw SQL from this
  service; a failed apply rolls back as one transaction and every pending action is
  recorded `FAILED_RETRYABLE`, never a partial write.
- **Part C**: `pipeline/audit.py` (SQLite `runs` / `actions` / `dq_issues` /
  `run_metrics` / `alerts`), a text health summary + alert rules printed after every run
  (`pipeline/observability.py`), one implemented alert condition (archive ratio > 10%)
  plus a few more (rejection-rate spike, counter regressions, unresolved IoT tags).
- **Part D**: tag resolution (equipment-code convention, with a system-class-shorthand
  fallback confirmed against real seed values), unit conversion, daily
  maximum-timestamp selection, counter-regression quarantine, dedupe on
  `(tag_id, timestamp_utc)` across overlapping exports.
- Tests: `pipeline/tests/` covers the active-date rule boundaries, the delta engine
  (including a same-run parent+child archive edge case caught by testing, not
  hypothesised -- see DECISIONS.md #3), and the IoT tag/unit/dedupe/daily-selection
  logic. All pure-Python, no server required.

## What is not done

- No dbt/Snowflake, and Celery orchestration is documented but not stood up here (see
  DECISIONS.md #11/#12 for why, and how the code already maps onto that target).
- No real dashboard: the health summary is text + the SQLite audit tables are meant to
  be queried directly. A production dashboard (Grafana/Metabase on top of the same
  `runs`/`actions`/`dq_issues`/`run_metrics` tables, or their Snowflake equivalent) would
  show, per run: source/target record counts, CREATE/UPDATE/ARCHIVE/NOOP/BLOCKED/REJECTED
  breakdowns (as a stacked bar over the last 30 runs, to spot trend changes), API error
  rate and retry count, run duration and source freshness (time since the last successful
  extraction), the archive ratio against its threshold, and a "data quality" panel listing
  open `dq_issues` grouped by `reason`, since that is the view the business would use to
  answer "who fixes a system without a section?" without reading logs.
- Existing-platform body/site reassignment is reported, not auto-applied (DECISIONS.md
  #11).
- No per-worker/shared rate limiting across multiple concurrent workers -- this is a
  single-process CLI; ARCHITECTURE_.md section 2 already describes the production
  evolution (a concurrency cap on the CMMS-calling Celery queue).
- The dispatch to `apply_sync_plan` is a `subprocess.run(["uv", "run", "manage.py", ...])`
  call, not the Celery task dispatch production would use -- the sandbox-appropriate stand-in
  for "triggering execution inside MDAdmin's process" (DECISIONS.md #13).
