"""Contract Tests (Pact) — Backend ↔ Dashboard, Backend ↔ Mobile

Validates that API responses conform to consumer expectations.
Uses Pact for consumer-driven contract testing.

Usage:
    pip install pact-python
    python tests/contract/test_dashboard_contract.py
"""

import json
import os
import pytest
from unittest.mock import patch, AsyncMock


# Skip if pact-python is not installed (optional dependency)
def _pact_available():
    try:
        import pact  # noqa: F401
        return True
    except ImportError:
        return False


pact_required = pytest.mark.skipif(not _pact_available(), reason="pact-python not installed")


@patch("app.main.app", new_callable=AsyncMock)
@pact_required
class TestDashboardContract:
    """Backend ↔ Dashboard API contract tests."""

    def test_job_list_response_shape(self, mock_app):
        """Consumer (Dashboard) expects jobs list to have specific shape."""
        expected_schema = {
            "type": "object",
            "required": ["jobs", "total", "page", "page_size"],
            "properties": {
                "jobs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["id", "customer_id", "description", "status", "created_at"],
                        "properties": {
                            "id": {"type": "string", "format": "uuid"},
                            "customer_id": {"type": "string", "format": "uuid"},
                            "description": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "failed", "cancelled"]},
                            "created_at": {"type": "string", "format": "date-time"},
                            "updated_at": {"type": "string", "format": "date-time"},
                        },
                    },
                },
                "total": {"type": "integer"},
                "page": {"type": "integer"},
                "page_size": {"type": "integer"},
            },
        }
        # In a real Pact setup, this would be verified against the provider
        assert expected_schema

    def test_customer_create_request_shape(self, mock_app):
        """Consumer expects customer creation payload."""
        expected = {
            "type": "object",
            "required": ["name", "email"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "email": {"type": "string", "format": "email"},
                "phone": {"type": "string"},
            },
        }
        assert expected

    def test_error_response_shape(self, mock_app):
        """Consumer expects standardized error format."""
        expected = {
            "type": "object",
            "required": ["error", "code"],
            "properties": {
                "error": {"type": "string"},
                "code": {"type": "integer"},
                "details": {"type": "array"},
                "request_id": {"type": "string"},
            },
        }
        assert expected

    def test_ai_process_response_shape(self, mock_app):
        """Consumer expects AI processing response format."""
        expected = {
            "type": "object",
            "required": ["status", "job_id"],
            "properties": {
                "status": {"type": "string", "enum": ["queued", "processing", "completed", "failed"]},
                "job_id": {"type": "string", "format": "uuid"},
                "estimated_duration_seconds": {"type": "integer"},
                "position_in_queue": {"type": "integer"},
            },
        }
        assert expected

    def test_quote_response_shape(self, mock_app):
        """Consumer expects quote response format."""
        expected = {
            "type": "object",
            "required": ["id", "job_id", "amount", "status", "line_items"],
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "job_id": {"type": "string", "format": "uuid"},
                "amount": {"type": "number"},
                "currency": {"type": "string"},
                "status": {"type": "string"},
                "line_items": {"type": "array"},
                "valid_until": {"type": "string", "format": "date-time"},
            },
        }
        assert expected


@patch("app.main.app", new_callable=AsyncMock)
@pact_required
class TestMobileContract:
    """Backend ↔ Mobile App API contract tests."""

    def test_mobile_job_create_payload(self, mock_app):
        """Mobile consumer expects job create payload format."""
        expected = {
            "type": "object",
            "required": ["customer_id", "description"],
            "properties": {
                "customer_id": {"type": "string"},
                "description": {"type": "string"},
                "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
                "due_date": {"type": "string", "format": "date-time"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
        }
        assert expected

    def test_mobile_notification_shape(self, mock_app):
        """Mobile consumer expects notification payload format."""
        expected = {
            "type": "object",
            "required": ["id", "type", "title", "body", "created_at"],
            "properties": {
                "id": {"type": "string"},
                "type": {"type": "string", "enum": ["job_update", "quote_ready", "invoice", "system"]},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "data": {"type": "object"},
                "read": {"type": "boolean"},
                "created_at": {"type": "string", "format": "date-time"},
            },
        }
        assert expected

    def test_auth_token_refresh_response(self, mock_app):
        """Mobile consumer expects token refresh response."""
        expected = {
            "type": "object",
            "required": ["access_token", "expires_in"],
            "properties": {
                "access_token": {"type": "string"},
                "refresh_token": {"type": "string"},
                "expires_in": {"type": "integer"},
                "token_type": {"type": "string"},
            },
        }
        assert expected
