-- The SQL twin of pipeline/pipeline/iot.py::daily_max_timestamp_readings() +
-- the counter-regression check in iot_to_cmms.py.
--
-- The regression check needs a *recursive* CTE, not a plain LAG(): the
-- business rule freezes the accepted baseline forever once a day is
-- rejected, so a real counter reset keeps failing every subsequent day
-- until a human fixes it (pipeline/pipeline/iot_to_cmms.py never advances
-- `baseline` on a rejected read -- see DECISIONS.md #10). A naive
-- `value_hours < lag(value_hours)` only compares each day to its immediate
-- predecessor regardless of whether that predecessor was itself accepted,
-- so it would flag a reset for exactly one day and then silently "heal" as
-- soon as the post-reset numbers start climbing again -- which is the wrong
-- answer to the business question. `baseline_walk` below carries the last
-- *accepted* value forward day by day, the same recursive-CTE tool used for
-- the tree walk in mdm_cmms_delta.sql, just walking over time instead of
-- over a hierarchy.
--
-- Scoping limitation of this proof of concept (documented, not hidden): the
-- Python pipeline seeds day 1's baseline from the CMMS's own pre-existing
-- meter value (one Asset/Get call per asset). That value is not in the
-- Filter response this project's extract.py pulls, so this mart's day 1 is
-- always "accepted" with no prior baseline to compare against -- it can only
-- detect regressions *within* the historian window, not against the CMMS's
-- prior reading. Pulling per-asset Get responses into a small
-- `raw_cmms_meters` table would close that gap.

with recursive resolved as (
    select
        d.tag_id,
        r.asset_code,
        d.timestamp_utc,
        d.value,
        d.unit,
        d.quality
    from {{ ref('int_iot_deduped') }} d
    join {{ ref('int_iot_tag_resolution') }} r using (tag_id)
),

converted as (
    select
        *,
        case
            when unit in ('h', 'hr', 'hrs', 'hour', 'hours') then value
            when unit in ('min', 'minute', 'minutes') then value / 60.0
            else null
        end as value_hours
    from resolved
),

good_only as (
    select *, date_trunc('day', timestamp_utc) as reading_day
    from converted
    where quality = 'GOOD' and value_hours is not null
),

daily_selected as (
    -- the reading at the MAXIMUM TIMESTAMP for the day, not the maximum value
    select
        asset_code,
        reading_day,
        tag_id,
        timestamp_utc,
        round(value_hours) as value_hours
    from good_only
    qualify row_number() over (
        partition by asset_code, reading_day
        order by timestamp_utc desc
    ) = 1
),

daily_ranked as (
    select
        *,
        row_number() over (partition by asset_code order by reading_day) as day_rank
    from daily_selected
),

baseline_walk as (
    select
        asset_code, reading_day, tag_id, timestamp_utc, value_hours, day_rank,
        cast(null as double) as baseline_before,
        false as is_counter_regression,
        value_hours as accepted_baseline
    from daily_ranked
    where day_rank = 1

    union all

    select
        d.asset_code, d.reading_day, d.tag_id, d.timestamp_utc, d.value_hours, d.day_rank,
        w.accepted_baseline as baseline_before,
        d.value_hours < w.accepted_baseline as is_counter_regression,
        case
            when d.value_hours >= w.accepted_baseline then d.value_hours
            else w.accepted_baseline  -- rejected: baseline stays frozen, same as the Python pipeline
        end as accepted_baseline
    from baseline_walk w
    join daily_ranked d on d.asset_code = w.asset_code and d.day_rank = w.day_rank + 1
)

select
    asset_code,
    reading_day,
    tag_id,
    timestamp_utc,
    value_hours,
    baseline_before as previous_value_hours,
    is_counter_regression
from baseline_walk
order by asset_code, reading_day
