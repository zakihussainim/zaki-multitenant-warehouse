locals {
  name = "zaki-multitenant-warehouse-${var.environment}-wlm"
}

resource "aws_redshift_subnet_group" "this" {
  name       = local.name
  subnet_ids = var.subnet_ids
}

# The parameter group holds the classic WLM queue configuration. It starts on automatic WLM;
# `python -m mtw queues --target provisioned --mode manual|auto` changes it, then reboots the cluster.
resource "aws_redshift_parameter_group" "this" {
  name   = local.name
  family = "redshift-2.0"

  parameter {
    name  = "wlm_json_configuration"
    value = jsonencode([{ auto_wlm = true }])
  }

  lifecycle {
    ignore_changes = [parameter]
  }
}

# One small node, created only for the classic-WLM comparison and then destroyed.
resource "aws_redshift_cluster" "this" {
  cluster_identifier = local.name
  database_name      = "warehouse"
  master_username    = "admin"

  manage_master_password = true

  node_type       = var.node_type
  cluster_type    = "single-node"
  number_of_nodes = 1

  iam_roles            = [var.redshift_role_arn]
  default_iam_role_arn = var.redshift_role_arn

  cluster_subnet_group_name    = aws_redshift_subnet_group.this.name
  cluster_parameter_group_name = aws_redshift_parameter_group.this.name
  vpc_security_group_ids       = [var.security_group_id]

  publicly_accessible                 = false
  encrypted                           = true
  automated_snapshot_retention_period = 1
  skip_final_snapshot                 = true
}
