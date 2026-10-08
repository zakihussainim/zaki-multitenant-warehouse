terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.60, < 6.0"
    }
  }
}

provider "aws" {
  region = "eu-west-2"
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = "eu-west-2"
  project    = "zaki-multitenant-warehouse"

  # The token GitHub issues for a workflow run names the owner and repository by name AND numeric ID.
  github_subject = "repo:${var.github_owner}@${var.github_owner_id}/${local.project}@${var.github_repo_id}:*"

  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn

  # Data buckets are named <project>-<env>-data-<account>. The pattern deliberately
  # does NOT match the state bucket (<project>-tfstate-<account>).
  env_bucket_arns = [
    "arn:aws:s3:::${local.project}-dev-*",
    "arn:aws:s3:::${local.project}-dev-*/*",
    "arn:aws:s3:::${local.project}-prod-*",
    "arn:aws:s3:::${local.project}-prod-*/*",
  ]

  # Roles created by the environments. Does NOT match the CI role itself
  # (<project>-github-actions-deploy), so CI cannot edit its own permissions.
  env_role_arns = [
    "arn:aws:iam::${local.account_id}:role/${local.project}-dev-*",
    "arn:aws:iam::${local.account_id}:role/${local.project}-prod-*",
  ]

  glue_arns = [
    "arn:aws:glue:${local.region}:${local.account_id}:catalog",
    "arn:aws:glue:${local.region}:${local.account_id}:database/mtw_*",
    "arn:aws:glue:${local.region}:${local.account_id}:table/mtw_*/*",
  ]

  # Serverless namespaces and workgroups are identified by generated IDs, so they cannot be matched by name.
  serverless_arns = [
    "arn:aws:redshift-serverless:${local.region}:${local.account_id}:namespace/*",
    "arn:aws:redshift-serverless:${local.region}:${local.account_id}:workgroup/*",
    "arn:aws:redshift-serverless:${local.region}:${local.account_id}:snapshot/*",
    "arn:aws:redshift-serverless:${local.region}:${local.account_id}:recoverypoint/*",
  ]

  # Provisioned clusters, parameter groups and subnet groups are identified by name.
  provisioned_arns = [
    "arn:aws:redshift:${local.region}:${local.account_id}:cluster:${local.project}-*",
    "arn:aws:redshift:${local.region}:${local.account_id}:parametergroup:${local.project}-*",
    "arn:aws:redshift:${local.region}:${local.account_id}:subnetgroup:${local.project}-*",
  ]
}

# ---------------------------------------------------------------------------
# S3 bucket that holds Terraform state
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "tf_state" {
  bucket = "${local.project}-tfstate-${local.account_id}"
}

resource "aws_s3_bucket_versioning" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------
# GitHub Actions -> AWS (OIDC)
# ---------------------------------------------------------------------------

# There is only one GitHub OIDC provider per AWS account. If another project already created it,
# this project looks it up. Set create_oidc_provider = true in a fresh account.
data "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 0 : 1
  url   = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "github_actions_assume_role" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.github_subject]
    }
  }
}

resource "aws_iam_role" "github_actions_deploy" {
  name               = "${local.project}-github-actions-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume_role.json
}

# ---------------------------------------------------------------------------
# What the CI role may do
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "github_actions_permissions" {
  statement {
    sid = "ReadWriteTerraformState"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      aws_s3_bucket.tf_state.arn,
      "${aws_s3_bucket.tf_state.arn}/*",
    ]
  }

  statement {
    sid       = "ManageEnvironmentBuckets"
    actions   = ["s3:*"]
    resources = local.env_bucket_arns
  }

  statement {
    sid       = "ManageGlueDatabases"
    actions   = ["glue:*"]
    resources = local.glue_arns
  }

  statement {
    sid       = "ManageServerlessWarehouses"
    actions   = ["redshift-serverless:*"]
    resources = local.serverless_arns
  }

  statement {
    sid       = "ListServerlessWarehouses"
    actions   = ["redshift-serverless:List*", "redshift-serverless:Get*"]
    resources = ["*"]
  }

  statement {
    sid       = "ManageProvisionedClusters"
    actions   = ["redshift:*"]
    resources = local.provisioned_arns
  }

  statement {
    sid = "ReadRedshiftAndNetwork"
    actions = [
      "redshift:DescribeClusters",
      "redshift:DescribeClusterSubnetGroups",
      "redshift:DescribeClusterParameterGroups",
      "redshift:DescribeClusterParameters",
      "redshift:DescribeTags",
      "redshift:DescribeLoggingStatus",
      "redshift:DescribeClusterSnapshots",
      "redshift:DescribeScheduledActions",
      "redshift:DescribeSnapshotCopyGrants",
      "redshift:DescribeAccountAttributes",
      "redshift:DescribeEventSubscriptions",
      "redshift:GetResourcePolicy",
      "ec2:DescribeVpcs",
      "ec2:DescribeVpcAttribute",
      "ec2:DescribeSubnets",
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeAvailabilityZones",
      "ec2:DescribeAccountAttributes",
      "ec2:DescribeInternetGateways",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DescribeAddresses",
    ]
    resources = ["*"]
  }

  statement {
    sid = "ManageRedshiftManagedPasswordSecrets"
    actions = [
      "secretsmanager:CreateSecret",
      "secretsmanager:DeleteSecret",
      "secretsmanager:DescribeSecret",
      "secretsmanager:TagResource",
      "secretsmanager:UntagResource",
      "secretsmanager:UpdateSecret",
      "secretsmanager:GetResourcePolicy",
      "secretsmanager:PutResourcePolicy",
      "secretsmanager:DeleteResourcePolicy",
      "secretsmanager:RotateSecret",
      "secretsmanager:CancelRotateSecret",
      "secretsmanager:ListSecretVersionIds",
    ]
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:redshift!*"]
  }

  # The managed admin secret is encrypted with the account's Secrets Manager key. Allowed only when used through Secrets Manager.
  statement {
    sid = "UseSecretsManagerKeyForRedshiftSecrets"
    actions = [
      "kms:Decrypt",
      "kms:Encrypt",
      "kms:GenerateDataKey",
      "kms:ReEncrypt*",
      "kms:DescribeKey",
      "kms:CreateGrant",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["secretsmanager.${local.region}.amazonaws.com"]
    }
  }

  statement {
    sid = "ManageEnvironmentRoles"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:GetRole",
      "iam:UpdateRole",
      "iam:UpdateAssumeRolePolicy",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:ListRoleTags",
      "iam:PutRolePolicy",
      "iam:GetRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
    ]
    resources = local.env_role_arns
  }

  statement {
    sid       = "PassEnvironmentRolesToRedshift"
    actions   = ["iam:PassRole"]
    resources = local.env_role_arns

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["redshift.amazonaws.com", "redshift-serverless.amazonaws.com"]
    }
  }

  statement {
    sid       = "CreateRedshiftServiceLinkedRoles"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["arn:aws:iam::${local.account_id}:role/aws-service-role/*"]

    condition {
      test     = "StringLike"
      variable = "iam:AWSServiceName"
      values   = ["redshift.amazonaws.com", "redshift-serverless.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "github_actions_permissions" {
  name   = "${local.project}-github-actions-permissions"
  role   = aws_iam_role.github_actions_deploy.id
  policy = data.aws_iam_policy_document.github_actions_permissions.json
}
