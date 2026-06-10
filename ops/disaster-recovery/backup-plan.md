# WorkTicket Disaster Recovery Plan

## RTO / RPO Targets

| Tier | Scope | RTO | RPO |
|------|-------|-----|-----|
| P0   | API availability | 5 min | 1 min |
| P1   | Full application | 30 min | 5 min |
| P2   | Celery job processing | 1 hour | 15 min |
| P3   | Analytics / reporting | 4 hours | 1 hour |

## Backup Strategy

### PostgreSQL (RDS)
- Automated backups: 30-day retention, daily snapshot
- WAL archiving: Continuous to S3 (via pgBackRest or WAL-G)
- PITR capability: Point-in-time recovery to any second within retention
- Cross-region: Copy final snapshot to DR region (us-west-2)
- Validation: Automated `pg_restore -l` integrity check every backup
- Frequency: Full backup daily, WAL continuous, restore test weekly

### Redis (ElastiCache)
- Automatic backups: Daily with 7-day retention
- Cross-region: Manual snapshot copy for DR

### S3
- Versioning enabled on all buckets
- Cross-region replication for uploads bucket
- Lifecycle: IA after 30 days, Glacier after 90, delete after 365

## Recovery Procedures

### Scenario 1: Single-AZ Outage (us-east-1a failure)
1. RDS Multi-AZ auto-failover: 60-120s
2. ElastiCache auto-failover: 30-60s
3. EKS node group spreads across AZs; unaffected
4. Verify: Check `/readyz`, queue depths, error rates

### Scenario 2: Full Region Outage (us-east-1 down)
1. DNS failover: Update Route53 to point to us-west-2
2. Promote DR RDS: Manual promote of read replica
3. Scale up DR EKS: Increase desired capacity from 2 to 5
4. Verify: Full synthetic monitoring suite in DR region
5. RTO target: 30 minutes

### Scenario 3: Data Corruption
1. Identify corruption scope (table, schema, full DB)
2. PITR restore: Restore to pre-corruption timestamp
3. Validate integrity with `pg_verifybackup`
4. Restart services pointing to restored DB
5. Replay any missed WAL segments if available

## Restore Drills
- Automated restore test runs weekly (Saturday 04:00 UTC)
- Full DR failover exercise: Monthly (first Saturday)
- Validate: SLO attainment, alert fidelity, runbook accuracy

## Cross-Region Replication
```
us-east-1 (Primary)          us-west-2 (DR)
├── RDS Multi-AZ            ├── RDS Single-AZ
├── ElastiCache Multi-AZ    ├── ElastiCache Single-AZ
├── EKS (ondemand + spot)   ├── EKS (ondemand, 2 nodes)
├── S3 buckets              ├── S3 (CRR target)
└── Route53 active          └── Route53 standby (weighted 0)
```

## Key Runbooks
- `ops/runbooks/cross-region-failover.md`
- `ops/runbooks/db-saturation.md`
- `ops/runbooks/full-outage.md`
- `ops/runbooks/rto-rpo.md`
