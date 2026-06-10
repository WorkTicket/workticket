# Audit Trail Examples

This document provides concrete examples of audit trail entries in the WorkTicket system.

## Authentication Flow

```
[2026-06-10T08:00:00Z] auth.login    | tenant=acme-corp | actor=user:john@acme.com | status=success
[2026-06-10T08:00:01Z] auth.token_refresh | tenant=acme-corp | actor=user:john@acme.com | status=success
[2026-06-10T12:00:00Z] auth.logout   | tenant=acme-corp | actor=user:john@acme.com | status=success
```

## Job Lifecycle

```
[2026-06-10T09:00:00Z] job.create     | tenant=acme-corp | actor=user:john@acme.com | resource=job:abc123 | status=pending
[2026-06-10T09:05:00Z] job.status_change | tenant=acme-corp | actor=system:celery | resource=job:abc123 | before=pending → after=in_progress
[2026-06-10T09:10:00Z] ai.process     | tenant=acme-corp | actor=system:celery | resource=job:abc123 | model=llama3.2 | tokens=450
[2026-06-10T09:10:05Z] ai.output      | tenant=acme-corp | actor=system:celery | resource=job:abc123 | output_size=2048 | duration_ms=5000
[2026-06-10T09:10:10Z] job.status_change | tenant=acme-corp | actor=system:celery | resource=job:abc123 | before=in_progress → after=completed
```

## Billing Flow

```
[2026-06-10T01:00:00Z] billing.invoice_create | tenant=acme-corp | actor=system:scheduler | resource=invoice:inv-456 | amount=$99.00 | period=2026-06
[2026-06-10T01:05:00Z] billing.payment | tenant=acme-corp | actor=system:stripe | resource=invoice:inv-456 | amount=$99.00 | payment_id=pi_123
```

## Security Events

```
[2026-06-10T03:00:00Z] security.access_denied | tenant=ext-corp | actor=user:evil@hack.com | resource=/api/jobs | ip=203.0.113.1 | reason=invalid_token
[2026-06-10T03:01:00Z] security.access_denied | tenant=ext-corp | actor=user:evil@hack.com | resource=/api/jobs | ip=203.0.113.1 | reason=rate_limit
[2026-06-10T04:00:00Z] admin.secret_rotation | tenant=system | actor=system:cron | resource=stripe_webhook_key | version=v4
```

## Signature Chain Verification

Each entry in the audit trail is cryptographically linked:

```
Entry 1: signature_1 = HMAC-SHA256(key, entry_1_data)
Entry 2: signature_2 = HMAC-SHA256(key, entry_2_data || signature_1)
Entry 3: signature_3 = HMAC-SHA256(key, entry_3_data || signature_2)
```

Tampering with any entry invalidates all subsequent signatures. To verify:

```python
import hmac
import hashlib

def verify_chain(entries, key):
    prev_sig = ""
    for entry in entries:
        expected = hmac.new(key, entry.data + prev_sig, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, entry.signature):
            raise TamperDetected(f"Entry {entry.id} has been tampered!")
        prev_sig = expected
    return True
```
