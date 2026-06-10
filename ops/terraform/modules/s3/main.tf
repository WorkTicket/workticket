variable "environment" { type = string }

resource "aws_s3_bucket" "uploads" {
  bucket = "workticket-${var.environment}-uploads"
  tags   = { Name = "workticket-${var.environment}-uploads", Purpose = "user-uploads" }
}

resource "aws_s3_bucket_versioning" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "uploads" {
  bucket                  = aws_s3_bucket.uploads.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "uploads" {
  bucket = aws_s3_bucket.uploads.id
  rule {
    id     = "expire-incomplete-multipart"
    status = "Enabled"
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
  rule {
    id     = "transition-older-versions"
    status = "Enabled"
    noncurrent_version_transition {
      noncurrent_days = 30
      storage_class   = "STANDARD_IA"
    }
    noncurrent_version_transition {
      noncurrent_days = 90
      storage_class   = "GLACIER"
    }
    noncurrent_version_expiration {
      noncurrent_days = 365
    }
  }
}

resource "aws_s3_bucket" "backups" {
  bucket = "workticket-${var.environment}-backups"
  tags   = { Name = "workticket-${var.environment}-backups", Purpose = "db-backups" }
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    id     = "expire-old-backups"
    status = "Enabled"
    expiration {
      days = 365
    }
  }
}

resource "aws_s3_bucket" "artifacts" {
  bucket = "workticket-${var.environment}-artifacts"
  tags   = { Name = "workticket-${var.environment}-artifacts", Purpose = "build-artifacts" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "expire-old-artifacts"
    status = "Enabled"
    expiration {
      days = 90
    }
  }
}

resource "aws_s3_bucket" "loki_chunks" {
  bucket = "workticket-${var.environment}-loki-chunks"
  tags   = { Name = "workticket-${var.environment}-loki-chunks", Purpose = "loki-storage" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "loki_chunks" {
  bucket = aws_s3_bucket.loki_chunks.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "loki_chunks" {
  bucket                  = aws_s3_bucket.loki_chunks.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket" "tempo_traces" {
  bucket = "workticket-${var.environment}-tempo-traces"
  tags   = { Name = "workticket-${var.environment}-tempo-traces", Purpose = "tempo-storage" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tempo_traces" {
  bucket = aws_s3_bucket.tempo_traces.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tempo_traces" {
  bucket                  = aws_s3_bucket.tempo_traces.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

output "uploads_bucket" {
  value = aws_s3_bucket.uploads.id
}

output "backups_bucket" {
  value = aws_s3_bucket.backups.id
}

output "artifacts_bucket" {
  value = aws_s3_bucket.artifacts.id
}

output "loki_chunks_bucket" {
  value = aws_s3_bucket.loki_chunks.id
}

output "tempo_traces_bucket" {
  value = aws_s3_bucket.tempo_traces.id
}
