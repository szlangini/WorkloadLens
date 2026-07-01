from __future__ import annotations

import json
from pathlib import Path

from workloadlens.report import aggregate_jsonl_paths
from workloadlens.utils.schema import parse_schema_tables


def test_parse_schema_tables_recovers_primary_keys() -> None:
    ddl = """
    CREATE TABLE sales (
        id INT PRIMARY KEY,
        amount DECIMAL(10,2),
        created_at DATE
    );
    CREATE TABLE events (
        event_id BIGINT,
        event_ts TIMESTAMP,
        PRIMARY KEY (event_id)
    );
    """
    tables = parse_schema_tables(ddl)
    assert len(tables) == 2
    sales = tables["sales"]
    assert sales.columns["id"] == "INT"
    assert sales.primary_key_columns["id"] == "INT"
    events = tables["events"]
    assert events.primary_key_columns["event_id"] == "INT"


def test_parse_schema_tables_recovers_foreign_keys() -> None:
    ddl = """
    CREATE TABLE customers (
        customer_id INT PRIMARY KEY,
        name TEXT
    );
    CREATE TABLE orders (
        order_id INT PRIMARY KEY,
        customer_id INT REFERENCES customers(customer_id)
    );
    CREATE TABLE line_items (
        id INT,
        order_id INT,
        CONSTRAINT fk_orders FOREIGN KEY (order_id) REFERENCES orders(order_id)
    );
    """
    tables = parse_schema_tables(ddl)
    assert tables["orders"].foreign_key_columns["customer_id"] == "INT"
    assert tables["line_items"].foreign_key_columns["order_id"] == "INT"


def test_aggregate_collects_schema_profile(tmp_path: Path) -> None:
    json_path = tmp_path / "schema.jsonl"
    records = [
        {
            "id": "schema.sql#1",
            "mode": "schema",
            "normalized_sql": "CREATE TABLE sales (id INT PRIMARY KEY, customer_id INT REFERENCES customers(customer_id), amount DECIMAL(10,2), created_at DATE)",
            "statement_type": "DDL",
        },
        {
            "id": "schema.sql#2",
            "mode": "schema",
            "normalized_sql": "CREATE TABLE events (event_id BIGINT, event_ts TIMESTAMP, PRIMARY KEY (event_id))",
            "statement_type": "DDL",
        },
    ]
    json_path.write_text("\n".join(json.dumps(rec) for rec in records), encoding="utf-8")

    bundle = aggregate_jsonl_paths([json_path])
    profile = bundle.schema_profile
    assert profile is not None
    assert profile.table_count == 2
    assert profile.total_columns == 6
    assert profile.column_type_counts["INT"] == 3
    assert profile.column_type_counts["DECIMAL"] == 1
    assert profile.primary_key_type_counts["INT"] == 2
    assert profile.foreign_key_type_counts["INT"] == 1


def test_parse_schema_tables_with_mixed_statements() -> None:
    ddl = """
    CONNECT TO TPCD;
    CREATE TABLE widgets (
        widget_id INTEGER PRIMARY KEY,
        description VARCHAR(50)
    );
    COMMIT WORK;
    """
    tables = parse_schema_tables(ddl)
    assert "widgets" in tables
    meta = tables["widgets"]
    assert meta.columns["widget_id"] == "INT"
    assert meta.primary_key_columns["widget_id"] == "INT"
