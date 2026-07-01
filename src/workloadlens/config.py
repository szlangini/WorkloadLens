from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional
import json

DEFAULT_CONFIG_FILENAME = "workloadlens.json"


def _resolve_path(base: Path, value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


@dataclass
class ProjectConfig:
    """Serializable project configuration for the pipeline command."""

    schema: str
    queries: str
    data_dir: Optional[str] = None
    output_dir: str = "outputs"
    coverage_json: str = "coverage.jsonl"
    data_json: str = "data_metrics.jsonl"
    report_pdf: str = "workloadlens_report.pdf"
    scope: str = "all"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProjectConfig":
        payload = dict(data)
        payload.setdefault("output_dir", "outputs")
        payload.setdefault("coverage_json", "coverage.jsonl")
        payload.setdefault("data_json", "data_metrics.jsonl")
        payload.setdefault("report_pdf", "workloadlens_report.pdf")
        payload.setdefault("scope", "all")
        return cls(**payload)

    def resolve_schema(self, config_path: Path) -> Path:
        return _resolve_path(config_path.parent, self.schema)

    def resolve_queries(self, config_path: Path) -> str:
        queries = Path(self.queries)
        if queries.is_absolute() or any(ch in self.queries for ch in "*?["):
            # Leave existing glob/absolute strings unchanged
            return self.queries
        return str(_resolve_path(config_path.parent, queries))

    def resolve_data_dir(self, config_path: Path) -> Optional[Path]:
        if not self.data_dir:
            return None
        return _resolve_path(config_path.parent, self.data_dir)

    def resolve_output_dir(self, config_path: Path) -> Path:
        return _resolve_path(config_path.parent, self.output_dir)

    def resolve_coverage_json(self, config_path: Path) -> Path:
        base = self.resolve_output_dir(config_path)
        return _resolve_path(base, self.coverage_json)

    def resolve_data_json(self, config_path: Path) -> Path:
        base = self.resolve_output_dir(config_path)
        return _resolve_path(base, self.data_json)

    def resolve_report_pdf(self, config_path: Path) -> Path:
        base = self.resolve_output_dir(config_path)
        return _resolve_path(base, self.report_pdf)


def load_config(path: Path) -> ProjectConfig:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Configuration file must contain a JSON object")
    return ProjectConfig.from_dict(data)


def save_config(config: ProjectConfig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.to_dict(), indent=2), encoding="utf-8")
