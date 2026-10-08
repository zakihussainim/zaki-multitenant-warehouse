from datetime import date

from helpers import expect_error
from mtw import ddl, names, security


def test_tuned_tables_have_keys_and_baseline_does_not():
    tuned = "\n".join(ddl.create_tables("tuned"))
    baseline = "\n".join(ddl.create_tables("baseline"))
    assert "DISTKEY (customer_key)" in tuned and "COMPOUND SORTKEY (tenant_id, order_date)" in tuned
    assert "DISTSTYLE ALL" in tuned  # small dimensions copied to every node
    assert "DISTKEY" not in baseline and "COMPOUND SORTKEY" not in baseline
    assert baseline.count("DISTSTYLE EVEN") == 3
    assert baseline.count("ALTER SORTKEY NONE") == 3  # stops automatic table optimisation from helping the baseline
    assert "VARCHAR(256)" in baseline and "VARCHAR(256)" not in tuned
    expect_error(ddl.create_tables, "other", exception=ValueError)


def test_both_variants_hold_the_same_columns():
    for variant in ("tuned", "baseline"):
        fact = ddl.create_tables(variant)[0]
        for column in ddl.ORDER_COLUMNS:
            assert f"\n  {column} " in fact


def test_copy_reads_the_same_files_for_both_variants():
    tuned = ddl.load_statements("bkt", "tuned")
    baseline = ddl.load_statements("bkt", "baseline")
    assert any("s3://bkt/raw/fact_orders/" in s and "core.fact_orders " in s for s in tuned)
    assert any("s3://bkt/raw/fact_orders/" in s and "fact_orders_baseline" in s for s in baseline)
    assert all("IAM_ROLE default" in s for s in tuned if s.startswith("COPY"))
    assert any(s.startswith("ANALYZE") for s in tuned)
    assert any("dim_date" in s for s in tuned) and not any("dim_date" in s for s in baseline)


def test_serving_objects_are_view_plus_materialized_views():
    objs = ddl.serving_objects()
    assert objs[0] == "serving.orders" and len(objs) == 4
    assert all(o.startswith("serving.") for o in objs)
    for statement in ddl.create_materialized_views():
        assert "core.fact_orders" in statement and "GROUP BY" in statement and "ORDER BY" not in statement
    assert len(ddl.refresh_materialized_views()) == 3


def test_serving_view_is_late_binding_and_unions_cold_data_only_when_asked():
    hot = ddl.serving_view(include_cold=False)
    both = ddl.serving_view(include_cold=True)
    assert hot.endswith("WITH NO SCHEMA BINDING") and both.endswith("WITH NO SCHEMA BINDING")
    assert "UNION ALL" in both and "spectrum.fact_orders_cold" in both
    assert "UNION ALL" not in hot and "spectrum" not in hot


def test_external_table_matches_hot_columns_and_is_partitioned():
    table = ddl.external_table("bkt")
    for column in ddl.ORDER_COLUMNS:
        assert f"\n  {column} " in table
    assert "PARTITIONED BY (order_month VARCHAR(7))" in table and "STORED AS PARQUET" in table
    assert "NOT NULL" not in table
    assert "LOCATION 's3://bkt/cold/fact_orders/'" in table


def test_unload_quotes_and_partitioning():
    sql = ddl.unload_cold("bkt", date(2025, 9, 1))
    assert "''2025-09-01''" in sql and "PARTITION BY (order_month)" in sql and "FORMAT PARQUET" in sql
    assert "CLEANPATH" not in sql  # never wipe the cold prefix automatically
    assert ddl.delete_cold(date(2025, 9, 1)) == "DELETE FROM core.fact_orders WHERE order_date < '2025-09-01'"
    assert "order_month='2025-01'" in ddl.add_partition("bkt", "2025-01")


def test_tenant_setup_is_idempotent_and_complete():
    steps = security.tenant_setup()
    assert len(steps) == 5 * len(names.TENANTS)
    for statement, ignore in steps:
        assert ignore
    texts = [s for s, _ in steps]
    assert "CREATE RLS POLICY policy_acme WITH (tenant_id VARCHAR(32)) USING (tenant_id = 'acme')" in texts
    assert "CREATE USER tenant_acme PASSWORD DISABLE" in texts


def test_protect_attaches_before_enabling_and_never_grants():
    objs = ddl.serving_objects()
    statements = security.protect(objs)
    assert sum(s.startswith("ATTACH RLS POLICY") for s in statements) == len(objs) * len(names.TENANTS)
    assert not any("GRANT" in s for s in statements)
    for obj in objs:
        enable = next(i for i, s in enumerate(statements) if s.endswith("ROW LEVEL SECURITY ON") and obj in s)
        attaches = [i for i, s in enumerate(statements) if s.startswith("ATTACH") and f" ON {obj} " in s]
        assert attaches and max(attaches) < enable
    assert any(s == "ALTER MATERIALIZED VIEW serving.mv_daily_orders ROW LEVEL SECURITY ON" for s in statements)
    assert any(s == "ALTER TABLE serving.orders ROW LEVEL SECURITY ON" for s in statements)


def test_grants_cover_each_tenant_on_each_serving_object_only():
    statements = security.grants(ddl.serving_objects())
    assert len(statements) == len(names.TENANTS) * (1 + 4)
    assert not any("core." in s for s in statements)
    assert not any("PUBLIC" in s for s in statements)
    assert "GRANT SELECT ON serving.orders TO ROLE role_hooli" in statements


def test_lock_down_removes_public_access_to_core():
    assert all("core" in s and "PUBLIC" in s for s in security.lock_down())


def test_evaluate_detects_leaks_gaps_and_open_base_tables():
    acme = names.TENANT_BY_ID["acme"]
    good = [[{"tenant_id": "acme"}]] * 4
    assert security.evaluate(acme, good, True) == []
    leaky = [[{"tenant_id": "acme"}, {"tenant_id": "globex"}]] + good[1:]
    problems = security.evaluate(acme, leaky, True)
    assert len(problems) == 1 and "globex" in problems[0]
    empty = [[]] + good[1:]
    assert "no rows" in security.evaluate(acme, empty, True)[0]
    assert "readable" in security.evaluate(acme, good, False)[0]


def test_probes_impersonate_the_tenant():
    allowed, refused = security.probe_statements(names.TENANT_BY_ID["umbrella"])
    assert allowed[0] == "SET SESSION AUTHORIZATION tenant_umbrella" == refused[0]
    assert len(allowed) == 1 + len(ddl.serving_objects())
    assert "core.fact_orders" in refused[1]
