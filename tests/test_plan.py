from __future__ import annotations

import duckdb

from workloadlens.core.plan import apply_ddl, apply_views


def test_apply_ddl_skips_non_create_statements() -> None:
    con = duckdb.connect()
    try:
        ddl = """
        CREATE TABLE foo (id INT);
        CONNECT TO TPCD;
        ALTER TABLE TPCD.foo ADD PRIMARY KEY (id);
        """
        apply_ddl(con, ddl)
        result = con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name"
        ).fetchall()
        assert result == [("foo",)]
    finally:
        con.close()


def test_apply_views_handles_mixed_statements() -> None:
    con = duckdb.connect()
    try:
        ddl = """
        CREATE VIEW bar AS SELECT 1 AS id;
        COMMIT WORK;
        """
        apply_views(con, ddl)
        value = con.execute("SELECT * FROM bar").fetchall()
        assert value == [(1,)]
    finally:
        con.close()
