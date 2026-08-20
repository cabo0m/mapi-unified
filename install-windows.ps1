[CmdletBinding()]
param(
    [string]$InstallDir = "",
    [string]$InstanceRoot = "",
    [string]$WheelPath = "",
    [switch]$NoPath,
    [switch]$AllowMissingChecksum,
    [switch]$SkipDoctor,
    [switch]$SkipMaintenanceTask,
    [string]$MaintenanceTaskName = "MAPI Aurora Memory Maintenance"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    throw "MAPI Windows install: $Message"
}

function Find-Python {
    $candidates = @()
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $py) {
        $candidates += [pscustomobject]@{ Executable = $py.Source; Prefix = @("-3.12") }
        $candidates += [pscustomobject]@{ Executable = $py.Source; Prefix = @("-3.11") }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        $candidates += [pscustomobject]@{ Executable = $python.Source; Prefix = @() }
    }
    foreach ($candidate in $candidates) {
        $previous = $ErrorActionPreference
        try {
            $ErrorActionPreference = "SilentlyContinue"
            & $candidate.Executable @($candidate.Prefix) -I -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" *> $null
            $code = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previous
        }
        if ($code -eq 0) { return $candidate }
    }
    Fail "Python 3.11 or newer was not found."
}

function Invoke-Python($PythonSpec, [string[]]$Arguments) {
    $all = @($PythonSpec.Prefix) + $Arguments
    & $PythonSpec.Executable @all
    if ($LASTEXITCODE -ne 0) { Fail "Python command failed with exit code $LASTEXITCODE." }
}

function Resolve-Wheel([string]$Requested) {
    if (-not [string]::IsNullOrWhiteSpace($Requested)) {
        $candidate = if ([IO.Path]::IsPathRooted($Requested)) {
            [IO.Path]::GetFullPath($Requested)
        } else {
            [IO.Path]::GetFullPath((Join-Path (Get-Location) $Requested))
        }
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { Fail "wheel not found: $candidate" }
        return Get-Item -LiteralPath $candidate
    }
    $roots = @($PSScriptRoot, (Join-Path $PSScriptRoot "dist"))
    $wheels = @()
    foreach ($root in $roots) {
        if (Test-Path -LiteralPath $root -PathType Container) {
            $wheels += @(Get-ChildItem -LiteralPath $root -Filter "mapi_agent_memory-*.whl" -File -ErrorAction SilentlyContinue)
        }
    }
    $wheels = @($wheels | Sort-Object FullName -Unique)
    if ($wheels.Count -ne 1) {
        Fail "expected exactly one mapi_agent_memory wheel. Found $($wheels.Count). Use -WheelPath when developing from source."
    }
    return $wheels[0]
}

function Verify-WheelChecksum($Wheel, [switch]$AllowMissing) {
    $candidates = @(
        (Join-Path $Wheel.Directory.FullName "SHA256SUMS.txt"),
        (Join-Path $PSScriptRoot "SHA256SUMS.txt")
    ) | Select-Object -Unique
    $checksumFile = $null
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $checksumFile = $candidate; break }
    }
    if ($null -eq $checksumFile) {
        if ($AllowMissing) { Write-Warning "SHA256SUMS.txt missing; continuing because -AllowMissingChecksum was explicit."; return }
        Fail "SHA256SUMS.txt is missing."
    }
    $expected = $null
    foreach ($line in Get-Content -LiteralPath $checksumFile) {
        if ($line -match '^([A-Fa-f0-9]{64})\s+\*?(.+)$' -and $Matches[2].Trim() -eq $Wheel.Name) {
            $expected = $Matches[1].ToLowerInvariant(); break
        }
    }
    if ($null -eq $expected) { Fail "checksum entry for $($Wheel.Name) not found" }
    $actual = (Get-FileHash -LiteralPath $Wheel.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { Fail "wheel checksum mismatch" }
}

if ($env:OS -ne "Windows_NT") { Fail "this installer is for Windows" }
if ([string]::IsNullOrWhiteSpace($InstallDir)) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { Fail "LOCALAPPDATA unavailable; pass -InstallDir" }
    $InstallDir = Join-Path $env:LOCALAPPDATA "MAPI"
}
if ([string]::IsNullOrWhiteSpace($InstanceRoot)) {
    if ([string]::IsNullOrWhiteSpace($HOME)) { Fail "HOME unavailable; pass -InstanceRoot" }
    $InstanceRoot = Join-Path $HOME ".mapi-agent-memory"
}
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$InstanceRoot = [IO.Path]::GetFullPath($InstanceRoot)
$Wheel = Resolve-Wheel $WheelPath
Verify-WheelChecksum $Wheel -AllowMissing:$AllowMissingChecksum
$Python = Find-Python

$VenvDir = Join-Path $InstallDir "venv"
$BinDir = Join-Path $InstallDir "bin"
New-Item -ItemType Directory -Force -Path $InstallDir, $BinDir, $InstanceRoot | Out-Null
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    Invoke-Python $Python @("-I", "-m", "venv", $VenvDir)
}
& $VenvPython -m pip install --disable-pip-version-check --upgrade $Wheel.FullName
if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }
& $VenvPython -m pip check
if ($LASTEXITCODE -ne 0) { Fail "installed dependencies are inconsistent" }

$MAPIExe = Join-Path $VenvDir "Scripts\mapi.exe"
$InitExe = Join-Path $VenvDir "Scripts\mapi-init.exe"
$MigrateExe = Join-Path $VenvDir "Scripts\mapi-migrate.exe"
$DoctorExe = Join-Path $VenvDir "Scripts\mapi-doctor.exe"
foreach ($required in @($MAPIExe, $InitExe, $MigrateExe, $DoctorExe)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { Fail "installed package missing entrypoint: $required" }
}

$BatchRoot = $InstanceRoot.Replace("%", "%%").Replace("^", "^^")
$Shim = Join-Path $BinDir "mapi.cmd"
$ShimBody = "@echo off`r`nset `"MAPI_ROOT=$BatchRoot`"`r`nset `"MAPI_ENV_FILE=$BatchRoot\.env`"`r`n`"%~dp0..\venv\Scripts\mapi.exe`" %*`r`n"
[IO.File]::WriteAllText($Shim, $ShimBody, [Text.Encoding]::ASCII)

$MaintenanceShim = Join-Path $BinDir "mapi-maintenance.cmd"
$MaintenanceBody = "@echo off`r`nset `"MAPI_ROOT=$BatchRoot`"`r`nset `"MAPI_ENV_FILE=$BatchRoot\.env`"`r`n`"%~dp0..\venv\Scripts\python.exe`" -m mapi.maintenance --root `"$BatchRoot`" --apply-safe-metadata --json`r`n"
[IO.File]::WriteAllText($MaintenanceShim, $MaintenanceBody, [Text.Encoding]::ASCII)

$UninstallerSource = Join-Path $PSScriptRoot "uninstall-windows.ps1"
if (Test-Path -LiteralPath $UninstallerSource -PathType Leaf) {
    Copy-Item -LiteralPath $UninstallerSource -Destination (Join-Path $InstallDir "uninstall-windows.ps1") -Force
}

$EnvFile = Join-Path $InstanceRoot ".env"
if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    & $InitExe --root $InstanceRoot --mode local --owner-key owner --agent-subject-key agent --agent-name Agent --agent-project-key agent-self --profile agent --non-interactive --no-verify-endpoint
    if ($LASTEXITCODE -ne 0) { Fail "fresh MAPI instance initialization failed" }
} else {
    Write-Host "Existing MAPI instance preserved: $InstanceRoot"
}
& $MigrateExe --root $InstanceRoot
if ($LASTEXITCODE -ne 0) { Fail "database migration failed" }

if (-not $NoPath) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @()
    if (-not [string]::IsNullOrWhiteSpace($userPath)) {
        $parts = @($userPath.Split(';') | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    }
    $normalizedBin = $BinDir.TrimEnd('\')
    $present = $false
    foreach ($part in $parts) {
        if ($part.TrimEnd('\').Equals($normalizedBin, [StringComparison]::OrdinalIgnoreCase)) { $present = $true; break }
    }
    if (-not $present) {
        $newPath = if ($parts.Count -gt 0) { ($parts + $BinDir) -join ';' } else { $BinDir }
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Write-Host "Added MAPI to user PATH: $BinDir"
    }
}

if (-not $SkipMaintenanceTask) {
    $schtasks = Get-Command schtasks.exe -ErrorAction SilentlyContinue
    if ($null -eq $schtasks) {
        Write-Warning "Task Scheduler unavailable; nightly maintenance was not registered."
    } else {
        $taskCommand = ('"{0}"' -f $MaintenanceShim)
        & $schtasks.Source /Create /F /SC DAILY /ST 03:17 /TN $MaintenanceTaskName /TR $taskCommand *> $null
        if ($LASTEXITCODE -ne 0) { Fail "nightly maintenance task could not be registered" }
        Write-Host "Registered nightly MAPI maintenance task: $MaintenanceTaskName"
    }
}

if (-not $SkipDoctor) {
    $previousRoot = $env:MAPI_ROOT
    $previousEnv = $env:MAPI_ENV_FILE
    try {
        $env:MAPI_ROOT = $InstanceRoot
        $env:MAPI_ENV_FILE = $EnvFile
        & $DoctorExe --root $InstanceRoot
        if ($LASTEXITCODE -ne 0) { Fail "doctor reported a blocked installation" }
    } finally {
        if ($null -eq $previousRoot) { Remove-Item Env:MAPI_ROOT -ErrorAction SilentlyContinue } else { $env:MAPI_ROOT = $previousRoot }
        if ($null -eq $previousEnv) { Remove-Item Env:MAPI_ENV_FILE -ErrorAction SilentlyContinue } else { $env:MAPI_ENV_FILE = $previousEnv }
    }
}

Write-Host ""
Write-Host "MAPI installed for Windows."
Write-Host "Program: $InstallDir"
Write-Host "Instance: $InstanceRoot"
Write-Host "Open a new terminal and run: mapi doctor"
