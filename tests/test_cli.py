from helpers import FakeApi
from mtw import cli, datapi, ddl, names


def test_every_command_parses():
    parser = cli.build_parser()
    for argv in (
        ["generate"],
        ["upload"],
        ["schema"],
        ["load"],
        ["serving"],
        ["security"],
        ["verify-security"],
        ["queues"],
        ["queues", "--target", "provisioned", "--mode", "auto"],
        ["benchmark", "--variant", "tuned"],
        ["usage", "--start", "2026-10-08 10:00:00", "--end", "2026-10-08 11:00:00"],
        ["report", "--a", "baseline", "--b", "tuned"],
        ["offload", "--dry-run"],
        ["footprint"],
        ["wlm-demo", "--label", "x", "--scenario", "runaway"],
    ):
        args = parser.parse_args(argv)
        assert callable(args.func), argv
    assert parser.parse_args(["generate"]).rows == 2_000_000


def test_verify_security_fails_closed_when_a_tenant_can_see_other_tenants(monkeypatch=None):
    leaky_rows = [{"tenant_id": "acme"}, {"tenant_id": "globex"}]
    fake = FakeApi(batch_rows={"FROM serving.orders": leaky_rows})
    fake.fail_on = {"FROM core.fact_orders": "permission denied for relation fact_orders"}
    original = cli._api
    cli._api = lambda args: fake
    try:
        code = cli.main(["verify-security"])
    finally:
        cli._api = original
    assert code == 1


def test_verify_security_passes_when_isolation_holds():
    class PerTenant(FakeApi):
        def timed_batch(self, statements):
            self.statements.extend(statements)
            user = statements[0].split()[-1]
            tenant = user.replace("tenant_", "")
            for sql in statements:
                if "core.fact_orders" in sql:
                    raise datapi.SqlError(sql, "permission denied for relation fact_orders")
            return [{"sql": s, "duration_ms": 1.0, "rows": [{"tenant_id": tenant}] if s.startswith("SELECT") else []} for s in statements]

    original = cli._api
    cli._api = lambda args: PerTenant()
    try:
        code = cli.main(["verify-security"])
    finally:
        cli._api = original
    assert code == 0


def test_security_command_orders_setup_protect_grant():
    fake = FakeApi()
    original = cli._api
    cli._api = lambda args: fake
    try:
        cli.main(["security"])
    finally:
        cli._api = original
    text = fake.statements
    first_attach = next(i for i, s in enumerate(text) if s.startswith("ATTACH"))
    last_enable = max(i for i, s in enumerate(text) if s.endswith("ROW LEVEL SECURITY ON"))
    first_grant = next(i for i, s in enumerate(text) if s.startswith("GRANT SELECT"))
    assert first_attach < last_enable < first_grant
    assert text.index("REVOKE ALL ON SCHEMA core FROM PUBLIC") < first_attach
