# WorkloadLens/src/workloadlens/emit/console.py
from __future__ import annotations
from collections import Counter
from typing import Dict
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from ..utils.math import counter_shares

console = Console()

_OPERATOR_DISPLAY_NAMES = {
    "scan": "scan",
    "aggregate": "group_by",
    "sort": "order_by",
    "limit": "limit",
}

_FEATURE_LABELS = {
    "has_order_by": "Order By",
    "has_join": "Join",
    "has_limit": "Limit",
    "has_union": "Union All",
    "has_group_by": "Group By",
    "has_window": "Window Functions",
    "has_cte": "CTEs",
}

def _display_name(op: str) -> str:
    return _OPERATOR_DISPLAY_NAMES.get(op, op)

def print_summary(n_ok: int, n_ast_only: int, n_err: int,
                  op_hist: Counter, op_shares: Dict[str, float], join_hist: Counter, join_shares: Dict[str, float],
                  proj_funcs: Counter, agg_funcs: Counter,
                  scalar_exprs: Counter, type_hist: Dict[str, Counter],
                  feature_counts: Counter, total_queries: int):
    console.print(Panel.fit(f"[bold]WorkloadLens[/bold] run summary\n"
                            f"[green]planned:[/green] {n_ok}   "
                            f"[yellow]ast-only:[/yellow] {n_ast_only}   "
                            f"[red]errors:[/red] {n_err}",
                            title="Summary", border_style="cyan"))

    if op_hist:
        t = Table(title="Operator histogram (Substrait)", show_lines=False)
        t.add_column("Operator")
        t.add_column("Count", justify="right")
        t.add_column("Share", justify="right")
        for k, v in op_hist.most_common():
            share = op_shares.get(k, 0.0) if op_shares else 0.0
            t.add_row(str(_display_name(k)), str(v), f"{share * 100:.1f}%")
        console.print(t)

    if join_hist:
        t = Table(title="Join types", show_lines=False)
        t.add_column("Type")
        t.add_column("Count", justify="right")
        t.add_column("Share", justify="right")
        for k, v in join_hist.most_common():
            share = join_shares.get(k, 0.0) if join_shares else 0.0
            t.add_row(str(k), str(v), f"{share * 100:.1f}%")
        console.print(t)

    if agg_funcs:
        t = Table(title="Top aggregate functions (AST)", show_lines=False)
        t.add_column("Function")
        t.add_column("Count", justify="right")
        for k, v in agg_funcs.most_common(15):
            t.add_row(str(k), str(v))
        console.print(t)

    if scalar_exprs:
        t = Table(title="Top scalar expressions (AST)", show_lines=False)
        t.add_column("Operator")
        t.add_column("Count", justify="right")
        for k, v in scalar_exprs.most_common(15):
            t.add_row(str(k), str(v))
        console.print(t)

    if type_hist:
        for op_name in sorted(type_hist):
            counter = type_hist[op_name]
            if not counter:
                continue
            title = f"Type distribution for {_display_name(op_name)}"
            t = Table(title=title, show_lines=False)
            t.add_column("Type")
            t.add_column("Count", justify="right")
            t.add_column("Share", justify="right")
            shares = counter_shares(counter)
            for key, value in counter.most_common(10):
                share = shares.get(key, 0.0)
                t.add_row(str(key), str(value), f"{share * 100:.1f}%")
            console.print(t)

    if feature_counts and total_queries:
        t = Table(title="Query feature prevalence", show_lines=False)
        t.add_column("Feature")
        t.add_column("Share", justify="right")
        for name, label in _FEATURE_LABELS.items():
            count = feature_counts.get(name, 0)
            share = (count / total_queries) * 100 if total_queries else 0.0
            t.add_row(label, f"{share:.1f}%")
        console.print(t)
