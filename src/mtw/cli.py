"""Command line for the whole project:  python -m mtw <command> --env dev

Typical order: generate, upload, schema, load, serving, security, verify-security, queues, benchmark (x4), offload, benchmark, report.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import benchmark, costs, datagen, datapi, ddl, names, offload, queues, security, wlm_demo

DEFAULT_END = "2026-09-30"


def _account_id():
    import boto3

    return boto3.client("sts", region_name=names.REGION).get_caller_identity()["Account"]


def _api(args):
    return datapi.DataApi(datapi.resolve_target(args.env, args.target))


def _bucket(args):
    return names.bucket_name(args.env, _account_id())


def _run_all(api, statements, label):
    for statement in statements:
        first_line = statement.strip().splitlines()[0][:90]
        print(f"  {label}: {first_line}")
        api.execute(statement)


def cmd_generate(args):
    written = datagen.generate(args.out, args.rows, args.seed, date.fromisoformat(args.end_date))
    for table, paths in written.items():
        print(f"{table}: {len(paths)} file(s)")
    print(f"cold cutoff for the offload step: {datagen.cold_cutoff(date.fromisoformat(args.end_date))}")


def cmd_upload(args):
    import boto3

    s3 = boto3.client("s3", region_name=names.REGION)
    bucket = _bucket(args)
    count = 0
    for path in sorted(Path(args.out).rglob("*.csv.gz")):
        table = path.parent.name
        s3.upload_file(str(path), bucket, f"raw/{table}/{path.name}")
        count += 1
    print(f"uploaded {count} file(s) to s3://{bucket}/raw/")


def cmd_schema(args):
    api = _api(args)
    _run_all(api, ddl.schemas(), "schema")
    for variant in (ddl.TUNED, ddl.BASELINE):
        _run_all(api, ddl.create_tables(variant), variant)


def cmd_load(args):
    api, bucket = _api(args), _bucket(args)
    for variant in (ddl.TUNED, ddl.BASELINE):
        for table in ddl.TABLE_NAMES[variant].values():
            api.execute(f"TRUNCATE {table}")
    api.execute(f"TRUNCATE {ddl.DIM_DATE}")
    for variant in (ddl.TUNED, ddl.BASELINE):
        _run_all(api, ddl.load_statements(bucket, variant), f"load {variant}")
    for table in (ddl.TABLE_NAMES["tuned"]["fact"], ddl.TABLE_NAMES["baseline"]["fact"]):
        print(table, api.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"], "rows")


def cmd_serving(args):
    api = _api(args)
    for statement in ddl.create_materialized_views():
        print("  mv:", statement.split(" AS ")[0])
        api.execute_quiet(statement, ignore=("already exists",))
    _run_all(api, [ddl.serving_view(include_cold=False)], "view")


def cmd_security(args):
    api = _api(args)
    for statement, ignore in security.tenant_setup():
        api.execute_quiet(statement, ignore)
    _run_all(api, security.lock_down(), "lock down")
    objects = ddl.serving_objects()
    _run_all(api, security.protect(objects), "protect")
    _run_all(api, security.grants(objects), "grant")
    print("done; now run verify-security")


def cmd_verify_security(args):
    api = _api(args)
    failures = 0
    for tenant in names.TENANTS:
        allowed, refused = security.probe_statements(tenant)
        results = api.timed_batch(allowed)
        rows = [entry["rows"] for entry in results[1:]]
        try:
            api.timed_batch(refused)
            refused_ok = False
        except datapi.SqlError as error:
            refused_ok = "permission denied" in error.message.lower()
        problems = security.evaluate(tenant, rows, refused_ok)
        print(f"{tenant.id:<10} {'OK' if not problems else 'FAIL'}")
        for problem in problems:
            print(f"    {problem}")
        failures += len(problems)
    if failures:
        print(f"{failures} isolation problem(s): do not give tenants access until this passes")
        return 1
    print("every tenant sees only its own rows, and cannot read the base tables")
    return 0


def cmd_queues(args):
    if args.target == "serverless":
        params = queues.apply_serverless(args.env)
        print("queues enabled on the workgroup:", json.dumps(json.loads(params[-1]["parameterValue"]))[:300], "...")
    else:
        config = queues.switch_cluster_wlm(args.env, args.mode)
        print(f"cluster WLM set to {args.mode}:", json.dumps(config)[:300])


def cmd_benchmark(args):
    api = _api(args)
    end = date.fromisoformat(args.end_date)
    label = args.label or args.variant
    print(f"benchmark '{label}' on the {args.variant} tables, {args.runs} runs each (result cache off)")
    results = benchmark.run_suite(api, args.variant, end, label, runs=args.runs)
    if args.target == "serverless" and args.billing:
        print("waiting is not needed here: run `python -m mtw usage` ten minutes later for the billed compute")
    path = benchmark.save(results, args.results)
    print("saved", path)


def cmd_usage(args):
    api = _api(args)
    hours = benchmark.billed_rpu_hours(api, args.start, args.end)
    print(json.dumps(benchmark.cost_summary(hours), indent=2))


def cmd_report(args):
    a, b = benchmark.load(Path(args.results) / f"benchmark-{args.a}.json"), benchmark.load(Path(args.results) / f"benchmark-{args.b}.json")
    print(benchmark.markdown_table(benchmark.compare(a, b), args.a, args.b))


def cmd_offload(args):
    import boto3

    api, bucket = _api(args), _bucket(args)
    s3 = boto3.client("s3", region_name=names.REGION)
    summary = offload.run_offload(api, s3, args.env, bucket, date.fromisoformat(args.end_date), months=args.months, dry_run=args.dry_run)
    print(json.dumps(summary, indent=2))


def cmd_footprint(args):
    """Storage numbers for the cost section: warehouse size per table, and bytes in S3."""
    import boto3

    api, bucket = _api(args), _bucket(args)
    rows = api.execute(
        "SELECT \"table\" AS table_name, size AS size_mb, tbl_rows FROM svv_table_info "
        f"WHERE schema = '{names.CORE}' ORDER BY 1"
    )
    for row in rows:
        print(f"{row['table_name']:<24} {row['size_mb']:>8} MB  {row['tbl_rows']:>12} rows")
    s3 = boto3.client("s3", region_name=names.REGION)
    cold = offload.prefix_size_bytes(s3, bucket, ddl.COLD_PREFIX + "/")
    print(f"cold Parquet on S3: {cold / 1024 / 1024:.1f} MB")
    hot_gb = sum(float(r["size_mb"]) for r in rows if r["table_name"] == "fact_orders") / 1024
    cold_gb = cold / 1024**3
    prices = costs.Prices()
    print(f"storage now: hot {hot_gb:.3f} GB + cold {cold_gb:.3f} GB = {costs.storage_monthly(hot_gb, cold_gb, prices):.4f} USD / month")
    print(f"projection at 1 TB moved to S3: saves about {costs.offload_saving_monthly(1024, prices):.2f} USD / month (prices are assumptions)")


def cmd_wlm_demo(args):
    api = _api(args)
    tenants = names.TENANT_BY_ID
    result = wlm_demo.run_demo(api, args.scenario, tenants[args.heavy], tenants[args.light], heavy_workers=args.workers)
    path = Path(args.results) / f"wlm-{args.label}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print("saved", path)


def build_parser():
    parser = argparse.ArgumentParser(prog="mtw", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text, warehouse=True):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--env", default="dev", choices=names.ENVIRONMENTS)
        if warehouse:
            p.add_argument("--target", default="serverless", choices=("serverless", "provisioned"))
        p.set_defaults(func=func)
        return p

    p = add("generate", cmd_generate, "write synthetic data files locally", warehouse=False)
    p.add_argument("--rows", type=int, default=2_000_000)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--end-date", default=DEFAULT_END)
    p.add_argument("--out", default="data")

    p = add("upload", cmd_upload, "copy the generated files to the S3 bucket", warehouse=False)
    p.add_argument("--out", default="data")

    add("schema", cmd_schema, "create schemas and the baseline and tuned tables")
    add("load", cmd_load, "COPY the data into both table variants")

    add("serving", cmd_serving, "create the serving view and materialized views")

    add("security", cmd_security, "create tenant roles, users, RLS policies and grants")
    add("verify-security", cmd_verify_security, "prove each tenant only sees its own rows")

    p = add("queues", cmd_queues, "turn on workload-management queues (Serverless: permanent; provisioned: reboots the cluster)")
    p.add_argument("--mode", default="manual", choices=("auto", "manual"), help="provisioned only")

    p = add("benchmark", cmd_benchmark, "time the query set on one table variant")
    p.add_argument("--variant", required=True, choices=("baseline", "tuned", "serving", "mv"))
    p.add_argument("--label")
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--end-date", default=DEFAULT_END)
    p.add_argument("--results", default="results")
    p.add_argument("--billing", action="store_true")

    p = add("usage", cmd_usage, "billed RPU-hours between two UTC timestamps (Serverless)")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)

    p = add("report", cmd_report, "markdown table comparing two saved benchmark runs", warehouse=False)
    p.add_argument("--a", required=True)
    p.add_argument("--b", required=True)
    p.add_argument("--results", default="results")

    p = add("offload", cmd_offload, "move cold orders to S3 and query them through Spectrum")
    p.add_argument("--months", type=int, default=12)
    p.add_argument("--end-date", default=DEFAULT_END)
    p.add_argument("--dry-run", action="store_true")

    add("footprint", cmd_footprint, "storage sizes and the storage cost model")

    p = add("wlm-demo", cmd_wlm_demo, "heavy tenant versus light tenant")
    p.add_argument("--scenario", default="concurrent", choices=("concurrent", "runaway"))
    p.add_argument("--heavy", default="acme", choices=sorted(names.TENANT_BY_ID))
    p.add_argument("--light", default="hooli", choices=sorted(names.TENANT_BY_ID))
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--label", required=True)
    p.add_argument("--results", default="results")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
