# WorkTicket Rightsizing Guide

## Current Resource Requests/Limits

| Service | Request CPU | Request Mem | Limit CPU | Limit Mem |
|---------|-----------|------------|----------|----------|
| Backend API | 500m | 512Mi | 2 | 1Gi |
| Celery worker (text) | 1 | 1Gi | 2 | 2Gi |
| Celery worker (image) | 1 | 1Gi | 2 | 2Gi |
| Celery worker (audio) | 1 | 1Gi | 2 | 2Gi |
| Celery worker (default) | 500m | 512Mi | 1 | 1Gi |
| PgBouncer | 100m | 128Mi | 500m | 256Mi |

## RightSizing Methodology

### 1. VPA Recommendations (non-intrusive)
Use VPA in "Off" mode for 2 weeks to collect recommendations:
```bash
kubectl apply -f ops/k8s/vpa/backend-vpa.yaml
```
Then review with:
```bash
kubectl get vpa workticket-api-vpa -o jsonpath='{.status.recommendation}'
```

### 2. Target Utilization
- CPU target: 60-80% average
- Memory target: 70-85% average
- If below target for 7 days: reduce requests by 15%
- If above target for 7 days: increase requests by 25%

### 3. Optimization Rules

**Backend API:**
- CPU is usually the bottleneck (concurrent requests)
- Memory grows with active connections pool
- If memory < 60% for 7d, reduce to 384Mi

**Celery Workers:**
- AI text: CPU-bound (LLM inference). Target CPU high.
- AI image: CPU+Memory (vision model). Monitor memory.
- AI audio: Memory-bound (Whisper model loads into memory).
- Default: I/O bound (DB queries, API calls). Low CPU.

**PgBouncer:**
- Low CPU, moderate memory
- Memory grows with max_client_conn
- Scale based on connection count: 100 conns ~ 50MB

### 4. Idle Resource Detection
- CronJob that queries Prometheus for idle resources
- Flags deployments with CPU < 10% for > 7 days
- Automatically reduces requests for flagged deployments

### 5. Dev/Staging Optimization
- Non-production clusters run on smaller instances (m6i.large vs r6g.large)
- Staging RDS: db.r6g.large, single-AZ, 7-day retention
- Dev clusters: T3 instances, scheduled shutdown on weekends
- Estimated dev/staging savings: ~$1,200/month

### 6. Monitoring
Grafana dashboards for resource utilization:
- Cluster: `ops/grafana-dashboards/workticket-overview.json`
- Per-service: Resource usage vs requests/limits
- Cost: Kubecost dashboard at cost.workticket.app
