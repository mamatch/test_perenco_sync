-- Every CMMS asset code gets at most one planned action per run. A failing
-- row here would mean the same code was matched by both the desired-state
-- branch and the archive-candidate branch of mdm_cmms_delta.sql -- a sign the
-- CTEs overlap somewhere.
select code, count(*) as n
from {{ ref('mdm_cmms_delta') }}
group by code
having count(*) > 1
