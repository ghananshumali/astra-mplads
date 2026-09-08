# ASTRA — start the API and the React frontend together (development).
#   powershell -ExecutionPolicy Bypass -File run_dev.ps1
#
# Backend  : http://localhost:8000  (docs at /docs)
# Frontend : http://localhost:5173
Set-Location $PSScriptRoot

if (-not (Test-Path "data\astra.db")) {
  Write-Host "[ASTRA] No database found. Run these first:" -ForegroundColor Yellow
  Write-Host "        python scripts\fetch_data.py"
  Write-Host "        python scripts\run_pipeline.py"
  exit 1
}
if (-not (Test-Path "frontend\node_modules")) {
  Write-Host "[ASTRA] Installing frontend dependencies..." -ForegroundColor Cyan
  Push-Location frontend; npm install; Pop-Location
}

function Test-PortBusy([int]$Port) {
  try { return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop) }
  catch { return $false }
}

if (Test-PortBusy 8000) {
  Write-Host "[ASTRA] Port 8000 is already in use." -ForegroundColor Red
  Write-Host "        Stop the process using it, or the API cannot start:" -ForegroundColor Red
  Write-Host "        Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object OwningProcess"
  exit 1
}
if (Test-PortBusy 5173) {
  Write-Host "[ASTRA] Port 5173 is busy - Vite will use the next free port." -ForegroundColor Yellow
  Write-Host "        That is fine: the API accepts any local origin." -ForegroundColor Yellow
  Write-Host "        Watch the Vite output below for the actual URL." -ForegroundColor Yellow
}

Write-Host "[ASTRA] Starting API on :8000 ..." -ForegroundColor Cyan
$api = Start-Process -PassThru -NoNewWindow python `
  -ArgumentList "-m","uvicorn","astra.api.main:app","--host","127.0.0.1","--port","8000"

Start-Sleep -Seconds 3
Write-Host "[ASTRA] Starting frontend on :5173 ..." -ForegroundColor Green
try {
  Push-Location frontend
  npm run dev
} finally {
  Pop-Location
  if ($api -and -not $api.HasExited) {
    Write-Host "[ASTRA] Stopping API..." -ForegroundColor Cyan
    Stop-Process -Id $api.Id -Force -ErrorAction SilentlyContinue
  }
}
