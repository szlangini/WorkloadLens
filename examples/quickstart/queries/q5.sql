-- Online vs in-store sales comparison
SELECT
    'online' AS channel_type,
    COUNT(*) AS num_sales,
    SUM(amount) AS total_amount,
    MIN(sale_date) AS first_sale,
    MAX(sale_date) AS last_sale
FROM sales
WHERE channel = 'online'

UNION ALL

SELECT
    'store' AS channel_type,
    COUNT(*) AS num_sales,
    SUM(amount) AS total_amount,
    MIN(sale_date) AS first_sale,
    MAX(sale_date) AS last_sale
FROM sales
WHERE channel = 'store';
