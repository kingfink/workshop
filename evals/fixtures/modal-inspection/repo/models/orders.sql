select orders.order_id, customers.region
from orders
left join customers on orders.customer_id = customers.customer_id
