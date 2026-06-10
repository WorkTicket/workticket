# Compliance Mapping — SOC 2 / ISO 27001:2022

See [AUDIT_LOG_SCHEMA.md](./AUDIT_LOG_SCHEMA.md) for the audit log schema and detailed compliance mapping.

## Summary of Controls

### Access Control
- **Authentication**: Clerk JWT with short-lived tokens + refresh rotation
- **Authorization**: Role-Based Access Control (RBAC) with tenant scoping
- **Data isolation**: PostgreSQL Row-Level Security (RLS) per tenant
- **API key management**: HMAC-signed, auto-rotated

### Data Protection
- **Encryption at rest**: AES-256-GCM for PII fields (field-level encryption)
- **Encryption in transit**: TLS 1.2+ with strong ciphers + custom DH params (4096-bit)
- **Key management**: External secrets via AWS Secrets Manager, HashiCorp Vault
- **Secret rotation**: Automated via ops/scripts/rotate-secrets.sh

### Supply Chain
- **SBOM**: CycloneDX + SPDX for all components (backend, dashboard, mobile, whisper)
- **Image signing**: Cosign keyless signing with OIDC
- **SLSA provenance**: Level 2 build provenance attestations
- **Container scanning**: Trivy on every PR and weekly schedule

### Monitoring & Incident Response
- **Observability**: OpenTelemetry distributed tracing + Prometheus metrics + structured logging
- **Alerting**: Prometheus AlertManager with SLO-based alerts
- **Incident response**: 23 operational runbooks covering all failure scenarios
- **Chaos engineering**: Weekly chaos test suite with 25+ failure injection scenarios

### Development Security
- **SAST**: Bandit (Python) + Semgrep (Python + TypeScript) on every PR
- **Secret scanning**: Gitleaks on every push
- **Dependency audit**: pip-audit + npm audit on every PR and weekly
- **Mutation testing**: mutmut with 70% score threshold
- **Fuzz testing**: SQL injection fuzzing + prompt injection adversarial testing
