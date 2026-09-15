-- The SQL twin of pipeline/pipeline/canonical.py::compute_plan() -- the
-- MDM -> CMMS delta (CREATE/UPDATE/UNARCHIVE/NOOP/ARCHIVE/BLOCKED), with the
-- recursive active-descendant guard expressed as a recursive CTE instead of
-- a Python function, and the 10% archive-ratio hard stop as a scalar
-- subquery instead of a second pass over a Python list.
--
-- Known, documented simplification versus the tested Python implementation
-- (see DECISIONS.md #3): pipeline/canonical.py resolves archive candidates
-- deepest-first so a platform and its only section can be archived together
-- in the *same run* (a bug I found and fixed by testing). This recursive CTE
-- checks descendants against the CMMS's current state only, in one
-- declarative pass -- so here a platform and its only section would BOTH
-- come back BLOCKED even though the Python pipeline correctly archives both.
-- Expressing that same-run, order-dependent resolution in pure SQL needs an
-- iterative fixed-point (recompute until nothing changes), which is exactly
-- the kind of thing that is easier to get right in a small, unit-tested
-- Python function than in a single SQL statement -- a deliberate trade-off,
-- not an oversight.

with current_structural as (
    select code, name, family, parent_code, archived
    from {{ ref('stg_cmms_assets') }}
    where family in ('PLATFORM', 'SECTION')
),

desired as (
    select * from {{ ref('int_mdm_desired_state') }}
),

-- every (ancestor, descendant) pair reachable through the *whole* CMMS tree
-- (all families), so an active Equipment/System several hops down still
-- blocks archiving the Section/Platform above it.
ancestry as (
    with recursive tree as (
        select parent_code as ancestor_code, code as descendant_code, archived as descendant_archived
        from {{ ref('stg_cmms_assets') }}
        where parent_code is not null

        union all

        select t.ancestor_code, c.code as descendant_code, c.archived as descendant_archived
        from tree t
        join {{ ref('stg_cmms_assets') }} c on c.parent_code = t.descendant_code
    )
    select distinct ancestor_code
    from tree
    where not descendant_archived
),

non_destructive as (
    select
        d.code,
        d.family,
        d.parent_code,
        case
            when c.code is null then 'CREATE'
            when c.archived then 'UNARCHIVE'
            when c.name != d.name or (d.parent_code is not null and c.parent_code != d.parent_code) then 'UPDATE'
            else 'NOOP'
        end as action,
        cast(null as varchar) as reason
    from desired d
    left join current_structural c using (code)
    where d.active
),

archive_candidates as (
    select
        c.code,
        c.family,
        c.parent_code,
        exists (select 1 from ancestry a where a.ancestor_code = c.code) as has_active_descendant
    from current_structural c
    left join desired d on d.code = c.code and d.active
    where not c.archived and d.code is null
),

ratio as (
    select
        count(*) filter (where not has_active_descendant) as eligible,
        (select count(*) from current_structural where not archived) as active_in_scope
    from archive_candidates
),

destructive as (
    select
        ac.code,
        ac.family,
        ac.parent_code,
        case
            when ac.has_active_descendant then 'BLOCKED'
            when (select eligible from ratio) * 1.0 / nullif((select active_in_scope from ratio), 0)
                 > {{ var('archive_ratio_threshold', 0.10) }} then 'BLOCKED'
            else 'ARCHIVE'
        end as action,
        case
            when ac.has_active_descendant then 'has active descendant (recursive)'
            when (select eligible from ratio) * 1.0 / nullif((select active_in_scope from ratio), 0)
                 > {{ var('archive_ratio_threshold', 0.10) }} then 'archive ratio exceeds threshold'
            else 'outside MDM scope'
        end as reason
    from archive_candidates ac
)

select code, family, parent_code, action, reason from non_destructive
union all
select code, family, parent_code, action, reason from destructive
