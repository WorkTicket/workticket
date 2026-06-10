variable "environment" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "desired_capacity" { type = number }
variable "instance_types" { type = list(string) }
variable "rds_security_group_id" { type = string }
variable "redis_security_group_id" { type = string }

resource "aws_eks_cluster" "this" {
  name     = "workticket-${var.environment}"
  role_arn = aws_iam_role.cluster.arn
  version  = "1.31"

  vpc_config {
    subnet_ids              = var.private_subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = false
  }

  encryption_config {
    resources = ["secrets"]
    provider {
      key_arn = aws_kms_key.eks.arn
    }
  }

  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  tags = { Name = "workticket-${var.environment}-eks" }

  depends_on = [
    aws_iam_role_policy_attachment.cluster_policy,
    aws_iam_role_policy_attachment.service_policy,
  ]
}

resource "aws_eks_addon" "coredns" {
  cluster_name  = aws_eks_cluster.this.name
  addon_name    = "coredns"
  addon_version = "v1.11.3-eksbuild.1"
}

resource "aws_eks_addon" "kube_proxy" {
  cluster_name  = aws_eks_cluster.this.name
  addon_name    = "kube-proxy"
  addon_version = "v1.31.0-eksbuild.2"
}

resource "aws_eks_addon" "vpc_cni" {
  cluster_name  = aws_eks_cluster.this.name
  addon_name    = "vpc-cni"
  addon_version = "v1.18.0-eksbuild.1"
}

resource "aws_eks_addon" "ebs_csi" {
  cluster_name  = aws_eks_cluster.this.name
  addon_name    = "aws-ebs-csi-driver"
  addon_version = "v1.35.0-eksbuild.1"
  service_account_role_arn = aws_iam_role.node.arn
}

resource "aws_kms_key" "eks" {
  description             = "EKS secrets encryption"
  deletion_window_in_days = 7
  enable_key_rotation     = true
  tags                    = { Name = "workticket-${var.environment}-eks-kms" }
}

resource "aws_iam_role" "cluster" {
  name = "workticket-${var.environment}-eks-cluster"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster_policy" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_iam_role_policy_attachment" "service_policy" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSServicePolicy"
}

resource "aws_eks_node_group" "ondemand" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "workticket-${var.environment}-ondemand"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.private_subnet_ids
  instance_types  = var.instance_types

  scaling_config {
    desired_size = var.desired_capacity
    min_size     = 3
    max_size     = 15
  }

  update_config {
    max_unavailable = 1
  }

  capacity_type = "ON_DEMAND"

  disk_size = 100

  labels = {
    "workticket.io/node-pool" = "ondemand"
  }

  tags = { Name = "workticket-${var.environment}-eks-ondemand" }
}

resource "aws_eks_node_group" "spot" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "workticket-${var.environment}-spot"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.private_subnet_ids
  instance_types  = ["m6i.large", "m6a.large", "c6i.large"]

  scaling_config {
    desired_size = 2
    min_size     = 1
    max_size     = 20
  }

  update_config {
    max_unavailable = 1
  }

  capacity_type = "SPOT"

  disk_size = 100

  labels = {
    "workticket.io/node-pool" = "spot"
  }

  taint {
    key    = "workticket.io/spot"
    value  = "true"
    effect = "NO_SCHEDULE"
  }

  tags = { Name = "workticket-${var.environment}-eks-spot" }
}

resource "aws_iam_role" "node" {
  name = "workticket-${var.environment}-eks-node"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "node_worker" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
}

resource "aws_iam_role_policy_attachment" "node_cni" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "node_ecr" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_iam_role_policy_attachment" "node_ssm" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_security_group" "cluster_additional" {
  name        = "workticket-${var.environment}-eks-additional"
  description = "Additional EKS cluster SG rules"
  vpc_id      = var.vpc_id

  ingress {
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [var.rds_security_group_id]
    description     = "Allow RDS to reach EKS API (notifications)"
  }

  egress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    security_groups = [var.rds_security_group_id]
    description = "EKS to RDS PostgreSQL"
  }

  egress {
    from_port   = 6379
    to_port     = 6379
    protocol    = "tcp"
    security_groups = [var.redis_security_group_id]
    description = "EKS to ElastiCache Redis"
  }

  tags = { Name = "workticket-${var.environment}-eks-additional-sg" }
}

output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "cluster_endpoint" {
  value = aws_eks_cluster.this.endpoint
}

output "cluster_ca_certificate" {
  value = aws_eks_cluster.this.certificate_authority[0].data
}

resource "aws_iam_openid_connect_provider" "this" {
  url             = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks.certificates[0].sha1_fingerprint]
}

data "tls_certificate" "eks" {
  url = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

output "oidc_provider_arn" {
  value = aws_iam_openid_connect_provider.this.arn
}

output "oidc_provider_url" {
  value = replace(aws_eks_cluster.this.identity[0].oidc[0].issuer, "https://", "")
}

output "node_instance_role_arn" {
  value = aws_iam_role.node.arn
}

output "kms_key_arn" {
  value = aws_kms_key.eks.arn
}
