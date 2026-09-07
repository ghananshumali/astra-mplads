# ASTRA one-shot demo launcher (dual-mode ingestion -> agents -> dashboard)
# Usage: powershell -ExecutionPolicy Bypass -File run_demo.ps1 [-Mode auto|live|offline]
param([ValidateSet('auto','live','offline')][string]$Mode = 'auto')
Set-Location $PSScriptRoot
Write-Host "[ASTRA] ingesting real MPLADS data (mode: $Mode)..." -ForegroundColor Cyan
python scripts\fetch_data.py --mode $Mode
if ($LASTEXITCODE -ne 0) { Write-Host "[ASTRA] ingestion failed" -ForegroundColor Red; exit 1 }
Write-Host "[ASTRA] running multi-agent pipeline..." -ForegroundColor Cyan
python scripts\run_pipeline.py
if ($LASTEXITCODE -ne 0) { Write-Host "[ASTRA] pipeline failed" -ForegroundColor Red; exit 1 }
Write-Host "[ASTRA] launching dashboard..." -ForegroundColor Green
streamlit run dashboard\app.py
