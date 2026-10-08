variable "environment" {
  description = "dev or prod"
  type        = string
}

variable "bucket_arn" {
  description = "ARN of the data bucket the warehouse may read and write"
  type        = string
}

variable "base_capacity" {
  description = "Redshift Serverless base capacity in RPUs (the smallest allowed is 8). Lower base means lower cost, higher means faster queries."
  type        = number
  default     = 8
}

variable "max_capacity" {
  description = "The most RPUs the workgroup may scale up to. A ceiling on how much one burst can cost per hour."
  type        = number
  default     = 32
}

variable "usage_limit_rpu_hours" {
  description = "Monthly compute budget in RPU-hours. When it is used up the workgroup stops accepting queries (breach action: deactivate)."
  type        = number
  default     = 150
}
