import json

from helpers import expect_error
from mtw import costs, names, queues


class FakeServerless:
    def __init__(self, existing=None, statuses=("AVAILABLE", "MODIFYING", "AVAILABLE")):
        self.existing = existing or []
        self.updates = []
        self._statuses = list(statuses)

    def get_workgroup(self, workgroupName):
        status = self._statuses.pop(0) if self._statuses else "AVAILABLE"
        return {"workgroup": {"status": status, "configParameters": self.existing}}

    def update_workgroup(self, workgroupName, configParameters):
        self.updates.append((workgroupName, configParameters))


class FakeCluster:
    def __init__(self):
        self.modified, self.rebooted, self.polls = [], 0, 0

    def describe_clusters(self, ClusterIdentifier):
        self.polls += 1
        applied = "in-sync" if self.rebooted and self.polls > 2 else "applying"
        return {"Clusters": [{"ClusterStatus": "available", "ClusterParameterGroups": [{"ParameterGroupName": "pg", "ParameterApplyStatus": applied}]}]}

    def modify_cluster_parameter_group(self, **kwargs):
        self.modified.append(kwargs)

    def reboot_cluster(self, ClusterIdentifier):
        self.rebooted += 1


def test_serverless_queues_respect_aws_limits():
    config = queues.serverless_queues()
    assert queues.validate_serverless(config) == []
    assert len(config) == len(names.TENANTS) <= queues.MAX_QUEUES
    assert sum(len(q["rules"]) for q in config) <= queues.MAX_RULES_TOTAL
    assert len(queues.serverless_parameter()["parameterValue"]) < queues.MAX_PARAMETER_BYTES


def test_each_tenant_has_its_own_queue_and_tier_cap():
    config = {q["name"]: q for q in queues.serverless_queues()}
    acme = config["q_acme"]
    assert acme["user_role"] == ["role_acme"] and acme["query_group"] == ["tenant_acme"]
    cap = lambda q: next(r for r in q["rules"] if r["rule_name"].endswith("_runtime"))["predicate"][0]["value"]
    assert cap(config["q_acme"]) > cap(config["q_initech"]) > cap(config["q_hooli"])
    assert all(r["action"] in ("abort", "log") for q in config.values() for r in q["rules"])
    assert any(r["predicate"][0]["metric_name"] == "nested_loop_join_row_count" for r in acme["rules"])


def test_validation_catches_problems():
    config = queues.serverless_queues()
    config[0]["rules"][0]["rule_name"] = config[1]["rules"][0]["rule_name"]
    assert any("unique" in p for p in queues.validate_serverless(config))
    assert any("limit" in p for p in queues.validate_serverless(config * 3))


def test_merge_keeps_other_parameters_and_replaces_ours():
    existing = [{"parameterKey": "datestyle", "parameterValue": "ISO"}, {"parameterKey": queues.WLM_KEY, "parameterValue": "[]"}]
    merged = queues.merged_parameters(existing, {"parameterKey": queues.WLM_KEY, "parameterValue": "[1]"})
    assert merged == [{"parameterKey": "datestyle", "parameterValue": "ISO"}, {"parameterKey": queues.WLM_KEY, "parameterValue": "[1]"}]


def test_apply_serverless_updates_then_waits_for_available():
    client = FakeServerless(existing=[{"parameterKey": "datestyle", "parameterValue": "ISO"}])
    sleeps = []
    params = queues.apply_serverless("dev", client=client, sleep=sleeps.append)
    name, sent = client.updates[0]
    assert name == "zaki-multitenant-warehouse-dev" and sent == params
    assert json.loads(sent[-1]["parameterValue"])[0]["name"] == "q_acme"
    assert sleeps  # waited at least once for the workgroup to finish modifying


def test_manual_wlm_is_valid():
    config = queues.manual_wlm()
    assert queues.validate_manual(config) == []
    real = [q for q in config if "query_concurrency" in q]
    assert sum(q["memory_percent_to_use"] for q in real) == 100
    assert "query_group" not in real[-1]  # default queue is last
    assert [q["query_group"] for q in real[:-1]] == [[t.query_group] for t in names.TENANTS]
    broken = queues.manual_wlm()
    broken[0]["memory_percent_to_use"] = 90
    assert queues.validate_manual(broken)


def test_switch_cluster_wlm_modifies_reboots_and_waits():
    for mode, expected in (("manual", "query_concurrency"), ("auto", "auto_wlm")):
        client = FakeCluster()
        queues.switch_cluster_wlm("dev", mode, client=client, sleep=lambda s: None)
        assert expected in client.modified[0]["Parameters"][0]["ParameterValue"]
        assert client.rebooted == 1
    expect_error(queues.switch_cluster_wlm, "dev", "other", client=FakeCluster(), exception=ValueError)


def test_costs():
    assert costs.rpu_hours(3600, 4) == 4
    assert abs(costs.compute_cost(4) - 1.5) < 1e-9
    assert costs.offload_saving_monthly(1024) > 0
    assert costs.scale(2.0, 10, 100) == 20.0
    assert costs.storage_monthly(1, 1) > 0
