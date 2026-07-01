import duckdb
import pytest
from workloadlens.core.plan import get_substrait_json
from tests.utils_substrait import has_substrait_support, ensure_substrait_loaded


@pytest.mark.skipif(
    not has_substrait_support(),
    reason="Substrait extension not available (set SQLCOV_SUBSTRAIT_PATH or allow INSTALL substrait FROM community)",
)
def test_get_substrait_json_smoke():
    con = duckdb.connect(config={"allow_unsigned_extensions": "true"})
    ensure_substrait_loaded(con)
    js = get_substrait_json(con, "SELECT 1")
    assert js and js.startswith("{")
