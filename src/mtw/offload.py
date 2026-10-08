"""Moves orders older than the cutoff out of the warehouse into Parquet files on S3, readable through Redshift Spectrum.

Safety rules: never delete from the warehouse until the external table returns exactly the number of rows that were
unloaded; never unload over files that already exist.
"""

import re

from . import datagen, ddl, names, security

MONTH = re.compile(r"order_month=(\d{4}-\d{2})/")


def list_keys(s3, bucket, prefix):
    keys, token = [], None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        keys.extend(item["Key"] for item in page.get("Contents", []))
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated"):
            return keys


def months_in(keys):
    return sorted({m.group(1) for k in keys if (m := MONTH.search(k))})


def prefix_size_bytes(s3, bucket, prefix):
    total, token = 0, None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        total += sum(item["Size"] for item in page.get("Contents", []))
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated"):
            return total


def run_offload(api, s3, env, bucket, end_date, months=12, dry_run=False, log=print):
    cutoff = datagen.cold_cutoff(end_date, months)
    hot_old = int(api.query_one(f"SELECT COUNT(*) AS n FROM {names.CORE}.fact_orders WHERE order_date < '{cutoff.isoformat()}'")["n"])
    existing = list_keys(s3, bucket, ddl.COLD_PREFIX + "/")
    log(f"cutoff {cutoff}: {hot_old:,} old rows in the warehouse, {len(existing)} files already in the cold prefix")
    summary = {"cutoff": cutoff.isoformat(), "rows_moved": 0, "months": [], "dry_run": dry_run}
    if dry_run:
        return summary
    if hot_old and existing:
        raise RuntimeError("old rows are still in the warehouse AND cold files exist; refusing to unload over them. Inspect s3://%s/%s first." % (bucket, ddl.COLD_PREFIX))

    api.execute(ddl.external_schema(names.glue_database(env)))
    if hot_old:
        log("unloading to Parquet ...")
        api.execute(ddl.unload_cold(bucket, cutoff))
        existing = list_keys(s3, bucket, ddl.COLD_PREFIX + "/")
    if not existing:
        log("nothing to offload")
        return summary

    api.execute_quiet(ddl.external_table(bucket), ignore=("already exists",))
    found = months_in(existing)
    for month in found:
        api.execute(ddl.add_partition(bucket, month))
    cold_rows = int(api.query_one(f"SELECT COUNT(*) AS n FROM {names.SPECTRUM}.fact_orders_cold")["n"])
    log(f"external table holds {cold_rows:,} rows across {len(found)} months")
    if hot_old:
        if cold_rows != hot_old:
            raise RuntimeError(f"row count mismatch: unloaded {hot_old:,} but the external table returns {cold_rows:,}. Nothing was deleted.")
        api.execute(ddl.delete_cold(cutoff))
        api.execute(f"VACUUM DELETE ONLY {names.CORE}.fact_orders")
        api.execute(f"ANALYZE {names.CORE}.fact_orders")
        log("deleted the old rows from the warehouse")
    api.execute(ddl.serving_view(include_cold=True))
    for statement in security.spectrum_usage():
        api.execute_quiet(statement, ignore=("does not exist",))  # roles only exist once `security` has run
    for statement in ddl.refresh_materialized_views():
        api.execute(statement)
    summary.update(rows_moved=hot_old, months=found)
    return summary