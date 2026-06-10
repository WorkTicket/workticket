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
    key            = "staging/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "workticket-terraform-locks"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Environment = "staging"
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

  environment      = "staging"
  vpc_cidr         = var.vpc_cidr
  azs              = slice(data.aws_availability_zones.available.names, 0, 2)
  private_subnets  = var.private_subnets
  public_subnets   = var.public_subnets
  database_subnets = var.database_subnets
}

module "rds" {
  source = "../../modules/rds"

  environment          = "staging"
  vpc_id               = module.vpc.vpc_id
  database_subnet_ids  = module.vpc.database_subnet_ids
  private_subnet_cidrs = module.vpc.private_subnet_cidrs
  instance_class       = "db.r6g.large"
  allocated_storage    = 100
  engine_version       = "16.3"
  database_name        = "workticket_staging"
  master_username      = "workticket_admin"
  deletion_protection  = false
  multi_az             = false
  backup_retention_days = 7
}

module "elasticache" {
  source = "../../modules/elasticache"

  environment          = "staging"
  vpc_id               = module.vpc.vpc_id
  private_subnet_ids   = module.vpc.private_subnet_ids
  private_subnet_cidrs = module.vpc.private_subnet_cidrs
  node_type              = "cache.r6g.large"
  broker_node_count      = 1
  cache_node_count       = 1
  parameter_group_family = "redis7"
}

module "s3" {
  source = "../../modules/s3"
  environment = "staging"
}

module "eks" {
  source = "../../modules/eks"

  environment        = "staging"
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  desired_capacity   = 2
  instance_types     = ["m6i.large"]
  rds_security_group_id = module.rds.security_group_id
  redis_security_group_id = module.elasticache.security_group_id
}

module "iam" {
  source = "../../modules/iam"

  environment        = "staging"
  oidc_provider_arn  = module.eks.oidc_provider_arn
  oidc_provider_url  = module.eks.oidc_provider_url
  namespace          = "workticket-staging"
  service_accounts = ["backend", "celery-worker", "celery-beat"]
}
