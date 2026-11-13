select
    customer_id,
    name as customer_name,
    cast(signup_ts as timestamp) as created_at,
    country
from {{ source('raw', 'customers') }}
