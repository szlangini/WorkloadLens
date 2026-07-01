-- Sales by customer country
SELECT
    c.country,
    c.segment,
    COUNT(*) AS num_sales,
    SUM(s.amount) AS total_amount
FROM sales s
JOIN customers c ON s.customer_id = c.customer_id
WHERE s.amount > 50.00
GROUP BY c.country, c.segment;
