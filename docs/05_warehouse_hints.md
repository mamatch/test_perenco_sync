# 5. Warehouse: DuckDB or Snowflake

The production pipeline runs on Snowflake with dbt. You may use either:

## Option A — DuckDB (no account needed)

DuckDB reads CSV files natively and can open a SQLite file through its `sqlite` extension:

```python
import duckdb
con = duckdb.connect("warehouse.duckdb")
con.execute("INSTALL sqlite; LOAD sqlite;")
con.execute("ATTACH './mdm_data/systemref.sqlite3' AS mdm (TYPE sqlite);")
con.execute("SELECT count(*) FROM mdm.systemref_systemunit").fetchone()
con.execute("SELECT count(*) FROM read_csv_auto('iot_historian/exports/*.csv', union_by_name=true)").fetchone()
```

dbt works with DuckDB through `dbt-duckdb` if you want to show dbt models and tests.

## Option B — Snowflake trial account

Create a free trial at signup.snowflake.com (30 days). Use the Snowflake CLI (`snow`) or the Python
connector. Load the SQLite extracts and the CSV files into a `RAW` schema (`PUT` + `COPY INTO`, or
`write_pandas`). If you go this way, we are interested in how you would call the CMMS API from
Snowflake (external access integration, Python UDF / stored procedure) versus from an orchestrator
outside Snowflake, and in Snowflake TASK graphs for orchestration.

Either way, please keep the setup reproducible: one command (or a short README) should be enough
for us to run your pipeline against a fresh sandbox.
