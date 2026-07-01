-- Running total per customer with rank
WITH customer_totals AS (
    SELECT
        customer_id,
        SUM(amount) AS lifetime_value
    FROM sales
    GROUP BY customer_id
)
SELECT
    c.name,
    c.segment,
    ct.lifetime_value,
    RANK() OVER (ORDER BY ct.lifetime_value DESC) AS value_rank,
    SUM(ct.lifetime_value) OVER (ORDER BY ct.lifetime_value DESC) AS running_total
FROM customer_totals ct
JOIN customers c ON ct.customer_id = c.customer_id;
