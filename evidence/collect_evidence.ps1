# WorkTicket Audit Evidence Collection Script v2.0
# Runs all evidence-gathering commands from the Master Audit (audit.html)
# Output: evidence/*.txt and evidence/*.json files
# Usage: powershell -ExecutionPolicy Bypass -File evidence/collect_evidence.ps1

$ErrorActionPreference = "Continue"
$EvidenceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $EvidenceDir

Write-Host "=== WorkTicket Evidence Collection v2.0 ===" -ForegroundColor Cyan
Write-Host "Evidence directory: $EvidenceDir" -ForegroundColor Gray
Write-Host ""

function Write-Phase { param($phase) Write-Host "`n--- Phase: $phase ---" -ForegroundColor Yellow }
function Write-Cmd { param($cmd) Write-Host "  RUN: $cmd" -ForegroundColor DarkGray }
function Write-Pass { param($msg) Write-Host "  PASS: $msg" -ForegroundColor Green }
function Write-Fail { param($msg) Write-Host "  FAIL: $msg" -ForegroundColor Red }
function Write-Warn { param($msg) Write-Host "  WARN: $msg" -ForegroundColor Magenta }

# ============================================================
# PHASE 1 — Repository Inventory
# ============================================================
Write-Phase "1/7 — Repository Inventory"
Set-Location $RepoRoot

Write-Cmd "git ls-files"
try {
    git ls-files --cached --others --exclude-standard 2>&1 | Out-File -FilePath "$EvidenceDir\repo_manifest.txt" -Encoding utf8
    Write-Pass "repo_manifest.txt ($((Get-Item "$EvidenceDir\repo_manifest.txt").Length) bytes)"
} catch { Write-Fail "repo_manifest.txt: $_" }

Write-Cmd "git log"
try {
    git log --oneline --stat -50 2>&1 | Out-File -FilePath "$EvidenceDir\commit_history.txt" -Encoding utf8
    Write-Pass "commit_history.txt"
} catch { Write-Fail "commit_history.txt: $_" }

Write-Cmd "git shortlog"
try {
    git shortlog -sn --all 2>&1 | Out-File -FilePath "$EvidenceDir\contributors.txt" -Encoding utf8
    Write-Pass "contributors.txt"
} catch { Write-Fail "contributors.txt: $_" }

Write-Cmd "Python file count"
try {
    $pyCount = (Get-ChildItem -Path $RepoRoot -Filter "*.py" -Recurse -ErrorAction SilentlyContinue).Count
    "Python files: $pyCount" | Out-File -FilePath "$EvidenceDir\python_file_count.txt" -Encoding utf8
    Write-Pass "python_file_count.txt ($pyCount files)"
} catch { Write-Fail "python_file_count.txt: $_" }

Write-Cmd "Env file scan"
try {
    Get-ChildItem -Path $RepoRoot -Include ".env*",".env" -Recurse -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName } | Out-File -FilePath "$EvidenceDir\env_files.txt" -Encoding utf8
    Write-Pass "env_files.txt"
} catch { Write-Fail "env_files.txt: $_" }

# ============================================================
# PHASE 2 — Dependency Surface
# ============================================================
Write-Phase "2/7 — Dependency Surface"
Set-Location $RepoRoot

Write-Cmd "Python pip freeze"
try {
    pip freeze 2>&1 | Out-File -FilePath "$EvidenceDir\python_deps_installed.txt" -Encoding utf8
    Write-Pass "python_deps_installed.txt"
} catch { Write-Fail "python_deps_installed.txt: $_" }

Write-Cmd "Requirements files"
try {
    Get-ChildItem -Path $RepoRoot -Filter "requirements*.txt" -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
        "=== $($_.FullName) ==="
        Get-Content $_.FullName
    } | Out-File -FilePath "$EvidenceDir\requirements_all.txt" -Encoding utf8
    Write-Pass "requirements_all.txt"
} catch { Write-Fail "requirements_all.txt: $_" }

Write-Cmd "Unpinned Python deps check"
try {
    Get-ChildItem -Path $RepoRoot -Filter "requirements*.txt" -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
        Select-String -Path $_.FullName -Pattern '>=|<=|~=|!=|>|<' -NotMatch | Where-Object { $_ -match '\S' }
    } | Out-File -FilePath "$EvidenceDir\unpinned_python_deps.txt" -Encoding utf8
    Write-Pass "unpinned_python_deps.txt"
} catch { Write-Fail "unpinned_python_deps.txt: $_" }

Write-Cmd "Node dependency check"
try {
    $packageJson = Get-ChildItem -Path $RepoRoot -Filter "package.json" -Recurse -Depth 3 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($packageJson) {
        $pkgDir = Split-Path -Parent $packageJson.FullName
        Push-Location $pkgDir
        try {
            npm list --depth=0 --json 2>&1 | Out-File -FilePath "$EvidenceDir\node_deps.json" -Encoding utf8
        } finally { Pop-Location }
        Write-Pass "node_deps.json"
        Select-String -Path $packageJson.FullName -Pattern '[\^~]' | Out-File -FilePath "$EvidenceDir\unpinned_node_deps.txt" -Encoding utf8
        Write-Pass "unpinned_node_deps.txt"
    } else {
        Write-Warn "No package.json found"
    }
} catch { Write-Fail "node_deps: $_" }

# ============================================================
# PHASE 3 — Secret & CVE Scanning
# ============================================================
Write-Phase "3/7 — Secret & CVE Scanning"
Set-Location $RepoRoot

Write-Cmd "Hardcoded password grep"
try {
    Select-String -Path (Get-ChildItem -Path $RepoRoot -Filter "*.py" -Recurse) -Pattern 'password\s*=\s*[''"][^'''']' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\hardcoded_pw_grep.txt" -Encoding utf8
    $pwCount = (Get-Content "$EvidenceDir\hardcoded_pw_grep.txt" | Where-Object { $_ -match '\S' }).Count
    if ($pwCount -eq 0) { Write-Pass "hardcoded_pw_grep.txt (0 findings)" }
    else { Write-Fail "hardcoded_pw_grep.txt ($pwCount findings!)" }
} catch { Write-Fail "hardcoded_pw_grep.txt: $_" }

Write-Cmd "Secret keyword scan"
try {
    $secretPatterns = @('SECRET_KEY\s*=\s*[''"]\S+', 'API_KEY\s*=\s*[''"]\S+', 'sk_live_', 'sk_test_')
    $found = @()
    foreach ($pat in $secretPatterns) {
        $matches = Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Include "*.py","*.js","*.ts","*.tsx","*.yml","*.yaml","*.json" -Recurse -ErrorAction SilentlyContinue) -Pattern $pat -ErrorAction SilentlyContinue
        $found += $matches
    }
    $found | Out-File -FilePath "$EvidenceDir\secret_pattern_scan.txt" -Encoding utf8
    Write-Pass "secret_pattern_scan.txt"
} catch { Write-Fail "secret_pattern_scan.txt: $_" }

Write-Cmd "TruffleHog scan"
try {
    $trufflehog = Get-Command trufflehog -ErrorAction SilentlyContinue
    if ($trufflehog) {
        trufflehog filesystem . --json 2>&1 | Out-File -FilePath "$EvidenceDir\secret_scan_trufflehog.json" -Encoding utf8
        Write-Pass "secret_scan_trufflehog.json"
    } else {
        Write-Warn "trufflehog not installed — skip"
        '{"status": "SKIPPED", "reason": "trufflehog not installed"}' | Out-File -FilePath "$EvidenceDir\secret_scan_trufflehog.json"
    }
} catch { Write-Fail "trufflehog: $_" }

# Safety check
Write-Cmd "Safety CVE check"
try {
    $safety = Get-Command safety -ErrorAction SilentlyContinue
    if ($safety) {
        safety check --json 2>&1 | Out-File -FilePath "$EvidenceDir\python_cves.json" -Encoding utf8
        Write-Pass "python_cves.json"
    } else {
        Write-Warn "safety not installed — skip"
        '{"status": "SKIPPED", "reason": "safety not installed"}' | Out-File -FilePath "$EvidenceDir\python_cves.json"
    }
} catch { Write-Fail "safety: $_" }

# ============================================================
# PHASE 4 — Security Code Audit
# ============================================================
Write-Phase "4/7 — Security Code Audit"
Set-Location $RepoRoot

Write-Cmd "JWT decode sites"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'jwt\.decode|decode_token|verify_token' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\jwt_decode_sites.txt" -Encoding utf8
    Write-Pass "jwt_decode_sites.txt"
} catch { Write-Fail "jwt_decode_sites.txt: $_" }

Write-Cmd "JWT disabled checks"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'verify_exp.*False|algorithms.*none|ALGORITHM.*none|verify_signature.*False' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\jwt_disabled_checks.txt" -Encoding utf8
    $jwtCount = (Get-Content "$EvidenceDir\jwt_disabled_checks.txt" | Where-Object { $_ -match '\S' }).Count
    if ($jwtCount -eq 0) { Write-Pass "jwt_disabled_checks.txt (0 findings — CLEAN)" }
    else { Write-Fail "jwt_disabled_checks.txt ($jwtCount findings!)" }
} catch { Write-Fail "jwt_disabled_checks.txt: $_" }

Write-Cmd "All endpoints"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern '@router\.|@app\.route|@api_view' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\all_endpoints.txt" -Encoding utf8
    Write-Pass "all_endpoints.txt"
} catch { Write-Fail "all_endpoints.txt: $_" }

Write-Cmd "Endpoints without auth"
try {
    # Find route decorators without permission/authenticate nearby
    $endpoints = Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern '@router\.|@app\.route' -ErrorAction SilentlyContinue
    $endpoints | ForEach-Object {
        $lineNum = $_.LineNumber
        $file = $_.Path
        # Check 10 lines before for auth decorators
        $content = Get-Content $file
        $start = [Math]::Max(0, $lineNum - 12)
        $end = [Math]::Min($content.Count - 1, $lineNum - 1)
        $hasAuth = $false
        for ($i = $start; $i -le $end; $i++) {
            if ($content[$i] -match 'require_|login_required|authenticate|Depends.*get_current_user|require_roles|RequireRoles') {
                $hasAuth = $true
                break
            }
        }
        if (-not $hasAuth) {
            "$($file):$($lineNum): $($_.Line.Trim())"
        }
    } | Out-File -FilePath "$EvidenceDir\endpoints_no_auth.txt" -Encoding utf8
    Write-Pass "endpoints_no_auth.txt"
} catch { Write-Fail "endpoints_no_auth.txt: $_" }

Write-Cmd "Direct ID lookups"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern '\.get\(id=|\.filter\(id=' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\direct_id_lookups.txt" -Encoding utf8
    Write-Pass "direct_id_lookups.txt"
} catch { Write-Fail "direct_id_lookups.txt: $_" }

Write-Cmd "Rate limiting"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'throttle|rate_limit|RateThrottle|RateLimit' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\rate_limit_sites.txt" -Encoding utf8
    Write-Pass "rate_limit_sites.txt"
} catch { Write-Fail "rate_limit_sites.txt: $_" }

Write-Cmd "CORS config"
try {
    Select-String -Path (Get-ChildItem -Path $RepoRoot -Include "*.py","*.yml","*.yaml","*.env*" -Recurse -ErrorAction SilentlyContinue) -Pattern 'CORS_ALLOW|cors_origins|CORSMiddleware|allow_origins' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\cors_config.txt" -Encoding utf8
    Write-Pass "cors_config.txt"
} catch { Write-Fail "cors_config.txt: $_" }

# ============================================================
# PHASE 5 — AI & Data Protection Audit
# ============================================================
Write-Phase "5/7 — AI & Data Protection Audit"
Set-Location $RepoRoot

Write-Cmd "AI call sites"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'openai|anthropic|llm|ai_client|chat\.completions|ollama' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\ai_call_sites.txt" -Encoding utf8
    Write-Pass "ai_call_sites.txt"
} catch { Write-Fail "ai_call_sites.txt: $_" }

Write-Cmd "AI write operations check"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'Invoice.*create|Job.*approve|Quote.*confirm|send_invoice|approve_job' -ErrorAction SilentlyContinue | Where-Object { $_.Path -notmatch 'test_' -and $_.Line -notmatch '#\s*|^\s*#' } | Out-File -FilePath "$EvidenceDir\ai_write_operations.txt" -Encoding utf8
    Write-Pass "ai_write_operations.txt"
} catch { Write-Fail "ai_write_operations.txt: $_" }

Write-Cmd "AI output validation"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'pydantic|jsonschema|validate|AIResponse|AIOutputSchema|_sanitize' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\ai_output_validation.txt" -Encoding utf8
    Write-Pass "ai_output_validation.txt"
} catch { Write-Fail "ai_output_validation.txt: $_" }

Write-Cmd "AI fallback"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'except.*ai|ai_fallback|AI_ENABLED|fallback|circuit_breaker' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\ai_fallback.txt" -Encoding utf8
    Write-Pass "ai_fallback.txt"
} catch { Write-Fail "ai_fallback.txt: $_" }

Write-Cmd "PII fields"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'email|phone|address|encrypted_' -ErrorAction SilentlyContinue | Where-Object { $_.Path -match 'models|serializ' } | Out-File -FilePath "$EvidenceDir\pii_fields.txt" -Encoding utf8
    Write-Pass "pii_fields.txt"
} catch { Write-Fail "pii_fields.txt: $_" }

Write-Cmd "PII in logs"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'logger.*email|log.*phone|print.*password|print.*secret' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\pii_in_logs.txt" -Encoding utf8
    $piiLogCount = (Get-Content "$EvidenceDir\pii_in_logs.txt" | Where-Object { $_ -match '\S' }).Count
    if ($piiLogCount -eq 0) { Write-Pass "pii_in_logs.txt (0 findings — CLEAN)" }
    else { Write-Fail "pii_in_logs.txt ($piiLogCount findings!)" }
} catch { Write-Fail "pii_in_logs.txt: $_" }

Write-Cmd "Audit log model"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'AuditLog|audit_log|ChangeLog|ActivityLog' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\audit_log_model.txt" -Encoding utf8
    Write-Pass "audit_log_model.txt"
} catch { Write-Fail "audit_log_model.txt: $_" }

Write-Cmd "Soft delete"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'deleted_at|is_deleted|SoftDeleteMixin|SafeDeleteMixin' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\soft_delete.txt" -Encoding utf8
    Write-Pass "soft_delete.txt"
} catch { Write-Fail "soft_delete.txt: $_" }

Write-Cmd "Deletion workflows"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'delete_tenant|gdpr_delete|purge_user|hard_delete|delete.me' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\deletion_workflows.txt" -Encoding utf8
    Write-Pass "deletion_workflows.txt"
} catch { Write-Fail "deletion_workflows.txt: $_" }

# ============================================================
# PHASE 6 — Production Readiness
# ============================================================
Write-Phase "6/7 — Production Readiness"
Set-Location $RepoRoot

Write-Cmd "Celery tasks"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern '@shared_task|@app\.task|@celery_app\.task' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\celery_tasks.txt" -Encoding utf8
    Write-Pass "celery_tasks.txt"
} catch { Write-Fail "celery_tasks.txt: $_" }

Write-Cmd "Celery retry config"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'bind=True|autoretry_for|max_retries|countdown' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\celery_retry_config.txt" -Encoding utf8
    Write-Pass "celery_retry_config.txt"
} catch { Write-Fail "celery_retry_config.txt: $_" }

Write-Cmd "Dead letter queue"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'CELERY_TASK_ROUTES|dead_letter|DEAD_LETTER|dlq|task_reject_on_worker_lost' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\celery_dlq.txt" -Encoding utf8
    Write-Pass "celery_dlq.txt"
} catch { Write-Fail "celery_dlq.txt: $_" }

Write-Cmd "Redis config"
try {
    Select-String -Path (Get-ChildItem -Path $RepoRoot -Include "*.py","*.yml","*.yaml","*.env*" -Recurse -ErrorAction SilentlyContinue) -Pattern 'REDIS_URL|CELERY_BROKER|CACHES|maxmemory' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\redis_config.txt" -Encoding utf8
    Write-Pass "redis_config.txt"
} catch { Write-Fail "redis_config.txt: $_" }

Write-Cmd "DB pool config"
try {
    Select-String -Path (Get-ChildItem -Path $RepoRoot -Include "*.py","*.yml","*.yaml" -Recurse -ErrorAction SilentlyContinue) -Pattern 'CONN_MAX_AGE|pool_size|max_overflow|DATABASES' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\db_pool_config.txt" -Encoding utf8
    Write-Pass "db_pool_config.txt"
} catch { Write-Fail "db_pool_config.txt: $_" }

Write-Cmd "Risky migrations"
try {
    Get-ChildItem -Path $RepoRoot -Filter "migrations" -Recurse -Directory -ErrorAction SilentlyContinue | ForEach-Object {
        Select-String -Path (Get-ChildItem -Path $_.FullName -Filter "*.py") -Pattern 'AlterField|AddField.*NOT NULL|RunSQL' -ErrorAction SilentlyContinue
    } | Out-File -FilePath "$EvidenceDir\risky_migrations.txt" -Encoding utf8
    Write-Pass "risky_migrations.txt"
} catch { Write-Fail "risky_migrations.txt: $_" }

Write-Cmd "State transitions"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'status.*=|state.*=|transition|VALID_TRANSITIONS|allowed_transitions|state_machine' -ErrorAction SilentlyContinue | Where-Object { $_.Path -notmatch 'test_' } | Out-File -FilePath "$EvidenceDir\state_transitions.txt" -Encoding utf8
    Write-Pass "state_transitions.txt"
} catch { Write-Fail "state_transitions.txt: $_" }

# ============================================================
# PHASE 7 — Observability & Infrastructure
# ============================================================
Write-Phase "7/7 — Observability & Infrastructure"
Set-Location $RepoRoot

Write-Cmd "Log statement count"
try {
    $logCount = (Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'logger\.|logging\.' -ErrorAction SilentlyContinue).Count
    "Log statements: $logCount" | Out-File -FilePath "$EvidenceDir\log_statement_count.txt" -Encoding utf8
    Write-Pass "log_statement_count.txt ($logCount log statements)"
} catch { Write-Fail "log_statement_count.txt: $_" }

Write-Cmd "Metrics sites"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'statsd|prometheus|metrics\.|histogram|counter' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\metrics_sites.txt" -Encoding utf8
    Write-Pass "metrics_sites.txt"
} catch { Write-Fail "metrics_sites.txt: $_" }

Write-Cmd "Tracing sites"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'opentelemetry|sentry_sdk|tracer|span|trace_id' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\tracing_sites.txt" -Encoding utf8
    Write-Pass "tracing_sites.txt"
} catch { Write-Fail "tracing_sites.txt: $_" }

Write-Cmd "Docker user check"
try {
    $dockerfile = Get-ChildItem -Path $RepoRoot -Filter "Dockerfile" -Recurse -Depth 2 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($dockerfile) {
        Get-Content $dockerfile.FullName | Out-File -FilePath "$EvidenceDir\dockerfile_analysis.txt" -Encoding utf8
        $userCount = (Select-String -Path $dockerfile.FullName -Pattern '^USER').Count
        "$userCount" | Out-File -FilePath "$EvidenceDir\dockerfile_user_count.txt" -Encoding utf8
        if ($userCount -ge 1) { Write-Pass "dockerfile_user_count.txt (non-root user configured)" }
        else { Write-Fail "dockerfile_user_count.txt (0 — runs as root!)" }
    } else {
        Write-Warn "No Dockerfile found"
    }
} catch { Write-Fail "dockerfile analysis: $_" }

Write-Cmd "Workflow permissions"
try {
    Get-ChildItem -Path "$RepoRoot\.github\workflows" -Filter "*.yml" -ErrorAction SilentlyContinue | ForEach-Object {
        "=== $($_.Name) ==="
        Select-String -Path $_.FullName -Pattern 'permissions:|secrets\.' -ErrorAction SilentlyContinue
    } | Out-File -FilePath "$EvidenceDir\workflow_permissions.txt" -Encoding utf8
    Write-Pass "workflow_permissions.txt"
} catch { Write-Fail "workflow_permissions.txt: $_" }

Write-Cmd "Dependabot"
try {
    $dep = Get-ChildItem -Path $RepoRoot -Filter "dependabot.yml" -Recurse -Depth 3 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($dep) {
        Get-Content $dep.FullName | Out-File -FilePath "$EvidenceDir\dependabot.txt" -Encoding utf8
        Write-Pass "dependabot.txt (configured)"
    } else {
        "DEPENDABOT NOT CONFIGURED" | Out-File -FilePath "$EvidenceDir\dependabot.txt" -Encoding utf8
        Write-Fail "dependabot.txt (MISSING!)"
    }
} catch { Write-Fail "dependabot.txt: $_" }

Write-Cmd "Docs structure"
try {
    Get-ChildItem -Path "$RepoRoot\docs" -Recurse -Depth 2 -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName } | Out-File -FilePath "$EvidenceDir\docs_structure.txt" -Encoding utf8
    Write-Pass "docs_structure.txt"
} catch { Write-Fail "docs_structure.txt: $_" }

Write-Cmd "GitHub templates"
try {
    Get-ChildItem -Path "$RepoRoot\.github" -Recurse -Include "*.md","*.yml" -Depth 2 -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName } | Out-File -FilePath "$EvidenceDir\github_templates.txt" -Encoding utf8
    Write-Pass "github_templates.txt"
} catch { Write-Fail "github_templates.txt: $_" }

Write-Cmd "Large files"
try {
    Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.FullName -notmatch 'migrations' } | ForEach-Object {
        $lines = (Get-Content $_.FullName | Measure-Object -Line).Lines
        if ($lines -gt 300) { "$($lines) lines  $($_.FullName)" }
    } | Sort-Object -Descending | Select-Object -First 20 | Out-File -FilePath "$EvidenceDir\large_files.txt" -Encoding utf8
    Write-Pass "large_files.txt"
} catch { Write-Fail "large_files.txt: $_" }

Write-Cmd "Pagination audit"
try {
    Select-String -Path (Get-ChildItem -Path "$RepoRoot\src" -Filter "*.py" -Recurse) -Pattern 'PageNumberPagination|LimitOffsetPagination|paginate' -ErrorAction SilentlyContinue | Out-File -FilePath "$EvidenceDir\pagination.txt" -Encoding utf8
    Write-Pass "pagination.txt"
} catch { Write-Fail "pagination.txt: $_" }

# ============================================================
# SUMMARY
# ============================================================
Write-Host ""
Write-Host "=== Collection Complete ===" -ForegroundColor Cyan
$evidenceFiles = Get-ChildItem -Path $EvidenceDir -File | Measure-Object
Write-Host "Evidence files generated: $($evidenceFiles.Count)" -ForegroundColor White
Write-Host ""
Write-Host "CRITICAL: Verify these files are EMPTY where expected:" -ForegroundColor Yellow
Write-Host "  evidence/jwt_disabled_checks.txt   (must be empty)" -ForegroundColor Red
Write-Host "  evidence/hardcoded_pw_grep.txt     (must be empty)" -ForegroundColor Red
Write-Host "  evidence/pii_in_logs.txt          (must be empty)" -ForegroundColor Red
Write-Host "  evidence/dockerfile_user_count.txt (must be >= 1)" -ForegroundColor Red
