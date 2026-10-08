data "aws_caller_identity" "current" {}

locals {
  account_id    = data.aws_caller_identity.current.account_id
  region        = "eu-west-2"
  name          = "zaki-multitenant-warehouse-${var.environment}"
  glue_database = "mtw_${var.environment}"
}

# ---------------------------------------------------------------------------
# Network: the account's default VPC. The warehouse is not publicly reachable;
# every SQL statement in this project goes through the Data API, not a network connection.
# ---------------------------------------------------------------------------

data "aws_vpc" "default" {
  default = true
}

# Only subnets in availability zones that Redshift supports. In London the default VPC has a subnet in euw2-az4 (eu-west-2d),
# which Redshift Serverless rejects, so zones are chosen by ID.
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }

  filter {
    name   = "availability-zone-id"
    values = var.availability_zone_ids
  }
}

data "aws_security_group" "default" {
  vpc_id = data.aws_vpc.default.id
  name   = "default"
}

# ---------------------------------------------------------------------------
# The role Redshift itself uses to reach S3 (COPY, UNLOAD) and the Glue catalog (Spectrum)
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "redshift_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["redshift.amazonaws.com", "redshift-serverless.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "redshift" {
  name               = "${local.name}-redshift"
  assume_role_policy = data.aws_iam_policy_document.redshift_assume.json
}

data "aws_iam_policy_document" "redshift_access" {
  statement {
    sid = "ReadWriteDataBucket"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:GetBucketLocation",
      "s3:ListBucket",
      "s3:ListBucketMultipartUploads",
      "s3:ListMultipartUploadParts",
      "s3:AbortMultipartUpload",
    ]
    resources = [var.bucket_arn, "${var.bucket_arn}/*"]
  }

  statement {
    sid     = "SpectrumReadsAndWritesTheCatalog"
    actions = ["glue:*"]
    resources = [
      "arn:aws:glue:${local.region}:${local.account_id}:catalog",
      "arn:aws:glue:${local.region}:${local.account_id}:database/${local.glue_database}",
      "arn:aws:glue:${local.region}:${local.account_id}:table/${local.glue_database}/*",
    ]
  }
}

resource "aws_iam_role_policy" "redshift_access" {
  name   = "${local.name}-redshift-access"
  role   = aws_iam_role.redshift.id
  policy = data.aws_iam_policy_document.redshift_access.json
}

# The Glue database that holds the external (cold) table definitions.
resource "aws_glue_catalog_database" "cold" {
  name = local.glue_database
}

# ---------------------------------------------------------------------------
# Redshift Serverless
# ---------------------------------------------------------------------------

resource "aws_redshiftserverless_namespace" "this" {
  namespace_name = local.name
  db_name        = "warehouse"
  admin_username = "admin"

  # Redshift generates the admin password and keeps it in Secrets Manager. It never appears in code or Terraform state output.
  manage_admin_password = true

  default_iam_role_arn = aws_iam_role.redshift.arn
  iam_roles            = [aws_iam_role.redshift.arn]
}

resource "aws_redshiftserverless_workgroup" "this" {
  namespace_name = aws_redshiftserverless_namespace.this.namespace_name
  workgroup_name = local.name

  base_capacity       = var.base_capacity
  max_capacity        = var.max_capacity
  publicly_accessible = false
  subnet_ids          = data.aws_subnets.default.ids
  security_group_ids  = [data.aws_security_group.default.id]

  lifecycle {
    precondition {
      condition     = length(data.aws_subnets.default.ids) >= 2
      error_message = "Redshift Serverless needs subnets in at least 2 supported availability zones. The default VPC has fewer; check var.availability_zone_ids or use a different VPC."
    }

    # The queue (WLM) configuration is applied by `python -m mtw queues`, because AWS does not allow it to be switched off again.
    # Terraform must not try to reset it.
    ignore_changes = [config_parameter]
  }
}

# A hard monthly ceiling on compute spend.
resource "aws_redshiftserverless_usage_limit" "compute" {
  resource_arn  = aws_redshiftserverless_workgroup.this.arn
  usage_type    = "serverless-compute"
  amount        = var.usage_limit_rpu_hours
  period        = "monthly"
  breach_action = "deactivate"
}
