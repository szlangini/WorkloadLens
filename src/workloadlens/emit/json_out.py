# WorkloadLens/src/workloadlens/emit/json_out.py
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, Iterable

def dump_jsonl(path: str | Path, records: Iterable[Dict[str, Any]]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
