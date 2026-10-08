output "data_bucket_name" {
  value = module.storage.bucket_name
}

output "workgroup_name" {
  value = module.warehouse.workgroup_name
}

output "namespace_name" {
  value = module.warehouse.namespace_name
}

output "glue_database_name" {
  value = module.warehouse.glue_database_name
}

output "cluster_identifier" {
  value = one(module.provisioned_wlm[*].cluster_identifier)
}
