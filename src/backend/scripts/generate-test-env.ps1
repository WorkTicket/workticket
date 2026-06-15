# Generate .env.test from .env.example for CI and local test environments.
# Replaces all secret/sensitive values with test-safe placeholders.
# Usage: .\scripts\generate-test-env.ps1 [-OutputPath .env.test]

param(
    [string]$OutputPath = ".env.test"
)

$templatePath = ".env.example"
if (-not (Test-Path $templatePath)) {
    Write-Error "$templatePath not found. Run from src/backend directory."
    exit 1
}

@"
debug=true

database_url=postgresql+asyncpg://postgres:postgres@localhost:5432/workticket_test
redis_url=redis://localhost:6379/0
clerk_secret_key=test_secret
clerk_publishable_key=test_publishable
clerk_jwt_issuer=https://test.clerk.accounts.dev
clerk_jwt_audience=test
stripe_secret_key=sk_test_placeholder
stripe_webhook_secret=whsec_test_placeholder
stripe_price_id=price_test
r2_endpoint_url=https://test.r2.cloudflarestorage.com
r2_access_key_id=test_access_key
r2_secret_access_key=test_secret_key
r2_bucket_name=workticket-media-test
sentry_dsn=https://test@sentry.io/0
posthog_api_key=test_ph_key
metrics_access_token=test_metrics_token
twilio_account_sid=AC00000000000000000000000000000000
twilio_auth_token=test_auth_token
twilio_from_number=+15555550123
resend_api_key=re_test_key
celery_task_signing_key=test_signing_key
allowed_hosts=localhost,127.0.0.1,test
app_base_url=http://localhost:8000
pii_encryption_key=test_pii_encryption_key_32_bytes_hex
push_token_encryption_key=test_push_encryption_key_32_bytes
"@ | Set-Content -Path $OutputPath -Encoding utf8

Write-Host "Generated $OutputPath with test-safe placeholders"
