select
    order_id,
    customer_id,
    cast(amount as decimal(10, 2)) as amount,
    cast(ordered_at as timestamp) as ordered_at
from {{ source('raw', 'orders') }}
