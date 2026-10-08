"""Tenant isolation: one role, one user and one row-level-security policy per tenant.

Pattern (and why): base tables in `core` are visible to the platform only. Tenants query `serving` objects, each of which
carries an RLS policy per tenant. Materialized views cannot be built on RLS-protected tables, so RLS is put on the
serving objects, not on the base tables.
"""

from . import ddl, names


def tenant_setup(tenants=names.TENANTS):
    """(statement, ignorable-error-phrases) pairs. CREATE ROLE/USER/GROUP/POLICY have no IF NOT EXISTS, so 'already exists'
    is the expected answer on a re-run."""
    steps = []
    for tenant in tenants:
        names.check_identifier(tenant.id)
        steps += [
            (f"CREATE ROLE {tenant.role}", ("already exists",)),
            (f"CREATE USER {tenant.user} PASSWORD DISABLE", ("already exists",)),
            (f"CREATE GROUP {tenant.group} WITH USER {tenant.user}", ("already exists",)),
            (f"GRANT ROLE {tenant.role} TO {tenant.user}", ("already",)),
            (
                f"CREATE RLS POLICY {tenant.policy} WITH (tenant_id VARCHAR(32)) USING (tenant_id = '{tenant.id}')",
                ("already exists",),
            ),
        ]
    return steps


def lock_down():
    """Tenants get no access to base tables or external tables, whatever the defaults say."""
    return [
        f"REVOKE ALL ON SCHEMA {names.CORE} FROM PUBLIC",
        f"REVOKE ALL ON ALL TABLES IN SCHEMA {names.CORE} FROM PUBLIC",
    ]


def protect(objects, tenants=names.TENANTS):
    """For each serving object: attach every tenant's policy, THEN switch RLS on, THEN (separately) grant SELECT.
    Order matters: with RLS on and no matching policy a role sees nothing, so the failure mode is 'no rows', never 'all rows'."""
    statements = []
    for obj in objects:
        for tenant in tenants:
            statements.append(f"ATTACH RLS POLICY {tenant.policy} ON {obj} TO ROLE {tenant.role}")
        if obj.rsplit(".", 1)[-1].startswith("mv_"):
            statements.append(f"ALTER MATERIALIZED VIEW {obj} ROW LEVEL SECURITY ON")
        else:
            statements.append(f"ALTER TABLE {obj} ROW LEVEL SECURITY ON")
    return statements


def grants(objects, tenants=names.TENANTS):
    statements = []
    for tenant in tenants:
        statements.append(f"GRANT USAGE ON SCHEMA {names.SERVING} TO ROLE {tenant.role}")
        for obj in objects:
            statements.append(f"GRANT SELECT ON {obj} TO ROLE {tenant.role}")
    return statements


# -- proof -------------------------------------------------------------------------------------------------------------
def probe_statements(tenant):
    """Two batches run as the tenant (SET SESSION AUTHORIZATION makes Redshift apply that user's policies):
    one that must succeed, one that must be refused."""
    allowed = [f"SET SESSION AUTHORIZATION {tenant.user}"]
    for obj in ddl.serving_objects():
        allowed.append(f"SELECT DISTINCT tenant_id FROM {obj} ORDER BY 1")
    refused = [f"SET SESSION AUTHORIZATION {tenant.user}", f"SELECT COUNT(*) AS n FROM {names.CORE}.fact_orders"]
    return allowed, refused


def evaluate(tenant, rows_per_probe, base_table_refused):
    """Pure check, easy to test. Returns a list of problems (empty means isolation holds for this tenant)."""
    problems = []
    for obj, rows in zip(ddl.serving_objects(), rows_per_probe):
        seen = {row["tenant_id"] for row in rows}
        if not seen:
            problems.append(f"{obj}: no rows at all (policy or data missing)")
        elif seen != {tenant.id}:
            problems.append(f"{obj}: tenant {tenant.id} can see {sorted(seen)}")
    if not base_table_refused:
        problems.append(f"{names.CORE}.fact_orders is readable by {tenant.user}")
    return problems
