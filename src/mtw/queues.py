"""Workload management: what stops one tenant's heavy query from hurting the others.

Redshift Serverless (since January 2026): up to 8 queues per workgroup, routed by role or query group, each with
monitoring rules (log or abort). There are no concurrency slots or memory percentages, because Serverless scales itself.
Provisioned clusters: classic manual WLM, with concurrency and memory per queue. Both are generated here.
"""

import json
import time

from . import names

# runtime cap in seconds by tier, and a point at which a very large scan is logged
TIER_LIMITS = {"large": 120, "medium": 60, "small": 30}
SCAN_ROW_LIMIT = 1_000_000_000

# The only metric names Redshift Serverless accepts in a queue rule (AWS rejects anything else when the configuration is applied).
VALID_METRICS = (
    "query_execution_time",
    "query_queue_time",
    "join_row_count",
    "nested_loop_join_row_count",
    "query_temp_blocks_to_disk",
    "query_blocks_read",
    "estimated_query_execution_time",
    "scan_row_count",
)
NESTED_LOOP_ROW_LIMIT = 100_000_000  # catches accidental cross joins long before they hurt anyone
MAX_RULES_TOTAL = 25  # Serverless limit, across all queues
MAX_QUEUES = 8
MAX_PARAMETER_BYTES = 8000

WLM_KEY = "wlm_json_configuration"


def serverless_queues(tenants=names.TENANTS):
    queues = []
    for tenant in tenants:
        cap = TIER_LIMITS[tenant.tier]
        queues.append(
            {
                "name": f"q_{tenant.id}",
                "user_role": [tenant.role],
                "query_group": [tenant.query_group],
                "rules": [
                    {
                        "rule_name": f"{tenant.id}_runtime",
                        "predicate": [{"metric_name": "query_execution_time", "operator": ">", "value": cap}],
                        "action": "abort",
                    },
                    {
                        "rule_name": f"{tenant.id}_crossjoin",
                        "predicate": [{"metric_name": "nested_loop_join_row_count", "operator": ">", "value": NESTED_LOOP_ROW_LIMIT}],
                        "action": "abort",
                    },
                    {
                        "rule_name": f"{tenant.id}_bigscan",
                        "predicate": [{"metric_name": "scan_row_count", "operator": ">", "value": SCAN_ROW_LIMIT}],
                        "action": "log",
                    },
                ],
            }
        )
    return queues


def validate_serverless(queues):
    problems = []
    if len(queues) > MAX_QUEUES:
        problems.append(f"{len(queues)} queues; the limit is {MAX_QUEUES}")
    rule_names = [r["rule_name"] for q in queues for r in q["rules"]]
    if len(rule_names) > MAX_RULES_TOTAL:
        problems.append(f"{len(rule_names)} rules; the limit is {MAX_RULES_TOTAL}")
    if len(set(rule_names)) != len(rule_names):
        problems.append("rule names must be unique")
    for q in queues:
        for r in q["rules"]:
            if len(r["predicate"]) > 3:
                problems.append(f"{r['rule_name']} has more than 3 predicates")
            for predicate in r["predicate"]:
                if predicate["metric_name"] not in VALID_METRICS:
                    problems.append(f"{r['rule_name']} uses an unknown metric: {predicate['metric_name']}")
    if len(json.dumps(queues, separators=(",", ":"))) > MAX_PARAMETER_BYTES:
        problems.append("configuration is larger than 8000 characters")
    return problems


def serverless_parameter(tenants=names.TENANTS):
    queues = serverless_queues(tenants)
    problems = validate_serverless(queues)
    if problems:
        raise ValueError("; ".join(problems))
    return {"parameterKey": WLM_KEY, "parameterValue": json.dumps(queues, separators=(",", ":"))}


# Workgroup-wide limits such as max_query_execution_time. AWS refuses them alongside wlm_json_configuration:
# the same limits must be written as rules inside the queue configuration instead (ours are).
QUERY_LIMIT_PREFIX = "max_"


def merged_parameters(existing, new):
    """update_workgroup replaces the whole list, so keep everything that was already set except the key we change
    and the individual query limits, which cannot be combined with queue configuration."""
    kept = [
        {"parameterKey": p["parameterKey"], "parameterValue": p["parameterValue"]}
        for p in existing
        if p["parameterKey"] != new["parameterKey"] and not p["parameterKey"].startswith(QUERY_LIMIT_PREFIX)
    ]
    return kept + [new]


def apply_serverless(env, tenants=names.TENANTS, client=None, sleep=time.sleep):
    """Turns the queues on. NOTE: AWS does not allow queues to be switched off again once enabled on a workgroup."""
    if client is None:
        import boto3

        client = boto3.client("redshift-serverless", region_name=names.REGION)
    workgroup = names.workgroup_name(env)
    current = client.get_workgroup(workgroupName=workgroup)["workgroup"]
    params = merged_parameters(current.get("configParameters", []), serverless_parameter(tenants))
    client.update_workgroup(workgroupName=workgroup, configParameters=params)
    for _ in range(120):
        status = client.get_workgroup(workgroupName=workgroup)["workgroup"]["status"]
        if status == "AVAILABLE":
            return params
        sleep(10)
    raise RuntimeError("workgroup did not return to AVAILABLE after the update")


# -- provisioned ----------------------------------------------------------------------------------------------------------
AUTO_WLM = [{"auto_wlm": True}]


def manual_wlm(tenants=names.TENANTS, heavy_tenant="acme"):
    """Manual WLM: each tenant gets its own queue with a memory share and a concurrency limit, routed by query group.
    The last entry is the default queue. Memory shares add up to 100."""
    memory = {"large": 25, "medium": 15, "small": 10}
    queues = []
    used = 0
    for tenant in tenants:
        share = memory[tenant.tier]
        used += share
        queues.append(
            {
                "query_group": [tenant.query_group],
                "query_concurrency": 2 if tenant.id == heavy_tenant else 3,
                "memory_percent_to_use": share,
                "max_execution_time": TIER_LIMITS[tenant.tier] * 1000,
            }
        )
    queues.append({"query_concurrency": 5, "memory_percent_to_use": 100 - used})
    return queues


def validate_manual(queues):
    problems = []
    real = [q for q in queues if "query_concurrency" in q]
    if sum(q["memory_percent_to_use"] for q in real) != 100:
        problems.append("memory shares must add up to 100")
    if sum(q["query_concurrency"] for q in real) > 50:
        problems.append("total concurrency above 50")
    if "query_group" in real[-1] or "user_group" in real[-1]:
        problems.append("the last queue must be the default queue (no groups)")
    return problems


def switch_cluster_wlm(env, mode, client=None, sleep=time.sleep):
    """Provisioned only. Changing the WLM configuration needs a reboot (a few minutes), which is why this is a command and not Terraform."""
    if client is None:
        import boto3

        client = boto3.client("redshift", region_name=names.REGION)
    if mode == "auto":
        config = AUTO_WLM
    elif mode == "manual":
        config = manual_wlm()
        problems = validate_manual(config)
        if problems:
            raise ValueError("; ".join(problems))
    else:
        raise ValueError(mode)
    cluster = names.cluster_identifier(env)
    group = client.describe_clusters(ClusterIdentifier=cluster)["Clusters"][0]["ClusterParameterGroups"][0]["ParameterGroupName"]
    client.modify_cluster_parameter_group(
        ParameterGroupName=group,
        Parameters=[{"ParameterName": WLM_KEY, "ParameterValue": json.dumps(config, separators=(",", ":")), "ApplyType": "dynamic"}],
    )
    client.reboot_cluster(ClusterIdentifier=cluster)
    for _ in range(180):
        sleep(10)
        state = client.describe_clusters(ClusterIdentifier=cluster)["Clusters"][0]
        applied = state["ClusterParameterGroups"][0]["ParameterApplyStatus"]
        if state["ClusterStatus"] == "available" and applied == "in-sync":
            return config
    raise RuntimeError("cluster did not come back in sync after the reboot")