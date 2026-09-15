-- Independent check (not just re-running the mart's own QUALIFY) that the
-- chosen reading for each (asset, day) really is the one at the maximum
-- timestamp among GOOD readings -- the business rule confirmed on the
-- clarification call ("value at the max timestamp", not max value).
with candidates as (
    select asset_code, reading_day, timestamp_utc
    from {{ ref('iot_daily_meter_candidates') }}
),

resolved_good as (
    select
        r.asset_code,
        date_trunc('day', d.timestamp_utc) as reading_day,
        d.timestamp_utc
    from {{ ref('int_iot_deduped') }} d
    join {{ ref('int_iot_tag_resolution') }} r using (tag_id)
    where d.quality = 'GOOD'
)

select
    c.asset_code,
    c.reading_day,
    c.timestamp_utc as chosen_timestamp,
    max(g.timestamp_utc) as actual_max_timestamp
from candidates c
join resolved_good g
    on g.asset_code = c.asset_code and g.reading_day = c.reading_day
group by c.asset_code, c.reading_day, c.timestamp_utc
having c.timestamp_utc < max(g.timestamp_utc)
