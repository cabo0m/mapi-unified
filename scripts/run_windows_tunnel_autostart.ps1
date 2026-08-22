[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ConfigPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-RunnerLog([string]$Message) {
    $line = "{0} {1}" -f ([DateTimeOffset]::Now.ToString("o")), $Message
    Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8
}

function Test-LoopbackPort([Uri]$Uri) {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $pending = $client.ConnectAsync($Uri.Host, $Uri.Port)
        if (-not $pending.Wait(1000)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Quote-ProcessArgument([string]$Value) {
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

function Start-ManagedProcess([string]$FilePath, [string[]]$Arguments, [hashtable]$Environment) {
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WorkingDirectory = Split-Path -Parent $FilePath
    $psi.Arguments = (($Arguments | ForEach-Object { Quote-ProcessArgument ([string]$_) }) -join " ")
    foreach ($item in $Environment.GetEnumerator()) {
        $psi.EnvironmentVariables[$item.Key] = [string]$item.Value
    }
    return [Diagnostics.Process]::Start($psi)
}

$config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($config.schema -ne "mapi.windows_tunnel_autostart.v1") { throw "unsupported config schema" }
$script:LogPath = [string]$config.log_path
$secretPath = Join-Path (Split-Path -Parent $ConfigPath) "control-plane-api-key.dpapi"
$secure = Get-Content -LiteralPath $secretPath -Raw -Encoding ASCII | ConvertTo-SecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

$mcpUri = [Uri][string]$config.mcp_server_url
$mapiProcess = $null
$tunnelProcess = $null
Write-RunnerLog "supervisor started"

try {
    while ($true) {
        $mapiReachable = Test-LoopbackPort $mcpUri
        if (-not $mapiReachable -and ($null -eq $mapiProcess -or $mapiProcess.HasExited)) {
            Write-RunnerLog "starting MAPI"
            $mapiProcess = Start-ManagedProcess -FilePath ([string]$config.mapi_exe) -Arguments @("start") -Environment @{}
            for ($attempt = 0; $attempt -lt 30; $attempt++) {
                Start-Sleep -Seconds 1
                if (Test-LoopbackPort $mcpUri) { break }
                if ($mapiProcess.HasExited) { break }
            }
            $mapiReachable = Test-LoopbackPort $mcpUri
            if (-not $mapiReachable) {
                Write-RunnerLog "MAPI did not become reachable; retrying"
                Start-Sleep -Seconds 5
                continue
            }
        }

        if ($mapiReachable -and ($null -eq $tunnelProcess -or $tunnelProcess.HasExited)) {
            Write-RunnerLog "starting tunnel client"
            $environment = @{
                CONTROL_PLANE_API_KEY = $apiKey
                CONTROL_PLANE_TUNNEL_ID = [string]$config.tunnel_id
                MCP_SERVER_URL = [string]$config.mcp_server_url
                HEALTH_LISTEN_ADDR = "127.0.0.1:0"
            }
            $tunnelProcess = Start-ManagedProcess -FilePath ([string]$config.tunnel_client_path) -Arguments @("run") -Environment $environment
        }

        Start-Sleep -Seconds 5
    }
} finally {
    if ($null -ne $tunnelProcess -and -not $tunnelProcess.HasExited) { $tunnelProcess.Kill() }
    if ($null -ne $mapiProcess -and -not $mapiProcess.HasExited) { $mapiProcess.Kill() }
    $apiKey = $null
    Write-RunnerLog "supervisor stopped"
}
