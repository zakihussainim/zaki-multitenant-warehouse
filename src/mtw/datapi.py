"""A thin wrapper over the Redshift Data API, for Redshift Serverless and for provisioned clusters.

The Data API needs no network access to the warehouse and no password in your shell: it uses your AWS login plus the
admin secret that Redshift keeps in Secrets Manager. Nothing in this repo contains an account ID or a password.
"""

import time
from dataclasses import dataclass

from . import names


class SqlError(RuntimeError):
    def __init__(self, sql, message):
        super().__init__(f"{message}\n--- statement ---\n{sql.strip()[:600]}")
        self.sql = sql
        self.message = message


@dataclass(frozen=True)
class Target:
    kind: str  # "serverless" or "provisioned"
    name: str  # workgroup name or cluster identifier
    secret_arn: str
    database: str = names.DATABASE

    def api_args(self):
        args = {"Database": self.database, "SecretArn": self.secret_arn}
        if self.kind == "serverless":
            args["WorkgroupName"] = self.name
        else:
            args["ClusterIdentifier"] = self.name
        return args


def resolve_target(env, kind="serverless", boto3_module=None):
    """Finds the secret ARN from the warehouse itself, so nothing needs to be written down anywhere."""
    if boto3_module is None:
        import boto3 as boto3_module  # imported late so unit tests need no AWS packages
    if kind == "serverless":
        client = boto3_module.client("redshift-serverless", region_name=names.REGION)
        namespace = client.get_namespace(namespaceName=names.namespace_name(env))["namespace"]
        secret = namespace.get("adminPasswordSecretArn")
        if not secret:
            raise RuntimeError("the namespace has no managed admin secret yet; is the warehouse deployed?")
        return Target("serverless", names.workgroup_name(env), secret)
    client = boto3_module.client("redshift", region_name=names.REGION)
    cluster = client.describe_clusters(ClusterIdentifier=names.cluster_identifier(env))["Clusters"][0]
    secret = cluster.get("MasterPasswordSecretArn")
    if not secret:
        raise RuntimeError("the cluster has no managed admin secret")
    return Target("provisioned", names.cluster_identifier(env), secret)


def _value(cell):
    for key in ("stringValue", "longValue", "doubleValue", "booleanValue"):
        if key in cell:
            return cell[key]
    return None  # isNull


class DataApi:
    def __init__(self, target, client=None, poll_seconds=1.0, timeout_seconds=1800, sleep=time.sleep):
        self.target = target
        if client is None:
            import boto3

            client = boto3.client("redshift-data", region_name=names.REGION)
        self.client = client
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        self._sleep = sleep

    # -- waiting -------------------------------------------------------------------------------------------------
    def _wait(self, statement_id, sql):
        waited = 0.0
        while True:
            described = self.client.describe_statement(Id=statement_id)
            status = described["Status"]
            if status == "FINISHED":
                return described
            if status in ("FAILED", "ABORTED"):
                raise SqlError(sql, described.get("Error") or f"statement {status.lower()}")
            if waited > self.timeout_seconds:
                raise SqlError(sql, f"timed out after {self.timeout_seconds} seconds (status {status})")
            self._sleep(self.poll_seconds)
            waited += self.poll_seconds

    def _rows(self, statement_id):
        rows, token = [], None
        while True:
            kwargs = {"Id": statement_id}
            if token:
                kwargs["NextToken"] = token
            page = self.client.get_statement_result(**kwargs)
            columns = [c["name"] for c in page["ColumnMetadata"]]
            rows.extend(dict(zip(columns, (_value(cell) for cell in record))) for record in page["Records"])
            token = page.get("NextToken")
            if not token:
                return rows

    # -- public --------------------------------------------------------------------------------------------------
    def execute(self, sql):
        """Runs one statement and returns rows (a list of dicts; empty for statements that return nothing)."""
        response = self.client.execute_statement(Sql=sql, **self.target.api_args())
        described = self._wait(response["Id"], sql)
        if described.get("HasResultSet"):
            return self._rows(response["Id"])
        return []

    def execute_quiet(self, sql, ignore):
        """Like execute, but a failure whose message contains any phrase in `ignore` is treated as success.
        Returns True when the statement ran, False when it was ignored."""
        try:
            self.execute(sql)
            return True
        except SqlError as error:
            if any(phrase.lower() in error.message.lower() for phrase in ignore):
                return False
            raise

    def query_one(self, sql):
        rows = self.execute(sql)
        return rows[0] if rows else None

    def timed_batch(self, statements):
        """Runs statements in order in ONE session (so SET commands apply to what follows) and returns, for each
        statement, a dict with `duration_ms` and `rows`. Session settings such as the query group or the result-cache
        switch only last for one batch, which is exactly what we want."""
        sql_text = "\n".join(statements)
        response = self.client.batch_execute_statement(Sqls=list(statements), **self.target.api_args())
        described = self._wait(response["Id"], sql_text)
        results = []
        for sub, sql in zip(described.get("SubStatements", []), statements):
            entry = {"sql": sql, "duration_ms": sub.get("Duration", 0) / 1_000_000.0, "rows": []}
            if sub.get("HasResultSet"):
                entry["rows"] = self._rows(sub["Id"])
            results.append(entry)
        return results
