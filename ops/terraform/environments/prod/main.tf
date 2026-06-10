terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
  backend "s3" {
    bucket         = "workticket-terraform-state"
    key            = "prod/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "workticket-terraform-locks"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Environment = "production"
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

  environment     = "prod"
  vpc_cidr        = var.vpc_cidr
  azs             = slice(data.aws_availability_zones.available.names, 0, 3)
  private_subnets = var.private_subnets
  public_subnets  = var.public_subnets
  database_subnets = var.database_subnets
}

module "rds" {
  source = "../../modules/rds"

  environment          = "prod"
  vpc_id               = module.vpc.vpc_id
  database_subnet_ids  = module.vpc.database_subnet_ids
  private_subnet_cidrs = module.vpc.private_subnet_cidrs
  instance_class       = var.rds_instance_class
  allocated_storage    = var.rds_allocated_storage
  engine_version       = "16.3"
  database_name        = "workticket"
  master_username      = "workticket_admin"
  deletion_protection  = true
  multi_az             = true
  backup_retention_days = 30
}

module "elasticache" {
  source = "../../modules/elasticache"

  environment            = "prod"
  vpc_id                 = module.vpc.vpc_id
  private_subnet_ids     = module.vpc.private_subnet_ids
  private_subnet_cidrs   = module.vpc.private_subnet_cidrs
  node_type              = var.redis_node_type
  broker_node_count      = var.redis_broker_node_count
  cache_node_count       = var.redis_cache_node_count
  parameter_group_family = "redis7"
}

module "s3" {
  source = "../../modules/s3"

  environment = "prod"
}

module "eks" {
  source = "../../modules/eks"

  environment        = "prod"
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  desired_capacity   = var.eks_desired_capacity
  instance_types     = var.eks_instance_types
  rds_security_group_id = module.rds.security_group_id
  redis_security_group_id = module.elasticache.security_group_id
}

module "iam" {
  source = "../../modules/iam"

  environment        = "prod"
  oidc_provider_arn  = module.eks.oidc_provider_arn
  oidc_provider_url  = module.eks.oidc_provider_url
  namespace          = "workticket"
  service_accounts = [
    "backend",
    "celery-worker",
    "celery-beat",
  ]
}
