-- Total sales amount by region
SELECT
    region,
    COUNT(*) AS num_sales,
    SUM(amount) AS total_amount,
    AVG(amount) AS avg_amount
FROM sales
WHERE sale_date >= DATE '2024-01-01'
GROUP BY region;
