variable "environment" {
  description = "dev or prod"
  type        = string
}

variable "redshift_role_arn" {
  description = "Role the cluster uses to reach S3 and Glue (the same role the serverless namespace uses)"
  type        = string
}

variable "subnet_ids" {
  description = "Subnets the cluster may be placed in"
  type        = list(string)
}

variable "security_group_id" {
  description = "Security group for the cluster (no inbound access is needed; SQL goes through the Data API)"
  type        = string
}

variable "node_type" {
  description = "Cluster node type. If ra3.large is not offered in the region, use ra3.xlplus."
  type        = string
  default     = "ra3.large"
}
