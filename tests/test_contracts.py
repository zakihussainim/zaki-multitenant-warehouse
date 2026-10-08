"""The Python code and the Terraform must agree on names. These tests read the Terraform text and compare."""
import re
from pathlib import Path

from mtw import names, queues

ROOT = Path(__file__).resolve().parents[1] / "terraform"


def read(*parts):
    return (ROOT.joinpath(*parts)).read_text()


def test_bucket_name_matches():
    text = read("modules", "storage", "main.tf")
    assert 'bucket        = "zaki-multitenant-warehouse-${var.environment}-data-${local.account_id}"' in text
    assert names.bucket_name("dev", "ACCT") == "zaki-multitenant-warehouse-dev-data-ACCT"


def test_warehouse_names_match():
    text = read("modules", "warehouse", "main.tf")
    assert 'name          = "zaki-multitenant-warehouse-${var.environment}"' in text
    assert 'glue_database = "mtw_${var.environment}"' in text
    assert 'namespace_name = local.name' in text and 'workgroup_name = local.name' in text
    assert f'db_name        = "{names.DATABASE}"' in text
    assert f'admin_username = "{names.ADMIN_USER}"' in text
    assert '"${local.name}-redshift"' in text
    assert names.redshift_role_name("dev") == "zaki-multitenant-warehouse-dev-redshift"
    assert names.glue_database("prod") == "mtw_prod"
    assert names.namespace_name("dev") == names.workgroup_name("dev") == "zaki-multitenant-warehouse-dev"


def test_cluster_name_and_database_match():
    text = read("modules", "provisioned_wlm", "main.tf")
    assert 'name = "zaki-multitenant-warehouse-${var.environment}-wlm"' in text
    assert names.cluster_identifier("dev") == "zaki-multitenant-warehouse-dev-wlm"
    assert f'database_name      = "{names.DATABASE}"' in text and f'master_username    = "{names.ADMIN_USER}"' in text
    assert "ignore_changes = [parameter]" in text  # the WLM json is changed by the CLI, not by Terraform


def test_serverless_workgroup_leaves_queue_settings_to_the_cli():
    assert "ignore_changes = [config_parameter]" in read("modules", "warehouse", "main.tf")
    assert queues.WLM_KEY == "wlm_json_configuration"


def test_both_environments_are_identical_apart_from_their_name_and_the_test_flag():
    def normalise(text):
        return "\n".join(line for line in text.splitlines() if "enable_provisioned_wlm_test =" not in line)

    dev, prod = read("envs", "dev", "main.tf"), read("envs", "prod", "main.tf")
    assert normalise(dev).replace('"dev"', '"prod"') == normalise(prod)
    assert "enable_provisioned_wlm_test = false" in prod  # the expensive cluster is never left on in prod
    for env in ("dev", "prod"):
        assert f'key          = "envs/{env}/terraform.tfstate"' in read("envs", env, "backend.tf")


def test_ci_role_only_reaches_this_projects_resources():
    text = read("bootstrap", "main.tf")
    assert 'project    = "zaki-multitenant-warehouse"' in text
    assert "mtw_*" in text and "${local.project}-dev-*" in text
    assert not re.search(r"\b\d{12}\b", "".join(p.read_text() for p in ROOT.rglob("*.tf"))), "no account IDs in committed files"
    assert "AdministratorAccess" not in text


def test_nothing_secret_is_committed():
    for path in ROOT.rglob("*"):
        assert path.name not in ("backend.hcl", "terraform.tfvars")
        assert not path.name.endswith(".tfstate")