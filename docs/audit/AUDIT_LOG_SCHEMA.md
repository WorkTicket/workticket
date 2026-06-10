# Audit Log Schema & Compliance Mapping

## 1. Audit Log Schema

### Table: `audit_log`

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID | Primary key |
| `tenant_id` | UUID | Multi-tenant isolation |
| `event_type` | VARCHAR(128) | Event classification |
| `actor_id` | UUID | Who performed the action |
| `actor_type` | VARCHAR(32) | user, system, api_key, webhook |
| `resource_type` | VARCHAR(64) | job, customer, invoice, ai_output, etc. |
| `resource_id` | UUID | Resource identifier |
| `action` | VARCHAR(64) | create, update, delete, read, export |
| `timestamp` | TIMESTAMPTZ | When the event occurred |
| `ip_address` | INET | Source IP |
| `user_agent` | TEXT | Client user agent |
| `changes` | JSONB | Before/after diff of modified fields |
| `metadata` | JSONB | Additional context |
| `signature` | TEXT | HMAC-SHA256 signature for immutability |
| `previous_signature` | TEXT | Chain integrity — links to prior log entry |

### Indices
- `idx_audit_tenant_time` on `(tenant_id, timestamp DESC)`
- `idx_audit_resource` on `(resource_type, resource_id)`
- `idx_audit_event_type` on `(event_type)`
- `idx_audit_actor` on `(actor_id)`

### Event Types
| Event Type | Description | Retention |
|-----------|-------------|-----------|
| `auth.login` | User authentication | 90 days |
| `auth.logout` | User session end | 90 days |
| `auth.token_refresh` | Token refresh | 90 days |
| `job.create` | Job created | 7 years |
| `job.update` | Job modified | 7 years |
| `job.delete` | Job soft-deleted | 7 years |
| `job.status_change` | Job status transition | 7 years |
| `ai.process` | AI processing request | 7 years |
| `ai.output` | AI output generated | 7 years |
| `billing.invoice_create` | Invoice created | 7 years |
| `billing.payment` | Payment processed | 7 years |
| `billing.refund` | Refund issued | 7 years |
| `admin.config_change` | Configuration change | 7 years |
| `admin.secret_rotation` | Secret rotated | 7 years |
| `security.access_denied` | 403/401 events | 1 year |

### Signature Chain
Each audit entry is cryptographically chained:
```
signature_n = HMAC-SHA256(key, entry_n || signature_{n-1})
```
This ensures immutability — any tampered entry breaks the chain.

## 2. Compliance Mapping

### SOC 2 (Trust Services Criteria)

| TSC | Control | Implementation |
|-----|---------|----------------|
| **CC5.2** (Risk Assessment) | Asset inventory, threat model | SBOM via Syft, Trivy scanning, dependency audit |
| **CC6.1** (Logical Access) | Authentication, RBAC | Clerk JWT auth, tenant-scoped RBAC, RLS |
| **CC6.2** (Access Provisioning) | User provisioning | RBAC model, API key management |
| **CC6.6** (External Threats) | Network security, firewalls | WAF, rate limiting, Nginx security headers |
| **CC6.7** (Data Transmission) | Encryption in transit | TLS 1.2+, HSTS, secure ciphers |
| **CC7.2** (Monitoring) | System monitoring | Prometheus, Grafana, Sentry, OTel tracing |
| **CC7.3** (SLA) | Incident response | Runbooks, on-call escalation, incident response doc |
| **CC8.1** (Change Management) | CI/CD pipeline | PR gates, SAST, unit/integration tests, canary deploys |

### ISO 27001:2022 Controls

| Control | Title | Implementation |
|---------|-------|----------------|
| **A.5.1** | Policies for Information Security | SECURITY.md, CODE_OF_CONDUCT.md, security.txt |
| **A.5.8** | Information Security in Project Management | SDLC with security gates (SAST, DAST, dependency audit) |
| **A.5.15** | Access Control | RBAC, tenant isolation, RLS |
| **A.5.17** | Authentication Information | Clerk JWT, HMAC-signed Celery tasks |
| **A.5.24** | Incident Management Planning | Runbooks (44 total), incident response playbook |
| **A.5.33** | Protection of Records | Immutable audit logging with cryptographic chain |
| **A.8.2** | Privileged Access Rights | RBAC model, principle of least privilege |
| **A.8.3** | Information Access Restriction | Tenant isolation, RLS, soft delete |
| **A.8.5** | Secure Authentication | JWT, HMAC signing, API key rotation |
| **A.8.8** | Vulnerability Management | Bandit, Semgrep, Trivy, Gitleaks, Dependabot |
| **A.8.9** | Configuration Management | IaC (Terraform), GitOps (ArgoCD), immutable infra |
| **A.8.10** | Information Deletion | Soft delete with purge policy, PII encryption |
| **A.8.12** | Data Leakage Prevention | PII encryption, RLS, SSRF validation, ClamAV |
| **A.8.16** | Monitoring Activities | OTel, Prometheus, Sentry, structured logging |
| **A.8.21** | Network Security | Internal networks, rate limiting, WAF |
| **A.8.24** | Cryptographic Controls | TLS 1.2+, HMAC, AES-256 for PII, task signing |
| **A.8.25** | Secure Development | CI/CD pipeline with SAST, dependency audit, code review |
| **A.8.28** | Secure Coding | Ruff, mypy, tsc, ESLint, Semgrep, Bandit |

## 3. Audit Trail Example

```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "tenant_id": "550e8400-e29b-41d4-a716-446655440000",
  "event_type": "job.status_change",
  "actor_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "actor_type": "user",
  "resource_type": "job",
  "resource_id": "d290f1ee-6c54-4b01-90e6-d701748f0851",
  "action": "update",
  "timestamp": "2026-06-10T14:30:00Z",
  "ip_address": "192.0.2.1",
  "user_agent": "Mozilla/5.0 ...",
  "changes": {
    "before": {"status": "pending"},
    "after": {"status": "in_progress"}
  },
  "metadata": {
    "trigger": "user_action",
    "correlation_id": "req-abc-123"
  },
  "signature": "3f8c9d2e1a4b7f6c...",
  "previous_signature": "2d7b5a9f1e8c4d3b..."
}
```

## 4. Retention & Purging

| Data Type | Retention | Justification |
|-----------|-----------|---------------|
| Audit logs | 7 years | Legal/financial compliance |
| Access logs | 90 days | Security monitoring |
| AI processing artifacts | 30 days | Storage optimization |
| PII data | Customer lifecycle + 30 days | GDPR compliance |
| Metrics (raw) | 365 days | Trend analysis |
| Metrics (aggregated) | Indefinite | Capacity planning |
| Backup archives | 30 daily + 12 monthly | Disaster recovery |
