CREATE TABLE sales (
    sale_id INTEGER,
    customer_id INTEGER,
    product_id INTEGER,
    sale_date DATE,
    amount DECIMAL(12, 2),
    region VARCHAR,
    channel VARCHAR
);

CREATE TABLE customers (
    customer_id INTEGER,
    name VARCHAR,
    country VARCHAR,
    segment VARCHAR
);

CREATE TABLE products (
    product_id INTEGER,
    category VARCHAR,
    brand VARCHAR,
    price DECIMAL(10, 2)
);
