# SLO / Service Level Objectives

## Defined SLOs

| Service | SLO Target | Measurement Window | SLI | Error Budget |
|---------|-----------|-------------------|-----|-------------|
| **API Availability** | 99.9% | 30 days | `up{job="api"}` Prometheus metric | 43.2 min/month downtime |
| **API P95 Latency** | < 500ms | 30 days | `http_request_duration_seconds` histogram | Requests exceeding 500ms count against budget |
| **API Error Rate** | < 1.0% | 30 days | `http_requests_total{status=~"5.."}` / total | 1% of all requests |
| **Payment Success Rate** | > 99.95% | 30 days | `stripe_payment_success / stripe_payment_attempts` | 0.05% failure budget |
| **AI Availability** | 99.5% | 30 days | AI circuit breaker state + response success rate | 3.6 hours/month |
| **Webhook Processing** | 99.9% | 30 days | Webhook 200 responses / total webhooks | 43.2 min/month |
| **Celery Task Success** | > 99.5% | 30 days | `celery_task_success / celery_task_total` | 0.5% failure budget |

## SLI Measurement

### API Availability SLI
```
sum(rate(up{job="api"}[5m])) / count(up{job="api"})
```
Target: >= 0.999 (99.9%)

### API P95 Latency SLI
```
histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))
```
Target: < 0.5 (500ms)

### API Error Rate SLI
```
sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m]))
```
Target: < 0.01 (1%)

### Payment Success SLI
```
rate(stripe_payment_success_total[30d]) / rate(stripe_payment_attempt_total[30d])
```
Target: >= 0.9995 (99.95%)

## Error Budget Policy

| Burn Rate | Alert Severity | Action |
|-----------|---------------|--------|
| < 1x | Info | Normal operation |
| 1x - 5x | Warning | Slack #ops-alerts notification |
| 5x - 10x | Critical | PagerDuty page on-call engineer |
| > 10x | Emergency | Incident commander + feature freeze |

**Error Budget Calculation**: `error_budget = (1 - SLO) * total_requests`

**Burn Rate**: `burn_rate = actual_error_rate / budgeted_error_rate`

## Alerting Rules

### Prometheus Alerts

```yaml
groups:
  - name: slo_alerts
    rules:
      - alert: HighErrorRate
        expr: |
          sum(rate(http_requests_total{status=~"5.."}[5m])) 
          / sum(rate(http_requests_total[5m])) > 0.01
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "API error rate exceeds SLO threshold"

      - alert: HighLatency
        expr: |
          histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m])) > 0.5
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "P95 latency exceeds 500ms SLO"

      - alert: ErrorBudgetBurn
        expr: |
          (sum(rate(http_requests_total{status=~"5.."}[1h])) 
          / sum(rate(http_requests_total[1h]))) / 0.01 > 5
        for: 10m
        labels:
          severity: critical
        annotations:
          summary: "Error budget burning at >5x rate"

      - alert: PaymentFailureRate
        expr: |
          rate(stripe_payment_failure_total[5m]) 
          / rate(stripe_payment_attempt_total[5m]) > 0.0005
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "Payment failure rate exceeds SLO"

      - alert: AIUnavailable
        expr: |
          workticket_ai_circuit_breaker_state{state="open"} == 1
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "AI circuit breaker is open"
```

## RTO/RPO Integration

See [ops/runbooks/rto-rpo.md](../ops/runbooks/rto-rpo.md) for recovery objectives:
- **RTO**: 1 hour (time to restore full service)
- **RPO**: 5 minutes (maximum acceptable data loss)

## Monitoring Dashboards

- **SLO Overview**: Grafana dashboard `workticket-slos`
- **Per-Endpoint SLO**: `GET /api/v1/slo/endpoints`
- **Beta Gate Check**: `GET /api/v1/beta-gate`

## Incident Response

See [ops/runbooks/INCIDENT_RESPONSE.md](../ops/runbooks/INCIDENT_RESPONSE.md) for the full incident response procedure:
1. **Detect**: Prometheus Alertmanager → PagerDuty
2. **Acknowledge**: On-call engineer within 5 minutes
3. **Triage**: Assess severity, blast radius, affected SLOs
4. **Mitigate**: Execute relevant runbook from ops/runbooks/
5. **Resolve**: Verify SLO metrics return to green
6. **Post-mortem**: Document timeline, root cause, prevention

## Review Cadence

- **Weekly**: SLO dashboard review in engineering standup
- **Monthly**: Error budget review with product team
- **Quarterly**: SLO target adjustment based on customer feedback and system maturity
