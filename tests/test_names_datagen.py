import csv
import gzip
import tempfile
from datetime import date
from pathlib import Path

from helpers import expect_error
from mtw import datagen, names

END = date(2026, 9, 30)


def test_tenant_shares_add_up_and_identifiers_are_safe():
    assert abs(sum(t.share for t in names.TENANTS) - 1.0) < 1e-9
    for tenant in names.TENANTS:
        names.check_identifier(tenant.id)
        names.check_identifier(tenant.role)
        names.check_identifier(tenant.user)
    assert len({t.id for t in names.TENANTS}) == len(names.TENANTS)


def test_check_identifier_rejects_injection():
    for bad in ("acme; drop table x", "Acme", "a b", "x'--", ""):
        expect_error(names.check_identifier, bad, exception=ValueError)


def test_resource_names_follow_one_pattern():
    assert names.bucket_name("dev", "123") == "zaki-multitenant-warehouse-dev-data-123"
    assert names.workgroup_name("prod") == "zaki-multitenant-warehouse-prod"
    assert names.glue_database("dev") == "mtw_dev"
    assert names.redshift_role_name("dev") == "zaki-multitenant-warehouse-dev-redshift"
    assert len(names.namespace_name("prod")) <= 64


def test_month_arithmetic():
    assert datagen.add_months(date(2026, 1, 1), -1) == date(2025, 12, 1)
    assert datagen.add_months(date(2026, 9, 1), -12) == date(2025, 9, 1)
    assert datagen.cold_cutoff(END) == date(2025, 9, 1)
    assert datagen.history_start(END) == date(2024, 10, 1)


def test_tenant_counts_sum_to_total():
    for total in (1000, 12345, 2_000_000):
        assert sum(datagen.tenant_counts(total).values()) == total


def _read(path):
    with gzip.open(path, "rt", newline="") as handle:
        return list(csv.reader(handle))


def test_generation_is_deterministic_and_well_formed():
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        first = datagen.generate(a, 5000, 3, END, rows_per_file=2000)
        second = datagen.generate(b, 5000, 3, END, rows_per_file=2000)
        assert len(first["fact_orders"]) == 3
        rows_a = [r for p in first["fact_orders"] for r in _read(p)]
        rows_b = [r for p in second["fact_orders"] for r in _read(p)]
        assert rows_a == rows_b and len(rows_a) == 5000
        other = datagen.generate(tempfile.mkdtemp(), 5000, 4, END, rows_per_file=2000)
        assert [r for p in other["fact_orders"] for r in _read(p)] != rows_a

        ids = [int(r[0]) for r in rows_a]
        assert ids == sorted(set(ids))
        customers = {int(r[0]): r[1] for r in _read(first["dim_customer"][0])}
        products = {int(r[0]): r[1] for r in _read(first["dim_product"][0])}
        for r in rows_a[:2000]:
            assert customers[int(r[2])] == r[1] and products[int(r[3])] == r[1]  # keys belong to the order's tenant
            assert datagen.history_start(END) <= date.fromisoformat(r[4]) <= END
            assert r[5].startswith(r[4])
            assert abs(float(r[9]) - round(int(r[6]) * float(r[7]) * (1 - float(r[8]) / 100), 2)) < 0.011
        assert {r[1] for r in rows_a} == {t.id for t in names.TENANTS}


def test_recent_months_have_more_orders_than_old_ones():
    with tempfile.TemporaryDirectory() as d:
        written = datagen.generate(d, 20000, 1, END, rows_per_file=20000)
        rows = _read(written["fact_orders"][0])
        cutoff = datagen.cold_cutoff(END).isoformat()
        hot = sum(1 for r in rows if r[4] >= cutoff)
        assert hot > len(rows) - hot


def test_dim_date_covers_every_day_once():
    rows = list(datagen.dim_date_rows(END))
    days = {r[1] for r in rows}
    assert len(days) == len(rows)
    assert min(days) == "2024-10-01" and max(days) == "2026-09-30"
    assert rows[0][0] == 20241001
