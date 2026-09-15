"""Extraction step: pulls the full CMMS asset tree into DuckDB.

dbt/SQL cannot make an authenticated HTTP call with retry/backoff/rate-limiting,
so this one step stays in Python -- and reuses the same, already-tested
CmmsClient the real pipeline (../pipeline) uses, rather than a second HTTP
client. Everything downstream of this table (staging models onward) is SQL.

    uv run python extract.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import duckdb

# Reuse the already-tested CmmsClient from ../pipeline instead of a second HTTP
# client -- sys.path, not an editable install, because the two are separate
# uv projects and this is a one-file script, not a dependency of warehouse.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from pipeline.clients.cmms import CmmsClient  # noqa: E402
from pipeline.config import get_settings  # noqa: E402

WAREHOUSE_DB = Path(__file__).resolve().parent / "warehouse.duckdb"

# docs/05_warehouse_hints.md shows attaching the MDM SQLite file directly with
# DuckDB's `sqlite_scanner` extension (ATTACH ... TYPE sqlite) -- the more
# elegant, zero-copy option when the extension can be downloaded. This project
# reads the same tables through plain Python sqlite3 instead and lands them as
# ordinary DuckDB tables, so `dbt run` never needs network access to fetch a
# DuckDB extension (relevant in network-restricted CI/sandbox environments;
# either approach produces the same staging models downstream).
MDM_TABLES = [
    "systemref_systemunit",
    "systemref_systemunittosectionassignment",
    "systemref_sectioncategory",
    "orgref_orgunit",
]


def pull_assets(cmms: CmmsClient) -> list[dict]:
    rows: list[dict] = []
    for archived in (False, True):
        for a in cmms.iter_assets(archived=archived):
            rows.append(
                {
                    "code": a["code"],
                    "name": a["name"],
                    "family": (a.get("family") or {}).get("code"),
                    "parent_code": (a.get("parent") or {}).get("code"),
                    "bodies": [b["name"] for b in a.get("bodies") or []],
                    "criticality_code": (a.get("criticality") or {}).get("code"),
                    "in_service_date": a.get("inServiceDate"),
                    "archived": archived,
                }
            )
    return rows


def pull_mdm_tables(mdm_db_path: Path) -> dict[str, list[dict]]:
    con = sqlite3.connect(mdm_db_path)
    con.row_factory = sqlite3.Row
    tables: dict[str, list[dict]] = {}
    for name in MDM_TABLES:
        tables[name] = [dict(r) for r in con.execute(f"SELECT * FROM {name}").fetchall()]
    con.close()
    return tables


def _load_json_rows(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict]) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(rows, f)
        tmp_path = f.name
    con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_json_auto('{tmp_path}')")
    Path(tmp_path).unlink()


def main() -> None:
    settings = get_settings()
    cmms = CmmsClient(
        settings.cmms_base_url,
        settings.cmms_tenant,
        settings.cmms_api_key,
        settings.cmms_rate_limit_per_minute,
        settings.http_max_retries,
        settings.http_timeout_seconds,
    )
    asset_rows = pull_assets(cmms)
    print(f"pulled {len(asset_rows)} CMMS assets ({cmms.calls} API calls, {cmms.retries} retries)")

    mdm_tables = pull_mdm_tables(settings.mdm_db_path)
    for name, rows in mdm_tables.items():
        print(f"pulled {len(rows)} rows from MDM table {name}")

    con = duckdb.connect(str(WAREHOUSE_DB))
    _load_json_rows(con, "raw_cmms_assets", asset_rows)
    for name, rows in mdm_tables.items():
        _load_json_rows(con, f"raw_mdm_{name}", rows)
    con.close()
    print(f"wrote raw_cmms_assets + {len(mdm_tables)} MDM tables to {WAREHOUSE_DB}")


if __name__ == "__main__":
    main()
