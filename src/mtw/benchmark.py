"""Runs a query set several times and records timings, a fingerprint of each result, and the compute that was billed."""

import hashlib
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

from . import costs, queries

RESULT_CACHE_OFF = "SET enable_result_cache_for_session TO off"


def fingerprint(rows):
    """Two runs that return the same answer have the same fingerprint, so a 'faster' variant cannot quietly be wrong."""
    text = json.dumps(sorted(json.dumps(r, sort_keys=True, default=str) for r in rows))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def statements_for(sql, query_group):
    batch = [RESULT_CACHE_OFF]
    if query_group:
        batch.append(f"SET query_group TO '{query_group}'")
    batch.append(sql)
    return batch


def run_suite(api, variant, end_date, label, runs=5, warmups=1, log=print):
    suite = queries.query_set(variant, end_date)
    started = datetime.now(timezone.utc)
    results = {}
    for name, (sql, group) in suite.items():
        timings, last_rows = [], []
        for attempt in range(warmups + runs):
            outcome = api.timed_batch(statements_for(sql, group))[-1]
            last_rows = outcome["rows"]
            if attempt >= warmups:
                timings.append(round(outcome["duration_ms"], 1))
        results[name] = {
            "timings_ms": timings,
            "median_ms": round(statistics.median(timings), 1),
            "min_ms": min(timings),
            "row_count": len(last_rows),
            "fingerprint": fingerprint(last_rows),
        }
        log(f"  {name:<24} median {results[name]['median_ms']:>9.1f} ms   rows {len(last_rows):>4}   {results[name]['fingerprint']}")
    return {
        "label": label,
        "variant": variant,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runs": runs,
        "queries": results,
    }


def billed_rpu_hours(api, start_iso, end_iso):
    """Serverless only. SYS_SERVERLESS_USAGE lags by a few minutes, so call this a little after the run."""
    row = api.query_one(
        "SELECT COALESCE(SUM(charged_seconds * compute_capacity), 0) / 3600.0 AS rpu_hours FROM sys_serverless_usage "
        f"WHERE start_time >= '{start_iso[:19].replace('T', ' ')}' AND end_time <= '{end_iso[:19].replace('T', ' ')}'"
    )
    return float(row["rpu_hours"]) if row and row["rpu_hours"] is not None else 0.0


def save(results, directory="results"):
    path = Path(directory) / f"benchmark-{results['label']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return path


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def compare(a, b):
    """Rows for every query that both runs have. 'speedup' is a/b, so 2.0 means b is twice as fast."""
    rows = []
    for name, left in a["queries"].items():
        right = b["queries"].get(name)
        if not right:
            continue
        speedup = left["median_ms"] / right["median_ms"] if right["median_ms"] else float("inf")
        rows.append(
            {
                "query": name,
                "a_ms": left["median_ms"],
                "b_ms": right["median_ms"],
                "speedup": round(speedup, 2),
                "change_pct": round((right["median_ms"] - left["median_ms"]) / left["median_ms"] * 100, 1) if left["median_ms"] else 0.0,
                "same_result": left["fingerprint"] == right["fingerprint"],
            }
        )
    return rows


def markdown_table(rows, a_label, b_label):
    lines = [
        f"| Query | {a_label} (ms) | {b_label} (ms) | Change | Same answer |",
        "|---|---:|---:|---:|:---:|",
    ]
    for r in rows:
        lines.append(f"| {r['query']} | {r['a_ms']:.0f} | {r['b_ms']:.0f} | {r['change_pct']:+.0f}% | {'yes' if r['same_result'] else 'NO'} |")
    return "\n".join(lines)


def cost_summary(rpu_hours, prices=costs.Prices()):
    return {"rpu_hours": round(rpu_hours, 4), "compute_cost_usd": round(costs.compute_cost(rpu_hours, prices), 4)}
