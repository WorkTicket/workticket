# Disaster Recovery Plan

## Recovery Objectives

| Metric | Private Beta Target | Production Target | Measurement |
|--------|-------------------|-------------------|-------------|
| **RTO** (Recovery Time Objective) | 4 hours | 1 hour | Time from incident declaration to full service restoration |
| **RPO** (Recovery Point Objective) | 1 hour | 15 minutes | Maximum acceptable data loss (time since last valid backup) |

## Recovery Strategy

### Database Recovery (PostgreSQL)

**Backup Strategy**:
- Full daily backups at 02:00 UTC to R2/S3
- Continuous WAL archiving with 5-minute `archive_timeout`
- Retention: 30 days for daily backups, 7 days for WAL archives

**Recovery Procedure**:
1. Stop write traffic to primary database
2. Identify latest valid backup (`aws s3 ls s3://workticket-backups/postgres/`)
3. Restore base backup: `pg_restore --dbname=$RESTORE_TARGET latest_backup.dump`
4. Replay WAL segments from archive to point of failure
5. Verify data integrity: `pg_restore --list latest_backup.dump | wc -l`
6. Resume write traffic gradually

**Validation**:
- Automated restore test runs daily (03:00 UTC)
- Manual restore drill monthly (first Monday)
- Integrity check on every backup

### Redis Recovery

**Backup Strategy**:
- Redis Broker: AOF persistence with `appendfsync everysec`
- Redis Cache: RDB snapshots every 15 minutes

**Recovery Procedure**:
1. Restart Redis with AOF/RDB files
2. Verify key count: `redis-cli DBSIZE`
3. Verify queue depth: `redis-cli LLEN celery`
4. Monitor for 5 minutes before declaring healthy

**Without Persistence** (worst case):
- Celery tasks in queue are lost (acceptable — tasks are idempotent)
- Cache misses hit database (acceptable — transient performance impact)
- Session blacklist is rebuilt from database

### Celery Workers Recovery

**Recovery Procedure**:
1. Restart Celery workers: `docker compose up -d celery-worker-*`
2. Verify worker registration: `celery -A app inspect active_queues`
3. Check dead letter queue for lost tasks
4. Replay dead letter queue: `python manage.py replay_dlq`
5. Monitor task processing rate for 10 minutes

### AI Service Recovery

**Degraded Mode**:
- AI circuit breaker opens automatically after 3 consecutive failures
- `AI_ENABLED` feature flag can disable AI globally
- Fallback response: "AI unavailable — fill manually"

**Recovery Procedure**:
1. Identify AI service status: `GET /api/v1/healthz`
2. Check circuit breaker state in Prometheus
3. Restart AI-related services if needed
4. Monitor AI success rate for 15 minutes
5. Close circuit breaker manually if needed

### Stripe Integration Recovery

**Outage Impact**:
- Payment processing fails → subscriptions and one-time charges affected
- Webhook processing stops → delayed subscription updates

**Recovery Procedure**:
1. Verify Stripe API status: https://status.stripe.com
2. Check webhook signature verification: `GET /api/v1/healthz`
3. Replay missed webhooks from Stripe Dashboard
4. Run manual reconciliation for affected period
5. Verify billing account states

### Full Stack Recovery

For complete infrastructure failure, follow the recovery sequence:

1. **Database** (30 min) — Restore PostgreSQL from backup
2. **Redis** (5 min) — Start Redis instances with persistence
3. **Backend API** (10 min) — Deploy backend containers
4. **Celery Workers** (5 min) — Start worker containers
5. **Nginx** (5 min) — Start reverse proxy
6. **Verification** (5 min) — Run health check suite
7. **Total estimated RTO**: 60 minutes

## Backup Validation Script

```bash
#!/bin/bash
# evidence/restore_test.sh — runs monthly
set -e

BACKUP_BUCKET="s3://workticket-backups/postgres/"
LATEST_BACKUP=$(aws s3 ls "$BACKUP_BUCKET" | sort | tail -1 | awk '{print $4}')
TEST_DB="workticket_restore_test_$(date +%Y%m%d)"

echo "Testing restore from: $LATEST_BACKUP"
aws s3 cp "${BACKUP_BUCKET}${LATEST_BACKUP}" /tmp/latest_backup.dump

createdb "$TEST_DB" || true
pg_restore --dbname="$TEST_DB" /tmp/latest_backup.dump

# Verify table count
TABLE_COUNT=$(psql -d "$TEST_DB" -t -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
echo "Tables restored: $TABLE_COUNT"

# Verify row count
JOB_COUNT=$(psql -d "$TEST_DB" -t -c "SELECT count(*) FROM jobs")
echo "Jobs restored: $JOB_COUNT"

# Cleanup
dropdb "$TEST_DB"
rm /tmp/latest_backup.dump

echo "RESTORE TEST PASSED"
exit 0
```

## Secret Recovery

- All secrets stored in HashiCorp Vault (see `app/secrets/`)
- Vault is backed up with Raft snapshot every hour
- Secret rotation documented in `ops/runbooks/` (key rotation grace period: 5 min)
- Recovery: restore Vault snapshot → verify secrets → restart services

## Cross-Region DR

For production, maintain a warm standby in a secondary region:

| Component | Primary Region | Secondary Region | Sync Method |
|-----------|---------------|-----------------|-------------|
| PostgreSQL | us-east-1 | us-west-2 | Streaming replication (async) |
| R2/S3 | Auto-replicated | Auto-replicated | CloudFlare/AWS managed |
| Redis | us-east-1 | us-west-2 | Manual restore from backup |
| Container Images | GHCR | GHCR | GitHub-managed |

## DR Testing Schedule

| Test | Frequency | Duration | Owner |
|------|----------|----------|-------|
| Backup restore validation | Daily (automated) | 10 min | CI pipeline |
| Restore drill (manual) | Monthly | 1 hour | DevOps |
| Full DR simulation | Quarterly | 4 hours | DevOps + Engineering |
| Cross-region failover | Annually | 8 hours | DevOps + SRE |

## Escalation

1. **RPO breach** (backup > 10 min stale): Slack #ops-alerts → PagerDuty
2. **RTO approaching** (service down > 30 min): PagerDuty + manager
3. **RTO exceeded** (service down > 1 hr): Incident commander + status page
4. **Cross-region DR activation**: CTO + VP Engineering approval required
