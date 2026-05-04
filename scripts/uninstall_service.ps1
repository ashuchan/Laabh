<#
.SYNOPSIS
    Uninstall the Laabh Windows service and revert the OS-level overrides
    set by install_service.ps1.

.DESCRIPTION
    Stops + removes the NSSM service, drops the powercfg requestsoverride,
    and (optionally) clears Windows Update Active Hours / restores standby
    timeouts. Runtime files under %PROGRAMDATA%\Laabh are preserved by
    default -- pass -PurgeRuntime to delete them too.

.PARAMETER PurgeRuntime
    If set, also deletes %PROGRAMDATA%\Laabh (logs, heartbeat). Off by default.

.PARAMETER RestorePowerDefaults
    If set, restores standby-timeout-ac to 30 minutes (Windows default).
    Off by default -- most workstations want sleep disabled regardless.
#>

[CmdletBinding()]
param(
    [string]$ServiceName = "Laabh",
    [string]$RuntimeDir  = (Join-Path $env:ProgramData "Laabh"),
    [switch]$PurgeRuntime,
    [switch]$RestorePowerDefaults
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $current = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($current)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "This script must be run from an elevated PowerShell prompt (Run as Administrator)."
    }
}

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

Assert-Admin

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "==> Stopping + removing service '$ServiceName'"
    # nssm stop returns non-zero when the service is already stopped -- accept that.
    Invoke-Native "nssm stop"   { nssm stop   $ServiceName confirm } -AllowedExitCodes @(0, 1, 5, 7)
    Invoke-Native "nssm remove" { nssm remove $ServiceName confirm }
} else {
    Write-Host "==> Service '$ServiceName' not present -- skipping nssm remove"
}

Write-Host "==> Clearing powercfg requestsoverride for SERVICE\$ServiceName"
Invoke-Native "powercfg requestsoverride clear" { powercfg /requestsoverride SERVICE $ServiceName }

if ($RestorePowerDefaults) {
    Write-Host "==> Restoring standby-timeout-ac (30 min)"
    Invoke-Native "powercfg standby" { powercfg /change standby-timeout-ac 30 }
    Invoke-Native "powercfg monitor" { powercfg /change monitor-timeout-ac 10 }
}

if ($PurgeRuntime) {
    if (Test-Path -LiteralPath $RuntimeDir) {
        Write-Host "==> Removing runtime tree $RuntimeDir"
        Remove-Item -LiteralPath $RuntimeDir -Recurse -Force
    }
} else {
    Write-Host "==> Preserving runtime tree at $RuntimeDir (pass -PurgeRuntime to delete)"
}

Write-Host ""
Write-Host "Done. Windows Update Active Hours were left as-is (they affect the OS, not the service)."
