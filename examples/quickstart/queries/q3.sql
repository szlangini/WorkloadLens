-- Top 10 products by revenue with customer and product details
SELECT
    p.category,
    p.brand,
    c.country,
    SUM(s.amount) AS revenue
FROM sales s
JOIN products p ON s.product_id = p.product_id
JOIN customers c ON s.customer_id = c.customer_id
WHERE s.sale_date BETWEEN DATE '2024-01-01' AND DATE '2024-12-31'
ORDER BY revenue DESC
LIMIT 10;
