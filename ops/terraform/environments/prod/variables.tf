variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "vpc_cidr" {
  description = "VPC CIDR block"
  type        = string
}

variable "private_subnets" {
  description = "Private subnet CIDRs"
  type        = list(string)
}

variable "public_subnets" {
  description = "Public subnet CIDRs"
  type        = list(string)
}

variable "database_subnets" {
  description = "Database subnet CIDRs"
  type        = list(string)
}

variable "rds_instance_class" {
  description = "RDS instance class"
  type        = string
}

variable "rds_allocated_storage" {
  description = "RDS allocated storage in GB"
  type        = number
}

variable "redis_node_type" {
  description = "ElastiCache node type"
  type        = string
}

variable "redis_broker_node_count" {
  description = "ElastiCache broker cluster size"
  type        = number
}

variable "redis_cache_node_count" {
  description = "ElastiCache cache cluster size"
  type        = number
}

variable "eks_desired_capacity" {
  description = "EKS managed node group desired capacity"
  type        = number
}

variable "eks_instance_types" {
  description = "EKS node instance types"
  type        = list(string)
}
