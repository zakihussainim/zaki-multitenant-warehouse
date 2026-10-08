"""Every name in the project in one place. The Terraform code builds the same names; tests/test_contracts.py checks they agree."""

import re
from dataclasses import dataclass

PROJECT = "zaki-multitenant-warehouse"
REGION = "eu-west-2"
ENVIRONMENTS = ("dev", "prod")
DATABASE = "warehouse"
ADMIN_USER = "admin"

# Redshift schemas
CORE = "core"  # base tables, only the platform (superuser) touches these
SERVING = "serving"  # what tenants may query: one late-binding view plus materialized views, all row-level secured
SPECTRUM = "spectrum"  # external schema over S3 (cold data)

PLATFORM_QUERY_GROUP = "platform"


@dataclass(frozen=True)
class Tenant:
    id: str
    share: float  # share of all orders
    tier: str  # large / medium / small: decides the runtime cap in the workload-management queue

    @property
    def role(self):
        return f"role_{self.id}"

    @property
    def user(self):
        return f"tenant_{self.id}"

    @property
    def group(self):
        return f"grp_{self.id}"

    @property
    def query_group(self):
        return f"tenant_{self.id}"

    @property
    def policy(self):
        return f"policy_{self.id}"


TENANTS = (
    Tenant("acme", 0.40, "large"),
    Tenant("globex", 0.25, "large"),
    Tenant("initech", 0.15, "medium"),
    Tenant("umbrella", 0.12, "medium"),
    Tenant("hooli", 0.08, "small"),
)

TENANT_BY_ID = {t.id: t for t in TENANTS}

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{1,30}$")


def check_identifier(value):
    """Everything that ends up inside SQL as an identifier or literal goes through here first."""
    if not _IDENTIFIER.match(value):
        raise ValueError(f"unsafe identifier: {value!r}")
    return value


def prefix(env):
    return f"{PROJECT}-{env}"


def bucket_name(env, account_id):
    return f"{prefix(env)}-data-{account_id}"


def namespace_name(env):
    return prefix(env)


def workgroup_name(env):
    return prefix(env)


def cluster_identifier(env):
    return f"{prefix(env)}-wlm"


def glue_database(env):
    return f"mtw_{env}"


def redshift_role_name(env):
    return f"{prefix(env)}-redshift"
