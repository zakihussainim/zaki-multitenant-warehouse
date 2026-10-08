"""Deterministic synthetic multi-tenant retail data (standard library only).

Writes gzip CSV files with no header, one directory per table, ready for COPY.
The same seed always gives the same files, so before/after numbers compare like with like.
"""

import csv
import gzip
import random
from datetime import date, datetime, timedelta
from pathlib import Path

from . import names

SEGMENTS = ("consumer", "smb", "enterprise", "public")
COUNTRIES = ("GB", "DE", "FR", "US", "ES", "IT", "NL", "SE")
CATEGORIES = ("books", "garden", "kitchen", "toys", "sport", "office", "beauty", "tools")
STATUSES = (("complete", 88), ("shipped", 6), ("returned", 4), ("cancelled", 2))
CHANNELS = ("web", "app", "store", "partner")

CUSTOMERS_PER_100K_ORDERS = 2500
PRODUCTS_PER_TENANT = 400
ROWS_PER_FILE = 250_000
HISTORY_MONTHS = 24


def month_start(d):
    return d.replace(day=1)


def add_months(d, months):
    index = d.year * 12 + (d.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def history_start(end_date, months=HISTORY_MONTHS):
    """First day of the first month of history (the month containing end_date counts as the last month)."""
    return add_months(month_start(end_date), -(months - 1))


def cold_cutoff(end_date, months=12):
    """Orders strictly before this date are cold (older than `months` full months)."""
    return add_months(month_start(end_date), -months)


def tenant_counts(total_rows):
    counts = {t.id: int(total_rows * t.share) for t in names.TENANTS}
    counts[names.TENANTS[0].id] += total_rows - sum(counts.values())
    return counts


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", newline="", encoding="utf-8", compresslevel=3) as handle:
        writer = csv.writer(handle)
        for row in rows:
            writer.writerow(row)


def dim_date_rows(end_date, months=HISTORY_MONTHS):
    day = history_start(end_date, months)
    while day <= end_date:
        yield (
            int(day.strftime("%Y%m%d")),
            day.isoformat(),
            day.year,
            (day.month - 1) // 3 + 1,
            day.month,
            month_start(day).isoformat(),
            day.strftime("%Y-%m"),
            "true" if day.weekday() >= 5 else "false",
        )
        day += timedelta(days=1)


def build_dimensions(total_rows, seed):
    """Returns (customers, products): lists of rows plus per-tenant key ranges for the fact generator."""
    rng = random.Random(f"dims-{seed}")
    counts = tenant_counts(total_rows)
    customers, products = [], []
    customer_keys, product_keys = {}, {}
    next_customer, next_product = 1, 1
    for tenant in names.TENANTS:
        n_customers = max(50, counts[tenant.id] * CUSTOMERS_PER_100K_ORDERS // 100_000)
        keys = list(range(next_customer, next_customer + n_customers))
        next_customer += n_customers
        customer_keys[tenant.id] = keys
        for key in keys:
            signup = date(2022, 1, 1) + timedelta(days=rng.randrange(0, 1200))
            customers.append(
                (key, tenant.id, f"{tenant.id}-customer-{key}", rng.choice(SEGMENTS), rng.choice(COUNTRIES), signup.isoformat())
            )
        pkeys = list(range(next_product, next_product + PRODUCTS_PER_TENANT))
        next_product += PRODUCTS_PER_TENANT
        product_keys[tenant.id] = [(key, round(rng.uniform(2, 250), 2)) for key in pkeys]
        for key, price in product_keys[tenant.id]:
            products.append((key, tenant.id, f"SKU-{key:06d}", rng.choice(CATEGORIES), f"{price:.2f}"))
    return customers, products, customer_keys, product_keys


def order_rows(total_rows, seed, end_date, customer_keys, product_keys):
    """Yields fact rows. Recent days are more likely (growing business); a few customers order far more than others."""
    rng = random.Random(f"orders-{seed}")
    start = history_start(end_date)
    span = (end_date - start).days + 1
    status_names = [s for s, _ in STATUSES]
    status_weights = [w for _, w in STATUSES]
    order_id = 1
    for tenant in names.TENANTS:
        customers = customer_keys[tenant.id]
        products = product_keys[tenant.id]
        for _ in range(tenant_counts(total_rows)[tenant.id]):
            day_index = min(span - 1, int(span * (1 - rng.random() ** 1.6)))
            day = start + timedelta(days=day_index)
            seconds = rng.randrange(0, 86400)
            ts = datetime(day.year, day.month, day.day) + timedelta(seconds=seconds)
            customer = customers[int(len(customers) * rng.random() ** 2)]  # skew: low indexes are the heavy buyers
            product, price = products[rng.randrange(len(products))]
            quantity = rng.choice((1, 1, 1, 2, 2, 3, 4, 5, 8))
            discount = rng.choice((0, 0, 0, 5, 10, 15, 25))
            revenue = round(quantity * price * (1 - discount / 100), 2)
            yield (
                order_id,
                tenant.id,
                customer,
                product,
                day.isoformat(),
                ts.strftime("%Y-%m-%d %H:%M:%S"),
                quantity,
                f"{price:.2f}",
                f"{discount:.2f}",
                f"{revenue:.2f}",
                rng.choices(status_names, status_weights)[0],
                rng.choice(CHANNELS),
            )
            order_id += 1


def generate(out_dir, total_rows, seed, end_date, rows_per_file=ROWS_PER_FILE):
    """Writes all tables under out_dir and returns {table: [paths]}."""
    out_dir = Path(out_dir)
    customers, products, customer_keys, product_keys = build_dimensions(total_rows, seed)
    written = {}
    _write(out_dir / "dim_date" / "part-0000.csv.gz", dim_date_rows(end_date))
    written["dim_date"] = [out_dir / "dim_date" / "part-0000.csv.gz"]
    _write(out_dir / "dim_customer" / "part-0000.csv.gz", customers)
    written["dim_customer"] = [out_dir / "dim_customer" / "part-0000.csv.gz"]
    _write(out_dir / "dim_product" / "part-0000.csv.gz", products)
    written["dim_product"] = [out_dir / "dim_product" / "part-0000.csv.gz"]

    written["fact_orders"] = []
    chunk, number = [], 0
    for row in order_rows(total_rows, seed, end_date, customer_keys, product_keys):
        chunk.append(row)
        if len(chunk) >= rows_per_file:
            path = out_dir / "fact_orders" / f"part-{number:04d}.csv.gz"
            _write(path, chunk)
            written["fact_orders"].append(path)
            chunk, number = [], number + 1
    if chunk:
        path = out_dir / "fact_orders" / f"part-{number:04d}.csv.gz"
        _write(path, chunk)
        written["fact_orders"].append(path)
    return written
