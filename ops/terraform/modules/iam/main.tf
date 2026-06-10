variable "environment" { type = string }
variable "oidc_provider_arn" { type = string }
variable "oidc_provider_url" { type = string }
variable "namespace" { type = string }
variable "service_accounts" { type = list(string) }

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

resource "aws_iam_role" "irsa" {
  for_each = toset(var.service_accounts)

  name = "workticket-${var.environment}-${each.key}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = var.oidc_provider_arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringLike = {
          "${replace(var.oidc_provider_url, "https://", "")}:sub" = "system:serviceaccount:${var.namespace}:${each.key}"
        }
      }
    }]
  })

  tags = { Name = "workticket-${var.environment}-${each.key}", Role = each.key }
}

resource "aws_iam_policy" "backend" {
  name        = "workticket-${var.environment}-backend"
  description = "Permissions for WorkTicket backend service account"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
        ]
        Resource = [
          "arn:aws:s3:::workticket-${var.environment}-uploads/*",
          "arn:aws:s3:::workticket-${var.environment}-uploads",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
        ]
        Resource = ["arn:aws:kms:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:alias/workticket-${var.environment}-*"]
      },
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
        ]
        Resource = ["arn:aws:secretsmanager:*:*:secret:${var.environment}/workticket/*"]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "backend" {
  role       = aws_iam_role.irsa["backend"].name
  policy_arn = aws_iam_policy.backend.arn
}

resource "aws_iam_role_policy_attachment" "backend_ssm" {
  role       = aws_iam_role.irsa["backend"].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_policy" "worker" {
  name        = "workticket-${var.environment}-worker"
  description = "Permissions for WorkTicket Celery worker service accounts"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket",
        ]
        Resource = [
          "arn:aws:s3:::workticket-${var.environment}-uploads/*",
          "arn:aws:s3:::workticket-${var.environment}-uploads",
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
        ]
        Resource = ["arn:aws:secretsmanager:*:*:secret:${var.environment}/workticket/*"]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "worker" {
  for_each = toset([for s in var.service_accounts : s if s != "backend"])
  role     = aws_iam_role.irsa[each.key].name
  policy_arn = aws_iam_policy.worker.arn
}

output "role_arns" {
  value = { for k, r in aws_iam_role.irsa : k => r.arn }
}
