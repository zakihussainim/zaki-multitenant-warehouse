output "namespace_name" {
  value = aws_redshiftserverless_namespace.this.namespace_name
}

output "workgroup_name" {
  value = aws_redshiftserverless_workgroup.this.workgroup_name
}

output "redshift_role_arn" {
  value = aws_iam_role.redshift.arn
}

output "glue_database_name" {
  value = aws_glue_catalog_database.cold.name
}

output "subnet_ids" {
  value = data.aws_subnets.default.ids
}

output "security_group_id" {
  value = data.aws_security_group.default.id
}
