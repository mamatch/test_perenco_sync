-- One row per CMMS asset. Note there is exactly one row per code: the mock's
-- Asset/Filter partitions assets by their current `archived` flag, so the two
-- pulls in extract.py (archived=false, archived=true) never overlap.
select
    code,
    name,
    family,
    parent_code,
    bodies,
    criticality_code,
    try_cast(in_service_date as timestamp) as in_service_date,
    archived
from {{ source('raw', 'raw_cmms_assets') }}
