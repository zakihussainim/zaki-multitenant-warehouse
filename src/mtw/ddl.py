"""All warehouse SQL that is not tenant-specific: schemas, tables (baseline and tuned), COPY, serving layer, offload."""

from . import names
from .names import CORE, SERVING, SPECTRUM

ORDER_COLUMNS = (
    "order_id",
    "tenant_id",
    "customer_key",
    "product_key",
    "order_date",
    "order_ts",
    "quantity",
    "unit_price",
    "discount_pct",
    "revenue",
    "status",
    "channel",
)

# name -> (type used by the tuned tables, type used by the deliberately untuned baseline tables)
_FACT_TYPES = {
    "order_id": ("BIGINT NOT NULL", "BIGINT"),
    "tenant_id": ("VARCHAR(32) NOT NULL", "VARCHAR(256)"),
    "customer_key": ("INTEGER NOT NULL", "INTEGER"),
    "product_key": ("INTEGER NOT NULL", "INTEGER"),
    "order_date": ("DATE NOT NULL", "DATE"),
    "order_ts": ("TIMESTAMP NOT NULL", "TIMESTAMP"),
    "quantity": ("INTEGER NOT NULL", "INTEGER"),
    "unit_price": ("DECIMAL(10,2) NOT NULL", "DECIMAL(10,2)"),
    "discount_pct": ("DECIMAL(5,2) NOT NULL", "DECIMAL(5,2)"),
    "revenue": ("DECIMAL(12,2) NOT NULL", "DECIMAL(12,2)"),
    "status": ("VARCHAR(12) NOT NULL", "VARCHAR(256)"),
    "channel": ("VARCHAR(12) NOT NULL", "VARCHAR(256)"),
}

_DIM_TYPES = {
    "dim_customer": (
        ("customer_key", "INTEGER NOT NULL", "INTEGER"),
        ("tenant_id", "VARCHAR(32) NOT NULL", "VARCHAR(256)"),
        ("customer_name", "VARCHAR(64) NOT NULL", "VARCHAR(256)"),
        ("segment", "VARCHAR(16) NOT NULL", "VARCHAR(256)"),
        ("country", "VARCHAR(2) NOT NULL", "VARCHAR(256)"),
        ("signup_date", "DATE NOT NULL", "DATE"),
    ),
    "dim_product": (
        ("product_key", "INTEGER NOT NULL", "INTEGER"),
        ("tenant_id", "VARCHAR(32) NOT NULL", "VARCHAR(256)"),
        ("sku", "VARCHAR(16) NOT NULL", "VARCHAR(256)"),
        ("category", "VARCHAR(24) NOT NULL", "VARCHAR(256)"),
        ("list_price", "DECIMAL(10,2) NOT NULL", "DECIMAL(10,2)"),
    ),
}

DIM_DATE_COLUMNS = "date_key INTEGER NOT NULL, full_date DATE NOT NULL, year SMALLINT NOT NULL, quarter SMALLINT NOT NULL, month SMALLINT NOT NULL, month_start DATE NOT NULL, month_label VARCHAR(7) NOT NULL, is_weekend BOOLEAN NOT NULL"

BASELINE = "baseline"
TUNED = "tuned"

TABLE_NAMES = {
    TUNED: {"fact": f"{CORE}.fact_orders", "customer": f"{CORE}.dim_customer", "product": f"{CORE}.dim_product"},
    BASELINE: {
        "fact": f"{CORE}.fact_orders_baseline",
        "customer": f"{CORE}.dim_customer_baseline",
        "product": f"{CORE}.dim_product_baseline",
    },
}
DIM_DATE = f"{CORE}.dim_date"


def schemas():
    return [f"CREATE SCHEMA IF NOT EXISTS {CORE}", f"CREATE SCHEMA IF NOT EXISTS {SERVING}"]


def create_tables(variant):
    """DDL for one variant. 'baseline' is how a first draft looks: even distribution, no sort key, oversized varchars.
    'tuned' is what the benchmark queries ask for: orders sorted by (tenant, date), distributed on the join key
    customer_key, small dimensions copied to every node, and tight column sizes."""
    if variant not in (BASELINE, TUNED):
        raise ValueError(variant)
    index = 0 if variant == TUNED else 1
    tables = TABLE_NAMES[variant]
    fact_cols = ",\n  ".join(f"{c} {_FACT_TYPES[c][index]}" for c in ORDER_COLUMNS)
    statements = []
    if variant == TUNED:
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {tables['fact']} (\n  {fact_cols}\n)\nDISTSTYLE KEY DISTKEY (customer_key)\nCOMPOUND SORTKEY (tenant_id, order_date)"
        )
    else:
        statements.append(f"CREATE TABLE IF NOT EXISTS {tables['fact']} (\n  {fact_cols}\n)\nDISTSTYLE EVEN")
    for dim, key in (("dim_customer", "customer"), ("dim_product", "product")):
        cols = ",\n  ".join(f"{c[0]} {c[1 + index]}" for c in _DIM_TYPES[dim])
        if variant == TUNED:
            diststyle = "DISTSTYLE KEY DISTKEY (customer_key)\nSORTKEY (customer_key)" if dim == "dim_customer" else "DISTSTYLE ALL\nSORTKEY (product_key)"
        else:
            diststyle = "DISTSTYLE EVEN"
        statements.append(f"CREATE TABLE IF NOT EXISTS {tables[key]} (\n  {cols}\n)\n{diststyle}")
    if variant == BASELINE:
        # Redshift's automatic table optimisation would otherwise pick a sort key for us and spoil the baseline.
        for table in tables.values():
            statements.append(f"ALTER TABLE {table} ALTER SORTKEY NONE")
    else:
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {DIM_DATE} ({DIM_DATE_COLUMNS})\nDISTSTYLE ALL\nSORTKEY (full_date)"
        )
    return statements


def copy_statement(table, bucket, prefix, region=names.REGION):
    return (
        f"COPY {table} FROM 's3://{bucket}/{prefix}/' IAM_ROLE default FORMAT CSV GZIP "
        f"DATEFORMAT 'auto' TIMEFORMAT 'auto' REGION '{region}'"
    )


def load_statements(bucket, variant):
    """Which S3 folders feed which table. The same files feed both variants, so the data is identical."""
    tables = TABLE_NAMES[variant]
    pairs = [
        (tables["fact"], "raw/fact_orders"),
        (tables["customer"], "raw/dim_customer"),
        (tables["product"], "raw/dim_product"),
    ]
    if variant == TUNED:
        pairs.append((DIM_DATE, "raw/dim_date"))
    statements = []
    for table, prefix in pairs:
        statements.append(copy_statement(table, bucket, prefix))
    for table, _ in pairs:
        statements.append(f"ANALYZE {table}")
    return statements


# -- serving layer ---------------------------------------------------------------------------------------------------
SERVING_VIEW = f"{SERVING}.orders"
MATERIALIZED_VIEWS = {
    "mv_monthly_revenue": (
        "SELECT tenant_id, CAST(DATE_TRUNC('month', order_date) AS DATE) AS month_start, COUNT(*) AS orders, "
        f"SUM(revenue) AS revenue, SUM(quantity) AS units FROM {CORE}.fact_orders GROUP BY 1, 2"
    ),
    "mv_top_products": (
        "SELECT tenant_id, product_key, CAST(DATE_TRUNC('month', order_date) AS DATE) AS month_start, COUNT(*) AS orders, "
        f"SUM(revenue) AS revenue, SUM(quantity) AS units FROM {CORE}.fact_orders GROUP BY 1, 2, 3"
    ),
    "mv_daily_orders": (
        f"SELECT tenant_id, order_date, COUNT(*) AS orders, SUM(revenue) AS revenue, SUM(quantity) AS units FROM {CORE}.fact_orders GROUP BY 1, 2"
    ),
}


def serving_objects():
    """Every object tenants may read. Row-level security and grants are applied to exactly this list."""
    return [SERVING_VIEW] + [f"{SERVING}.{name}" for name in MATERIALIZED_VIEWS]


def serving_view(include_cold):
    cols = ", ".join(ORDER_COLUMNS)
    hot = f"SELECT {cols} FROM {CORE}.fact_orders"
    body = hot if not include_cold else f"{hot} UNION ALL SELECT {cols} FROM {SPECTRUM}.fact_orders_cold"
    return f"CREATE OR REPLACE VIEW {SERVING_VIEW} AS {body} WITH NO SCHEMA BINDING"


def create_materialized_views():
    return [f"CREATE MATERIALIZED VIEW {SERVING}.{name} AS {query}" for name, query in MATERIALIZED_VIEWS.items()]


def refresh_materialized_views():
    return [f"REFRESH MATERIALIZED VIEW {SERVING}.{name}" for name in MATERIALIZED_VIEWS]


# -- offload of cold data to S3 (Spectrum) -------------------------------------------------------------------------------
COLD_PREFIX = "cold/fact_orders"


def external_schema(glue_db):
    return (
        f"CREATE EXTERNAL SCHEMA IF NOT EXISTS {SPECTRUM} FROM DATA CATALOG DATABASE '{glue_db}' "
        f"IAM_ROLE default CREATE EXTERNAL DATABASE IF NOT EXISTS"
    )


def external_table(bucket):
    cols = ",\n  ".join(f"{c} {_FACT_TYPES[c][0].replace(' NOT NULL', '')}" for c in ORDER_COLUMNS)
    return (
        f"CREATE EXTERNAL TABLE {SPECTRUM}.fact_orders_cold (\n  {cols}\n)\n"
        f"PARTITIONED BY (order_month VARCHAR(7))\nSTORED AS PARQUET\nLOCATION 's3://{bucket}/{COLD_PREFIX}/'"
    )


def unload_cold(bucket, cutoff):
    cols = ", ".join(ORDER_COLUMNS)
    inner = (
        f"SELECT {cols}, TO_CHAR(order_date, ''YYYY-MM'') AS order_month FROM {CORE}.fact_orders "
        f"WHERE order_date < ''{cutoff.isoformat()}''"
    )
    return f"UNLOAD ('{inner}') TO 's3://{bucket}/{COLD_PREFIX}/' IAM_ROLE default FORMAT PARQUET PARTITION BY (order_month)"


def add_partition(bucket, month):
    return (
        f"ALTER TABLE {SPECTRUM}.fact_orders_cold ADD IF NOT EXISTS PARTITION (order_month='{month}') "
        f"LOCATION 's3://{bucket}/{COLD_PREFIX}/order_month={month}/'"
    )


def delete_cold(cutoff):
    return f"DELETE FROM {CORE}.fact_orders WHERE order_date < '{cutoff.isoformat()}'"
