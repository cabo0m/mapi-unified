[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    Write-Host "windows_install_smoke skipped: non-Windows runner"
    exit 0
}

function Query-TaskExitCode([string]$Name) {
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & schtasks.exe /Query /TN $Name *> $null
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Base = Join-Path $env:TEMP ("mapi-unified-windows-smoke-" + [guid]::NewGuid().ToString("N"))
$Out = Join-Path $Base "out"
$Extract = Join-Path $Base "extract"
$Install = Join-Path $Base "installed"
$Instance = Join-Path $Base "instance"
$TaskName = "MAPI Unified Smoke " + [guid]::NewGuid().ToString("N")
$DefaultDb = if ([string]::IsNullOrWhiteSpace($HOME)) { $null } else { Join-Path $HOME ".mapi-agent-memory\data\mapi.db" }
New-Item -ItemType Directory -Force -Path $Out, $Extract | Out-Null

$DefaultHashBefore = $null
if ($null -ne $DefaultDb -and (Test-Path -LiteralPath $DefaultDb -PathType Leaf)) {
    $DefaultHashBefore = (Get-FileHash -LiteralPath $DefaultDb -Algorithm SHA256).Hash
}

try {
    Push-Location $Root
    try {
        $bundleText = & python scripts\build_windows_bundle.py --output-dir $Out
        if ($LASTEXITCODE -ne 0) { throw "bundle build failed" }
        $bundle = ($bundleText -join "`n") | ConvertFrom-Json
        Expand-Archive -LiteralPath $bundle.bundle -DestinationPath $Extract
        $tokens = $null; $errors = $null
        [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $Extract "install-windows.ps1"), [ref]$tokens, [ref]$errors) | Out-Null
        if ($errors.Count -gt 0) { throw "installer syntax invalid" }

        & (Join-Path $Extract "install-windows.ps1") -InstallDir $Install -InstanceRoot $Instance -NoPath -MaintenanceTaskName $TaskName
        if ($LASTEXITCODE -ne 0) { throw "bundle install failed" }
        $Shim = Join-Path $Install "bin\mapi.cmd"
        if (-not (Test-Path -LiteralPath $Shim -PathType Leaf)) { throw "mapi shim missing" }
        if (-not (Test-Path -LiteralPath (Join-Path $Instance ".env") -PathType Leaf)) { throw "instance env missing" }
        if (-not (Test-Path -LiteralPath (Join-Path $Instance "data\mapi.db") -PathType Leaf)) { throw "instance database missing" }
        if ((Query-TaskExitCode $TaskName) -ne 0) { throw "maintenance task missing" }

        $version = (& $Shim version).Trim()
        if ($LASTEXITCODE -ne 0) { throw "mapi version failed" }
        $doctorText = & $Shim doctor
        if ($LASTEXITCODE -ne 0) { throw "installed doctor failed" }
        $doctor = ($doctorText -join "`n") | ConvertFrom-Json
        if ($doctor.status -eq "BLOCKED") { throw "installed doctor blocked" }

        & (Join-Path $Install "uninstall-windows.ps1") -InstallDir $Install -InstanceRoot $Instance -MaintenanceTaskName $TaskName
        if ($LASTEXITCODE -ne 0) { throw "uninstall failed" }
        if (Test-Path -LiteralPath $Install) { throw "install directory remained" }
        if (-not (Test-Path -LiteralPath $Instance -PathType Container)) { throw "instance was not preserved" }
        if ((Query-TaskExitCode $TaskName) -eq 0) { throw "maintenance task remained" }
        if ($null -ne $DefaultHashBefore) {
            if (-not (Test-Path -LiteralPath $DefaultDb -PathType Leaf)) { throw "default user database disappeared" }
            $after = (Get-FileHash -LiteralPath $DefaultDb -Algorithm SHA256).Hash
            if ($after -ne $DefaultHashBefore) { throw "default user database changed" }
        }
        Write-Host "windows_install_smoke=PASS"
        Write-Host "installed_version=$version"
        Write-Host "doctor_status=$($doctor.status)"
        Write-Host "maintenance_task_lifecycle=PASS"
        Write-Host "instance_preserved=PASS"
        Write-Host "default_user_db_unchanged=PASS"
    } finally {
        Pop-Location
    }
} finally {
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & schtasks.exe /Delete /F /TN $TaskName *> $null
        $LASTEXITCODE = 0
    } finally {
        $ErrorActionPreference = $previous
    }
    Remove-Item -LiteralPath $Base -Recurse -Force -ErrorAction SilentlyContinue
}
