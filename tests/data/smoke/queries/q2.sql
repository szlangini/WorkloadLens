SELECT country, SUM(amount) + 5 AS total_amount
FROM sales
JOIN dim ON sales.id = dim.id
WHERE amount > 0
GROUP BY country
HAVING SUM(amount) > 100;
