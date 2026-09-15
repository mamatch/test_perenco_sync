-- MDM-owned desired state (platforms, sections), the SQL twin of
-- pipeline/pipeline/timeutil.py::is_active() + clients/mdm.py::desired_hierarchy().
-- Active-scope rule (confirmed, see DECISIONS.md #1):
--   date_start <= today and (date_end is null or date_end > today)

with platforms as (
    select
        system_unit_id,
        code,
        name,
        body_name,
        date_start <= current_date
            and (date_end is null or date_end > current_date)
            and is_field and org_active as active
    from {{ ref('stg_mdm_system_units') }}
),

sections as (
    select
        p.code as platform_code,
        p.code || '_' || s.section_code as code,
        s.section_name as name,
        p.body_name,
        p.active
            and s.date_start <= current_date
            and (s.date_end is null or s.date_end > current_date) as active
    from {{ ref('stg_mdm_sections') }} s
    join platforms p using (system_unit_id)
)

select code, name, 'PLATFORM' as family, cast(null as varchar) as parent_code, body_name, active, 0 as depth
from platforms
union all
select code, name, 'SECTION' as family, platform_code as parent_code, body_name, active, 1 as depth
from sections
