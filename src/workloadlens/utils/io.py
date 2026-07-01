# WorkloadLens/src/workloadlens/utils/io.py
from __future__ import annotations
from pathlib import Path
from typing import List
import glob

def read_text(p: Path) -> str:
    return Path(p).read_text(encoding="utf-8")

def iter_sql_files(spec: str) -> List[Path]:
    """
    Expand a file spec into a sorted list of .sql files.

    Supports:
      • Absolute or relative globs (e.g., /abs/path/*.sql, ./queries/**/*.sql)
      • A directory path (lists *.sql in that directory)
      • A single file path

    Returns an empty list if nothing matches.
    """
    # If the spec includes wildcard characters, prefer glob.glob
    if any(ch in spec for ch in ["*", "?", "["]):
        recursive = "**" in spec
        paths = [Path(p) for p in glob.glob(spec, recursive=recursive)]
    else:
        p = Path(spec)
        if p.is_dir():
            paths = list(p.glob("*.sql"))
        elif p.is_file():
            paths = [p]
        else:
            paths = []

    # Keep only .sql, unique, sorted
    paths = [p for p in paths if p.suffix.lower() == ".sql"]
    # Use string sort for stable order across platforms
    return sorted(set(paths), key=lambda x: str(x))
