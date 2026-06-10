variable "environment" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "private_subnet_cidrs" { type = list(string) }
variable "node_type" { type = string }
variable "parameter_group_family" { type = string }
variable "broker_node_count" {
  type    = number
  default = 3
}
variable "cache_node_count" {
  type    = number
  default = 3
}

resource "aws_elasticache_subnet_group" "this" {
  name       = "workticket-${var.environment}-redis"
  subnet_ids = var.private_subnet_ids
  tags       = { Name = "workticket-${var.environment}-redis-subnet-group" }
}

resource "aws_security_group" "redis" {
  name        = "workticket-${var.environment}-redis"
  description = "Redis ElastiCache access"
  vpc_id      = var.vpc_id

  ingress {
    from_port   = 6379
    to_port     = 6379
    protocol    = "tcp"
    cidr_blocks = var.private_subnet_cidrs
    description = "Redis from private subnets"
  }

  ingress {
    from_port   = 6380
    to_port     = 6380
    protocol    = "tcp"
    cidr_blocks = var.private_subnet_cidrs
    description = "Redis TLS from private subnets"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "workticket-${var.environment}-redis-sg" }
}

resource "aws_elasticache_parameter_group" "broker" {
  name   = "workticket-${var.environment}-redis-broker"
  family = var.parameter_group_family

  parameter {
    name  = "maxmemory-policy"
    value = "noeviction"
  }
  parameter {
    name  = "timeout"
    value = "300"
  }
  parameter {
    name  = "tcp-keepalive"
    value = "300"
  }
}

resource "aws_elasticache_parameter_group" "cache" {
  name   = "workticket-${var.environment}-redis-cache"
  family = var.parameter_group_family

  parameter {
    name  = "maxmemory-policy"
    value = "allkeys-lru"
  }
  parameter {
    name  = "timeout"
    value = "300"
  }
}

resource "random_password" "redis_auth" {
  length  = 32
  special = false
}

resource "aws_elasticache_replication_group" "broker" {
  replication_group_id          = "workticket-${var.environment}-broker"
  description                   = "WorkTicket Celery broker Redis"
  node_type                     = var.node_type
  num_cache_clusters            = var.broker_node_count
  port                          = 6379
  parameter_group_name          = aws_elasticache_parameter_group.broker.name
  subnet_group_name             = aws_elasticache_subnet_group.this.name
  security_group_ids            = [aws_security_group.redis.id]
  automatic_failover_enabled    = true
  multi_az_enabled              = var.broker_node_count >= 3
  transit_encryption_enabled    = true
  at_rest_encryption_enabled    = true
  auto_minor_version_upgrade    = true
  data_tiering_enabled          = false
  auth_token                    = random_password.redis_auth.result

  tags = { Name = "workticket-${var.environment}-redis-broker", Role = "broker" }
}

resource "aws_elasticache_replication_group" "cache" {
  replication_group_id          = "workticket-${var.environment}-cache"
  description                   = "WorkTicket app cache Redis"
  node_type                     = var.node_type
  num_cache_clusters            = var.cache_node_count
  port                          = 6379
  parameter_group_name          = aws_elasticache_parameter_group.cache.name
  subnet_group_name             = aws_elasticache_subnet_group.this.name
  security_group_ids            = [aws_security_group.redis.id]
  automatic_failover_enabled    = true
  multi_az_enabled              = var.cache_node_count >= 3
  transit_encryption_enabled    = true
  at_rest_encryption_enabled    = true
  auto_minor_version_upgrade    = true
  data_tiering_enabled          = false
  auth_token                    = random_password.redis_auth.result

  tags = { Name = "workticket-${var.environment}-redis-cache", Role = "cache" }
}

output "broker_primary_endpoint" {
  value = aws_elasticache_replication_group.broker.primary_endpoint_address
}

output "broker_reader_endpoint" {
  value = aws_elasticache_replication_group.broker.reader_endpoint_address
}

output "cache_primary_endpoint" {
  value = aws_elasticache_replication_group.cache.primary_endpoint_address
}

output "cache_reader_endpoint" {
  value = aws_elasticache_replication_group.cache.reader_endpoint_address
}

output "auth_token" {
  value     = random_password.redis_auth.result
  sensitive = true
}

output "security_group_id" {
  value = aws_security_group.redis.id
}

output "port" {
  value = 6379
}

output "tls_port" {
  value = 6380
}
