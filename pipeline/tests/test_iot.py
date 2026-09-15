from datetime import datetime, timezone

from pipeline.iot import AssetRef, IotRow, convert_to_hours, daily_max_timestamp_readings, dedupe, resolve_tag


def _dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _row(tag, ts, value, quality="GOOD", unit="h", exported="2026-09-02T01:30:00"):
    return IotRow(tag, _dt(ts), value, unit, quality, _dt(exported), "f.csv")


ASSETS = {
    "OLW": AssetRef("OLW", "PLATFORM", None),
    "OLW_UTIL": AssetRef("OLW_UTIL", "SECTION", "OLW"),
    "OLW-K-101A": AssetRef("OLW-K-101A", "CO_CE", "SYS_OLW_001"),
    "SYS_JNR_004": AssetRef("SYS_JNR_004", "SYS_PG", "JNR_UTIL"),
    "JNR_UTIL": AssetRef("JNR_UTIL", "SECTION", "JNR"),
    "JNR": AssetRef("JNR", "PLATFORM", None),
}


def test_resolve_tag_direct_equipment_match():
    code, reason = resolve_tag("GA-OLW.K-101A.RUN_HRS", ASSETS)
    assert code == "OLW-K-101A"
    assert reason == ""


def test_resolve_tag_falls_back_to_system_class_shorthand():
    code, reason = resolve_tag("GA-JNR.PG.RUN_HRS", ASSETS)
    assert code == "SYS_JNR_004"
    assert reason == ""


def test_resolve_tag_unresolved_when_nothing_matches():
    code, reason = resolve_tag("GA-OLW.P-777.RUN_HRS", ASSETS)
    assert code is None
    assert reason == "unresolved_tag"


def test_resolve_tag_ignores_non_run_hrs_tags():
    code, reason = resolve_tag("GA-OLW.K-101A.PRESSURE", ASSETS)
    assert code is None
    assert reason == "not_a_run_hrs_tag"


def test_convert_to_hours_known_and_unknown_units():
    row = _row("t", "2026-09-01T00:00:00", 120.0, unit="min")
    hours, err = convert_to_hours(row)
    assert err == ""
    assert hours == 2.0
    bad = _row("t", "2026-09-01T00:00:00", 1.0, unit="bar")
    hours, err = convert_to_hours(bad)
    assert hours is None
    assert err == "unknown_unit:bar"


def test_dedupe_keeps_latest_export_and_flags_value_conflicts():
    a = _row("t", "2026-09-01T00:00:00", 10.0, exported="2026-09-02T01:30:00")
    b = _row("t", "2026-09-01T00:00:00", 11.0, exported="2026-09-03T01:30:00")  # same key, later export, different value
    rows, conflicts = dedupe([a, b])
    assert len(rows) == 1
    assert rows[0].value == 11.0
    assert len(conflicts) == 1


def test_daily_selection_picks_max_timestamp_not_max_value():
    rows = [
        _row("t", "2026-09-01T04:00:00", 100.0),
        _row("t", "2026-09-01T21:00:00", 90.0),  # later timestamp, lower value: still the one that counts
    ]
    selected = daily_max_timestamp_readings(rows, "A1")
    day = rows[0].timestamp_utc.date()
    assert selected[day].value == 90.0


def test_daily_selection_excludes_bad_quality():
    rows = [
        _row("t", "2026-09-01T04:00:00", 100.0, quality="GOOD"),
        _row("t", "2026-09-01T21:00:00", 999.0, quality="BAD"),
    ]
    selected = daily_max_timestamp_readings(rows, "A1")
    day = rows[0].timestamp_utc.date()
    assert selected[day].value == 100.0
