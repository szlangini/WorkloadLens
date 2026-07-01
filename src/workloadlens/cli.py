from __future__ import annotations

# WorkloadLens/src/workloadlens/cli.py
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple, Set
import json
import os
import re
import subprocess
import sys
import time
import typer
from rich.console import Console
from rich.table import Table
from collections import Counter, defaultdict
import sqlglot
from sqlglot import exp

from .version import __version__
from .utils.io import read_text, iter_sql_files
from .core.normalize import extract_functions
from .core.plan import connect, apply_ddl, apply_views, get_substrait_json
from .coverage.events import summarize_substrait_json
from .coverage.predicates import extract_predicates
from .coverage.operators import summarize_ast_operators, count_filter_predicates, count_projection_expressions
from .emit.json_out import dump_jsonl
from .emit.console import print_summary
from .coverage.types_ast import (
    summarize_ast_operator_types,
    collect_filter_expression_ops,
    collect_top_level_order_types,
    collect_limit_metrics,
    collect_union_all_fan_in,
    collect_group_by_key_counts,
    compute_filter_predicate_depth,
    detect_limit_clause,
)
from .utils.schema import parse_schema_types, parse_schema_tables
from .utils.math import counter_shares
from .utils.sqlcleanup import normalize_sql_dialect, classify_statement_type
from .report import (
    aggregate_jsonl_paths,
    aggregate_labeled_jsonl,
    aggregated_report_from_dict,
    aggregated_report_to_dict,
    generate_pdf_report,
    generate_comparison_pdf,
    generate_focused_comparison_pdf,
    generate_types_only_comparison_pdf,
    DEFAULT_PALETTE,
    ReportOptions,
)
from .utils.expr_depth import compute_expression_depths
from .datafiles import (
    list_data_files,
    scan_data_directory,
    write_data_metrics,
    DEFAULT_EXTENSIONS,
    compute_column_mcv_metrics,
)

# Avoid recursion errors on very deep ASTs (e.g., huge UNION chains).
sys.setrecursionlimit(20000)
from .config import (
    ProjectConfig,
    load_config,
    save_config,
    DEFAULT_CONFIG_FILENAME,
)


app = typer.Typer(add_completion=False)
console = Console()

COLUMN_SAMPLE_ENABLED_DEFAULT = True
COLUMN_SAMPLE_THRESHOLD_DEFAULT = 2 * 1024 * 1024 * 1024  # 2 GiB
COLUMN_SAMPLE_FRACTION_DEFAULT = 0.25
DEFAULT_DATA_THREADS = min(32, max(1, os.cpu_count() or 1))

# Operators that may only be visible in the AST (DuckDB plans can omit them)
# - keep the AST counts alongside the plan counters so downstream features work.
_AST_ONLY_OPERATOR_KEYS = {"window", "top_k", "union_all", "cte"}

_SQL_LIMIT_RE = re.compile(r"(?is)\bLIMIT\b")
_SQL_TOP_RE = re.compile(r"(?is)\bSELECT\s+TOP\b")
_SQL_FETCH_RE = re.compile(r"(?is)\bFETCH\s+(?:FIRST|NEXT)\b")
_SQL_ROWNUM_RE = re.compile(r"(?is)\bROWNUM\b")


def _sql_limit_syntaxes(raw_sql: str) -> Set[str]:
    hits: Set[str] = set()
    if not raw_sql:
        return hits
    if _SQL_LIMIT_RE.search(raw_sql):
        hits.add("limit")
    if _SQL_TOP_RE.search(raw_sql):
        hits.add("top")
    if _SQL_FETCH_RE.search(raw_sql):
        hits.add("fetch")
    if _SQL_ROWNUM_RE.search(raw_sql):
        hits.add("rownum")
    return hits


def _append_timestamp_pdf(path: Path) -> Path:
    return path


def _format_bytes_cli(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    number = float(value)
    for unit in units:
        if abs(number) < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{number:.0f} {unit}"
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} PB"


@app.command("version")
def version():
    """Show current WorkloadLens version."""
    console.print(__version__)


@app.command("init")
def init_command(
    schema: Path = typer.Option(..., exists=False, help="Path to CREATE TABLE schema SQL."),
    queries: str = typer.Option("queries/*.sql", help="Glob or path to SQL files."),
    data_dir: Optional[Path] = typer.Option(None, "--data", help="Directory containing data files (.tbl/.csv/.dat)."),
    output_dir: Path = typer.Option(Path("outputs"), "--output-dir", help="Where to store generated artifacts."),
    config_path: Path = typer.Option(Path(DEFAULT_CONFIG_FILENAME), "--config", help="Config file to create."),
    scope: str = typer.Option("all", "--scope", help="Default scope passed to analyze/report (all|queries).", case_sensitive=False),
    layout: str = typer.Option(
        "flat",
        "--layout",
        help="Output layout (flat|tidy). Tidy creates coverage/, data/, reports/ subfolders.",
        case_sensitive=False,
    ),
):
    """Create a reusable WorkloadLens project configuration."""

    scope_value = (scope or "all").lower()
    if scope_value not in {"all", "queries"}:
        raise typer.BadParameter("Scope must be one of: all, queries.")

    layout_value = (layout or "flat").lower()
    if layout_value not in {"flat", "tidy"}:
        raise typer.BadParameter("Layout must be one of: flat, tidy.")

    config = ProjectConfig(
        schema=str(schema),
        queries=queries,
        data_dir=str(data_dir) if data_dir else None,
        output_dir=str(output_dir),
        scope=scope_value,
    )
    if layout_value == "tidy":
        config.coverage_json = "coverage/coverage.jsonl"
        config.data_json = "data/data_metrics.jsonl"
        config.report_pdf = "reports/workloadlens_report.pdf"
    save_config(config, config_path)
    console.print(f"[green]Created config:[/green] {config_path.resolve()}")
    console.print(
        f"Schema -> {schema}\nQueries -> {queries}\nData dir -> {data_dir or '(not set)'}\nOutputs -> {output_dir}"
    )


def is_query_expression(e: exp.Expression) -> bool:
    """
    Return True if the expression is a top-level query statement.
    Robust against SQLGlot version changes (some types may not exist).
    """
    if e is None:
        return False

    candidates = []
    for name in ("Select", "Union", "Values", "Subquery", "Query"):
        t = getattr(exp, name, None)
        if t is not None:
            candidates.append(t)
    if candidates and isinstance(e, tuple(candidates)):
        return True

    sel = getattr(exp, "Select", None)
    vals = getattr(exp, "Values", None)
    has_select = sel is not None and (next(e.find_all(sel), None) is not None)
    has_values = vals is not None and (next(e.find_all(vals), None) is not None)
    return has_select or has_values

def _extract_limit_info(ast: exp.Expression, raw_sql: Optional[str] = None) -> Dict[str, Any]:
    """Return limit metadata for the outermost query."""
    limit_value: Optional[int] = None
    has_limit = False
    has_order = False
    syntax: Optional[str] = None

    select_node = ast if isinstance(ast, exp.Select) else next(ast.find_all(exp.Select), None)
    if isinstance(select_node, exp.Select):
        order_node = select_node.args.get("order")
        has_order = order_node is not None
        has_limit, limit_value, syntax = detect_limit_clause(select_node, raw_sql=raw_sql)
    return {
        "has_limit": has_limit,
        "value": limit_value,
        "has_order_by": has_order,
        "syntax": syntax,
    }


def _non_query_statement_records(
    source: Path,
    sql_text: str,
    canonical_dialect: str,
) -> List[Dict[str, Any]]:
    if not sql_text or not sql_text.strip():
        return []
    try:
        statements = sqlglot.parse(sql_text, error_level="ignore")
    except Exception:
        return []
    records: List[Dict[str, Any]] = []
    for idx, statement in enumerate(statements, start=1):
        if statement is None or is_query_expression(statement):
            continue
        stmt_type = classify_statement_type(statement)
        try:
            normalized = statement.sql(dialect=canonical_dialect)
        except Exception:
            try:
                normalized = statement.sql()
            except Exception:
                normalized = ""
        length_bytes = len(normalized.encode("utf-8")) if normalized else 0
        records.append(
            {
                "id": f"{source.name}#non_query{idx}",
                "file": str(source),
                "mode": "schema",
                "normalized_sql": normalized or None,
                "operators": {},
                "join_types": {},
                "functions": {
                    "projection": [],
                    "aggregate": [],
                    "window": [],
                    "expression": [],
                    "all": [],
                },
                "operator_types": {},
                "operator_shares": None,
                "operator_type_shares": None,
                "features": {},
                "sql_length_bytes": length_bytes,
                "operator_total": 0,
                "statement_type": stmt_type,
                "predicates": [],
                "substrait_json": None,
                "errors": None,
                "warnings": None,
                "unsupported_features": None,
                "is_query": False,
                "operator_source": "ast",
            }
        )
    return records


def _append_schema_statements(coverage_path: Path, schema_path: Path, canonical_dialect: str = "duckdb") -> bool:
    if not schema_path or not schema_path.exists():
        return False
    schema_sql = read_text(schema_path)
    if not schema_sql.strip():
        return False
    records = _non_query_statement_records(schema_path, schema_sql, canonical_dialect)
    if not records:
        return False
    coverage_path.parent.mkdir(parents=True, exist_ok=True)
    with coverage_path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return True


@app.command()
def run(
    query_args: List[str] = typer.Argument(
        None,
        metavar="QUERY",
        help="Optional one or more paths/globs to SQL file(s); equivalent to --queries.",
    ),
    schema: Optional[Path] = typer.Option(None, "--schema", help="Path to schema DDL (tables)"),
    views: Optional[Path] = typer.Option(None, "--views", help="Path to views DDL"),
    queries: Optional[str] = typer.Option(None, "--queries", "-q", help="Glob or path to .sql files"),
    out_jsonl: Optional[Path] = typer.Option("coverage.jsonl", "--json", help="Where to write JSONL output"),
    canonical_dialect: str = typer.Option("duckdb", "--canonical-dialect", help="Dialect to normalize into"),
    read_dialect: Optional[str] = typer.Option(None, "--read-dialect", help="Hint original dialect (e.g., snowflake, trino)"),
    limit: Optional[int] = typer.Option(None, "--limit", help="Only process first N files"),
    debug: bool = typer.Option(False, "--debug", help="Include Substrait JSON and show predicate summary"),
    require_substrait: bool = typer.Option(False, "--require-substrait", help="Fail if Substrait extension is not available"),
    dump_plans: Optional[Path] = typer.Option(None, "--dump-plans", help="Directory to dump per-query Substrait JSON"),
    disable_optimizer: bool = typer.Option(False, "--disable-optimizer", help="Disable DuckDB optimizer (keeps explicit Filter nodes)"),
    preset: Optional[str] = typer.Option(
        None,
        "--preset",
        help="Use built-in defaults (e.g. 'tpcds' loads project TPC-DS schema and queries).",
    ),
    query_name: Optional[str] = typer.Option(
        None,
        "--query-name",
        help="Convenience shortcut: run a single query like 'query47' from the preset directory.",
    ),
    scope: str = typer.Option(
        "all",
        "--scope",
        help="Statement scope to include (all | queries).",
        case_sensitive=False,
    ),
    origin_types: bool = typer.Option(
        True,
        "--origin-types/--no-origin-types",
        help="Attribute logical types back to source columns when available.",
    ),
    skip_schema: bool = typer.Option(
        False,
        "--skip-schema",
        help="Exclude schema/DDL statements from output (still applied for catalog).",
    ),
):
    """Run SQL coverage analysis across queries."""
    preset = (preset or "").lower() or None
    scope_value = (scope or "all").lower()
    if scope_value not in {"all", "queries"}:
        raise typer.BadParameter("Scope must be one of: all, queries.")
    if skip_schema:
        scope_value = "queries"
    include_schema_statements = scope_value != "queries"

    resolved_cli = Path(__file__).resolve()
    parents = resolved_cli.parents
    # src/workloadlens/cli.py -> parents[0]=src/workloadlens, [1]=src, [2]=project root
    project_root = parents[2] if len(parents) >= 3 else Path.cwd()

    explicit_files: List[str] = []

    if query_args and queries:
        raise typer.BadParameter("Provide either positional QUERY or the --queries option, not both.")

    if query_args:
        for arg in query_args:
            explicit_files.extend(iter_sql_files(arg))

    if schema is not None and not schema.exists():
        console.print(f"[red]Schema file not found:[/red] {schema}")
        raise typer.Exit(2)

    default_schema_candidates = [
        project_root / "tests" / "data" / "tpcds_schema.sql",
        project_root / "tpcds-kit" / "tools" / "tpcds.sql",
        project_root / "tpcds_schema.sql",
        Path("tests") / "data" / "tpcds_schema.sql",
        Path("tpcds_schema.sql"),
    ]
    default_query_glob = Path("queries") / "*.sql"
    default_single_query_dir = Path("queries")

    if preset == "tpcds":
        default_query_glob = (project_root / "queries" / "modified" / "*.sql")
        default_single_query_dir = project_root / "queries" / "modified"

    default_schema = None
    for candidate in default_schema_candidates:
        if candidate.exists():
            default_schema = candidate
            break

    if schema is None and default_schema and default_schema.exists():
        schema = default_schema

    query_pattern = queries
    if query_name:
        name = query_name if query_name.endswith(".sql") else f"{query_name}.sql"
        candidate = (default_single_query_dir / name).resolve()
        if not candidate.exists():
            console.print(f"[red]Query not found:[/red] {candidate}")
            raise typer.Exit(2)
        query_pattern = str(candidate)
    elif not query_pattern and not explicit_files:
        query_pattern = str(default_query_glob)

    if explicit_files:
        files = explicit_files
    else:
        files = iter_sql_files(query_pattern)
    if not files:
        typer.echo(f"No SQL files found for {query_pattern}")
        raise typer.Exit(2)
    if limit:
        files = files[:limit]

    console.print(f"[cyan]Processing {len(files)} SQL file(s)[/cyan]")
    preview = files[:5]
    for path in preview:
        console.print(f"  • {Path(path).resolve()}")
    if len(files) > len(preview):
        console.print(f"  • … and {len(files) - len(preview)} more")

    if schema:
        console.print(f"[cyan]Schema:[/cyan] {schema.resolve()}")
    else:
        console.print("[yellow]No schema applied – DuckDB will see empty catalog[/yellow]")
    if views:
        console.print(f"[cyan]Views:[/cyan] {views}")
    if out_jsonl:
        console.print(f"[cyan]JSON output:[/cyan] {Path(out_jsonl).resolve()}")

    schema_sql_text = ""
    views_sql_text = ""

    con, has_substrait = connect()
    if not has_substrait:
        if require_substrait:
            console.print("[red]Substrait extension not available and --require-substrait is set. Aborting.[/red]")
            raise typer.Exit(1)
        console.print("[yellow]Warning:[/yellow] Substrait not available; results will be AST-only.")

    def _configure_connection(connection: duckdb.DuckDBPyConnection, *, verbose: bool) -> None:
        if not disable_optimizer:
            return
        ok = False
        for stmt in (
            "PRAGMA disable_optimizer",
            "PRAGMA enable_optimizer=false",
            "SET enable_optimizer=false",
            "SET optimizer=false",
        ):
            try:
                connection.execute(stmt)
                ok = True
                break
            except Exception:
                pass
        if verbose and not ok:
            console.print("[yellow]Warning:[/yellow] Could not disable optimizer in this DuckDB build; plans may fold filters.")

    def _reinitialize_connection(error_list: List[str]) -> None:
        nonlocal con, has_substrait
        try:
            con.close()
        except Exception:
            pass
        try:
            con, has_substrait = connect()
            _configure_connection(con, verbose=False)
            if schema_sql_text:
                apply_ddl(con, schema_sql_text)
            if views_sql_text:
                apply_views(con, views_sql_text)
        except Exception as recon_err:
            has_substrait = False
            error_list.append(f"plan: reconnect_failed: {type(recon_err).__name__}: {recon_err}")

    _configure_connection(con, verbose=True)

    schema_types: Dict[str, Dict[str, str]] = {}
    aux_records: List[Dict[str, Any]] = []

    if schema:
        schema_sql_text = read_text(schema)
        apply_ddl(con, schema_sql_text)
        schema_types.update(parse_schema_types(schema_sql_text))
        if include_schema_statements:
            aux_records.extend(_non_query_statement_records(schema, schema_sql_text, canonical_dialect))
    if views:
        views_sql_text = read_text(views)
        apply_views(con, views_sql_text)
        schema_types.update(parse_schema_types(views_sql_text))
        if include_schema_statements:
            aux_records.extend(_non_query_statement_records(views, views_sql_text, canonical_dialect))
    if dump_plans:
        dump_plans.mkdir(parents=True, exist_ok=True)

    results: List[Dict[str, Any]] = []
    n_ok = n_ast_only = n_err = 0
    op_hist = Counter()
    join_hist = Counter()
    proj_hist = Counter()
    agg_hist = Counter()
    scalar_hist = Counter()
    type_hist = defaultdict(Counter)
    feature_counter = Counter()
    limit_syntax_counts = Counter()
    limit_missing: List[Tuple[str, str]] = []
    total_queries = 0

    with console.status("Analyzing queries...", spinner="dots"):
        for path in files:
            console.log(f"Analyzing {path}")
            raw = read_text(path)
            cleaned_sql = normalize_sql_dialect(raw)
            sql_length_bytes = len(raw.encode("utf-8"))
            parsed = None
            parse_errors: List[str] = []
            if read_dialect:
                dialect_attempts = [read_dialect]
            else:
                syntax_hits = _sql_limit_syntaxes(cleaned_sql)
                if "top" in syntax_hits:
                    dialect_attempts = ["tsql", None]
                else:
                    dialect_attempts = [None, "tsql"]
            for dialect in dialect_attempts:
                try:
                    candidates = sqlglot.parse(cleaned_sql, read=dialect)
                    parsed = [p for p in candidates if p is not None]
                    if parsed:
                        break
                except Exception as e:
                    name = dialect or "default"
                    parse_errors.append(f"{name}: {e}")

            if not parsed:
                detail = "; ".join(parse_errors) if parse_errors else "unknown parse failure"
                console.print(f"[red]Parse error in {path}: {detail}[/red]")
                n_err += 1
                continue

            for idx, ast in enumerate(parsed, start=1):
                if not is_query_expression(ast):
                    continue
                statement_type = classify_statement_type(ast)

                qid = f"{path.name}#{idx}"
                mode = "unknown"
                errors: List[str] = []
                warnings: List[str] = []
                op_counts: Dict[str, int] = {}
                join_counts: Dict[str, int] = {}
                plan_json = ""
                preds: List[Dict[str, Any]] = []
                unsupported: List[str] = []

                try:
                    norm_sql = ast.sql(dialect=canonical_dialect)
                    funcs = extract_functions(ast)
                except Exception as e:
                    n_err += 1
                    errors.append(f"normalize: {type(e).__name__}: {e}")
                    results.append({
                        "id": qid,
                        "file": str(path),
                        "mode": "error",
                        "error": "; ".join(errors),
                        "operators": {},
                        "join_types": {},
                        "functions": {
                            "projection": [],
                            "aggregate": [],
                            "window": [],
                            "expression": [],
                            "all": [],
                        },
                        "operator_types": {},
                        "operator_types_origin": None,
                        "operator_shares": None,
                        "operator_type_shares": None,
                        "features": {},
                        "sql_length_bytes": sql_length_bytes,
                        "operator_total": 0,
                        "statement_type": statement_type,
                        "limit": _extract_limit_info(ast, raw_sql=raw),
                        "predicates": [],
                        "substrait_json": None,
                        "errors": errors,
                        "warnings": warnings or None,
                        "unsupported_features": unsupported or None,
                        "is_query": False,
                        "operator_source": "ast",
                        "filter_expression_ops": {},
                    })
                    continue

                ast_op_counts, ast_join_counts = summarize_ast_operators(ast)
                projection_expr_total = count_projection_expressions(ast)
                filter_expr_total = count_filter_predicates(ast)
                scalar_depth, filter_depth = compute_expression_depths(ast)
                group_by_key_counts = collect_group_by_key_counts(ast)
                if not group_by_key_counts:
                    group_by_key_counts = [0]
                group_by_key_count = max(group_by_key_counts)

                if schema_types:
                    filter_depth = compute_filter_predicate_depth(ast, schema_types)
                op_counts = ast_op_counts.copy()
                join_counts = ast_join_counts.copy()
                op_type_counts: Dict[str, Counter] = {}
                origin_type_counts: Dict[str, Counter] = {}
                top_level_order_types: Counter[str] = Counter()
                filter_expr_ops: Counter[str] = Counter()
                order_limit_values: List[int] = []
                limit_without_order_values: List[int] = []
                union_all_fan_in_values: List[int] = []
                if schema_types:
                    filter_expr_ops = collect_filter_expression_ops(ast, schema_types)
                    top_level_order_types = collect_top_level_order_types(ast, schema_types)
                order_unique_keys: set[str] = set(top_level_order_types)
                order_limit_values, limit_without_order_values = collect_limit_metrics(ast)
                union_all_fan_in_values = collect_union_all_fan_in(ast)
                if origin_types and schema_types:
                    origin_type_counts = summarize_ast_operator_types(ast, schema_types)

                if has_substrait:
                    try:
                        plan_json = get_substrait_json(con, norm_sql)
                        if plan_json:
                            plan_op_counts, plan_join_counts, op_type_counts = summarize_substrait_json(plan_json)
                            plan_op_counter = Counter(plan_op_counts)
                            ast_op_counter = Counter(ast_op_counts)
                            for key, value in ast_op_counter.items():
                                if key in _AST_ONLY_OPERATOR_KEYS:
                                    if value and plan_op_counter.get(key, 0) < value:
                                        plan_op_counter[key] = value
                                elif key not in plan_op_counter:
                                    plan_op_counter[key] = value
                            op_counts = dict(plan_op_counter)

                            plan_join_counter = Counter(plan_join_counts)
                            ast_join_counter = Counter(ast_join_counts)
                            for key, value in ast_join_counter.items():
                                if key not in plan_join_counter:
                                    plan_join_counter[key] = value
                            join_counts = dict(plan_join_counter)
                            # Reclassify comma joins that land as CROSS in the plan into INNER based
                            # on the AST view, which carries the SQL surface semantics.
                            cross_count = plan_join_counter.get("JOIN_TYPE_CROSS", 0)
                            ast_inner = ast_join_counts.get("JOIN_TYPE_INNER", 0)
                            if cross_count and ast_inner and ast_inner >= cross_count:
                                plan_join_counter.pop("JOIN_TYPE_CROSS", None)
                                plan_join_counter["JOIN_TYPE_INNER"] = (
                                    plan_join_counter.get("JOIN_TYPE_INNER", 0) + cross_count
                                )
                            join_counts = dict(plan_join_counter)
                            preds = extract_predicates(plan_json)
                            if preds:
                                filter_expr_total = len(preds)
                            mode = "plan_typed"
                            n_ok += 1
                            if dump_plans:
                                safe_id = qid.replace("/", "_")
                                (dump_plans / f"{safe_id}.json").write_text(
                                    json.dumps(json.loads(plan_json), indent=2),
                                    encoding="utf-8",
                                )
                        else:
                            mode = "ast_only"
                            n_ast_only += 1
                    except Exception as e:
                        mode = "ast_only"
                        n_ast_only += 1
                        err_msg = f"plan: {type(e).__name__}: {e}"
                        upper_msg = f"{type(e).__name__}: {e}".upper()
                        if "NOTIMPLEMENT" in upper_msg and "WINDOW" in upper_msg:
                            warnings.append(err_msg)
                            unsupported.append("window")
                        else:
                            errors.append(err_msg)
                            fatal = False
                            if type(e).__name__ == "FatalException" or "DATABASE HAS BEEN INVALIDATED" in upper_msg:
                                fatal = True
                            elif type(e).__name__ == "InternalException":
                                if "FAILED TO BIND" in upper_msg or "BIND COLUMN REFERENCE" in upper_msg:
                                    fatal = True
                            if fatal:
                                _reinitialize_connection(errors)
                else:
                    mode = "ast_only"
                    n_ast_only += 1

                operator_type_shares: Dict[str, Dict[str, float]] = {}
                operator_shares: Dict[str, float] = {}

                if not op_type_counts:
                    if origin_type_counts:
                        op_type_counts = {key: Counter(counter) for key, counter in origin_type_counts.items()}

                if op_type_counts:
                    for key, counter in op_type_counts.items():
                        shares = counter_shares(counter)
                        if shares:
                            operator_type_shares[key] = shares
                    sort_counter = op_type_counts.get("sort")
                    if sort_counter:
                        order_unique_keys.update(sort_counter.keys())

                elif origin_type_counts:
                    sort_counter = origin_type_counts.get("sort")
                    if sort_counter:
                        order_unique_keys.update(sort_counter.keys())

                op_hist.update(op_counts)
                join_hist.update(join_counts)
                proj_hist.update(funcs["projection_funcs"])
                agg_hist.update(funcs["agg_funcs"])
                scalar_hist.update(funcs["scalar_funcs"])
                if op_type_counts:
                    for key, counter in op_type_counts.items():
                        if counter:
                            type_hist[key].update(counter)

                if op_counts:
                    operator_shares = counter_shares(Counter(op_counts))

                limit_info = _extract_limit_info(ast, raw_sql=raw)
                total_queries += 1
                syntax = limit_info.get("syntax") or ("unknown" if limit_info.get("has_limit") else None)
                if syntax:
                    limit_syntax_counts[syntax] += 1

                raw_limit_hits = _sql_limit_syntaxes(raw)
                if raw_limit_hits and not limit_info.get("has_limit"):
                    if not order_limit_values and not limit_without_order_values:
                        snippet = " ".join(raw.strip().split())
                        if len(snippet) > 160:
                            snippet = snippet[:157] + "..."
                        limit_missing.append((qid, snippet))
                union_all_total = op_counts.get("union_all", 0)
                features = {
                    "has_join": op_counts.get("join", 0) > 0,
                    "has_group_by": op_counts.get("aggregate", 0) > 0,
                    "has_order_by": op_counts.get("sort", 0) > 0,
                    "has_limit": (op_counts.get("limit", 0) > 0) or bool(limit_info.get("has_limit")),
                    "has_union": union_all_total > 0,
                    "has_cte": op_counts.get("cte", 0) > 0,
                    "has_window": bool(funcs["window_funcs"]),
                }
                if unsupported and "window" in unsupported:
                    features["has_window"] = True
                feature_counter.update({name: 1 for name, enabled in features.items() if enabled})

                operator_total = sum(op_counts.values())

                results.append({
                    "id": qid,
                    "file": str(path),
                    "mode": mode,
                    "normalized_sql": norm_sql,
                    "operators": op_counts,
                    "join_types": join_counts,
                    "functions": {
                        "projection": funcs["projection_funcs"],
                        "aggregate": funcs["agg_funcs"],
                        "window": funcs["window_funcs"],
                        "expression": funcs["scalar_funcs"],
                        "operators": funcs["expression_ops"],
                        "all": funcs["all_funcs"],
                    },
                    "operator_types": {name: dict(counter) for name, counter in (op_type_counts or {}).items()},
                    "operator_types_origin": {
                        name: dict(counter) for name, counter in (origin_type_counts or {}).items()
                    } if origin_type_counts else None,
                    "order_by_top_types": dict(top_level_order_types) if top_level_order_types else None,
                    "order_by_top_types_unique": list(order_unique_keys) if order_unique_keys else None,
                    "operator_shares": operator_shares or None,
                    "operator_type_shares": operator_type_shares or None,
                    "features": features,
                    "sql_length_bytes": sql_length_bytes,
                    "operator_total": operator_total,
                    "statement_type": statement_type,
                    "limit": limit_info,
                    "projection_expression_count": int(projection_expr_total),
                    "filter_expression_count": int(filter_expr_total),
                    "max_scalar_expression_depth": int(scalar_depth),
                    "max_filter_predicate_depth": int(filter_depth),
                    "limit_with_order_values": order_limit_values or None,
                    "limit_without_order_values": limit_without_order_values or None,
                    "union_all_fan_in_values": union_all_fan_in_values or None,
                    "predicates": preds or [],
                    "substrait_json": plan_json if debug else None,
                    "errors": errors or None,
                    "warnings": warnings or None,
                    "unsupported_features": unsupported or None,
                    "is_query": True,
                    "operator_source": "substrait" if mode == "plan_typed" else "ast",
                    "filter_expression_ops": dict(filter_expr_ops),
                    "group_by_key_count": int(group_by_key_count),
                    "group_by_key_counts": [int(value) for value in group_by_key_counts],
                })

    if aux_records:
        schema_count = len(aux_records)
        results = aux_records + results
    else:
        schema_count = 0

    # Write JSONL output
    if out_jsonl:
        json_path = Path(out_jsonl)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        dump_jsonl(json_path, results)
        console.print(f"[green]Results JSON written:[/green] {json_path.resolve()} ({len(results)} records)")

    if results:
        console.print(
            f"[green]Finished {len(results)} statement(s): "
            f"{n_ok} plan_typed, {n_ast_only} ast_only, {n_err} errors"
            f"{f', {schema_count} schema/view statements' if schema_count else ''}[/green]"
        )
    else:
        console.print("[yellow]No statements analyzed.[/yellow]")

    # Optional predicate summary table
    if debug:
        table = Table(title="Detected Predicates (first 10)")
        table.add_column("Query ID")
        table.add_column("Where")
        table.add_column("Function")
        table.add_column("Columns")
        table.add_column("Literals")
        shown = 0
        for r in results:
            for p in r.get("predicates", []):
                table.add_row(
                    r["id"],
                    str(p.get("where")),
                    str(p.get("function")),
                    ", ".join(map(str, p.get("columns") or [])),
                    ", ".join(map(str, p.get("literals") or [])),
                )
                shown += 1
                if shown >= 10:
                    break
            if shown >= 10:
                break
        if shown:
            console.print(table)

        console.print("[bold]Limit detection debug[/bold]")
        console.print(f"Total queries: {total_queries}")
        for key in ("limit", "fetch", "top", "rownum", "unknown"):
            if limit_syntax_counts.get(key):
                console.print(f"Limits ({key}): {limit_syntax_counts.get(key)}")
        if limit_missing:
            console.print("Queries containing LIMIT syntax but not recognized (first 10):")
            for qid, snippet in limit_missing[:10]:
                console.print(f"- {qid}: {snippet}")

    # Print coverage summary
    overall_op_shares = counter_shares(op_hist)
    overall_join_shares = counter_shares(join_hist)

    print_summary(
        n_ok,
        n_ast_only,
        n_err,
        op_hist,
        overall_op_shares,
        join_hist,
        overall_join_shares,
        proj_hist,
        agg_hist,
        scalar_hist,
        {name: Counter(counter) for name, counter in type_hist.items()},
        feature_counter,
        max(len(results) - schema_count, 0),
    )


@app.command("data")
def data_command(
    source: Path = typer.Argument(..., exists=False, help="Directory containing .tbl/.csv/.dat files"),
    out_jsonl: Path = typer.Option(Path("data_stats.jsonl"), "--out", "-o", help="Where to write JSONL metrics"),
    extensions: List[str] = typer.Option(
        [],
        "--ext",
        "-e",
        help="File extension to include (defaults to .tbl, .csv, and .dat). Repeat for multiple extensions.",
    ),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", help="Recurse into subdirectories"),
    count_rows: bool = typer.Option(True, "--rows/--no-rows", help="Count rows (may be slow on large files)"),
    schema: Optional[Path] = typer.Option(
        None, "--schema", help="Schema DDL used to map columns when collecting per-column stats."
    ),
    column_stats: bool = typer.Option(
        False,
        "--column-stats/--no-column-stats",
        help="Collect per-column Most Common Value (MCV) frequencies (requires --schema).",
    ),
    mcv_k: int = typer.Option(20, "--mcv-k", help="Top-K size for Most Common Value share when --column-stats is enabled."),
    column_sample: bool = typer.Option(
        COLUMN_SAMPLE_ENABLED_DEFAULT,
        "--column-sample/--column-no-sample",
        help="Enable sampling when gathering column stats on large datasets.",
    ),
    null_marker: Optional[str] = typer.Option(
        None, "--null-marker", help="Treat this string literal as NULL when scanning data files."
    ),
    column_sample_threshold: int = typer.Option(
        COLUMN_SAMPLE_THRESHOLD_DEFAULT,
        "--column-sample-threshold",
        help="Total data volume (bytes) above which sampling is applied (default: 2 GiB).",
    ),
    column_sample_fraction: float = typer.Option(
        COLUMN_SAMPLE_FRACTION_DEFAULT,
        "--column-sample-fraction",
        help="Fraction of rows to sample per table when sampling is enabled (0-1).",
    ),
    threads: int = typer.Option(
        DEFAULT_DATA_THREADS,
        "--threads",
        "-t",
        help="Worker threads for row counting and column stats (default: auto).",
    ),
):
    """Scan a directory of data files and emit basic table metrics."""

    if not source.exists() or not source.is_dir():
        console.print(f"[red]Directory not found:[/red] {source}")
        raise typer.Exit(1)

    max_threads = max(1, os.cpu_count() or 1)
    worker_threads = max(1, min(int(threads or 1), max_threads))

    exts = extensions or list(DEFAULT_EXTENSIONS)
    metrics = scan_data_directory(
        source,
        extensions=exts,
        recursive=recursive,
        count_rows=count_rows,
        threads=worker_threads,
    )

    records: List[Any] = list(metrics)
    total_bytes = sum(metric.size_bytes for metric in metrics if metric.size_bytes > 0)

    column_metrics: List[Any] = []
    if column_stats:
        if schema is None:
            console.print("[red]--column-stats requires --schema to map column names.[/red]")
            raise typer.Exit(1)
        if mcv_k <= 0:
            console.print("[red]--mcv-k must be greater than zero.[/red]")
            raise typer.Exit(1)
        sample_fraction: Optional[float] = None
        if column_sample:
            if column_sample_fraction <= 0 or column_sample_fraction > 1:
                console.print("[red]--column-sample-fraction must be between 0 and 1.[/red]")
                raise typer.Exit(1)
            if column_sample_threshold > 0 and total_bytes > column_sample_threshold:
                sample_fraction = column_sample_fraction
                console.print(
                    f"[yellow]Total data volume {_format_bytes_cli(total_bytes)} exceeds "
                    f"{_format_bytes_cli(column_sample_threshold)}; sampling approximately "
                    f"{sample_fraction * 100:.1f}% of rows for column stats.[/yellow]"
                )
        schema_sql = read_text(schema)
        schema_tables = parse_schema_tables(schema_sql)
        table_files = list_data_files(source, extensions=exts, recursive=recursive)
        column_metrics, histogram_metrics = compute_column_mcv_metrics(
            table_files,
            schema_tables,
            requested_k=mcv_k,
            logger=console.print,
            null_marker=null_marker,
            sample_fraction=sample_fraction,
            threads=worker_threads,
        )
        if column_metrics:
            table_count = len({metric.table for metric in column_metrics if getattr(metric, "table", None)})
            console.print(
                f"[green]Computed column stats for {len(column_metrics)} column(s) across {table_count} table(s).[/green]"
            )
        if histogram_metrics:
            console.print(
                f"[green]Computed histogram skew for {len(histogram_metrics)} numeric column(s).[/green]"
            )
        records.extend(column_metrics)
        records.extend(histogram_metrics)

    write_data_metrics(records, out_jsonl)
    resolved = out_jsonl.resolve()
    if metrics:
        console.print(
            f"[green]Captured metrics for {len(metrics)} table(s);[/green] wrote {resolved}"
        )
    else:
        console.print(f"[yellow]No matching data files found;[/yellow] wrote {resolved}")


def _invoke_cli(args: List[str]) -> None:
    command = [sys.executable, "-m", "workloadlens.cli", *args]
    result = subprocess.run(command)
    if result.returncode != 0:
        raise typer.Exit(result.returncode)


def _estimate_data_volume_bytes(source: Path) -> int:
    """Cheaply approximate the total size of data files under a directory."""

    if not source.exists():
        return 0

    allowed_exts = {ext.lower() for ext in DEFAULT_EXTENSIONS}
    total = 0
    try:
        entries = source.rglob("*")
    except OSError:
        return 0

    for entry in entries:
        try:
            if entry.is_file() and entry.suffix.lower() in allowed_exts:
                try:
                    total += entry.stat().st_size
                except OSError:
                    continue
        except OSError:
            continue
    return total


def _normalize_ndv_denominator(value: str) -> str:
    normalized = (value or "rows").lower()
    if normalized not in {"rows", "non_null"}:
        raise typer.BadParameter("NDV ratio denominator must be one of: rows, non_null.")
    return normalized


def _normalize_ndv_universe(value: str) -> str:
    normalized = (value or "all").lower()
    if normalized not in {"all", "eligible"}:
        raise typer.BadParameter("NDV column universe must be one of: all, eligible.")
    return normalized


@app.command("pipeline")
def pipeline_command(
    config_path: Path = typer.Option(Path(DEFAULT_CONFIG_FILENAME), "--config", help="Project config file"),
    skip_analyze: bool = typer.Option(False, "--skip-analyze", help="Skip the analyze/run step"),
    skip_data: bool = typer.Option(False, "--skip-data", help="Skip the data metrics step"),
    skip_report: bool = typer.Option(False, "--skip-report", help="Skip PDF generation"),
    require_substrait: bool = typer.Option(False, "--require-substrait", help="Fail if Substrait isn't available"),
    verbose: bool = typer.Option(False, "--verbose", help="Print timing/sampling summary after pipeline runs"),
    auto_open: bool = typer.Option(True, "--open/--no-open", help="Auto-open the PDF after rendering"),
    column_stats: bool = typer.Option(
        True, "--column-stats/--no-column-stats", help="Collect per-column MCV stats when scanning data."
    ),
    mcv_k: int = typer.Option(20, "--mcv-k", help="Top-K size for per-column MCV share."),
    threads: int = typer.Option(
        DEFAULT_DATA_THREADS,
        "--threads",
        "-t",
        help="Worker threads for data metrics (rows and column stats).",
    ),
    ndv_ratio_denominator: str = typer.Option(
        "non_null",
        "--ndv-ratio-denominator",
        help="Denominator for NDV ratio plots (rows|non_null).",
        case_sensitive=False,
    ),
    ndv_column_universe: str = typer.Option(
        "all",
        "--ndv-column-universe",
        help="Column universe for NDV ratio plots (all|eligible).",
        case_sensitive=False,
    ),
    ndv_debug: bool = typer.Option(False, "--ndv-debug", help="Print NDV ratio debug summary"),
    ndv_debug_output: Optional[Path] = typer.Option(
        None, "--ndv-debug-output", help="Write NDV ratio debug summary to this file (implies --ndv-debug)"
    ),
    json_out: Optional[Path] = typer.Option(None, "--json-out", help="Write aggregated metrics to this JSON file"),
    export_pngs: bool = typer.Option(False, "--export-pngs", help="Also export each page as a PNG (defaults to OUT stem)"),
    png_dir: Optional[Path] = typer.Option(None, "--png-dir", help="Directory for exported PNGs (implies --export-pngs)"),
):
    """Execute analyze, data scan, and report generation using a project config."""

    try:
        config = load_config(config_path)
    except Exception as exc:  # pragma: no cover - surfaced to CLI
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    output_dir = config.resolve_output_dir(config_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    ndv_ratio_value = _normalize_ndv_denominator(ndv_ratio_denominator)
    ndv_universe_value = _normalize_ndv_universe(ndv_column_universe)
    ndv_debug = ndv_debug or ndv_debug_output is not None

    coverage_path = config.resolve_coverage_json(config_path)
    data_path = config.resolve_data_json(config_path)
    report_path = config.resolve_report_pdf(config_path)
    schema_path = config.resolve_schema(config_path)
    queries_value = config.resolve_queries(config_path)
    data_dir = config.resolve_data_dir(config_path)

    console.print(f"[cyan]Using config:[/cyan] {config_path.resolve()}")

    start_time = time.perf_counter()
    stage_times: Dict[str, float] = {}
    sampling_messages: List[str] = []

    if not skip_analyze:
        analyze_start = time.perf_counter()
        run_args = [
            "run",
            "--schema",
            str(schema_path),
            "--queries",
            queries_value,
            "--json",
            str(coverage_path),
            "--scope",
            config.scope,
        ]
        if require_substrait:
            run_args.append("--require-substrait")
        console.print("[green]Analyzing queries...[/green]")
        _invoke_cli(run_args)
        stage_times["analyze"] = time.perf_counter() - analyze_start
        if config.scope == "queries" and schema_path and schema_path.exists():
            appended = _append_schema_statements(coverage_path, schema_path)
            if appended:
                console.print("[cyan]Added schema statements to coverage for reporting (scope='queries').[/cyan]")

    if data_dir and not skip_data:
        data_start = time.perf_counter()
        data_args = [
            "data",
            str(data_dir),
            "--out",
            str(data_path),
            "--threads",
            str(max(1, threads)),
            "--rows",
        ]
        sampling_flag: Optional[str] = None
        if column_stats:
            if not schema_path:
                console.print("[yellow]Column stats requested but schema is missing; skipping column stats.[/yellow]")
            else:
                data_args += ["--column-stats", "--schema", str(schema_path)]
                if mcv_k <= 0:
                    console.print("[red]--mcv-k must be greater than zero.[/red]")
                    raise typer.Exit(1)
                data_args += ["--mcv-k", str(mcv_k)]
                column_sample = COLUMN_SAMPLE_ENABLED_DEFAULT
                column_sample_fraction = COLUMN_SAMPLE_FRACTION_DEFAULT
                column_sample_threshold = COLUMN_SAMPLE_THRESHOLD_DEFAULT
                if (
                    verbose
                    and column_sample
                    and 0 < column_sample_fraction <= 1
                    and column_sample_threshold > 0
                ):
                    total_bytes = _estimate_data_volume_bytes(data_dir)
                    if total_bytes > column_sample_threshold:
                        sampling_flag = (
                            f"Sampled ~{column_sample_fraction * 100:.1f}% of rows "
                            f"(data volume {_format_bytes_cli(total_bytes)} > "
                            f"{_format_bytes_cli(column_sample_threshold)})"
                        )
        console.print("[green]Scanning data files...[/green]")
        _invoke_cli(data_args)
        stage_times["data"] = time.perf_counter() - data_start
        if sampling_flag:
            sampling_messages.append(sampling_flag)
    elif not data_dir:
        console.print("[yellow]No data directory configured; skipping data metrics.[/yellow]")
    elif skip_data:
        console.print("[yellow]Data metrics skipped (--skip-data).[/yellow]")

    if not skip_report:
        report_start = time.perf_counter()
        report_args = [
            "report",
            "--sql",
            str(coverage_path),
            "--out",
            str(report_path),
            "--scope",
            config.scope,
            "--ndv-ratio-denominator",
            ndv_ratio_value,
            "--ndv-column-universe",
            ndv_universe_value,
        ]
        if export_pngs:
            report_args.append("--export-pngs")
        if png_dir:
            report_args += ["--png-dir", str(png_dir)]
        if data_dir:
            report_args += ["--data", str(data_path)]
        if json_out:
            report_args += ["--json-out", str(json_out)]
        if not auto_open:
            report_args.append("--no-open")
        if ndv_debug:
            report_args.append("--ndv-debug")
        if ndv_debug_output:
            report_args += ["--ndv-debug-output", str(ndv_debug_output)]
        console.print("[green]Rendering PDF report...[/green]")
        _invoke_cli(report_args)
        stage_times["report"] = time.perf_counter() - report_start

    total_elapsed = time.perf_counter() - start_time
    if verbose:
        console.print("[cyan]Pipeline telemetry[/cyan]")
        for name in ("analyze", "data", "report"):
            if name in stage_times:
                console.print(f"  • {name:<8}: {stage_times[name]:.2f}s")
        console.print(f"  • total   : {total_elapsed:.2f}s")
        if sampling_messages:
            console.print("  • sampling:")
            for msg in sampling_messages:
                console.print(f"    - {msg}")


def _workload_signal_vector(report) -> Dict[str, float]:
    """Flatten operator_type counts into a normalised percentage vector for L1 sorting."""
    stats = report.query_statements if report.query_statements.total_queries else report.all_statements
    out: Dict[str, float] = {}
    types = ("INT", "TEXT", "DECIMAL", "DATE")
    type_map = {
        "INT": ("INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT"),
        "TEXT": ("TEXT", "STRING", "VARCHAR", "CHAR"),
        "DECIMAL": ("DECIMAL", "NUMERIC", "DOUBLE", "FLOAT", "REAL"),
        "DATE": ("DATE", "TIMESTAMP", "TIME", "DATETIME"),
    }
    for role in ("filter", "join_key", "aggregate", "group_by"):
        counts = stats.operator_type_counts.get(role, {})
        total = sum(counts.values()) or 1
        for canonical in types:
            value = sum(counts.get(raw, 0) for raw in type_map[canonical])
            out[f"{role}:{canonical}"] = value / total * 100.0
    return out


def _l1_distance(a: Dict[str, float], b: Dict[str, float]) -> float:
    keys = set(a.keys()) | set(b.keys())
    return sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in keys)


def _order_workloads(aggregated, sort_by: str, reference: Optional[str]):
    """Return a new ordered dict of aggregated workloads per --sort-by."""
    if not aggregated or sort_by == "input":
        return aggregated
    labels = list(aggregated.keys())
    if sort_by == "label":
        labels = sorted(labels, key=lambda l: l.lower())
    elif sort_by == "distance":
        ref_label = reference if reference and reference in aggregated else labels[0]
        ref_vec = _workload_signal_vector(aggregated[ref_label])
        labels = sorted(
            labels,
            key=lambda l: (
                0 if l == ref_label else 1,
                _l1_distance(_workload_signal_vector(aggregated[l]), ref_vec),
                l.lower(),
            ),
        )
    return {label: aggregated[label] for label in labels}


@app.command("compare")
def compare_command(
    config_entries: List[str] = typer.Option(
        [],
        "--config",
        "-c",
        help="Label=path/to/workloadlens.json (repeat for multiple workloads).",
    ),
    out: Path = typer.Option(Path("workloadlens_comparison.pdf"), "--out", "-o", help="Output PDF path"),
    palette: Optional[str] = typer.Option(None, "--palette", help="Comma-separated list of hex colors"),
    ndv_ratio_denominator: str = typer.Option(
        "non_null",
        "--ndv-ratio-denominator",
        help="Denominator for NDV ratio plots (rows|non_null).",
        case_sensitive=False,
    ),
    ndv_column_universe: str = typer.Option(
        "all",
        "--ndv-column-universe",
        help="Column universe for NDV ratio plots (all|eligible).",
        case_sensitive=False,
    ),
    ndv_debug: bool = typer.Option(False, "--ndv-debug", help="Print NDV ratio debug summary"),
    ndv_debug_output: Optional[Path] = typer.Option(
        None, "--ndv-debug-output", help="Write NDV ratio debug summary to this file (implies --ndv-debug)"
    ),
    scope: str = typer.Option("queries", "--scope", help="Render scope (all|queries)", case_sensitive=False),
    origin_types: bool = typer.Option(True, "--origin-types/--no-origin-types", help="Include origin logical types"),
    types_only: bool = typer.Option(
        False,
        "--types-only",
        help="(Hidden) Render only logical types for operators and stored columns",
        hidden=True,
    ),
    focused_only: bool = typer.Option(
        False,
        "--focused-only",
        help="(Hidden) Render only limit/group-by/union fan-in comparisons",
        hidden=True,
    ),
    export_pngs: bool = typer.Option(False, "--export-pngs", help="Also export each page as a PNG (defaults to OUT stem)"),
    png_dir: Optional[Path] = typer.Option(None, "--png-dir", help="Directory for exported PNGs (implies --export-pngs)"),
    auto_open: bool = typer.Option(True, "--open/--no-open", help="Automatically open the generated PDF"),
    json_out: Optional[Path] = typer.Option(None, "--json-out", help="Write aggregated metrics to this JSON file"),
    aggregated_in: List[Path] = typer.Option(
        [],
        "--aggregated-in",
        help="Path to aggregated metrics JSON produced by a prior compare/report (--json-out). Repeatable.",
    ),
    render_style: str = typer.Option(
        "bars",
        "--render-style",
        help="Rendering style for grouped panels: bars (default), heatmap, dot-plot, small-multiples, paper.",
        case_sensitive=False,
    ),
    sort_by: str = typer.Option(
        "input",
        "--sort-by",
        help="Order workloads by: input (default, preserves -c order), label (alphabetical), distance (L1 distance from --reference).",
        case_sensitive=False,
    ),
    reference: Optional[str] = typer.Option(
        None,
        "--reference",
        help="Label used as the reference for --sort-by distance. Defaults to the first workload.",
    ),
    paper_style: bool = typer.Option(
        False,
        "--paper-style",
        help="Shorthand for --render-style paper. Use with --preset fig3|fig4|fig7|all.",
    ),
    paper_preset: Optional[str] = typer.Option(
        None,
        "--preset",
        help="Paper-style preset: fig3, fig4, fig7, or all. Requires --paper-style (or --render-style paper).",
        case_sensitive=False,
    ),
    paper_focus: Optional[str] = typer.Option(
        None,
        "--focus",
        help="Comma-separated workload labels to keep in paper-style output (default: TPC-DS,PROD-DS,Snowflake for bars).",
    ),
    paper_baselines: Optional[Path] = typer.Option(
        None,
        "--baselines",
        help="Path to production_baselines.yaml for paper-style baseline overlays.",
    ),
):
    """Compare multiple WorkloadLens configs (e.g., TPCH vs TPCDS) in a single PDF."""

    scope_value = (scope or "queries").lower()
    if scope_value not in {"all", "queries"}:
        raise typer.BadParameter("Scope must be one of: all, queries.")

    # Validate paper-style flags up-front so the user sees the error before
    # the config-loading noise.
    paper_preset_check = (paper_preset or "").lower() or None
    if paper_style or paper_preset_check:
        if not paper_preset_check:
            raise typer.BadParameter("--paper-style requires --preset {fig3|fig4|fig7|all}.")
        if paper_preset_check not in {"fig3", "fig4", "fig7", "all"}:
            raise typer.BadParameter("--preset must be one of: fig3, fig4, fig7, all.")

    ndv_ratio_value = _normalize_ndv_denominator(ndv_ratio_denominator)
    ndv_universe_value = _normalize_ndv_universe(ndv_column_universe)
    ndv_debug = ndv_debug or ndv_debug_output is not None

    if not config_entries and not aggregated_in:
        console.print("[red]Provide --config entries and/or --aggregated-in JSON files.[/red]")
        raise typer.Exit(1)

    config_map = _parse_workload_arguments(config_entries)
    coverage_map: Dict[str, List[Path]] = {}
    data_map: Dict[str, List[Path]] = {}
    metadata_sets: Dict[str, Dict[str, set[str]]] = {}

    for label, paths in config_map.items():
        coverage_files: List[Path] = []
        data_files: List[Path] = []
        meta_fields = metadata_sets.setdefault(
            label,
            {
                "config_paths": set(),
                "schema_paths": set(),
                "query_specs": set(),
                "data_directories": set(),
                "coverage_files": set(),
                "data_metrics": set(),
            },
        )
        for cfg_path in paths:
            try:
                cfg = load_config(cfg_path)
            except Exception as exc:
                console.print(f"[yellow]Skipping config {cfg_path}: {exc}[/yellow]")
                continue
            meta_fields["config_paths"].add(str(cfg_path.resolve()))
            schema_path = cfg.resolve_schema(cfg_path)
            if schema_path:
                meta_fields["schema_paths"].add(str(schema_path))
            queries_value = cfg.resolve_queries(cfg_path)
            if queries_value:
                meta_fields["query_specs"].add(str(queries_value))
            data_dir = cfg.resolve_data_dir(cfg_path)
            if data_dir:
                meta_fields["data_directories"].add(str(data_dir))
            coverage_path = cfg.resolve_coverage_json(cfg_path)
            if coverage_path.exists():
                coverage_files.append(coverage_path)
                meta_fields["coverage_files"].add(str(coverage_path))
            else:
                console.print(f"[yellow]Coverage file not found for {label}: {coverage_path}[/yellow]")
            data_path = cfg.resolve_data_json(cfg_path)
            if data_path and data_path.exists():
                data_files.append(data_path)
                meta_fields["data_metrics"].add(str(data_path))
            elif data_path:
                console.print(f"[yellow]Data metrics file not found for {label}: {data_path}[/yellow]")
        if coverage_files:
            coverage_map[label] = coverage_files
        else:
            console.print(f"[yellow]Skipping workload '{label}': no coverage files were found.[/yellow]")
        if data_files:
            data_map[label] = data_files

    if not coverage_map and not aggregated_in:
        console.print("[red]No workloads with coverage results were found.[/red]")
        raise typer.Exit(1)

    metadata_payload: Dict[str, Dict[str, Any]] = {}
    for label, fields in metadata_sets.items():
        if label not in coverage_map:
            continue
        payload = {field: sorted(values) for field, values in fields.items() if values}
        payload["workload_label"] = label
        metadata_payload[label] = payload

    aggregated = aggregate_labeled_jsonl(coverage_map, data_map or None, metadata=metadata_payload or None) if coverage_map else {}

    aggregated_from_files: Dict[str, Any] = {}
    for agg_path in aggregated_in:
        try:
            payload = json.loads(Path(agg_path).read_text(encoding="utf-8"))
        except Exception as exc:
            console.print(f"[yellow]Skipping aggregated input {agg_path}: {exc}[/yellow]")
            continue
        if not isinstance(payload, dict):
            console.print(f"[yellow]Skipping aggregated input {agg_path}: expected JSON object mapping labels.[/yellow]")
            continue
        for label, report_data in payload.items():
            if not isinstance(report_data, dict):
                console.print(f"[yellow]Skipping label '{label}' in {agg_path}: value must be an object.[/yellow]")
                continue
            try:
                aggregated_from_files[label] = aggregated_report_from_dict(report_data)
            except Exception as exc:
                console.print(f"[yellow]Skipping label '{label}' in {agg_path}: {exc}[/yellow]")
                continue
    if aggregated_from_files:
        console.print(f"[green]Loaded {len(aggregated_from_files)} workload(s) from aggregated JSON input.[/green]")

    aggregated.update(aggregated_from_files)
    if not aggregated:
        console.print("[red]Unable to load any workloads. Provide configs or --aggregated-in JSON files.[/red]")
        raise typer.Exit(1)

    palette_values = None
    if palette:
        palette_values = [color.strip() for color in palette.split(",") if color.strip()]
        if not palette_values:
            palette_values = None

    ndv_ratio_value = _normalize_ndv_denominator(ndv_ratio_denominator)
    ndv_universe_value = _normalize_ndv_universe(ndv_column_universe)
    ndv_debug = ndv_debug or ndv_debug_output is not None

    out = _append_timestamp_pdf(out)

    png_output_dir: Optional[Path] = None
    if export_pngs or png_dir:
        png_output_dir = png_dir or out.parent / f"{out.stem}_png"

    operator_sources = {report.metadata.get("operator_source", "AST") for report in aggregated.values()}
    source_label = operator_sources.pop() if len(operator_sources) == 1 else "Mixed"

    render_style_value = (render_style or "bars").lower()
    if paper_style and render_style_value == "bars":
        render_style_value = "paper"
    if render_style_value not in {"bars", "heatmap", "dot-plot", "small-multiples", "paper"}:
        raise typer.BadParameter("--render-style must be one of: bars, heatmap, dot-plot, small-multiples, paper.")

    paper_preset_value = (paper_preset or "").lower() or None
    if render_style_value == "paper":
        if not paper_preset_value:
            raise typer.BadParameter("--paper-style requires --preset {fig3|fig4|fig7|all}.")
        if paper_preset_value not in {"fig3", "fig4", "fig7", "all"}:
            raise typer.BadParameter("--preset must be one of: fig3, fig4, fig7, all.")
    elif paper_preset_value:
        raise typer.BadParameter("--preset is only valid with --paper-style (or --render-style paper).")

    paper_focus_list: Optional[List[str]] = None
    if paper_focus:
        paper_focus_list = [item.strip() for item in paper_focus.split(",") if item.strip()]
        if not paper_focus_list:
            paper_focus_list = None

    sort_by_value = (sort_by or "input").lower()
    if sort_by_value not in {"input", "label", "distance"}:
        raise typer.BadParameter("--sort-by must be one of: input, label, distance.")

    aggregated = _order_workloads(aggregated, sort_by_value, reference)

    options = ReportOptions(
        include_origin_types=origin_types,
        scope=scope_value,
        operator_source=source_label,
        expect_data_metrics=True,
        png_output_dir=png_output_dir,
        ndv_ratio_denominator=ndv_ratio_value,
        ndv_column_universe=ndv_universe_value,
        ndv_debug=ndv_debug,
        ndv_debug_output=ndv_debug_output,
        render_style=render_style_value,
        paper_preset=paper_preset_value,
        paper_focus_bars=paper_focus_list,
        paper_focus_cdf=paper_focus_list,
        paper_baselines_path=paper_baselines,
    )

    try:
        if types_only and focused_only:
            console.print("[red]Choose only one of --types-only or --focused-only.[/red]")
            raise typer.Exit(1)
        if focused_only:
            generate_focused_comparison_pdf(aggregated, out, palette_values or DEFAULT_PALETTE, options)
        elif types_only:
            generate_types_only_comparison_pdf(aggregated, out, palette_values or DEFAULT_PALETTE, options)
        else:
            generate_comparison_pdf(aggregated, out, palette_values or DEFAULT_PALETTE, options)
    except RuntimeError as err:
        console.print(f"[red]{err}[/red]")
        raise typer.Exit(1)

    if json_out:
        json_payload = {label: aggregated_report_to_dict(report) for label, report in aggregated.items()}
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(json_payload, indent=2) + "\n", encoding="utf-8")
        console.print(f"[green]Wrote comparison JSON:[/green] {json_out.resolve()}")

    resolved = out.resolve()
    console.print(f"[green]Comparison report written to:[/green] {resolved}")
    if auto_open:
        _open_file(resolved)


@app.command("report")
def report_command(
    inputs: Optional[List[Path]] = typer.Argument(
        None, metavar="JSON", help="One or more coverage JSON/JSONL files or directories"
    ),
    sql_inputs: List[Path] = typer.Option(
        [],
        "--sql",
        help="Path or glob to SQL coverage JSON/JSONL files (overrides positional inputs when provided)",
    ),
    data_inputs: List[Path] = typer.Option(
        [],
        "--data",
        help="Path or glob to data metrics JSON/JSONL files produced by `workloadlens data`",
    ),
    out: Path = typer.Option(Path("workloadlens_report.pdf"), "--out", "-o", help="Path to the output PDF report"),
    palette: Optional[str] = typer.Option(None, "--palette", help="Comma-separated list of hex colors to override the default palette"),
    ndv_ratio_denominator: str = typer.Option(
        "non_null",
        "--ndv-ratio-denominator",
        help="Denominator for NDV ratio plots (rows|non_null).",
        case_sensitive=False,
    ),
    ndv_column_universe: str = typer.Option(
        "all",
        "--ndv-column-universe",
        help="Column universe for NDV ratio plots (all|eligible).",
        case_sensitive=False,
    ),
    ndv_debug: bool = typer.Option(False, "--ndv-debug", help="Print NDV ratio debug summary"),
    ndv_debug_output: Optional[Path] = typer.Option(
        None, "--ndv-debug-output", help="Write NDV ratio debug summary to this file (implies --ndv-debug)"
    ),
    auto_open: bool = typer.Option(True, "--open/--no-open", help="Automatically open the generated PDF"),
    scope: str = typer.Option("all", "--scope", help="Render scope (all|queries)", case_sensitive=False),
    origin_types: bool = typer.Option(True, "--origin-types/--no-origin-types", help="Use origin logical types where available"),
    workloads: List[str] = typer.Option(
        [],
        "--workload",
        "-w",
        help="Label=path to coverage JSON/JSONL file or directory (repeat for multiple workloads)",
    ),
    workload_data: List[str] = typer.Option(
        [],
        "--workload-data",
        help="Label=path to data metrics JSON/JSONL file for the workload (repeatable, matches --workload labels).",
    ),
    json_out: Optional[Path] = typer.Option(None, "--json-out", help="Write aggregated metrics to this JSON file"),
    export_pngs: bool = typer.Option(False, "--export-pngs", help="Also export each page as a PNG (defaults to OUT stem)"),
    png_dir: Optional[Path] = typer.Option(None, "--png-dir", help="Directory for exported PNGs (implies --export-pngs)"),
):
    """Generate a PDF report summarising operator and type distributions."""

    scope_value = (scope or "all").lower()
    if scope_value not in {"all", "queries"}:
        raise typer.BadParameter("Scope must be one of: all, queries.")

    palette_values = None
    if palette:
        palette_values = [color.strip() for color in palette.split(",") if color.strip()]
        if not palette_values:
            palette_values = None

    ndv_ratio_value = _normalize_ndv_denominator(ndv_ratio_denominator)
    ndv_universe_value = _normalize_ndv_universe(ndv_column_universe)
    ndv_debug = ndv_debug or ndv_debug_output is not None

    out = _append_timestamp_pdf(out)

    png_output_dir: Optional[Path] = None
    if export_pngs or png_dir:
        png_output_dir = png_dir or out.parent / f"{out.stem}_png"

    positional_inputs = list(inputs or [])
    sql_sources = list(sql_inputs) if sql_inputs else positional_inputs
    data_sources = list(data_inputs)

    if workloads:
        workload_map = _parse_workload_arguments(workloads)
        workload_data_map = _parse_workload_arguments(workload_data) if workload_data else {}
        if workload_data_map:
            unknown = set(workload_data_map) - set(workload_map)
            if unknown:
                console.print(
                    "[yellow]Ignoring --workload-data entries for unknown label(s): "
                    + ", ".join(sorted(unknown))
                    + "[/yellow]"
                )
                for label in unknown:
                    workload_data_map.pop(label, None)
        if inputs:
            console.print("[yellow]Positional inputs ignored because --workload was provided.[/yellow]")
        if sql_inputs:
            console.print("[yellow]--sql paths ignored because --workload was provided.[/yellow]")
        if data_inputs:
            console.print("[yellow]--data paths ignored because --workload was provided.[/yellow]")
        aggregated = aggregate_labeled_jsonl(workload_map, workload_data_map)
        if not aggregated:
            console.print("[red]No valid JSON/JSONL files found for the supplied workloads.[/red]")
            raise typer.Exit(1)

        operator_sources = {report.metadata.get("operator_source", "AST") for report in aggregated.values()}
        source_label = operator_sources.pop() if len(operator_sources) == 1 else "Mixed"
        options = ReportOptions(
            include_origin_types=origin_types,
            scope=scope_value,
            operator_source=source_label,
            expect_data_metrics=False,
            png_output_dir=png_output_dir,
            ndv_ratio_denominator=ndv_ratio_value,
            ndv_column_universe=ndv_universe_value,
            ndv_debug=ndv_debug,
            ndv_debug_output=ndv_debug_output,
        )

        if len(aggregated) == 1:
            bundle = next(iter(aggregated.values()))
            stats = bundle.all_statements if scope_value != "queries" else bundle.query_statements
            if stats.total_queries == 0 and bundle.query_statements.query_statements == 0:
                console.print("[red]No query records found in the provided inputs.[/red]")
                raise typer.Exit(1)
            try:
                generate_pdf_report(bundle, out, palette_values or DEFAULT_PALETTE, options)
            except RuntimeError as err:
                console.print(f"[red]{err}[/red]")
                raise typer.Exit(1)
        else:
            try:
                generate_comparison_pdf(aggregated, out, palette_values or DEFAULT_PALETTE, options)
            except RuntimeError as err:
                console.print(f"[red]{err}[/red]")
                raise typer.Exit(1)

        if json_out:
            json_payload = {label: aggregated_report_to_dict(report) for label, report in aggregated.items()}
            json_out.parent.mkdir(parents=True, exist_ok=True)
            json_out.write_text(json.dumps(json_payload, indent=2) + "\n", encoding="utf-8")
            console.print(f"[green]Wrote report JSON:[/green] {json_out.resolve()}")

        resolved = out.resolve()
        console.print(f"[green]Report written to:[/green] {resolved}")
        if auto_open:
            _open_file(resolved)
        return
    elif workload_data:
        console.print("[yellow]--workload-data ignored because no --workload entries were provided.[/yellow]")

    if not sql_sources:
        console.print("[red]Provide at least one SQL JSON/JSONL input via positional args or --sql.[/red]")
        raise typer.Exit(1)

    bundle = aggregate_jsonl_paths(sql_sources, data_sources or None)
    stats = bundle.all_statements if scope_value != "queries" else bundle.query_statements
    if stats.total_queries == 0 and bundle.query_statements.query_statements == 0:
        console.print("[red]No query records found in the provided inputs.[/red]")
        raise typer.Exit(1)

    try:
        options = ReportOptions(
            include_origin_types=origin_types,
            scope=scope_value,
            operator_source=bundle.metadata.get("operator_source", "AST"),
            expect_data_metrics=bool(data_sources),
            png_output_dir=png_output_dir,
            ndv_ratio_denominator=ndv_ratio_value,
            ndv_column_universe=ndv_universe_value,
            ndv_debug=ndv_debug,
            ndv_debug_output=ndv_debug_output,
        )
        generate_pdf_report(bundle, out, palette_values or DEFAULT_PALETTE, options)
    except RuntimeError as err:
        console.print(f"[red]{err}[/red]")
        raise typer.Exit(1)

    if json_out:
        json_payload = aggregated_report_to_dict(bundle)
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(json_payload, indent=2) + "\n", encoding="utf-8")
        console.print(f"[green]Wrote report JSON:[/green] {json_out.resolve()}")

    resolved = out.resolve()
    console.print(f"[green]Report written to:[/green] {resolved}")

    if auto_open:
        _open_file(resolved)


def _open_file(path: Path) -> None:
    try:
        if sys.platform.startswith("darwin"):
            subprocess.Popen(["open", str(path)])
        elif sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        console.print(f"[yellow]Could not open PDF automatically:[/yellow] {path}")


def _parse_workload_arguments(entries: List[str]) -> Dict[str, List[Path]]:
    mapping: Dict[str, List[Path]] = {}
    for entry in entries:
        if "=" not in entry:
            raise typer.BadParameter("Workloads must be specified as label=path")
        label, value = entry.split("=", 1)
        label = label.strip()
        value = value.strip()
        if not label or not value:
            raise typer.BadParameter("Workload label and path must be non-empty")
        mapping.setdefault(label, []).append(Path(value))
    return mapping


@app.command("config-check")
def config_check_command(
    configs: List[Path] = typer.Argument(..., exists=True, help="One or more workloadlens.json files to validate"),
) -> None:
    """Validate WorkloadLens project configs (paths, globs, outputs)."""

    if not configs:
        console.print("[red]Provide at least one config file.[/red]")
        raise typer.Exit(1)

    any_errors = False
    for cfg_path in configs:
        try:
            config = load_config(cfg_path)
        except Exception as exc:
            console.print(f"[red]Failed to load {cfg_path}: {exc}[/red]")
            any_errors = True
            continue
        errors, warnings = _validate_project_config(config, cfg_path)
        if errors:
            any_errors = True
            console.print(f"[red]{cfg_path}[/red]")
            for issue in errors:
                console.print(f"  [red]×[/red] {issue}")
            for warn in warnings:
                console.print(f"  [yellow]![/yellow] {warn}")
        else:
            console.print(f"[green]✓ {cfg_path}[/green]")
            for warn in warnings:
                console.print(f"  [yellow]![/yellow] {warn}")

    if any_errors:
        raise typer.Exit(1)
    console.print("[green]All configs look good.[/green]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()


def _validate_project_config(config: ProjectConfig, config_path: Path) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []

    schema_path = config.resolve_schema(config_path)
    if not schema_path or not schema_path.exists():
        errors.append(f"Schema file not found: {schema_path}")

    queries_value = config.resolve_queries(config_path)
    query_files = iter_sql_files(queries_value)
    if not query_files:
        errors.append(f"No SQL files matched queries pattern: {queries_value}")

    if config.data_dir:
        data_dir = config.resolve_data_dir(config_path)
        if not data_dir or not data_dir.exists():
            errors.append(f"Data directory not found: {data_dir}")
        else:
            matched_files = [
                entry
                for entry in data_dir.rglob("*")
                if entry.is_file() and entry.suffix.lower() in DEFAULT_EXTENSIONS
            ]
            if not matched_files:
                warnings.append(f"No data files with extensions {DEFAULT_EXTENSIONS} found in {data_dir}")

    output_dir = config.resolve_output_dir(config_path)
    if not output_dir.exists():
        warnings.append(f"Output directory does not exist yet (will be created): {output_dir}")

    coverage_path = config.resolve_coverage_json(config_path)
    if not coverage_path.parent.exists():
        warnings.append(f"Coverage JSON directory does not exist yet: {coverage_path.parent}")

    data_json = config.resolve_data_json(config_path)
    if data_json and not data_json.parent.exists():
        warnings.append(f"Data metrics directory does not exist yet: {data_json.parent}")

    report_path = config.resolve_report_pdf(config_path)
    if not report_path.parent.exists():
        warnings.append(f"Report directory does not exist yet: {report_path.parent}")

    if config.scope not in {"all", "queries"}:
        errors.append(f"Invalid scope '{config.scope}'. Must be 'all' or 'queries'.")

    return errors, warnings
