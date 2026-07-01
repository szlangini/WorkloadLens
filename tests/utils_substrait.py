from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import duckdb


def _load_local_extension(con: duckdb.DuckDBPyConnection, path: str | None) -> bool:
    if not path:
        return False
    candidate = Path(path)
    if not candidate.exists():
        return False
    try:
        con.execute(f"LOAD '{candidate}'")
        return True
    except duckdb.InvalidInputException:
        # Version mismatch or incompatible build; fall back to community install.
        return False
    except Exception:
        return False


def _install_from_community(con: duckdb.DuckDBPyConnection) -> bool:
    con.execute("INSTALL substrait FROM community")
    con.execute("LOAD substrait")
    return True


@lru_cache(maxsize=1)
def has_substrait_support() -> bool:
    os.environ.setdefault("DUCKDB_ALLOW_UNSIGNED_EXTENSIONS", "1")
    try:
        con = duckdb.connect(config={"allow_unsigned_extensions": "true"})
        try:
            path = os.environ.get("SQLCOV_SUBSTRAIT_PATH")
            if _load_local_extension(con, path):
                return True
            return _install_from_community(con)
        finally:
            con.close()
    except Exception:
        return False


def ensure_substrait_loaded(con: duckdb.DuckDBPyConnection) -> None:
    os.environ.setdefault("DUCKDB_ALLOW_UNSIGNED_EXTENSIONS", "1")
    path = os.environ.get("SQLCOV_SUBSTRAIT_PATH")
    if _load_local_extension(con, path):
        return
    _install_from_community(con)
