import pytest

from app.ai.audit import _AUDIT_SIGNING_KEY, _hash_payload, _sign_audit_event


class TestAuditSigning:
    def test_signature_is_deterministic(self):
        sig1 = _sign_audit_event("event-1", "2026-01-01T00:00:00", "ai_gateway", "abc123")
        sig2 = _sign_audit_event("event-1", "2026-01-01T00:00:00", "ai_gateway", "abc123")
        assert sig1 == sig2

    def test_different_payload_different_signature(self):
        if not _AUDIT_SIGNING_KEY:
            pytest.skip("AUDIT_SIGNING_KEY not set")
        sig1 = _sign_audit_event("event-1", "2026-01-01T00:00:00", "ai_gateway", "abc123")
        sig2 = _sign_audit_event("event-1", "2026-01-01T00:00:00", "ai_gateway", "def456")
        assert sig1 != sig2

    def test_payload_hash_is_deterministic(self):
        p1 = _hash_payload({"company_id": "c1", "success": True})
        p2 = _hash_payload({"success": True, "company_id": "c1"})
        assert p1 == p2

    def test_empty_key_returns_empty(self):
        sig = _sign_audit_event("e1", "t1", "src", "hash")
        if not _AUDIT_SIGNING_KEY:
            assert sig == ""
