select
    a.id as assignment_id,
    a.system_unit_id,
    a.date_start,
    a.date_end,
    sc.code as section_code,
    sc.name as section_name
from {{ source('raw', 'raw_mdm_systemref_systemunittosectionassignment') }} a
join {{ source('raw', 'raw_mdm_systemref_sectioncategory') }} sc on sc.id = a.section_category_id
