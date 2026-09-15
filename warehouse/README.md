# warehouse — dbt on DuckDB (optional stretch)

Proof of concept answering "how would dbt/DuckDB fit in?" from ARCHITECTURE_.md
section 11 and `DECISIONS.md` #11: the delta-computation logic that lives in
`pipeline/pipeline/canonical.py` and `iot.py` (pure Python, unit-tested), expressed
instead as dbt models on DuckDB. **This does not replace `pipeline/`** -- that
remains the actual submitted, tested Part B/C/D, and is what actually calls the
CMMS/MDM write APIs. This project only proves out the *read/transform* half of the
production design already sketched in ARCHITECTURE_.md's diagram (Snowflake/staging
→ reconciliation layer → command/audit store).

## Run it

With the sandbox up (`make up`, or the local `make local-cmms` + `make local-mdm-seed`
route from the repo root):

```bash
cd warehouse
uv sync
uv run python extract.py          # the only HTTP call / sqlite3 read in this project
uv run dbt build --profiles-dir .  # runs every model, then every test
```

Inspect the results directly:

```bash
uv run python -c "
import duckdb
con = duckdb.connect('warehouse.duckdb')
print(con.execute('select action, count(*) from mdm_cmms_delta group by 1').fetchall())
print(con.execute('select * from iot_daily_meter_candidates where is_counter_regression').fetchall())
"
```

## What's here

```
extract.py                              the one Python step: pulls the CMMS asset
                                         tree (reuses ../pipeline's CmmsClient) and
                                         the 4 MDM tables it needs (stdlib sqlite3),
                                         lands both as DuckDB tables.
models/sources.yml                      declares what extract.py landed
models/staging/                         1:1 cleaning views over the raw sources
models/intermediate/
  int_iot_deduped.sql                   dedupe on (tag_id, timestamp_utc)
  int_iot_tag_resolution.sql            tag -> asset resolution (equipment code,
                                         then system-class-shorthand fallback)
  int_mdm_desired_state.sql             the active-scope rule + section codes
models/marts/
  mdm_cmms_delta.sql                    CREATE/UPDATE/UNARCHIVE/NOOP/ARCHIVE/BLOCKED,
                                         recursive CTE for the active-descendant
                                         guard, archive-ratio hard stop
  iot_daily_meter_candidates.sql        daily max-timestamp selection + a second
                                         recursive CTE that carries a frozen
                                         baseline forward, day by day, so a
                                         genuine counter reset keeps failing every
                                         day after it -- not just the first one
tests/                                  3 singular tests, independent re-checks
                                         (not just re-deriving the model's own logic)
```

## Why DuckDB's `sqlite_scanner` isn't used

`docs/05_warehouse_hints.md` shows attaching the MDM SQLite file directly
(`ATTACH ... TYPE sqlite`), which is the more elegant, zero-copy option. This
project reads the same 4 tables through plain Python `sqlite3` in `extract.py`
instead, so `dbt run` never needs network access to download a DuckDB extension --
useful in network-restricted CI/sandbox environments (which is exactly what forced
the switch while building this). Either approach feeds the same staging models
downstream; swapping back to `ATTACH` is a one-line change to `profiles.yml` plus
pointing `stg_mdm_*.sql` at the attached tables instead of `raw_mdm_*`.

## Two honest gaps versus `pipeline/` (by design, not oversight)

1. **`mdm_cmms_delta.sql`** checks active descendants against the CMMS's current
   state in one declarative pass. `pipeline/pipeline/canonical.py` resolves
   candidates deepest-first so a platform and its only section can be archived
   *together in the same run* (a bug found by testing -- see `DECISIONS.md` #3).
   Reproducing that here needs an iterative fixed-point, which is exactly the kind
   of thing that's easier to get right in a small, unit-tested Python function than
   in one SQL statement. Concretely: `OLW-OLD` (platform) comes back `BLOCKED` here
   even though the tested Python pipeline correctly archives it and its section
   together.
2. **`iot_daily_meter_candidates.sql`**'s regression baseline is seeded from day 1
   of the historian window, not from the CMMS's pre-existing meter value (that
   would need a `raw_cmms_meters` table via `Asset/Get`, left out to keep
   `extract.py` to the calls it already makes). `JNR-K-101` is a good example: the
   Python pipeline flags it as a regression every single day (its CMMS seed meter
   is higher than the whole historian trend); this mart never flags it at all,
   because it has no CMMS baseline to compare day 1 against.
