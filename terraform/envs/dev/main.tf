locals {
  environment = "dev"

  # Flip to true and merge to create the single provisioned cluster used for the classic-WLM test.
  # Flip back to false and merge to destroy it. It bills by the hour while it exists (roughly 0.2 pounds an hour).
  enable_provisioned_wlm_test = false
}

module "storage" {
  source      = "../../modules/storage"
  environment = local.environment
}

module "warehouse" {
  source                = "../../modules/warehouse"
  environment           = local.environment
  bucket_arn            = module.storage.bucket_arn
  base_capacity         = 8
  max_capacity          = 32
  usage_limit_rpu_hours = 150
}

module "provisioned_wlm" {
  count             = local.enable_provisioned_wlm_test ? 1 : 0
  source            = "../../modules/provisioned_wlm"
  environment       = local.environment
  redshift_role_arn = module.warehouse.redshift_role_arn
  subnet_ids        = module.warehouse.subnet_ids
  security_group_id = module.warehouse.security_group_id
}
