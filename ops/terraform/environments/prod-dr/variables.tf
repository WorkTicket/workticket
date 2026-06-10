variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-west-2"
}

variable "vpc_cidr" {
  description = "VPC CIDR block"
  type        = string
  default     = "10.2.0.0/16"
}

variable "private_subnets" {
  description = "Private subnet CIDRs"
  type        = list(string)
  default     = ["10.2.1.0/24", "10.2.2.0/24"]
}

variable "public_subnets" {
  description = "Public subnet CIDRs"
  type        = list(string)
  default     = ["10.2.101.0/24", "10.2.102.0/24"]
}

variable "database_subnets" {
  description = "Database subnet CIDRs"
  type        = list(string)
  default     = ["10.2.201.0/24", "10.2.202.0/24"]
}
