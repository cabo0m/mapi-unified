[CmdletBinding()]
param(
    [string]$InstallDir = "",
    [string]$InstanceRoot = "",
    [switch]$RemoveInstance,
    [string]$MaintenanceTaskName = "MAPI Aurora Memory Maintenance"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") { throw "MAPI Windows uninstall: Windows only" }
if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw "LOCALAPPDATA unavailable; pass -InstallDir" }
    $InstallDir = Join-Path $env:LOCALAPPDATA "MAPI"
}
if ([string]::IsNullOrWhiteSpace($InstanceRoot) -and -not [string]::IsNullOrWhiteSpace($HOME)) {
    $InstanceRoot = Join-Path $HOME ".mapi-agent-memory"
}
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$BinDir = Join-Path $InstallDir "bin"

$schtasks = Get-Command schtasks.exe -ErrorAction SilentlyContinue
if ($null -ne $schtasks) {
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & $schtasks.Source /Delete /F /TN $MaintenanceTaskName *> $null
    } finally {
        $ErrorActionPreference = $previous
    }
}

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not [string]::IsNullOrWhiteSpace($userPath)) {
    $normalizedBin = $BinDir.TrimEnd('\')
    $kept = @($userPath.Split(';') | Where-Object {
        -not [string]::IsNullOrWhiteSpace($_) -and
        -not $_.TrimEnd('\').Equals($normalizedBin, [StringComparison]::OrdinalIgnoreCase)
    })
    [Environment]::SetEnvironmentVariable("Path", ($kept -join ';'), "User")
}

if (Test-Path -LiteralPath $InstallDir) { Remove-Item -LiteralPath $InstallDir -Recurse -Force }
if ($RemoveInstance -and -not [string]::IsNullOrWhiteSpace($InstanceRoot)) {
    $resolved = [IO.Path]::GetFullPath($InstanceRoot)
    if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}

Write-Host "MAPI Windows program installation removed."
if (-not $RemoveInstance -and -not [string]::IsNullOrWhiteSpace($InstanceRoot)) {
    Write-Host "Instance data preserved: $InstanceRoot"
}
