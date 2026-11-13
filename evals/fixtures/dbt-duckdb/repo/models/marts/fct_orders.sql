select
    order_id,
    customer_id,
    amount,
    ordered_at
from {{ ref('stg_orders') }}
