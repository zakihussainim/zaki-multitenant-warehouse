output "cluster_identifier" {
  value = aws_redshift_cluster.this.cluster_identifier
}

output "parameter_group_name" {
  value = aws_redshift_parameter_group.this.name
}
