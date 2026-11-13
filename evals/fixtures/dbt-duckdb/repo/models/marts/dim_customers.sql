with orders as (
    select
        customer_id,
        count(*) as order_count,
        sum(amount) as lifetime_value
    from {{ ref('stg_orders') }}
    group by customer_id
)

select
    customers.customer_id,
    customers.customer_name,
    customers.created_at,
    customers.country,
    coalesce(orders.order_count, 0) as order_count,
    coalesce(orders.lifetime_value, 0) as lifetime_value
from {{ ref('stg_customers') }} as customers
left join orders on orders.customer_id = customers.customer_id
