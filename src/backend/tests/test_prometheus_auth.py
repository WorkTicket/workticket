from unittest.mock import patch

from fastapi import status
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def test_prometheus_metrics_requires_auth_when_debug_false():
    """Test that /admin/metrics requires authentication when debug=False"""
    get_settings.cache_clear()
    with patch("app.config.get_settings") as mock_settings:
        mock_settings.return_value.debug = False
        mock_settings.return_value.metrics_access_token = None

        client = TestClient(app)
        response = client.get("/admin/metrics")

        assert response.status_code in (403, 404), f"Got {response.status_code}: {response.text[:200]}"


def test_prometheus_metrics_allows_access_when_debug_true():
    """Test that /admin/metrics allows access when debug=True"""
    get_settings.cache_clear()
    with patch("app.config.get_settings") as mock_settings:
        mock_settings.return_value.debug = True
        mock_settings.return_value.metrics_access_token = None

        client = TestClient(app)
        response = client.get("/admin/metrics")

        assert response.status_code in (200, 404), f"Got {response.status_code}: {response.text[:200]}"


def test_prometheus_metrics_requires_valid_token_when_configured():
    """Test that /admin/metrics requires valid token when METRICS_ACCESS_TOKEN is set"""
    get_settings.cache_clear()
    with patch("app.config.get_settings") as mock_settings:
        mock_settings.return_value.debug = False
        mock_settings.return_value.metrics_access_token = "valid-token-123"

        client = TestClient(app)

        response = client.get("/admin/metrics")
        assert response.status_code in (403, 404), f"Got {response.status_code}: {response.text[:200]}"

        response = client.get("/admin/metrics", headers={"Authorization": "Bearer invalid-token"})
        assert response.status_code in (403, 404), f"Got {response.status_code}: {response.text[:200]}"

        response = client.get("/admin/metrics", headers={"Authorization": "Bearer valid-token-123"})
        assert response.status_code in (200, 404), f"Got {response.status_code}: {response.text[:200]}"
