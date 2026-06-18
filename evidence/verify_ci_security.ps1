# WorkTicket CI/CD Security Gate Verification
# Validates that all required CI/CD security controls are in place
# Usage: powershell -File evidence/verify_ci_security.ps1

$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PassCount = 0
$FailCount = 0
$WarnCount = 0

function Check { 
    param($label, $result, $detail = "")
    if ($result) { 
        Write-Host "  [PASS] $label" -ForegroundColor Green
        $script:PassCount++
    } else { 
        Write-Host "  [FAIL] $label" -ForegroundColor Red
        if ($detail) { Write-Host "         $detail" -ForegroundColor DarkRed }
        $script:FailCount++
    }
}

function CheckWarn {
    param($label, $result, $detail = "")
    if ($result) { 
        Write-Host "  [PASS] $label" -ForegroundColor Green
        $script:PassCount++
    } else { 
        Write-Host "  [WARN] $label" -ForegroundColor Yellow
        if ($detail) { Write-Host "         $detail" -ForegroundColor DarkYellow }
        $script:WarnCount++
    }
}

Write-Host "`n=== CI/CD Security Gate Verification ===" -ForegroundColor Cyan
Write-Host ""

# Gate 1: Secret Scanning
Write-Host "--- Gate 1: Secret Scanning ---" -ForegroundColor Yellow
$gitleaksWorkflow = Get-ChildItem -Path "$RepoRoot\.github\workflows" -Filter "*.yml" -Recurse | 
    ForEach-Object { if ((Get-Content $_.FullName -Raw) -match "gitleaks|trufflehog|detect-secrets") { $_ } } |
    Select-Object -First 1
Check "Gitleaks/secret scanning in CI" ($gitleaksWorkflow -ne $null) "Expected: CI workflow with secret scanning"

$semgrepWorkflow = Get-ChildItem -Path "$RepoRoot\.github\workflows" -Filter "*.yml" -Recurse |
    ForEach-Object { if ((Get-Content $_.FullName -Raw) -match "semgrep") { $_ } } |
    Select-Object -First 1
Check "Semgrep SAST in CI" ($semgrepWorkflow -ne $null) "Expected: CI workflow with semgrep scanning"

# Gate 2: Dependency Vulnerability Scanning
Write-Host "--- Gate 2: Dependency Scanning ---" -ForegroundColor Yellow
$trivyWorkflow = Get-ChildItem -Path "$RepoRoot\.github\workflows" -Filter "*.yml" -Recurse |
    ForEach-Object { if ((Get-Content $_.FullName -Raw) -match "trivy|container-scan") { $_ } } |
    Select-Object -First 1
Check "Trivy container scanning in CI" ($trivyWorkflow -ne $null) "Expected: CI workflow with Trivy"

$pipAuditWorkflow = Get-ChildItem -Path "$RepoRoot\.github\workflows" -Filter "*.yml" -Recurse |
    ForEach-Object { if ((Get-Content $_.FullName -Raw) -match "pip-audit|pip_audit|safety") { $_ } } |
    Select-Object -First 1
Check "Python dependency CVE scanning" ($pipAuditWorkflow -ne $null) "Expected: CI workflow with pip-audit or safety"

# Gate 3: SAST (Static Analysis)
Write-Host "--- Gate 3: Static Analysis ---" -ForegroundColor Yellow
$banditWorkflow = Get-ChildItem -Path "$RepoRoot\.github\workflows" -Filter "*.yml" -Recurse |
    ForEach-Object { if ((Get-Content $_.FullName -Raw) -match "bandit") { $_ } } |
    Select-Object -First 1
Check "Bandit Python SAST in CI" ($banditWorkflow -ne $null) "Expected: CI workflow with bandit"

$ruffWorkflow = Get-ChildItem -Path "$RepoRoot\.github\workflows" -Filter "*.yml" -Recurse |
    ForEach-Object { if ((Get-Content $_.FullName -Raw) -match "ruff|lint") { $_ } } |
    Select-Object -First 1
Check "Linting in CI" ($ruffWorkflow -ne $null) "Expected: CI workflow with ruff or linting"

# Gate 4: Test Suite
Write-Host "--- Gate 4: Test Suite ---" -ForegroundColor Yellow
$testWorkflow = Get-ChildItem -Path "$RepoRoot\.github\workflows" -Filter "*.yml" -Recurse |
    ForEach-Object { if ((Get-Content $_.FullName -Raw) -match "pytest|test") { $_ } } |
    Select-Object -First 1
Check "Pytest in CI" ($testWorkflow -ne $null) "Expected: CI workflow running pytest"

$coverageWorkflow = Get-ChildItem -Path "$RepoRoot\.github\workflows" -Filter "*.yml" -Recurse |
    ForEach-Object { if ((Get-Content $_.FullName -Raw) -match "coverage|--cov") { $_ } } |
    Select-Object -First 1
Check "Coverage reporting in CI" ($coverageWorkflow -ne $null) "Expected: CI workflow with coverage"

$securityTestWorkflow = Get-ChildItem -Path "$RepoRoot\.github\workflows" -Filter "*.yml" -Recurse |
    ForEach-Object { if ((Get-Content $_.FullName -Raw) -match "security.test|security-test") { $_ } } |
    Select-Object -First 1
Check "Security-focused test suite in CI" ($securityTestWorkflow -ne $null) "Expected: Dedicated security tests workflow"

# Gate 5: Supply Chain Security
Write-Host "--- Gate 5: Supply Chain ---" -ForegroundColor Yellow
$sbomWorkflow = Get-ChildItem -Path "$RepoRoot\.github\workflows" -Filter "*.yml" -Recurse |
    ForEach-Object { if ((Get-Content $_.FullName -Raw) -match "sbom|cyclonedx|spdx") { $_ } } |
    Select-Object -First 1
Check "SBOM generation in CI" ($sbomWorkflow -ne $null) "Expected: SBOM generation workflow"

$dependabot = Get-ChildItem -Path $RepoRoot -Filter "dependabot.yml" -Recurse -Depth 3 |
    Select-Object -First 1
Check "Dependabot configured" ($dependabot -ne $null) "Expected: .github/dependabot.yml"

$cosignWorkflow = Get-ChildItem -Path "$RepoRoot\.github\workflows" -Filter "*.yml" -Recurse |
    ForEach-Object { if ((Get-Content $_.FullName -Raw) -match "cosign|signing|provenance") { $_ } } |
    Select-Object -First 1
CheckWarn "Container image signing (Cosign)" ($cosignWorkflow -ne $null) "Nice-to-have: Image signing"

# Gate 6: Build Verification
Write-Host "--- Gate 6: Build Verification ---" -ForegroundColor Yellow
$dockerWorkflow = Get-ChildItem -Path "$RepoRoot\.github\workflows" -Filter "*.yml" -Recurse |
    ForEach-Object { if ((Get-Content $_.FullName -Raw) -match "docker.build|docker build") { $_ } } |
    Select-Object -First 1
Check "Docker build in CI" ($dockerWorkflow -ne $null) "Expected: Docker build workflow"

# Check Dockerfile for non-root user
$dockerfile = Get-ChildItem -Path $RepoRoot -Filter "Dockerfile" -Recurse -Depth 3 |
    Select-Object -First 1
if ($dockerfile) {
    $hasUser = (Select-String -Path $dockerfile.FullName -Pattern '^USER\s+\w+' -ErrorAction SilentlyContinue).Count -gt 0
    Check "Docker runs as non-root" $hasUser "Expected: USER instruction in Dockerfile"
} else {
    Check "Docker runs as non-root" $false "No Dockerfile found"
}

# Gate 7: Branch Protection (informational — requires GitHub API)
Write-Host "--- Gate 7: Branch Protection (check GitHub settings) ---" -ForegroundColor Yellow
Write-Host "  [INFO] Verify in GitHub: Settings > Branches > Branch protection rules" -ForegroundColor Gray
Write-Host "  [INFO] Required: Require PR reviews, Require status checks, Require branches up to date" -ForegroundColor Gray

# Gate 8: PR Template & Issue Templates
Write-Host "--- Gate 8: Templates ---" -ForegroundColor Yellow
$prTemplate = Get-ChildItem -Path $RepoRoot -Filter "pull_request_template.md" -Recurse -Depth 3 |
    Select-Object -First 1
Check "PR template exists" ($prTemplate -ne $null) "Expected: .github/pull_request_template.md"

$issueTemplates = Get-ChildItem -Path "$RepoRoot\.github\ISSUE_TEMPLATE" -Filter "*.md" -ErrorAction SilentlyContinue
Check "Issue templates exist" ($issueTemplates.Count -ge 1) "Expected: .github/ISSUE_TEMPLATE/*.md"

# Gate 9: Workflow Permissions
Write-Host "--- Gate 9: Least-Privilege Workflows ---" -ForegroundColor Yellow
$workflows = Get-ChildItem -Path "$RepoRoot\.github\workflows" -Filter "*.yml" -Recurse -ErrorAction SilentlyContinue
$workflowsWithPermissions = 0
foreach ($wf in $workflows) {
    $content = Get-Content $wf.FullName -Raw
    if ($content -match "permissions:") { $workflowsWithPermissions++ }
}
CheckWarn "Workflows have explicit permissions" ($workflowsWithPermissions -gt 0) "Expected: permissions: read-all or explicit per-job permissions in workflows"

# Gate 10: Deployment Safety
Write-Host "--- Gate 10: Deployment Safety ---" -ForegroundColor Yellow
$deployWorkflow = Get-ChildItem -Path "$RepoRoot\.github\workflows" -Filter "*.yml" -Recurse |
    ForEach-Object { if ((Get-Content $_.FullName -Raw) -match "deploy|canary|blue-green") { $_ } } |
    Select-Object -First 1
Check "Deployment workflow exists" ($deployWorkflow -ne $null) "Expected: Deploy or canary deploy workflow"

# Summary
Write-Host ""
Write-Host "=== CI/CD Security Gate Results ===" -ForegroundColor Cyan
Write-Host "PASS: $PassCount" -ForegroundColor Green
Write-Host "FAIL: $FailCount" -ForegroundColor Red
Write-Host "WARN: $WarnCount" -ForegroundColor Yellow
Write-Host ""

if ($FailCount -eq 0) {
    Write-Host "All critical CI/CD security gates PASS." -ForegroundColor Green
} else {
    Write-Host "$FailCount CI/CD security gates FAILED. Fix before release." -ForegroundColor Red
    exit 1
}
