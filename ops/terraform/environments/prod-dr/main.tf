terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  backend "s3" {
    bucket         = "workticket-terraform-state"
    key            = "prod-dr/terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "workticket-terraform-locks"
    encrypt        = true
  }
}

provider "aws" {
  region = "us-west-2"
  default_tags {
    tags = {
      Environment = "production-dr"
      Project     = "workticket"
      ManagedBy   = "terraform"
    }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

module "vpc" {
  source = "../../modules/vpc"

  environment      = "prod-dr"
  vpc_cidr         = "10.2.0.0/16"
  azs              = slice(data.aws_availability_zones.available.names, 0, 2)
  private_subnets  = ["10.2.1.0/24", "10.2.2.0/24"]
  public_subnets   = ["10.2.101.0/24", "10.2.102.0/24"]
  database_subnets = ["10.2.201.0/24", "10.2.202.0/24"]
}

module "rds" {
  source = "../../modules/rds"

  environment          = "prod-dr"
  vpc_id               = module.vpc.vpc_id
  database_subnet_ids  = module.vpc.database_subnet_ids
  private_subnet_cidrs = module.vpc.private_subnet_cidrs
  instance_class       = "db.r6g.large"
  allocated_storage    = 200
  engine_version       = "16.3"
  database_name        = "workticket"
  master_username      = "workticket_admin"
  deletion_protection  = true
  multi_az             = false
  backup_retention_days = 30
}

module "elasticache" {
  source = "../../modules/elasticache"

  environment            = "prod-dr"
  vpc_id                 = module.vpc.vpc_id
  private_subnet_ids     = module.vpc.private_subnet_ids
  private_subnet_cidrs   = module.vpc.private_subnet_cidrs
  node_type              = "cache.r6g.large"
  broker_node_count      = 1
  cache_node_count       = 1
  parameter_group_family = "redis7"
}

module "eks" {
  source = "../../modules/eks"

  environment        = "prod-dr"
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  desired_capacity   = 2
  instance_types     = ["m6i.large"]
  rds_security_group_id  = module.rds.security_group_id
  redis_security_group_id = module.elasticache.security_group_id
}

module "s3" {
  source = "../../modules/s3"
  environment = "prod-dr"
}
