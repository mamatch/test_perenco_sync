select
    su.id as system_unit_id,
    su.code,
    su.name,
    su.date_start,
    su.date_end,
    ou.name as body_name,
    ou.is_field,
    ou.is_active as org_active
from {{ source('raw', 'raw_mdm_systemref_systemunit') }} su
join {{ source('raw', 'raw_mdm_orgref_orgunit') }} ou on ou.id = su.org_unit_id
