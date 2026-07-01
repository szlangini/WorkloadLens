from __future__ import annotations

from pathlib import Path
from typing import Iterable, Union


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _candidate_directories() -> list[Path]:
    root = _project_root()
    tests_dir = Path(__file__).resolve().parent
    candidates = [
        root / "queries",
        root / "queries" / "all",
        root / "queries" / "modified",
        tests_dir / "queries",
        tests_dir / "queries" / "all",
    ]
    ordered: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if path.exists() and path not in seen:
            ordered.append(path)
            seen.add(path)
    return ordered


def _candidate_names(identifier: Union[str, int]) -> Iterable[str]:
    names: list[str] = []
    if isinstance(identifier, int):
        names.append(f"q{identifier:03d}.sql")
        names.append(f"query{identifier}.sql")
    else:
        base = identifier
        names.append(base)
        lowered = base.lower()
        if lowered.startswith("q"):
            stem = lowered[1:].split(".", 1)[0]
            if stem.isdigit():
                names.append(f"query{int(stem)}.sql")
        elif lowered.startswith("query"):
            digits = "".join(ch for ch in lowered if ch.isdigit())
            if digits:
                names.append(f"q{int(digits):03d}.sql")
    seen: set[str] = set()
    for name in names:
        if name not in seen:
            seen.add(name)
            yield name


def resolve_query_path(identifier: Union[str, int]) -> Path:
    """
    Return the filesystem path for a TPC-DS query, supporting both legacy names like
    'query12.sql' and the newer zero-padded naming ('q012.sql').
    """
    for name in _candidate_names(identifier):
        for directory in _candidate_directories():
            candidate = directory / name
            if candidate.exists():
                return candidate
    raise FileNotFoundError(f"Unable to locate TPC-DS query for identifier {identifier!r}")

