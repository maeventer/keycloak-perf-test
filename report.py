#!/usr/bin/env python3
"""Stage 5: Parse both version results and produce a comparison report."""

import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
import config

TOP_N_QUERIES = 20

# Regex to normalise SQL literals to ?
_LITERAL_RE = re.compile(
    r"'(?:[^'\\]|\\.)*'"          # single-quoted strings
    r"|\"(?:[^\"\\]|\\.)*\""      # double-quoted strings
    r"|(?<!['\w])\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"  # UUIDs
    r"|(?<!['\w.-])\b\d+(?:\.\d+)?\b(?!['\w])"  # bare numbers
)

# Operations to show in the timing table (add_execution_* are merged)
_OPS_ORDER = [
    "create_flow",
    "add_execution",
    "get_executions",
    "raise_priority",
    "delete_flow",
]


def _percentiles(data: list[float]) -> dict:
    if len(data) < 10:
        med = statistics.median(data) if data else None
        return {"mean": round(statistics.mean(data), 1) if data else None,
                "p50": round(med, 1) if med is not None else None,
                "p95": "n/a", "p99": "n/a"}
    qs = statistics.quantiles(data, n=100)
    return {
        "mean": round(statistics.mean(data), 1),
        "p50":  round(qs[49], 1),
        "p95":  round(qs[94], 1),
        "p99":  round(qs[98], 1),
    }


def _load_timings(version_tag: str) -> dict[str, list[float]]:
    """Load timings and merge add_execution_1/2/3 into add_execution."""
    path = Path(config.RESULTS_DIR) / version_tag / "flows_timings.json"
    if not path.exists():
        print(f"WARNING: {path} not found; timing data for {version_tag} will be empty.")
        return {}
    raw = json.loads(path.read_text())
    grouped: dict[str, list[float]] = defaultdict(list)
    for entry in raw:
        op = entry["operation"]
        if op.startswith("add_execution_"):
            op = "add_execution"
        grouped[op].append(entry["duration_ms"])
    return grouped


def _parse_query_log(version_tag: str) -> dict:
    """Parse MariaDB general log, normalise queries, count by type and pattern."""
    path = Path(config.RESULTS_DIR) / version_tag / "query.log"
    if not path.exists():
        return {"total": 0, "by_type": {}, "top_patterns": []}

    type_counts: Counter = Counter()
    pattern_counts: Counter = Counter()

    for line in path.read_text(errors="replace").splitlines():
        # MariaDB general log (no timestamp): \t\t<thread_id> Query\t<sql>
        # The thread_id and command are space-separated in one tab-field.
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        # The command field contains "<spaces><thread_id> Query" or similar
        cmd_field = parts[-2] if len(parts) >= 2 else ""
        if not cmd_field.split()[-1:] == ["Query"]:
            continue
        sql = parts[-1].strip()
        stmt_type = sql.split()[0].upper() if sql else ""
        if stmt_type in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            type_counts[stmt_type] += 1
        normalised = _LITERAL_RE.sub("?", sql)
        pattern_counts[normalised] += 1

    total = sum(type_counts.values())
    top_patterns = [
        {"count": cnt, "query": pat}
        for pat, cnt in pattern_counts.most_common(TOP_N_QUERIES)
    ]
    return {"total": total, "by_type": dict(type_counts), "top_patterns": top_patterns}


def generate_report():
    versions = [v[0] for v in config.VERSIONS]
    if len(versions) < 2:
        raise SystemExit("report.py requires at least two versions in config.VERSIONS")
    results_dir = Path(config.RESULTS_DIR)

    timing_stats = {}
    sql_stats = {}
    for v in versions:
        timings = _load_timings(v)
        timing_stats[v] = {op: _percentiles(timings.get(op, [])) for op in _OPS_ORDER}
        sql_stats[v] = _parse_query_log(v)

    # --- Build comparison.json ---
    comparison = {"timing": timing_stats, "sql": sql_stats}
    (results_dir / "comparison.json").write_text(json.dumps(comparison, indent=2))

    # --- Build comparison.txt ---
    lines = []

    col_w = 13
    lines.append("=== HTTP Timing Comparison (ms) ===")
    header = f"{'Operation':<20}" + "".join(
        f" | {(v+' p50'):>{col_w}} | {(v+' p99'):>{col_w}}" for v in versions
    )
    lines.append(header)
    lines.append("-" * len(header))
    for op in _OPS_ORDER:
        row = f"{op:<20}"
        for v in versions:
            s = timing_stats[v][op]
            row += f" | {str(s['p50']):>{col_w}} | {str(s['p99']):>{col_w}}"
        lines.append(row)

    lines.append("")
    lines.append("=== SQL Query Count Comparison (flow operations window only) ===")
    lines.append(f"{'Statement':<10}" + "".join(f" | {v:>{col_w}}" for v in versions))
    lines.append("-" * (10 + len(versions) * (col_w + 3)))
    for stmt in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        row = f"{stmt:<10}" + "".join(f" | {sql_stats[v]['by_type'].get(stmt, 0):>{col_w},}" for v in versions)
        lines.append(row)
    lines.append(f"{'TOTAL':<10}" + "".join(f" | {sql_stats[v]['total']:>{col_w},}" for v in versions))

    for v in versions:
        lines.append("")
        lines.append(f"=== Top {TOP_N_QUERIES} Most Frequent Queries ({v}) ===")
        for i, entry in enumerate(sql_stats[v]["top_patterns"], start=1):
            lines.append(f"{i:>3}. [{entry['count']:>7,}x] {entry['query']}")

    txt = "\n".join(lines) + "\n"
    (results_dir / "comparison.txt").write_text(txt)
    print(txt)
    print(f"[report] Saved comparison.json and comparison.txt to {results_dir}/")


if __name__ == "__main__":
    generate_report()
