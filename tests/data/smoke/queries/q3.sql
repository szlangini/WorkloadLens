SELECT s.id, d.country
FROM sales s
JOIN dim d ON s.id = d.id
WHERE s.amount > 42;
