-- Monthly sales report (run by the finance dashboard on every page load)
SELECT *
FROM orders o
WHERE EXTRACT(YEAR FROM o.created_at) = 2025
  AND o.id NOT IN (SELECT order_id FROM refunds)
ORDER BY o.created_at DESC
LIMIT 50 OFFSET 10000;

SELECT c.id,
       c.name,
       (SELECT COUNT(*) FROM orders o2 WHERE o2.customer_id = c.id) AS orders_count
FROM customers c
WHERE lower(c.email) = 'ana@example.com';

SELECT DISTINCT c.id, c.name
FROM customers c
JOIN orders o ON o.customer_id = c.id
WHERE o.status = 'PAID' OR c.name = 'VIP';

UPDATE products SET active = false;

SELECT id, name FROM products ORDER BY RANDOM() LIMIT 5;

SELECT order_id FROM order_items
UNION
SELECT order_id FROM refunds;
