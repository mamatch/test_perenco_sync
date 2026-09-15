-- Deterministic tag_id -> CMMS asset resolution (docs/04_mdm_and_iot.md,
-- ARCHITECTURE_.md part A section 5), the SQL twin of
-- pipeline/pipeline/iot.py::resolve_tag():
--   tag_id = "<country>-<platform>.<suffix>.RUN_HRS"
--   1. "<platform>-<suffix>" is a known, active, non-structural asset code
--      -> that equipment.
--   2. else, exactly one asset under that platform has family "SYS_<suffix>"
--      -> that system (a platform's single aggregate system, addressed by
--      class shorthand instead of an individual equipment tag).
--   3. else: absent from this model entirely -- the mart below treats that
--      as "unresolved_tag" rather than guessing.

with tags as (
    select distinct tag_id
    from {{ ref('int_iot_deduped') }}
    where upper(tag_id) like '%.RUN_HRS'
),

parsed as (
    select
        tag_id,
        split_part(tag_id, '.', 1) as country_platform,
        split_part(tag_id, '.', 2) as suffix
    from tags
    -- exactly 3 dot-separated parts: <country-platform>.<suffix>.RUN_HRS
    where length(tag_id) - length(replace(tag_id, '.', '')) = 2
),

platform_split as (
    select
        tag_id,
        suffix,
        substr(country_platform, 4) as platform_code
    from parsed
    where substr(country_platform, 3, 1) = '-'
),

direct_match as (
    select
        p.tag_id,
        a.code as asset_code,
        'equipment_code' as resolution_method
    from platform_split p
    join {{ ref('stg_cmms_assets') }} a
        on a.code = p.platform_code || '-' || p.suffix
        and a.family not in ('PLATFORM', 'SECTION', 'LOC_TAG')
        and not a.archived
),

system_class_candidates as (
    select
        p.tag_id,
        a.code as asset_code,
        count(*) over (partition by p.tag_id) as n_matches
    from platform_split p
    join {{ ref('stg_cmms_assets') }} a
        on a.family = 'SYS_' || p.suffix
        and not a.archived
        and (
            a.parent_code = p.platform_code
            or a.parent_code in (
                select code from {{ ref('stg_cmms_assets') }} sect
                where sect.parent_code = p.platform_code
            )
        )
    where p.tag_id not in (select tag_id from direct_match)
),

system_class_match as (
    select tag_id, asset_code, 'system_class_shorthand' as resolution_method
    from system_class_candidates
    where n_matches = 1
)

select tag_id, asset_code, resolution_method from direct_match
union all
select tag_id, asset_code, resolution_method from system_class_match
