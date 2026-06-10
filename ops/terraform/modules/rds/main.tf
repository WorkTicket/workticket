variable "environment" { type = string }
variable "vpc_id" { type = string }
variable "database_subnet_ids" { type = list(string) }
variable "private_subnet_cidrs" { type = list(string) }
variable "instance_class" { type = string }
variable "allocated_storage" { type = number }
variable "engine_version" { type = string }
variable "database_name" { type = string }
variable "master_username" { type = string }
variable "deletion_protection" { type = bool, default = true }
variable "multi_az" { type = bool, default = true }
variable "backup_retention_days" { type = number, default = 30 }

resource "aws_db_subnet_group" "this" {
  name       = "workticket-${var.environment}"
  subnet_ids = var.database_subnet_ids
  tags       = { Name = "workticket-${var.environment}-db-subnet-group" }
}

resource "aws_security_group" "rds" {
  name        = "workticket-${var.environment}-rds"
  description = "RDS PostgreSQL access"
  vpc_id      = var.vpc_id

  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = var.private_subnet_cidrs
    description = "PostgreSQL from private subnets"
  }

  ingress {
    from_port   = 6432
    to_port     = 6432
    protocol    = "tcp"
    cidr_blocks = var.private_subnet_cidrs
    description = "PgBouncer from private subnets"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "workticket-${var.environment}-rds-sg" }
}

resource "aws_db_parameter_group" "this" {
  name   = "workticket-${var.environment}-pg16"
  family = "postgres16"

  parameter {
    name  = "shared_preload_libraries"
    value = "pg_stat_statements,auto_explain,pgcrypto"
  }
  parameter {
    name  = "pg_stat_statements.track"
    value = "ALL"
  }
  parameter {
    name  = "auto_explain.log_min_duration"
    value = "1000"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "500"
  }
  parameter {
    name  = "idle_in_transaction_session_timeout"
    value = "30000"
  }
  parameter {
    name  = "statement_timeout"
    value = "30000"
  }
}

resource "aws_db_instance" "primary" {
  identifier = "workticket-${var.environment}"

  engine                      = "postgres"
  engine_version              = var.engine_version
  db_subnet_group_name        = aws_db_subnet_group.this.name
  parameter_group_name        = aws_db_parameter_group.this.name
  vpc_security_group_ids      = [aws_security_group.rds.id]

  instance_class              = var.instance_class
  allocated_storage           = var.allocated_storage
  storage_type                = "gp3"
  storage_encrypted           = true
  iops                        = 12000
  db_name                     = var.database_name
  username                    = var.master_username
  manage_master_user_password = true

  multi_az                     = var.multi_az
  backup_retention_period      = var.backup_retention_days
  backup_window               = "03:00-04:00"
  maintenance_window           = "sun:05:00-sun:06:00"
  copy_tags_to_snapshot        = true
  delete_automated_backups     = false
  deletion_protection          = var.deletion_protection
  skip_final_snapshot          = false
  final_snapshot_identifier    = "workticket-${var.environment}-final"

  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  performance_insights_enabled          = true
  performance_insights_retention_period = 7
  auto_minor_version_upgrade            = true
  monitoring_interval                   = 60
  ca_cert_identifier                    = "rds-ca-rsa2048-g1"

  tags = { Name = "workticket-${var.environment}-postgres" }
}

resource "aws_db_instance" "read_replica" {
  count = var.multi_az ? 1 : 0

  identifier = "workticket-${var.environment}-replica"

  engine                      = "postgres"
  engine_version              = var.engine_version
  db_subnet_group_name        = aws_db_subnet_group.this.name
  parameter_group_name        = aws_db_parameter_group.this.name
  vpc_security_group_ids      = [aws_security_group.rds.id]

  instance_class              = var.instance_class
  allocated_storage           = var.allocated_storage
  storage_type                = "gp3"
  storage_encrypted           = true
  iops                        = 6000

  replicate_source_db         = aws_db_instance.primary.identifier

  backup_retention_period     = 0
  copy_tags_to_snapshot       = true
  deletion_protection         = false
  skip_final_snapshot         = true

  performance_insights_enabled          = true
  performance_insights_retention_period = 7

  tags = { Name = "workticket-${var.environment}-postgres-replica" }
}

resource "random_password" "pgbouncer_auth" {
  length  = 32
  special = false
}

output "primary_endpoint" {
  value = aws_db_instance.primary.endpoint
}

output "primary_arn" {
  value = aws_db_instance.primary.arn
}

output "read_replica_endpoint" {
  value = var.multi_az ? aws_db_instance.read_replica[0].endpoint : null
}

output "security_group_id" {
  value = aws_security_group.rds.id
}

output "master_username" {
  value = var.master_username
}

output "pgbouncer_auth_password" {
  value     = random_password.pgbouncer_auth.result
  sensitive = true
}

output "database_name" {
  value = var.database_name
}
