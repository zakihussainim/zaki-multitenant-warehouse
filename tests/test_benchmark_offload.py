import threading
from datetime import date

from helpers import FakeApi, FakeS3, expect_error
from mtw import benchmark, datapi, ddl, names, offload, queries, wlm_demo

END = date(2026, 9, 30)


def test_query_sets_exist_for_every_variant_and_point_at_the_right_tables():
    for variant in queries.VARIANTS:
        suite = queries.query_set(variant, END)
        assert "tenant_monthly_revenue" in suite and "hot_rollup" in suite
    baseline = "\n".join(sql for sql, _ in queries.query_set("baseline", END).values())
    tuned = "\n".join(sql for sql, _ in queries.query_set("tuned", END).values())
    serving = "\n".join(sql for sql, _ in queries.query_set("serving", END).values())
    mv = "\n".join(sql for sql, _ in queries.query_set("mv", END).values())
    assert "fact_orders_baseline" in baseline and "fact_orders_baseline" not in tuned
    assert "serving.orders" in serving and "core.fact_orders" not in serving
    assert "mv_monthly_revenue" in mv and "core.fact_orders" not in mv
    expect_error(queries.query_set, "nope", END, exception=ValueError)


def test_same_question_has_the_same_filters_across_variants():
    base = queries.query_set("baseline", END)["tenant_monthly_revenue"][0]
    tuned = queries.query_set("tuned", END)["tenant_monthly_revenue"][0]
    assert base.replace("fact_orders_baseline", "fact_orders") == tuned
    assert "'2025-09-01'" in tuned  # the hot window matches the offload cutoff


def test_queries_run_under_the_tenants_query_group():
    suite = queries.query_set("tuned", END)
    assert suite["tenant_monthly_revenue"][1] == "tenant_acme"
    assert suite["hot_rollup"][1] is None


def test_statements_turn_the_result_cache_off_first():
    batch = benchmark.statements_for("SELECT 1", "tenant_acme")
    assert batch[0].startswith("SET enable_result_cache_for_session TO off")
    assert batch[1] == "SET query_group TO 'tenant_acme'" and batch[2] == "SELECT 1"
    assert len(benchmark.statements_for("SELECT 1", None)) == 2


def test_fingerprint_ignores_row_order_and_detects_changes():
    a = [{"x": 1}, {"x": 2}]
    assert benchmark.fingerprint(a) == benchmark.fingerprint(list(reversed(a)))
    assert benchmark.fingerprint(a) != benchmark.fingerprint([{"x": 1}, {"x": 3}])


def test_run_suite_records_timings_and_discards_warmups():
    api = FakeApi(ms=7.0, batch_rows={"SELECT": [{"x": 1}]})
    results = benchmark.run_suite(api, "tuned", END, "tuned", runs=3, warmups=1, log=lambda *a: None)
    first = next(iter(results["queries"].values()))
    assert first["timings_ms"] == [7.0, 7.0, 7.0] and first["median_ms"] == 7.0 and first["row_count"] == 1
    statements = [s for s in api.statements if s.startswith("SELECT")]
    assert len(statements) == len(queries.query_set("tuned", END)) * 4  # 1 warm-up + 3 timed


def test_compare_and_table_flag_different_answers():
    a = {"queries": {"q1": {"median_ms": 1000.0, "fingerprint": "aa"}, "q2": {"median_ms": 500.0, "fingerprint": "bb"}}}
    b = {"queries": {"q1": {"median_ms": 250.0, "fingerprint": "aa"}, "q2": {"median_ms": 400.0, "fingerprint": "zz"}, "q3": {"median_ms": 1.0, "fingerprint": "c"}}}
    rows = benchmark.compare(a, b)
    assert [r["query"] for r in rows] == ["q1", "q2"]
    assert rows[0]["speedup"] == 4.0 and rows[0]["change_pct"] == -75.0 and rows[0]["same_result"]
    assert not rows[1]["same_result"]
    table = benchmark.markdown_table(rows, "before", "after")
    assert "| q1 | 1000 | 250 | -75% | yes |" in table and "| q2 | 500 | 400 | -20% | NO |" in table


def test_save_and_load_round_trip(tmp_path=None):
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        results = {"label": "x", "queries": {}}
        path = benchmark.save(results, directory)
        assert path.name == "benchmark-x.json" and benchmark.load(path) == results


def test_billed_rpu_hours_reads_the_usage_view():
    api = FakeApi(counts={"sys_serverless_usage": {"rpu_hours": "1.25"}})
    assert benchmark.billed_rpu_hours(api, "2026-10-08T10:00:00+00:00", "2026-10-08T11:00:00+00:00") == 1.25
    assert "'2026-10-08 10:00:00'" in api.statements[0]
    assert benchmark.cost_summary(2.0)["compute_cost_usd"] == 0.75


# -- offload -------------------------------------------------------------------------------------------------------------
def cold_keys():
    return {f"cold/fact_orders/order_month=2025-0{m}/part-0.parquet": 100 for m in range(1, 4)}


def test_offload_unloads_verifies_deletes_then_swaps_the_view():
    api = FakeApi(counts={"WHERE order_date <": {"n": 500}, "fact_orders_cold": {"n": 500}})
    s3 = FakeS3()

    original = api.execute

    def execute_and_create_files(sql):
        original(sql)
        if sql.startswith("UNLOAD"):
            s3.keys.update(cold_keys())
        return []

    api.execute = execute_and_create_files
    summary = offload.run_offload(api, s3, "dev", "bkt", END, log=lambda *a: None)
    statements = api.statements
    kinds = [next((k for k in ("CREATE EXTERNAL SCHEMA", "UNLOAD", "CREATE EXTERNAL TABLE", "ADD IF NOT EXISTS PARTITION", "DELETE FROM", "VACUUM", "CREATE OR REPLACE VIEW", "REFRESH MATERIALIZED") if k in s), None) for s in statements]
    kinds = [k for k in kinds if k]
    assert kinds.index("UNLOAD") < kinds.index("CREATE EXTERNAL TABLE") < kinds.index("DELETE FROM") < kinds.index("CREATE OR REPLACE VIEW")
    assert kinds.count("ADD IF NOT EXISTS PARTITION") == 3 and kinds.count("REFRESH MATERIALIZED") == 3
    assert summary["rows_moved"] == 500 and summary["months"] == ["2025-01", "2025-02", "2025-03"]
    assert any("UNION ALL" in s for s in statements)


def test_offload_never_deletes_when_counts_disagree():
    api = FakeApi(counts={"WHERE order_date <": {"n": 500}, "fact_orders_cold": {"n": 499}})
    s3 = FakeS3()
    original = api.execute

    def execute(sql):
        original(sql)
        if sql.startswith("UNLOAD"):
            s3.keys.update(cold_keys())
        return []

    api.execute = execute
    expect_error(offload.run_offload, api, s3, "dev", "bkt", END, log=lambda *a: None, exception=RuntimeError, contains="Nothing was deleted")
    assert not any(s.startswith("DELETE") for s in api.statements)


def test_offload_refuses_to_unload_over_existing_cold_files():
    api = FakeApi(counts={"WHERE order_date <": {"n": 10}})
    expect_error(offload.run_offload, api, FakeS3(cold_keys()), "dev", "bkt", END, log=lambda *a: None, exception=RuntimeError, contains="refusing")
    assert not any(s.startswith("UNLOAD") for s in api.statements)


def test_offload_dry_run_changes_nothing():
    api = FakeApi(counts={"WHERE order_date <": {"n": 10}})
    summary = offload.run_offload(api, FakeS3(), "dev", "bkt", END, dry_run=True, log=lambda *a: None)
    assert summary["dry_run"] and all(s.startswith("SELECT") for s in api.statements)


def test_offload_rerun_after_success_only_refreshes_the_external_side():
    api = FakeApi(counts={"WHERE order_date <": {"n": 0}, "fact_orders_cold": {"n": 500}})
    summary = offload.run_offload(api, FakeS3(cold_keys()), "dev", "bkt", END, log=lambda *a: None)
    assert not any(s.startswith(("UNLOAD", "DELETE")) for s in api.statements)
    assert summary["rows_moved"] == 0 and len(summary["months"]) == 3


def test_months_parser_and_prefix_size():
    keys = list(cold_keys()) + ["cold/fact_orders/_SUCCESS"]
    assert offload.months_in(keys) == ["2025-01", "2025-02", "2025-03"]
    assert offload.prefix_size_bytes(FakeS3(cold_keys()), "b", "cold/") == 300


# -- wlm demo ------------------------------------------------------------------------------------------------------------
def test_percentile_and_summary():
    assert wlm_demo.percentile([1, 2, 3, 4, 100], 0.5) == 3
    assert wlm_demo.percentile([], 0.9) == 0.0
    summary = wlm_demo.summarise([10, 20, 30])
    assert summary["count"] == 3 and summary["median_ms"] == 20 and summary["max_ms"] == 30


def test_demo_runs_heavy_and_light_queries_under_their_own_query_groups():
    api = FakeApi(ms=1.0, fail_on={"a.revenue > b.revenue": "Query cancelled by monitoring rule: abort"})
    result = wlm_demo.run_demo(api, "runaway", names.TENANT_BY_ID["acme"], names.TENANT_BY_ID["hooli"], heavy_workers=2, light_queries=3, warmup_seconds=0.05, log=lambda *a: None)
    assert result["light"]["count"] == 3
    assert result["heavy_outcomes"].get("aborted", 0) >= 1
    groups = [s for s in api.statements if s.startswith("SET query_group")]
    assert "SET query_group TO 'tenant_acme'" in groups and "SET query_group TO 'tenant_hooli'" in groups
    assert any(s == "SET statement_timeout TO 240000" for s in api.statements)
    expect_error(wlm_demo.run_demo, api, "other", names.TENANTS[0], names.TENANTS[1], exception=ValueError)


def test_demo_tells_a_safety_timeout_apart_from_a_queue_rule_abort():
    api = FakeApi(ms=1.0, fail_on={"a.revenue > b.revenue": "ERROR: canceling statement due to statement timeout"})
    result = wlm_demo.run_demo(api, "runaway", names.TENANT_BY_ID["acme"], names.TENANT_BY_ID["hooli"], heavy_workers=1, light_queries=2, warmup_seconds=0.05, log=lambda *a: None)
    assert result["heavy_outcomes"].get("hit_safety_timeout", 0) >= 1 and "aborted" not in result["heavy_outcomes"]