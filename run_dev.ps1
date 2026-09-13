# ASTRA — start the poller, the API and the React frontend together (development).
#   powershell -ExecutionPolicy Bypass -File run_dev.ps1              # everything
#   powershell -ExecutionPolicy Bypass -File run_dev.ps1 -NoPoller    # site only
#
# Poller   : checks mplads.mospi.gov.in every minute and keeps data\astra.db current
# Backend  : http://localhost:8000  (docs at /docs)
# Frontend : http://localhost:5173
#
# Ctrl+C stops all three. To keep the data updating while the site is closed,
# run the poller on its own instead:  python -m astra.ingestion.poller
param([switch]$NoPoller)
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

$poller = $null
if ($NoPoller) {
  Write-Host "[ASTRA] -NoPoller: the site will show the data as of the last portal check." -ForegroundColor Yellow
} else {
  Write-Host "[ASTRA] Starting poller (checks the portal every minute) ..." -ForegroundColor Cyan
  $poller = Start-Process -PassThru -NoNewWindow python -ArgumentList "-m","astra.ingestion.poller"
  $null = $poller.Handle   # keeps the exit code readable once it has exited
}

Write-Host "[ASTRA] Starting API on :8000 ..." -ForegroundColor Cyan
$api = Start-Process -PassThru -NoNewWindow python `
  -ArgumentList "-m","uvicorn","astra.api.main:app","--host","127.0.0.1","--port","8000"

Start-Sleep -Seconds 3
if ($poller -and $poller.HasExited) {
  if ($poller.ExitCode -eq 3) {
    Write-Host "[ASTRA] A poller is already running in another window - the site will use it." -ForegroundColor Green
  } else {
    Write-Host "[ASTRA] The poller exited (code $($poller.ExitCode)); the site will show the last stored data." -ForegroundColor Yellow
  }
  $poller = $null
}

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
  if ($poller -and -not $poller.HasExited) {
    Write-Host "[ASTRA] Stopping poller..." -ForegroundColor Cyan
    Stop-Process -Id $poller.Id -Force -ErrorAction SilentlyContinue
  }
}
