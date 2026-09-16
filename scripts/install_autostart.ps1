# ASTRA — start the supervisor automatically when you sign in to Windows.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1            # install
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -StartNow  # install and start now
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Status    # is it installed / running
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Uninstall # remove it
#
# What it installs: a Task Scheduler task named "ASTRA", for your account only,
# that runs `pythonw -m astra.ops.supervisor` (no window) at sign-in, and
# restarts it within a minute if it ever exits with an error. It runs on battery
# too. It does not need administrator rights and changes no other setting.
#
# If Windows refuses the task, it falls back to a shortcut in your Startup
# folder, which starts the supervisor at sign-in but cannot restart it.
#
# Stopping ASTRA without uninstalling:  python -m astra.ops.supervisor --stop
param([switch]$Uninstall, [switch]$StartNow, [switch]$Status)

$ErrorActionPreference = "Stop"
$TaskName = "ASTRA"
$Root = Split-Path -Parent $PSScriptRoot
$Shortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "ASTRA.lnk"

function Get-AstraTask {
  Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

if ($Status) {
  $task = Get-AstraTask
  if ($task) {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "[ASTRA] Task '$TaskName' is installed ($($task.State)); last run $($info.LastRunTime), result $($info.LastTaskResult)."
  } elseif (Test-Path $Shortcut) {
    Write-Host "[ASTRA] Starts from the Startup folder shortcut: $Shortcut"
  } else {
    Write-Host "[ASTRA] Not installed."
  }
  Push-Location $Root
  try { python -m astra.ops.supervisor --status } finally { Pop-Location }
  exit 0
}

if ($Uninstall) {
  if (Get-AstraTask) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[ASTRA] Removed the '$TaskName' task." -ForegroundColor Green
  }
  if (Test-Path $Shortcut) {
    Remove-Item $Shortcut
    Write-Host "[ASTRA] Removed the Startup folder shortcut." -ForegroundColor Green
  }
  Push-Location $Root
  try { python -m astra.ops.supervisor --stop } finally { Pop-Location }
  exit 0
}

# The interpreter that has ASTRA's packages, and its windowless twin.
Push-Location $Root
try {
  $python = (& python -c "import sys, fastapi, pandas, uvicorn; print(sys.executable)" 2>$null)
} finally { Pop-Location }
if (-not $python) {
  Write-Host "[ASTRA] 'python' does not have ASTRA's packages (fastapi, pandas, uvicorn)." -ForegroundColor Red
  Write-Host "        Install them first:  pip install -r requirements.txt"
  exit 1
}
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }
if (-not (Test-Path (Join-Path $Root "frontend\node_modules"))) {
  Write-Host "[ASTRA] Frontend dependencies are missing; the website will not start until you run 'npm install' in frontend." -ForegroundColor Yellow
}

$user = "$env:USERDOMAIN\$env:USERNAME"
try {
  $action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m astra.ops.supervisor" -WorkingDirectory $Root
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
  $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force `
    -Description "ASTRA: keeps the MPLADS portal data, API and website running (astra.ops.supervisor)." | Out-Null
  if (Test-Path $Shortcut) { Remove-Item $Shortcut }
  Write-Host "[ASTRA] Installed the '$TaskName' task: starts at sign-in, restarts on failure." -ForegroundColor Green
  if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "[ASTRA] Started. Site: http://localhost:5173  Logs: $Root\data\logs" -ForegroundColor Green
  }
} catch {
  Write-Host "[ASTRA] Task Scheduler refused the task: $($_.Exception.Message)" -ForegroundColor Yellow
  Write-Host "[ASTRA] Using a Startup folder shortcut instead (starts at sign-in; no automatic restart of the supervisor itself)." -ForegroundColor Yellow
  $shell = New-Object -ComObject WScript.Shell
  $link = $shell.CreateShortcut($Shortcut)
  $link.TargetPath = $pythonw
  $link.Arguments = "-m astra.ops.supervisor"
  $link.WorkingDirectory = $Root
  $link.WindowStyle = 7
  $link.Description = "ASTRA supervisor"
  $link.Save()
  Write-Host "[ASTRA] Created $Shortcut" -ForegroundColor Green
  if ($StartNow) {
    Start-Process -FilePath $pythonw -ArgumentList "-m", "astra.ops.supervisor" -WorkingDirectory $Root -WindowStyle Hidden
    Write-Host "[ASTRA] Started. Site: http://localhost:5173  Logs: $Root\data\logs" -ForegroundColor Green
  }
}
Write-Host "        Before using run_dev.ps1, stop it:  python -m astra.ops.supervisor --stop"
