"""Integration 3 -- IoT historian -> CMMS meters (ARCHITECTURE_.md part A section 5).

Orchestrates the pure functions in iot.py against the real filesystem, the
CMMS client and the audit store: parse every export currently in the landing
zone, dedupe on (tag_id, timestamp_utc), resolve each tag to a CMMS asset,
convert units, keep GOOD quality only, pick the maximum-timestamp reading per
asset per UTC day, guard against counter regressions, and push exactly one
MeterUpdate per (asset, day) that has not already been accepted.
"""

from __future__ import annotations

import logging
from datetime import date

from .audit import ActionRecord, AuditStore
from .clients.cmms import CmmsClient, CmmsNotFound, CmmsUnavailable, CmmsValidationError
from .config import Settings
from .iot import AssetRef, IotRow, convert_to_hours, daily_max_timestamp_readings, dedupe, parse_csv_files, resolve_tag

logger = logging.getLogger("pipeline.iot_to_cmms")


def _pull_assets(cmms: CmmsClient) -> tuple[dict[str, AssetRef], set[str]]:
    assets: dict[str, AssetRef] = {}
    archived_codes: set[str] = set()
    for archived in (False, True):
        for a in cmms.iter_assets(archived=archived):
            assets[a["code"]] = AssetRef(code=a["code"], family=(a.get("family") or {}).get("code"), parent_code=(a.get("parent") or {}).get("code"))
            if archived:
                archived_codes.add(a["code"])
    return assets, archived_codes


def run(cmms: CmmsClient, audit: AuditStore, settings: Settings, run_id: str | None = None) -> str:
    with audit.run("iot_to_cmms", run_id) as run_id:
        csv_paths = sorted(settings.iot_exports_dir.glob("*.csv"))
        raw_rows, parse_errors = parse_csv_files(csv_paths)
        for err in parse_errors:
            audit.record_dq_issue(run_id, "iot_to_cmms", "IOT_ROW", f"{err['file']}:{err['line']}", "unparseable_row", err)
        rows, conflicts = dedupe(raw_rows)
        for c in conflicts:
            audit.record_dq_issue(run_id, "iot_to_cmms", "IOT_ROW", c["tag_id"], "conflicting_duplicate", c)

        audit.record_metric(run_id, "iot_rows_read", len(raw_rows))
        audit.record_metric(run_id, "iot_rows_after_dedupe", len(rows))
        audit.record_metric(run_id, "iot_files_processed", len(csv_paths))

        assets, archived_codes = _pull_assets(cmms)

        # Resolve each distinct tag_id to a CMMS asset code once (cached),
        # convert its unit to hours, and group the surviving rows by asset --
        # everything downstream (daily selection, regression check, sending)
        # operates per asset, one meter stream at a time.
        resolution_cache: dict[str, tuple[str | None, str]] = {}
        rows_by_asset: dict[str, list[IotRow]] = {}
        for row in rows:
            if row.tag_id not in resolution_cache:
                resolution_cache[row.tag_id] = resolve_tag(row.tag_id, assets)
            asset_code, reason = resolution_cache[row.tag_id]
            if asset_code is None:
                if reason != "not_a_run_hrs_tag":
                    audit.record_dq_issue(run_id, "iot_to_cmms", "IOT_TAG", row.tag_id, reason, {"timestamp_utc": row.timestamp_utc.isoformat()})
                continue
            if asset_code in archived_codes:
                audit.record_dq_issue(run_id, "iot_to_cmms", "IOT_TAG", row.tag_id, "target_asset_archived", {"asset_code": asset_code})
                continue
            hours, unit_err = convert_to_hours(row)
            if unit_err:
                audit.record_dq_issue(run_id, "iot_to_cmms", "IOT_TAG", row.tag_id, unit_err, {"timestamp_utc": row.timestamp_utc.isoformat()})
                continue
            rows_by_asset.setdefault(asset_code, []).append(IotRow(row.tag_id, row.timestamp_utc, hours, "h", row.quality, row.exported_at_utc, row.source_file))

        multi_tag_assets = {a: {r.tag_id for r in rs} for a, rs in rows_by_asset.items()}
        for asset_code, tags in multi_tag_assets.items():
            if len(tags) > 1:
                audit.record_dq_issue(run_id, "iot_to_cmms", "IOT_TAG", asset_code, "multiple_tags_for_asset", sorted(tags))

        sent = rejected = quarantined_regression = no_good = 0

        for asset_code, asset_rows in rows_by_asset.items():
            # `days_with_data` is every day that has at least one row (any
            # quality); `selected` (from daily_max_timestamp_readings) is only
            # the GOOD-quality, max-timestamp reading per day. The gap between
            # the two -- a day with data but nothing GOOD -- is its own
            # data-quality issue, flagged below before we even look at values.
            days_with_data: dict[date, list[IotRow]] = {}
            for r in asset_rows:
                days_with_data.setdefault(r.timestamp_utc.date(), []).append(r)
            selected = daily_max_timestamp_readings(asset_rows, asset_code)

            for day, day_rows in days_with_data.items():
                if day not in selected:
                    audit.record_dq_issue(run_id, "iot_to_cmms", "METER", asset_code, "no_good_reading_for_day", {"day": day.isoformat(), "rows": len(day_rows)})
                    no_good += 1

            # Counter-regression baseline: what we last actually sent for this
            # asset (from our own idempotency store), falling back to the
            # CMMS's current meter value on a fresh asset we've never sent to.
            baseline = _baseline_value(cmms, audit, asset_code)

            for day in sorted(selected):  # walk days in order so `baseline` advances monotonically
                reading = selected[day]
                value_int = round(reading.value)
                already = audit.sent_for_day(asset_code, day.isoformat())
                if already is not None and already["value"] == value_int:
                    # idempotency: this exact (asset, day, value) was already
                    # accepted in a previous run -- do nothing, just record it.
                    audit.record_action(run_id, ActionRecord("IOT", "CMMS", "METER", asset_code, "NOOP", "SUCCESS", f"day {day} already sent with value {value_int}"))
                    baseline = value_int
                    continue
                if baseline is not None and value_int < baseline:
                    # a cumulative running-hours counter should never go down;
                    # quarantine instead of sending a value that would corrupt
                    # the CMMS's own meter history.
                    audit.record_dq_issue(
                        run_id, "iot_to_cmms", "METER", asset_code, "counter_regression",
                        {"day": day.isoformat(), "value": value_int, "previous": baseline, "tag_id": reading.tag_id},
                    )
                    audit.record_action(run_id, ActionRecord("IOT", "CMMS", "METER", asset_code, "UPDATE", "REJECTED", f"counter regression on {day}: {value_int} < {baseline}", retry_count=0))
                    quarantined_regression += 1
                    continue
                payload = {"assetCode": asset_code, "dateTime": reading.timestamp_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "value": value_int, "name": "Running hours", "unitCode": "H"}
                try:
                    result = cmms.meter_update(payload)
                    audit.record_meter_sent(run_id, asset_code, day.isoformat(), reading.tag_id, payload["dateTime"], value_int)
                    audit.record_action(run_id, ActionRecord("IOT", "CMMS", "METER", asset_code, "UPDATE", "SUCCESS", f"day {day}", payload, {"messages": result}))
                    baseline = value_int
                    sent += 1
                except CmmsValidationError as exc:
                    audit.record_action(run_id, ActionRecord("IOT", "CMMS", "METER", asset_code, "UPDATE", "REJECTED", "; ".join(exc.messages), payload))
                    audit.record_dq_issue(run_id, "iot_to_cmms", "METER", asset_code, "cmms_rejected_reading", exc.messages)
                    rejected += 1
                except (CmmsNotFound, CmmsUnavailable) as exc:
                    status = "REJECTED" if isinstance(exc, CmmsNotFound) else "FAILED_RETRYABLE"
                    audit.record_action(run_id, ActionRecord("IOT", "CMMS", "METER", asset_code, "UPDATE", status, "; ".join(exc.messages), payload))
                    rejected += 1

        audit.record_metric(run_id, "iot_meters_sent", sent)
        audit.record_metric(run_id, "iot_meters_rejected", rejected)
        audit.record_metric(run_id, "iot_counter_regressions", quarantined_regression)
        audit.record_metric(run_id, "iot_days_without_good_reading", no_good)
        return run_id


def _baseline_value(cmms: CmmsClient, audit: AuditStore, asset_code: str) -> int | None:
    """Prefer our own idempotency store (what we ourselves last sent) over the
    CMMS's live value, so a regression is judged against our own history even
    if the CMMS value was independently edited. Only falls back to asking the
    CMMS directly the first time we ever touch this asset's meter."""
    last = audit.last_sent_meter(asset_code)
    if last is not None:
        return int(last["value"])
    try:
        asset = cmms.get_asset(asset_code)
    except (CmmsNotFound, CmmsUnavailable):
        return None
    for meter in asset.get("meters", []):
        if meter["name"].strip().lower() == "running hours":
            return int(meter["value"])
    return None
