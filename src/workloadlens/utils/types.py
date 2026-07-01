from __future__ import annotations

from typing import Optional


def normalize_type_label(value: Optional[str]) -> Optional[str]:
    """Normalize type names so related variants collapse into a single label."""
    if not value:
        return None
    label = value.strip().upper()
    if not label:
        return None

    if label.startswith("DECIMAL") or label.startswith("NUMERIC"):
        return "DECIMAL"
    if label.startswith("VARCHAR") or label.startswith("TEXT") or label.startswith("CHAR") or label.startswith("STRING"):
        return "TEXT"
    if label in {"BOOL", "BOOLEAN"}:
        return "BOOLEAN"
    if label in {"FLOAT", "REAL"} or label.startswith("FP"):
        return "DOUBLE"
    if label.startswith("DOUBLE") or label == "DOUBLE PRECISION":
        return "DOUBLE"
    if label in {"TINYINT", "SMALLINT", "BIGINT", "INTEGER", "INT"}:
        return "INT"
    if label.startswith("INT"):
        return "INT"
    if label.startswith("I") and label[1:].isdigit():
        return "INT"
    if label in {"DATE"} or label.startswith("DATE"):
        return "DATE"
    if label.startswith("TIMESTAMP"):
        return "TIMESTAMP"
    if label.startswith("TIME"):
        return "TIME"
    if label.startswith("LIST"):
        return "LIST"
    if label.startswith("MAP"):
        return "MAP"
    if label.startswith("STRUCT"):
        return "STRUCT"
    if label.startswith("JSON"):
        return "JSON"
    return label
