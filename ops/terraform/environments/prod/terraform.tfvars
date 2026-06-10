aws_region = "us-east-1"

vpc_cidr        = "10.0.0.0/16"
private_subnets  = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
public_subnets   = ["10.0.101.0/24", "10.0.102.0/24", "10.0.103.0/24"]
database_subnets = ["10.0.201.0/24", "10.0.202.0/24", "10.0.203.0/24"]

rds_instance_class    = "db.r6g.large"
rds_allocated_storage = 200

redis_node_type          = "cache.r6g.large"
redis_broker_node_count  = 3
redis_cache_node_count   = 3

eks_desired_capacity = 5
eks_instance_types   = ["m6i.large", "m6a.large"]
