from __future__ import annotations

import pytest

from workloadlens.core.plan import connect, apply_ddl, get_substrait_json
from workloadlens.coverage.events import summarize_substrait_json
from workloadlens.utils.math import counter_shares
from tests.utils_substrait import has_substrait_support


@pytest.mark.skipif(
    not has_substrait_support(),
    reason="Substrait extension not available (set SQLCOV_SUBSTRAIT_PATH or allow INSTALL substrait FROM community)",
)
def test_substrait_type_histogram_distribution():
    schema_sql = """
    CREATE TABLE sales (
        id INTEGER,
        amount DECIMAL(12, 2),
        flag BOOLEAN,
        category VARCHAR
    );
    CREATE TABLE dim (
        id INTEGER,
        region VARCHAR,
        created DATE
    );
    """

    query = """
    WITH high_sales AS (
        SELECT s.id, s.amount, s.flag, s.category
        FROM sales s
        WHERE s.amount > 100 AND s.flag
    )
    SELECT d.region, SUM(h.amount) AS total_amount, COUNT(*) AS order_count
    FROM high_sales h
    JOIN dim d ON h.id = d.id
    WHERE d.created > DATE '2020-01-01'
    GROUP BY d.region
    HAVING SUM(h.amount) > 200
    ORDER BY total_amount DESC
    LIMIT 10
    """

    con, has_substrait = connect()
    if not has_substrait:
        pytest.skip("DuckDB connection does not have Substrait support")
    try:
        apply_ddl(con, schema_sql)
        plan_json = get_substrait_json(con, query)
    finally:
        con.close()

    op_counts, join_counts, type_hist = summarize_substrait_json(plan_json)

    assert op_counts.get("aggregate", 0) >= 1
    assert join_counts.get("JOIN_TYPE_INNER", 0) >= 1

    type_map = {name: dict(counter) for name, counter in type_hist.items()}

    assert type_map.get("scan", {}).get("INT", 0) >= 2
    assert type_map.get("scan", {}).get("DECIMAL", 0) >= 1
    assert type_map.get("scan", {}).get("TEXT", 0) >= 1

    assert type_map.get("filter", {}).get("DECIMAL", 0) >= 1
    assert type_map.get("filter", {}).get("INT", 0) >= 1

    assert type_map.get("join", {}).get("INT", 0) >= 1

    assert type_map.get("aggregate", {}).get("DECIMAL", 0) >= 1
    assert type_map.get("group_by", {}).get("TEXT", 0) >= 1

    assert type_map.get("aggregate_result", {}).get("DECIMAL", 0) >= 1

    assert type_map.get("sort", {}).get("DECIMAL", 0) >= 1

    share_map = {name: counter_shares(counter) for name, counter in type_hist.items()}
    assert abs(share_map.get("scan", {}).get("INT", 0.0) - 0.5) < 1e-9
    assert abs(share_map.get("scan", {}).get("DECIMAL", 0.0) - 0.25) < 1e-9
    assert abs(share_map.get("scan", {}).get("TEXT", 0.0) - 0.25) < 1e-9
    assert abs(share_map.get("join", {}).get("INT", 0.0) - 1.0) < 1e-9
