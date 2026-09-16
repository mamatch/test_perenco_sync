"""Command-line entrypoint.

    uv run python -m pipeline run-all
    uv run python -m pipeline run mdm-to-cmms
    uv run python -m pipeline run cmms-to-mdm
    uv run python -m pipeline run iot-to-cmms
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import cmms_to_mdm, iot_to_cmms, mdm_to_cmms
from .audit import AuditStore
from .clients.cmms import CmmsClient
from .clients.mdm import MdmClient
from .config import get_settings
from .observability import evaluate_alerts, health_summary

INTEGRATIONS = ("mdm-to-cmms", "cmms-to-mdm", "iot-to-cmms")


def _make_clients(settings):
    cmms = CmmsClient(
        settings.cmms_base_url,
        settings.cmms_tenant,
        settings.cmms_api_key,
        settings.cmms_rate_limit_per_minute,
        settings.http_max_retries,
        settings.http_timeout_seconds,
    )
    audit = AuditStore(settings.audit_db_path)
    return cmms, audit


def _run_one(name: str) -> str:
    """Builds fresh clients (one CmmsClient, one AuditStore, one MdmClient
    where needed) and dispatches to the matching integration's run(). Each
    integration owns its own MdmClient connection so mdm_to_cmms and
    cmms_to_mdm never share one across runs."""
    settings = get_settings()
    cmms, audit = _make_clients(settings)
    if name == "mdm-to-cmms":
        with MdmClient(settings.mdm_db_path) as mdm:
            run_id = mdm_to_cmms.run(cmms, mdm, audit, settings)
    elif name == "cmms-to-mdm":
        with MdmClient(settings.mdm_db_path) as mdm:
            run_id = cmms_to_mdm.run(cmms, mdm, audit, settings)
    elif name == "iot-to-cmms":
        run_id = iot_to_cmms.run(cmms, audit, settings)
    else:
        raise ValueError(name)

    # health_summary/evaluate_alerts read back from the audit store what the
    # run itself just wrote -- this is Part C's "observability", printed
    # after every run rather than requiring a separate dashboard to see it.
    print(health_summary(audit, run_id))
    for alert in evaluate_alerts(audit, run_id, settings):
        print(f"ALERT[{alert.severity}] {alert.message}")
    print(f"CMMS calls this process: total={cmms.calls} retries={cmms.retries}")
    print()
    audit.close()
    return run_id


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    parser = argparse.ArgumentParser(prog="pipeline")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run-all", help="run mdm-to-cmms, cmms-to-mdm, iot-to-cmms in sequence")
    run_p = sub.add_parser("run", help="run a single integration")
    run_p.add_argument("integration", choices=INTEGRATIONS)

    args = parser.parse_args(argv)

    if args.cmd == "run-all":
        for name in INTEGRATIONS:
            _run_one(name)
    else:
        _run_one(args.integration)
    return 0


if __name__ == "__main__":
    sys.exit(main())
