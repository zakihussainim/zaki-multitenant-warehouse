"""The benchmark queries. Each is a realistic tenant question, written once and pointed at different tables per variant.

Variants: baseline (untuned tables), tuned (sorted/distributed tables), serving (the late-binding view over hot + cold),
mv (materialized views). Queries that only make sense on some variants are left out of the others.
"""

from datetime import timedelta

from . import datagen, ddl, names

VARIANTS = ("baseline", "tuned", "serving", "mv")


def query_set(variant, end_date, hot_months=12):
    """Returns {name: (sql, query_group_or_None)}."""
    if variant not in VARIANTS:
        raise ValueError(variant)
    hot_start = datagen.cold_cutoff(end_date, hot_months).isoformat()
    three_months = datagen.add_months(datagen.month_start(end_date), -2).isoformat()
    recent = (end_date - timedelta(days=29)).isoformat()
    big, mid, small = names.TENANTS[0], names.TENANTS[1], names.TENANTS[2]

    if variant in ("baseline", "tuned"):
        tables = ddl.TABLE_NAMES[variant]
        fact, customer, product = tables["fact"], tables["customer"], tables["product"]
    else:
        fact = ddl.SERVING_VIEW
        customer, product = ddl.TABLE_NAMES["tuned"]["customer"], ddl.TABLE_NAMES["tuned"]["product"]

    if variant == "mv":
        s = names.SERVING
        return {
            "tenant_monthly_revenue": (
                f"SELECT TO_CHAR(month_start, 'YYYY-MM') AS month, SUM(revenue) AS revenue, SUM(orders) AS orders FROM {s}.mv_monthly_revenue "
                f"WHERE tenant_id = '{big.id}' AND month_start >= '{hot_start}' GROUP BY 1 ORDER BY 1",
                big.query_group,
            ),
            "top_products": (
                f"SELECT p.category, p.sku, SUM(m.revenue) AS revenue FROM {s}.mv_top_products m JOIN {product} p ON m.product_key = p.product_key "
                f"WHERE m.tenant_id = '{mid.id}' AND m.month_start >= '{three_months}' GROUP BY 1, 2 ORDER BY 3 DESC, 2 LIMIT 20",
                mid.query_group,
            ),
            "recent_daily": (
                f"SELECT order_date, SUM(orders) AS orders, SUM(revenue) AS revenue FROM {s}.mv_daily_orders "
                f"WHERE tenant_id = '{small.id}' AND order_date >= '{recent}' GROUP BY 1 ORDER BY 1",
                small.query_group,
            ),
            "hot_rollup": (
                f"SELECT tenant_id, SUM(revenue) AS revenue, SUM(orders) AS orders FROM {s}.mv_monthly_revenue "
                f"WHERE month_start >= '{hot_start}' GROUP BY 1 ORDER BY 1",
                None,
            ),
        }

    queries = {
        "tenant_monthly_revenue": (
            f"SELECT d.month_label AS month, SUM(f.revenue) AS revenue, COUNT(*) AS orders FROM {fact} f "
            f"JOIN {ddl.DIM_DATE} d ON f.order_date = d.full_date "
            f"WHERE f.tenant_id = '{big.id}' AND f.order_date >= '{hot_start}' GROUP BY 1 ORDER BY 1",
            big.query_group,
        ),
        "top_products": (
            f"SELECT p.category, p.sku, SUM(f.revenue) AS revenue FROM {fact} f JOIN {product} p ON f.product_key = p.product_key "
            f"WHERE f.tenant_id = '{mid.id}' AND f.order_date >= '{three_months}' GROUP BY 1, 2 ORDER BY 3 DESC, 2 LIMIT 20",
            mid.query_group,
        ),
        "segment_revenue": (
            f"SELECT c.segment, c.country, SUM(f.revenue) AS revenue, COUNT(DISTINCT f.customer_key) AS customers FROM {fact} f "
            f"JOIN {customer} c ON f.customer_key = c.customer_key "
            f"WHERE f.tenant_id = '{big.id}' AND f.order_date >= '{hot_start}' GROUP BY 1, 2 ORDER BY 1, 2",
            big.query_group,
        ),
        "recent_daily": (
            f"SELECT f.order_date, COUNT(*) AS orders, SUM(f.revenue) AS revenue FROM {fact} f "
            f"WHERE f.tenant_id = '{small.id}' AND f.order_date >= '{recent}' GROUP BY 1 ORDER BY 1",
            small.query_group,
        ),
        "hot_rollup": (
            f"SELECT f.tenant_id, SUM(f.revenue) AS revenue, COUNT(*) AS orders FROM {fact} f "
            f"WHERE f.order_date >= '{hot_start}' GROUP BY 1 ORDER BY 1",
            None,
        ),
        "all_time_revenue": (
            f"SELECT d.month_label AS month, SUM(f.revenue) AS revenue, COUNT(*) AS orders FROM {fact} f "
            f"JOIN {ddl.DIM_DATE} d ON f.order_date = d.full_date GROUP BY 1 ORDER BY 1",
            None,
        ),
    }
    return queries
