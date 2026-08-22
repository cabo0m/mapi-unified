# Installation

MAPI supports two local installations and one remote deployment path:

- **Windows local (Aurora):** MAPI and the MCP client run on the same Windows machine.
- **Linux local:** MAPI and the MCP client run on the same Linux machine.
- **Linux/VPS remote (Polaris):** MAPI runs on a Linux server behind HTTPS and its built-in OAuth owner login. Use this path for ChatGPT web.

Do not expose port `8015` directly to the Internet. Local MAPI stays on `127.0.0.1`; a remote deployment uses a TLS reverse proxy in front of that loopback listener.

## Supported systems

Verified release gates cover:

- Windows 11, Python 3.11 and 3.12;
- Ubuntu Linux, Python 3.11 and 3.12.

Docker and macOS are not release-verified. The model-free core does not require an API key, GPU, local model or model download.

## Prerequisites

You need:

- Git;
- Python 3.11 or 3.12;
- permission to create a local virtual environment;
- for a remote VPS deployment, a DNS name and HTTPS reverse proxy.

Check the tools before installing:

### Windows

```powershell
git --version
py -0p
```

If the Python launcher is unavailable but `python` is installed, verify it with:

```powershell
python --version
```

### Linux

```bash
git --version
python3 --version
```

On Ubuntu, install the basic prerequisites when needed:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
```

## Windows: local installation step by step

This is the recommended Windows source-install path.

### 1. Clone the Unified MAPI repository

```powershell
git clone https://github.com/cabo0m/mapi-unified.git
cd mapi-unified
```

### 2. Create a virtual environment

Prefer Python 3.12 when it is installed:

```powershell
py -3.12 -m venv .venv
```

Python 3.11 is also supported:

```powershell
py -3.11 -m venv .venv
```

If you do not use the Windows Python launcher:

```powershell
python -m venv .venv
```

### 3. Install MAPI

Activation is optional. The commands below call the virtual environment directly, so they also work when PowerShell activation is blocked by execution policy:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
```

### 4. Initialize a fresh local instance

```powershell
.\.venv\Scripts\mapi.exe init --mode local
```

The initializer creates the private instance under `%USERPROFILE%\.mapi-agent-memory` by default. Runtime data, backups and the generated `.env` live outside the source checkout.

### 5. Verify the instance

```powershell
.\.venv\Scripts\mapi.exe doctor
```

The command must complete without a blocking error before you connect an MCP client.

### 6. Start the MCP server

```powershell
.\.venv\Scripts\mapi.exe start
```

Keep this terminal open. The default local MCP endpoint is:

```text
http://127.0.0.1:8015/mcp/
```

### 7. Run the protocol smoke test

Open a second PowerShell window in the `mapi-unified` checkout and run:

```powershell
.\.venv\Scripts\python.exe scripts\smoke_mcp.py
```

The smoke test uses fictional data and verifies initialization, tool discovery, safe write/read flow, links, timeline access and admin denial under the safe agent profile.

### 8. Configure permanent ChatGPT tunnel autostart

After the Secure MCP Tunnel has connected successfully once, register Aurora and
the tunnel as one Windows logon task. Download and extract the current official
Windows AMD64 `tunnel-client-runtime-cloudflared` release, create the tunnel in
OpenAI Platform, and keep the resulting `tunnel_id`.

Run this once from the repository checkout:

```powershell
.\scripts\configure_windows_tunnel_autostart.ps1 `
  -TunnelId "tunnel_REPLACE_WITH_YOUR_ID" `
  -TunnelClientPath "$env:USERPROFILE\Downloads\tunnel-client-runtime-cloudflared-v0.0.12-windows-amd64\tunnel-client-runtime-cloudflared.exe"
```

The configurator asks for the OpenAI runtime API key using a hidden secure
prompt. It copies the tunnel runtime into `%LOCALAPPDATA%\MAPI\tunnel`,
protects the key with Windows DPAPI for the current Windows user, and registers
the `MAPI Aurora` Task Scheduler task. It starts the task immediately.

After this one-time step:

- Aurora starts automatically after Windows sign-in;
- the tunnel starts only after the local MCP port is reachable;
- both processes are restarted if they stop;
- the ChatGPT connection remains saved and does not need to be recreated;
- no PowerShell window needs to remain open.

Do not delete the Windows account that configured the task: DPAPI intentionally
binds the stored runtime key to that account. To rotate the key or change the
tunnel, run the configurator again with the new values.

### Optional Windows release-bundle installer

`install-windows.ps1` is intended for a release bundle that contains a built wheel and checksum file. A plain source clone does not contain that wheel. Do not run the bundle installer from a source clone unless you explicitly provide a built wheel with `-WheelPath` and understand the checksum option.

For development and evaluation, use the source-install steps above.

A Windows release bundle also installs the one-time tunnel configurator. After
the bundle installer finishes, run:

```powershell
& "$env:LOCALAPPDATA\MAPI\configure_windows_tunnel_autostart.ps1" `
  -TunnelId "tunnel_REPLACE_WITH_YOUR_ID" `
  -TunnelClientPath "<path-to-extracted-tunnel-client-runtime-cloudflared.exe>"
```

After that command succeeds, Aurora and the tunnel are managed automatically.

## Linux: local installation step by step

Use this path when MAPI and the MCP client run on the same Linux machine.

### 1. Clone the repository

```bash
git clone https://github.com/cabo0m/mapi-unified.git
cd mapi-unified
```

### 2. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install MAPI

```bash
python -m pip install --upgrade pip
python -m pip install -e .
```

### 4. Initialize a fresh local instance

```bash
mapi init --mode local
```

The default instance root is `~/.mapi-agent-memory`.

### 5. Verify the instance

```bash
mapi doctor
```

### 6. Start the MCP server

```bash
mapi start
```

Keep this terminal running. The local endpoint is:

```text
http://127.0.0.1:8015/mcp/
```

### 7. Run the protocol smoke test

In a second terminal:

```bash
cd mapi-unified
source .venv/bin/activate
python scripts/smoke_mcp.py
```

## Linux/VPS: remote installation for ChatGPT web

Use this path when ChatGPT or another remote MCP client must reach MAPI over HTTPS.

You need:

- a Linux VPS;
- a DNS name such as `mapi.example.com` pointing to that VPS;
- inbound HTTPS on port `443` and normally HTTP on `80` for certificate provisioning;
- a TLS reverse proxy such as Caddy or nginx.

MAPI itself remains bound to `127.0.0.1:8015`.

### 1. Install the source checkout

```bash
git clone https://github.com/cabo0m/mapi-unified.git
cd mapi-unified
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

### 2. Initialize remote owner authentication

Replace the example hostname with your real HTTPS hostname:

```bash
mapi init --mode vps-remote-auth --public-url https://mapi.example.com --service-name polaris
```

The interactive initializer asks for the single owner login and password. The plaintext password is not stored; MAPI stores a salted verifier. The same mode enables OAuth authorization code + PKCE, Dynamic Client Registration and refresh-token support for compatible remote MCP clients.

When prompted, allow systemd installation if you want MAPI and nightly maintenance managed by the operating system. Non-interactive provisioning can request it explicitly with `--install-service`.

### 3. Check the generated service

When `polaris` is the service name:

```bash
sudo systemctl status polaris.service
sudo systemctl status polaris-maintenance.timer
```

The exact generated unit names are also recorded by the initializer.

### 4. Configure HTTPS forwarding

The initializer writes a proxy template to:

```text
~/.mapi-agent-memory/generated/reverse-proxy-security-template.txt
```

For Caddy, the essential forwarding shape is:

```caddyfile
mapi.example.com {
    reverse_proxy 127.0.0.1:8015
}
```

The reverse proxy terminates TLS and forwards traffic only. In `vps-remote-auth` mode, do **not** add Basic Auth or inject identity headers in front of MAPI; MAPI owns the OAuth login boundary.

Do not bind MAPI to `0.0.0.0` and do not publish port `8015` through the firewall.

### 5. Verify local and remote state

```bash
mapi doctor
mapi remote status
```

The server also prints the recommended MCP URL when it starts. For this example it should be:

```text
https://mapi.example.com/mcp/
```

If public HTTPS is not ready yet, MAPI reports the configured URL without falsely claiming that it is reachable. Finish DNS/TLS/reverse-proxy configuration, then repeat the checks.

### 6. Connect the remote MCP client

Continue with [MCP integration](MCP_INTEGRATION.md), section **Linux/VPS: connect ChatGPT web to remote MAPI**.

## Using a custom instance root

All normal runtime commands automatically load the default instance. If you initialized another root, use the same root consistently:

```text
mapi doctor --root <instance-root>
mapi start --root <instance-root>
mapi recover --root <instance-root>
```

`mapi recover` is preview-first unless `--execute` is explicitly requested.

## Optional extras

The core installation is model-free. Install optional features only when you intend to configure and test them:

```bash
python -m pip install -e ".[semantic]"
python -m pip install -e ".[gemini]"
```

The semantic extra may download model dependencies. Neither extra is required for first-run MAPI or MCP connectivity.

## Upgrade

1. Stop the MAPI runtime.
2. Create and verify a SQLite backup.
3. Update the source checkout.
4. Update the virtual environment with `python -m pip install -e .`.
5. Run `mapi migrate`.
6. Run `mapi doctor`.
7. Start MAPI again.
8. Run the MCP smoke test before resuming normal use.

Migrations are forward-only. If an upgrade fails, restore matching code and the verified pre-upgrade database backup.

## Uninstall

A source installation separates code from runtime data. Removing the checkout or virtual environment does not automatically delete `~/.mapi-agent-memory` / `%USERPROFILE%\.mapi-agent-memory`.

Delete persistent instance data only after confirming the exact path and the backups you want to keep.
