# Zero-Cost / Low-Cost Production Deployment Options
#
# Since you're not using AWS, here are paths that work with these manifests:

## Option 1: Oracle Cloud Free Tier (recommended, $0/month forever)
# - 4 ARM cores (Ampere A1), 24 GB RAM total across instances
# - 200 GB block storage
# - 10 TB outbound bandwidth
# - Run K3s directly on ARM instances
# - All our K8s manifests work on ARM (multi-arch images needed)
#
# Deploy:
#   curl -sfL https://get.k3s.io | sh -
#   export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
#   kubectl apply -k src/k8s/overlays/prod

## Option 2: Self-Hosted K3s on Any Machine ($0/month)
# - Old PC, Raspberry Pi, or dedicated server
# - These manifests work with zero cloud dependencies
# - Replace ExternalSecrets with:
#     kubectl create secret generic workticket-secrets --from-env-file=.env.prod
# - cert-manager works with Let's Encrypt on any K8s cluster
# - All monitoring (Prometheus/Grafana/Loki/Tempo) runs in-cluster

## Option 3: Hetzner VPS ($4-6/month)
# - 2 vCPU, 4 GB RAM, 40 GB SSD
# - Deploy K3s, run both app + monitoring on same node
# - Perfect for the current docker-compose stack size

## What works without AWS:
# - All K8s Deployments, Services, Ingress — platform-agnostic
# - cert-manager + Let's Encrypt — works anywhere
# - ArgoCD — works on any K8s
# - Prometheus/Grafana/Loki/Tempo — in-cluster, no external deps
# - KEDA — metrics-server works on any K8s
# - Kyverno — admission controller works anywhere
# - NetworkPolicy — needs a CNI that supports it (Calico, Cilium)
# - PgBouncer — connects to any PostgreSQL

## What needs cloud (skip for now):
# - Terraform modules — AWS-only, keep as future reference
# - ExternalSecrets -> AWS Secrets Manager — AWS-only
# - IRSA ServiceAccounts — AWS EKS-only
# - S3 storage for Loki/Tempo — use local PVs instead

## Quick Start (no-cloud path):
#
# 1. On any K8s cluster:
#    kubectl create namespace workticket
#    kubectl create secret generic workticket-secrets --from-env-file=.env
#    kubectl apply -k src/k8s/overlays/prod
#
# 2. Install ArgoCD (optional, for GitOps):
#    kubectl create namespace argocd
#    kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
#
# 3. Install cert-manager:
#    kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.16.0/cert-manager.yaml
#
# 4. Install monitoring (optional):
#    helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
#    helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack -n monitoring --create-namespace
