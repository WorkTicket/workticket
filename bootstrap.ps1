# WorkTicket Beta v1 - First-Time Setup (Windows PowerShell)
# Run this script from the repo root

$ErrorActionPreference = "Stop"

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  WorkTicket Beta v1 - First-Time Setup" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

Set-Location -LiteralPath "$PSScriptRoot\src"

# 1. Create .env if not present
if (-not (Test-Path -LiteralPath ".env")) {
    Write-Host "[1/4] Creating .env from .env.example..." -ForegroundColor Yellow
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
    Write-Host "  .env created with dev defaults." -ForegroundColor Green
    Write-Host "  Edit .env to set production secrets before deploying." -ForegroundColor Yellow
} else {
    Write-Host "[1/4] .env already exists, skipping." -ForegroundColor Green
}

# 2. Build Docker images
Write-Host "[2/4] Building Docker images..." -ForegroundColor Yellow
docker compose build --parallel
Write-Host "  Docker images built." -ForegroundColor Green

# 3. Start services
Write-Host "[3/4] Starting services..." -ForegroundColor Yellow
docker compose up -d
Write-Host "  Services starting..." -ForegroundColor Green

# 4. Wait for backend to be healthy
Write-Host "[4/4] Waiting for backend to be ready..." -ForegroundColor Yellow
$retries = 60
for ($i = 1; $i -le $retries; $i++) {
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:8000/livez" -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            Write-Host "  Backend is ready!" -ForegroundColor Green
            break
        }
    } catch {
        # Still waiting
    }
    if ($i -eq $retries) {
        Write-Host "  WARNING: Backend not ready after ${retries}s. Check: docker compose logs backend" -ForegroundColor Red
    }
    Start-Sleep -Seconds 1
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  WorkTicket Beta v1 is running!" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Services:" -ForegroundColor White
Write-Host "    API:       http://localhost:8000" -ForegroundColor Gray
Write-Host "    API Docs:  http://localhost:8000/docs" -ForegroundColor Gray
Write-Host "    Health:    http://localhost:8000/health" -ForegroundColor Gray
Write-Host ""
Write-Host "  Monitoring:" -ForegroundColor White
Write-Host "    Logs:      docker compose logs -f" -ForegroundColor Gray
Write-Host "    Status:    docker compose ps" -ForegroundColor Gray
Write-Host "============================================" -ForegroundColor Cyan
