-- Row identity is (tag_id, timestamp_utc): exports overlap by 12h, so the
-- same reading can legitimately appear in two files. Keep the copy from the
-- most recently exported file (mirrors pipeline/pipeline/iot.py::dedupe).
select * exclude (rn)
from (
    select
        *,
        row_number() over (
            partition by tag_id, timestamp_utc
            order by exported_at_utc desc
        ) as rn
    from {{ ref('stg_iot_readings') }}
)
where rn = 1
