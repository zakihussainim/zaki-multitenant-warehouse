from helpers import FakeDataClient, expect_error, fake_api
from mtw import datapi, names


def test_execute_returns_rows_with_typed_values_and_nulls():
    api, client = fake_api({"SELECT a": {"rows": [{"a": 1, "b": "x", "c": 2.5, "d": None, "e": True}]}})
    rows = api.execute("SELECT a")
    assert rows == [{"a": 1, "b": "x", "c": 2.5, "d": None, "e": True}]
    kind, sql, kwargs = client.calls[0]
    assert kwargs["WorkgroupName"] == "wg" and kwargs["SecretArn"] == "arn:secret" and kwargs["Database"] == "warehouse"


def test_statement_without_result_set_returns_empty_list():
    api, _ = fake_api()
    assert api.execute("CREATE TABLE t (a int)") == []


def test_failure_raises_sql_error_with_the_statement():
    api, _ = fake_api({"BOOM": {"error": "relation does not exist"}})
    error = expect_error(api.execute, "SELECT BOOM", exception=datapi.SqlError, contains="relation does not exist")
    assert "SELECT BOOM" in str(error)


def test_execute_quiet_ignores_only_listed_messages():
    api, _ = fake_api({"CREATE ROLE": {"error": 'role "x" already exists'}, "DROP": {"error": "permission denied"}})
    assert api.execute_quiet("CREATE ROLE x", ignore=("already exists",)) is False
    expect_error(api.execute_quiet, "DROP TABLE y", ignore=("already exists",), exception=datapi.SqlError)


def test_timed_batch_reports_each_statement_with_duration_in_ms():
    api, client = fake_api({"SELECT 1": {"rows": [{"n": 1}], "ms": 12.5}, "SET": {"ms": 1}})
    out = api.timed_batch(["SET x TO y", "SELECT 1"])
    assert [round(o["duration_ms"], 1) for o in out] == [1.0, 12.5]
    assert out[1]["rows"] == [{"n": 1}] and out[0]["rows"] == []
    assert client.calls[0][0] == "batch"


def test_timed_batch_failure_raises():
    api, _ = fake_api({"bad": {"error": "permission denied for relation fact_orders"}})
    expect_error(api.timed_batch, ["SET a TO b", "SELECT bad"], exception=datapi.SqlError, contains="permission denied")


def test_provisioned_target_uses_cluster_identifier():
    target = datapi.Target("provisioned", "cl", "arn:secret")
    assert target.api_args()["ClusterIdentifier"] == "cl" and "WorkgroupName" not in target.api_args()


class FakeBoto:
    def __init__(self, secret="arn:secret"):
        self.secret = secret

    def client(self, service, region_name=None):
        outer = self

        class C:
            def get_namespace(self, namespaceName):
                return {"namespace": {"namespaceName": namespaceName, "adminPasswordSecretArn": outer.secret}}

            def describe_clusters(self, ClusterIdentifier):
                return {"Clusters": [{"MasterPasswordSecretArn": outer.secret}]}

        return C()


def test_resolve_target_finds_the_secret_from_the_warehouse():
    target = datapi.resolve_target("dev", "serverless", boto3_module=FakeBoto())
    assert target.name == names.workgroup_name("dev") and target.secret_arn == "arn:secret"
    target = datapi.resolve_target("dev", "provisioned", boto3_module=FakeBoto())
    assert target.name == names.cluster_identifier("dev")
    expect_error(datapi.resolve_target, "dev", "serverless", boto3_module=FakeBoto(secret=None), exception=RuntimeError)
