<#
.SYNOPSIS
    Install Laabh as a resilient Windows service via NSSM.

.DESCRIPTION
    Wires up everything required for an unattended trading-day execution:

      - Installs/reinstalls the "Laabh" service against a chosen Python
        interpreter and project directory.
      - Sets up online log rotation (daily + 10 MB cap) so logs never
        require a service restart to roll.
      - Configures NSSM throttle + SCM recovery actions for escalating
        restart delays (5s -> 30s -> 5m, daily reset).
      - Declares a service dependency on PostgreSQL + Tcpip so cold-boot
        ordering doesn't crash-loop the scheduler.
      - Tunes NSSM stop methods so SIGBREAK has 20 s to drain in-flight jobs.
      - Disables sleep/hibernate on AC and registers the service as a
        wake-keeping component (powercfg /requestsoverride).
      - Sets Windows Update Active Hours to cover the trading window.
      - Creates %PROGRAMDATA%\Laabh\{logs,state} ahead of first start.

    Re-running this script is safe: it stops + reinstalls cleanly.

.PARAMETER PythonExe
    Absolute path to python.exe in the venv that has Laabh dependencies installed.
    Default: .\venv\Scripts\python.exe under the project root.

.PARAMETER ProjectDir
    Project root (the directory containing pyproject.toml and src\). Default:
    the parent of this script.

.PARAMETER PostgresService
    Windows service name of the local Postgres install. Default: postgresql-x64-16.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1

.EXAMPLE
    .\scripts\install_service.ps1 -PythonExe "C:\Python312\python.exe" `
                                  -ProjectDir "C:\Laabh" `
                                  -PostgresService "postgresql-x64-16"
#>

[CmdletBinding()]
param(
    [string]$ServiceName    = "Laabh",
    [string]$DisplayName    = "Laabh Trading Scheduler",
    [string]$Description    = "APScheduler + Angel One WebSocket runner for the Laabh personal trading system.",
    [string]$ProjectDir     = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExe      = $null,
    [string]$RuntimeDir     = (Join-Path $env:ProgramData "Laabh"),
    [string]$PostgresService = "postgresql-x64-16",
    [int]   $StopGraceMs    = 20000
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $current = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($current)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This script must be run from an elevated PowerShell prompt (Run as Administrator)."
    }
}

function Assert-Tool($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "$name is not on PATH. Install it (e.g. 'choco install $name' or 'scoop install $name') and re-run."
    }
}

function Test-ServiceExists($name) {
    return [bool](Get-Service -Name $name -ErrorAction SilentlyContinue)
}

# Native EXEs (nssm, sc.exe, powercfg) don't trip $ErrorActionPreference="Stop"
# on non-zero exit. Wrap each call in this helper so a silent failure surfaces
# as an exception instead of a half-installed service.
#
# Scriptblock form avoids PowerShell's parameter-binding ambiguity around
# ValueFromRemainingArguments + named parameters: just pass `{ exe arg arg }`
# and let PowerShell tokenize the call site naturally.
function Invoke-Native {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position=0)] [string] $What,
        [Parameter(Mandatory, Position=1)] [scriptblock] $Block,
        [int[]] $AllowedExitCodes = @(0)
    )
    & $Block | Out-Null
    $code = $LASTEXITCODE
    if (-not ($AllowedExitCodes -contains $code)) {
        throw "[$What] block exited with code $code"
    }
}

# ---------- pre-flight ----------
Assert-Admin
Assert-Tool "nssm"
Assert-Tool "sc.exe"
Assert-Tool "powercfg"

if (-not $PythonExe) {
    $PythonExe = Join-Path $ProjectDir "venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python interpreter not found at '$PythonExe'. Pass -PythonExe with the venv path."
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir "src\main.py"))) {
    throw "ProjectDir '$ProjectDir' does not look like the Laabh repo (no src\main.py)."
}

Write-Host "==> Project: $ProjectDir"
Write-Host "==> Python : $PythonExe"
Write-Host "==> Runtime: $RuntimeDir"

# ---------- runtime layout (off-OneDrive) ----------
$LogDir   = Join-Path $RuntimeDir "logs"
$StateDir = Join-Path $RuntimeDir "state"
foreach ($d in @($RuntimeDir, $LogDir, $StateDir)) {
    if (-not (Test-Path -LiteralPath $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
        Write-Host "    created $d"
    }
}

# ---------- stop + remove existing service ----------
if (Test-ServiceExists $ServiceName) {
    Write-Host "==> Existing service detected; stopping + removing"
    # nssm stop returns non-zero when the service is already stopped -- accept that.
    Invoke-Native "nssm stop"   { nssm stop   $ServiceName confirm } -AllowedExitCodes @(0, 1, 5, 7)
    Invoke-Native "nssm remove" { nssm remove $ServiceName confirm }
}

# ---------- install ----------
Write-Host "==> Installing service '$ServiceName'"
Invoke-Native "nssm install" { nssm install $ServiceName $PythonExe "-m src.main" }

# core
Invoke-Native "nssm set DisplayName"  { nssm set $ServiceName DisplayName  $DisplayName }
Invoke-Native "nssm set Description"  { nssm set $ServiceName Description  $Description }
Invoke-Native "nssm set AppDirectory" { nssm set $ServiceName AppDirectory $ProjectDir }
Invoke-Native "nssm set Start"        { nssm set $ServiceName Start SERVICE_AUTO_START }

# environment -- pin runtime dir so heartbeat + future state writes never
# touch the OneDrive-synced project tree
Invoke-Native "nssm set AppEnvironmentExtra" { nssm set $ServiceName AppEnvironmentExtra "LAABH_RUNTIME_DIR=$RuntimeDir" "PYTHONUNBUFFERED=1" }

# stdout/stderr with online (no-restart) rotation
$OutLog = Join-Path $LogDir "laabh.out.log"
$ErrLog = Join-Path $LogDir "laabh.err.log"
Invoke-Native "nssm set AppStdout" { nssm set $ServiceName AppStdout $OutLog }
Invoke-Native "nssm set AppStderr" { nssm set $ServiceName AppStderr $ErrLog }
Invoke-Native "nssm set AppStdoutCreationDisposition" { nssm set $ServiceName AppStdoutCreationDisposition 4 }   # OPEN_ALWAYS -- append
Invoke-Native "nssm set AppStderrCreationDisposition" { nssm set $ServiceName AppStderrCreationDisposition 4 }
Invoke-Native "nssm set AppRotateFiles"   { nssm set $ServiceName AppRotateFiles  1 }
Invoke-Native "nssm set AppRotateOnline"  { nssm set $ServiceName AppRotateOnline 1 }
Invoke-Native "nssm set AppRotateSeconds" { nssm set $ServiceName AppRotateSeconds 86400 }       # daily roll
Invoke-Native "nssm set AppRotateBytes"   { nssm set $ServiceName AppRotateBytes  10485760 }     # 10 MB

# graceful stop -- SIGBREAK with 20 s drain window, then escalate
Invoke-Native "nssm set AppStopMethodSkip"    { nssm set $ServiceName AppStopMethodSkip    0 }
Invoke-Native "nssm set AppStopMethodConsole" { nssm set $ServiceName AppStopMethodConsole $StopGraceMs }
Invoke-Native "nssm set AppStopMethodWindow"  { nssm set $ServiceName AppStopMethodWindow  5000 }
Invoke-Native "nssm set AppStopMethodThreads" { nssm set $ServiceName AppStopMethodThreads 5000 }
Invoke-Native "nssm set AppKillProcessTree"   { nssm set $ServiceName AppKillProcessTree   1 }

# NSSM-level restart throttle: a service that crashes within 60 s of start
# is considered "bad" and the AppExit policy applies.
Invoke-Native "nssm set AppExit"         { nssm set $ServiceName AppExit Default Restart }
Invoke-Native "nssm set AppRestartDelay" { nssm set $ServiceName AppRestartDelay 5000 }
Invoke-Native "nssm set AppThrottle"     { nssm set $ServiceName AppThrottle     60000 }

Write-Host "==> Configuring service dependencies (Postgres + Tcpip)"
# 'depend=' takes a /-separated list of services. The space after `=` is
# REQUIRED by sc.exe argument parsing -- preserve it via the stop-parsing
# token `--%` so PowerShell doesn't reformat the tokens.
Invoke-Native "sc.exe config depend" { sc.exe config $ServiceName depend= "$PostgresService/Tcpip" }

Write-Host "==> Configuring SCM recovery actions (5s -> 30s -> 5min)"
# SCM recovery: layered on top of NSSM. After 5 quick failures NSSM gives up;
# SCM then takes over with longer back-off windows.
Invoke-Native "sc.exe failure"     { sc.exe failure     $ServiceName reset= 86400 actions= restart/5000/restart/30000/restart/300000 }
Invoke-Native "sc.exe failureflag" { sc.exe failureflag $ServiceName 1 }

# ---------- power & sleep ----------
Write-Host "==> Disabling sleep/hibernate on AC"
Invoke-Native "powercfg standby"   { powercfg /change standby-timeout-ac    0 }
Invoke-Native "powercfg hibernate" { powercfg /change hibernate-timeout-ac  0 }
Invoke-Native "powercfg monitor"   { powercfg /change monitor-timeout-ac    0 }

Write-Host "==> Registering service as wake-keeping (powercfg /requestsoverride)"
# Suppresses the OS putting the box to sleep when the service is running.
Invoke-Native "powercfg requestsoverride" { powercfg /requestsoverride SERVICE $ServiceName SYSTEM AWAYMODE EXECUTION }

# ---------- Windows Update Active Hours (covers market window) ----------
Write-Host "==> Setting Windows Update Active Hours: 09:00 - 18:00"
$wuPath = "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
if (-not (Test-Path $wuPath)) {
    New-Item -Path $wuPath -Force | Out-Null
}
# 9 AM start, 6 PM end (covers 09:15 entry through 15:30 close + post-close jobs).
# Max active-hours window is 18 hours; we fit comfortably in 9.
New-ItemProperty -Path $wuPath -Name "ActiveHoursStart"   -PropertyType DWord -Value 9  -Force | Out-Null
New-ItemProperty -Path $wuPath -Name "ActiveHoursEnd"     -PropertyType DWord -Value 18 -Force | Out-Null
New-ItemProperty -Path $wuPath -Name "SmartActiveHoursState" -PropertyType DWord -Value 0 -Force | Out-Null
New-ItemProperty -Path $wuPath -Name "IsActiveHoursEnabled"  -PropertyType DWord -Value 1 -Force | Out-Null

# ---------- start ----------
Write-Host "==> Starting '$ServiceName'"
Invoke-Native "nssm start" { nssm start $ServiceName }

Start-Sleep -Seconds 3
$svc = Get-Service -Name $ServiceName
Write-Host ""
Write-Host "Service status: $($svc.Status)"
Write-Host "Logs:           $LogDir"
Write-Host "Heartbeat:      $StateDir\heartbeat.txt"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  - Tail logs:      Get-Content '$ErrLog' -Wait"
Write-Host "  - Status:         Get-Service $ServiceName"
Write-Host "  - Manual stop:    nssm stop $ServiceName"
Write-Host "  - Uninstall:      .\scripts\uninstall_service.ps1"
