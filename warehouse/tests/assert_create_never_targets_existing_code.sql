-- A CREATE action must never target a code that already exists in the CMMS
-- (that should have been an UPDATE, UNARCHIVE or NOOP instead) -- independent
-- re-check of mdm_cmms_delta.sql's join logic.
select d.code
from {{ ref('mdm_cmms_delta') }} d
join {{ ref('stg_cmms_assets') }} c on c.code = d.code
where d.action = 'CREATE'
