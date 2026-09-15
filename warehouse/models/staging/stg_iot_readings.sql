-- DuckDB reads the historian CSVs natively -- no separate parsing step needed
-- (compare pipeline/pipeline/iot.py::parse_csv_files, which does the same job
-- in Python for the tested pipeline).
select
    tag_id,
    timestamp_utc::timestamp as timestamp_utc,
    try_cast(value as double) as value,
    lower(trim(unit)) as unit,
    upper(trim(quality)) as quality,
    exported_at_utc::timestamp as exported_at_utc,
    filename as source_file
from read_csv_auto(
    '{{ env_var("IOT_EXPORTS_GLOB", "../iot_historian/exports/*.csv") }}',
    union_by_name = true,
    filename = true
)
