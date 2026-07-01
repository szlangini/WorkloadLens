# workloadlens/tests/test_plan_json_parser.py
from workloadlens.coverage.events import summarize_substrait_json
import json

def test_summarize_substrait_json_counts_ops_and_joins():
    # Minimal synthetic Substrait-like JSON
    plan = {
        "relations": [
            {"rel": {
                "project": {
                    "input": {"join": {
                        "type": "INNER",
                        "left": {"read": {}},
                        "right": {"read": {}}
                    }}
                }
            }}
        ]
    }
    ops, joins, types = summarize_substrait_json(json.dumps(plan))
    assert ops.get("project", 0) >= 1
    assert ops.get("join", 0) >= 1
    assert ops.get("scan", 0) >= 2
    assert joins.get("INNER", 0) >= 1
    assert isinstance(types, dict)
