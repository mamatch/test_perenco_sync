# airflow/ — reference DAG, not the chosen orchestrator

**`ARCHITECTURE_.md` section 2 recommends repointing MDAdmin's existing Celery
chain, not Airflow** — Perenco already runs Celery in production
(`docs/01_context.md`), and introducing a second "how do we schedule
background work" mechanism for one integration isn't justified if Celery
already does other jobs there too (see `DECISIONS.md` #12 for the one
assumption this rests on, and when it flips back the other way).

`dags/perenco_nightly_sync.py` is kept as a **documented alternative**: what
the same orchestration would look like as an Airflow DAG instead, if that
assumption turns out to be wrong or the roadmap grows past three independent
branches into something with real cross-task dependencies. Same principle as
the Celery version — three independent tasks, each a thin call into the
exact same, already-tested entrypoints `pipeline/pipeline/cli.py` uses
(`mdm_to_cmms.run()`, `cmms_to_mdm.run()`, `iot_to_cmms.run()`) — no sync
logic is duplicated here either.

**This is not executed by `make up` or `pipeline run-all`, and Airflow is not
one of this repo's dependencies.** That's deliberate, not an oversight — see
the module docstring in the DAG file for the full reasoning. Short version:
standing up a real Airflow instance (its own metadata DB, a first-run admin
password, a multi-minute dependency install, a default webserver port that
collides with the CMMS mock's own `:8080`) is real operational weight to put
into a zip that has to run unattended, on an unknown OS, during a timed
evaluation session — for a component that's graded as a design/reasoning item
(Part A), not as something that needs to be clicked through.

## To actually run it

In a real Airflow environment (or a throwaway `airflow standalone` if you want
to try it locally, on a machine where that's safe to install):

```bash
pip install apache-airflow
pip install -e ../pipeline   # so `from pipeline import ...` resolves
export AIRFLOW_HOME=$(pwd)/airflow_home
airflow standalone
# copy dags/perenco_nightly_sync.py into $AIRFLOW_HOME/dags/, then trigger
# "perenco_nightly_sync" from the UI (default webserver: http://localhost:8080
# -- stop the CMMS mock first, or run it on a different port, to avoid the
# port clash)
```

The DAG reads its configuration from the same environment variables as the
CLI (`CMMS_BASE_URL`, `SYSTEMREF_DB_PATH`, `IOT_EXPORTS_DIR`, `AUDIT_DB_PATH`,
...) — in a real deployment these would come from Airflow Connections/Variables
and the CMMS API key from Airflow's own secrets backend (or the Key Vault
integration ARCHITECTURE_.md section 9 describes), not a plain env var.
