"""Does one tenant's heavy workload hurt another tenant? Runs a heavy tenant flat out while a light tenant asks small questions.

Scenarios:
  concurrent  several heavy aggregation queries at once (tests queue concurrency and memory isolation; provisioned shows it best)
  runaway     an accidental cross join (tests that the monitoring rule aborts it; Serverless queues do this)
"""

import statistics
import threading
import time

from . import datapi, names

HEAVY_SQL = {
    "concurrent": (
        f"SELECT COUNT(*) AS n, SUM(a.revenue) AS revenue FROM {names.CORE}.fact_orders a "
        f"JOIN {names.CORE}.fact_orders b ON a.tenant_id = b.tenant_id AND a.order_date = b.order_date "
        "WHERE a.tenant_id = '{tenant}'"
    ),
    "runaway": (
        f"SELECT COUNT(*) AS n FROM {names.CORE}.fact_orders a, {names.CORE}.fact_orders b "
        "WHERE a.revenue > b.revenue AND a.tenant_id = '{tenant}'"
    ),
}

LIGHT_SQL = f"SELECT COUNT(*) AS n, SUM(revenue) AS revenue FROM {names.CORE}.fact_orders WHERE tenant_id = '{{tenant}}' AND order_date >= (SELECT MAX(order_date) - 6 FROM {names.CORE}.fact_orders)"


def percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def summarise(latencies):
    return {
        "count": len(latencies),
        "median_ms": round(statistics.median(latencies), 1) if latencies else 0.0,
        "p95_ms": round(percentile(latencies, 0.95), 1),
        "max_ms": round(max(latencies), 1) if latencies else 0.0,
    }


def run_demo(api, scenario, heavy_tenant, light_tenant, heavy_workers=6, light_queries=20, warmup_seconds=5, clock=time.monotonic, log=print):
    if scenario not in HEAVY_SQL:
        raise ValueError(scenario)
    stop = threading.Event()
    heavy_log, lock = [], threading.Lock()

    def heavy_worker():
        sql = HEAVY_SQL[scenario].format(tenant=heavy_tenant.id)
        while not stop.is_set():
            began = clock()
            outcome = "completed"
            try:
                api.timed_batch([f"SET query_group TO '{heavy_tenant.query_group}'", sql])
            except datapi.SqlError as error:
                outcome = "aborted" if "abort" in error.message.lower() or "cancel" in error.message.lower() else "failed"
            with lock:
                heavy_log.append({"outcome": outcome, "seconds": round(clock() - began, 1)})

    workers = [threading.Thread(target=heavy_worker, daemon=True) for _ in range(heavy_workers)]
    for worker in workers:
        worker.start()
    time.sleep(warmup_seconds)  # let the heavy load build up before the light tenant starts asking

    latencies = []
    light_sql = LIGHT_SQL.format(tenant=light_tenant.id)
    for _ in range(light_queries):
        batch = ["SET enable_result_cache_for_session TO off", f"SET query_group TO '{light_tenant.query_group}'", light_sql]
        began = clock()
        api.timed_batch(batch)
        latencies.append((clock() - began) * 1000)  # wall clock, so waiting in a queue counts
    stop.set()
    for worker in workers:
        worker.join(timeout=120)

    outcomes = {}
    for entry in heavy_log:
        outcomes[entry["outcome"]] = outcomes.get(entry["outcome"], 0) + 1
    result = {
        "scenario": scenario,
        "heavy_tenant": heavy_tenant.id,
        "light_tenant": light_tenant.id,
        "heavy_workers": heavy_workers,
        "light": summarise(latencies),
        "heavy_outcomes": outcomes,
    }
    log(f"light tenant {light_tenant.id}: {result['light']}")
    log(f"heavy tenant {heavy_tenant.id}: {outcomes}")
    return result
