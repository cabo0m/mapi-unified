[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^tunnel_[A-Za-z0-9]+$')]
    [string]$TunnelId,

    [Parameter(Mandatory = $true)]
    [string]$TunnelClientPath,

    [string]$MapiExe = "",
    [string]$McpServerUrl = "http://127.0.0.1:8015/mcp/",
    [string]$TaskName = "MAPI Aurora",
    [switch]$DoNotStart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Fail([string]$Message) {
    throw "MAPI Aurora autostart: $Message"
}

if ($env:OS -ne "Windows_NT") { Fail "Windows only" }
if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { Fail "LOCALAPPDATA unavailable" }

if ([string]::IsNullOrWhiteSpace($MapiExe)) {
    $candidates = @(
        (Join-Path $PSScriptRoot "venv\Scripts\mapi.exe"),
        (Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\Scripts\mapi.exe")
    )
    $MapiExe = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($MapiExe)) {
        Fail "MAPI executable not found; pass -MapiExe explicitly"
    }
}
$TunnelClientPath = [IO.Path]::GetFullPath($TunnelClientPath)
$MapiExe = [IO.Path]::GetFullPath($MapiExe)
if (-not (Test-Path -LiteralPath $TunnelClientPath -PathType Leaf)) {
    Fail "tunnel client not found: $TunnelClientPath"
}
if (-not (Test-Path -LiteralPath $MapiExe -PathType Leaf)) {
    Fail "MAPI executable not found: $MapiExe"
}
if (-not [Uri]::IsWellFormedUriString($McpServerUrl, [UriKind]::Absolute)) {
    Fail "invalid MCP server URL"
}
$uri = [Uri]$McpServerUrl
if ($uri.Scheme -ne "http" -or -not @("127.0.0.1", "localhost", "::1").Contains($uri.Host)) {
    Fail "Windows tunnel autostart requires a loopback HTTP MCP URL"
}

$RuntimeDir = Join-Path $env:LOCALAPPDATA "MAPI\tunnel"
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

$ManagedClient = Join-Path $RuntimeDir "tunnel-client-runtime-cloudflared.exe"
Copy-Item -LiteralPath $TunnelClientPath -Destination $ManagedClient -Force

$RunnerSource = Join-Path $PSScriptRoot "run_windows_tunnel_autostart.ps1"
if (-not (Test-Path -LiteralPath $RunnerSource -PathType Leaf)) {
    Fail "runner script missing: $RunnerSource"
}
$ManagedRunner = Join-Path $RuntimeDir "run_windows_tunnel_autostart.ps1"
Copy-Item -LiteralPath $RunnerSource -Destination $ManagedRunner -Force

Write-Host "Paste the OpenAI runtime API key. It will be stored with Windows DPAPI for this user."
$Secret = Read-Host "Runtime API key" -AsSecureString
$SecretPath = Join-Path $RuntimeDir "control-plane-api-key.dpapi"
$Secret | ConvertFrom-SecureString | Set-Content -LiteralPath $SecretPath -Encoding ASCII

$ConfigPath = Join-Path $RuntimeDir "aurora-tunnel.json"
$config = [ordered]@{
    schema = "mapi.windows_tunnel_autostart.v1"
    tunnel_id = $TunnelId
    tunnel_client_path = $ManagedClient
    mapi_exe = $MapiExe
    mcp_server_url = $McpServerUrl
    log_path = (Join-Path $RuntimeDir "aurora-tunnel.log")
}
$config | ConvertTo-Json | Set-Content -LiteralPath $ConfigPath -Encoding UTF8

$PowerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$TaskCommand = ('"{0}" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{1}" -ConfigPath "{2}"' -f $PowerShell, $ManagedRunner, $ConfigPath)
& schtasks.exe /Create /F /SC ONLOGON /RL LIMITED /TN $TaskName /TR $TaskCommand *> $null
if ($LASTEXITCODE -ne 0) { Fail "Task Scheduler registration failed" }

if (-not $DoNotStart) {
    & schtasks.exe /Run /TN $TaskName *> $null
    if ($LASTEXITCODE -ne 0) { Fail "task was registered but could not be started" }
}

Write-Host ""
Write-Host "Aurora autostart configured."
Write-Host "Task: $TaskName"
Write-Host "MAPI and the Secure MCP Tunnel will start automatically after Windows sign-in."
Write-Host "The ChatGPT connection does not need to be created again."
