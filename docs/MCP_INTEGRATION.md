# MCP integration

MAPI exposes Streamable HTTP MCP. The correct connection path depends on where the client runs.

| Client location | MAPI location | Endpoint | Supported path |
|---|---|---|---|
| Codex or another MCP client on Windows | same Windows machine | `http://127.0.0.1:8015/mcp/` | local |
| Codex or another MCP client on Linux | same Linux machine | `http://127.0.0.1:8015/mcp/` | local |
| ChatGPT web | same Windows machine through Secure MCP Tunnel | private `http://127.0.0.1:8015/mcp/` behind the tunnel | local private tunnel |
| ChatGPT web | Linux/VPS or another remotely reachable host | `https://<your-host>/mcp/` | remote HTTPS + OAuth |

ChatGPT does not directly connect to a localhost MCP server. On Windows, OpenAI Secure MCP Tunnel provides the supported private path without exposing port `8015`. On a VPS, use `vps-remote-auth` behind HTTPS. Never rebind the local listener to the public network.

The MAPI protocol smoke test is:

```text
python scripts/smoke_mcp.py
```

It verifies the HTTP MCP initialize/list/call flow, bootstrap, fictional write/read behavior, links, timeline access and safe profile boundaries.

## Windows: connect a local MCP client

First install and start MAPI using the Windows steps in [Installation](INSTALLATION.md).

The server terminal should report:

```text
MAPI MCP address: http://127.0.0.1:8015/mcp/
```

### Codex on Windows

The user-level Codex configuration is normally:

```text
%USERPROFILE%\.codex\config.toml
```

Add:

```toml
[mcp_servers.mapi]
url = "http://127.0.0.1:8015/mcp/"
```

Then:

1. keep `mapi start` running;
2. restart or reload Codex;
3. run `codex mcp list` or use `/mcp`;
4. confirm that MAPI tools are present;
5. begin with `bootstrap_agent_context` for the project you want to work on;
6. search with `find_memories` before creating another durable memory.

A normal agent profile should expose memory tools such as `find_memories`, `get_memory`, `get_memory_links`, `save_memory` and `propose_memory` while keeping dangerous admin operations outside the ordinary local-agent surface.

### Generic MCP client on Windows

Clients that accept JSON-style MCP configuration commonly use a shape like:

```json
{
  "mcpServers": {
    "mapi": {
      "url": "http://127.0.0.1:8015/mcp/",
      "transport": "http"
    }
  }
}
```

Client-specific field names may differ. The interoperable contract is the HTTP MCP endpoint URL.

## Windows: connect ChatGPT once through Secure MCP Tunnel

Complete the Windows installation and confirm that
`http://127.0.0.1:8015/mcp/` works locally. Then:

1. create a tunnel in OpenAI Platform and associate it with the intended
   ChatGPT workspace;
2. create a runtime API key for `tunnel-client`;
3. download the official Windows AMD64
   `tunnel-client-runtime-cloudflared` bundle;
4. run `scripts/configure_windows_tunnel_autostart.ps1` once;
5. in ChatGPT developer mode, create the MAPI connection with
   **Connection = Tunnel**, select the tunnel, and choose no MCP-side OAuth for
   a local no-auth MAPI instance.

The configurator registers a per-user Windows logon task. On every later sign-in
it starts Aurora, waits for port `8015`, starts the tunnel client, and supervises
both processes. The ChatGPT connection is persistent: the user does not repeat
these steps after reboot.

The runtime API key is not written to the task command, JSON configuration,
repository or plaintext environment file. It is stored with Windows DPAPI and
decrypted only inside the current user's background task.

## Linux: connect a local MCP client

First install and start MAPI using the Linux local steps in [Installation](INSTALLATION.md).

The endpoint is:

```text
http://127.0.0.1:8015/mcp/
```

### Codex on Linux

Edit:

```text
~/.codex/config.toml
```

Add:

```toml
[mcp_servers.mapi]
url = "http://127.0.0.1:8015/mcp/"
```

Then:

1. keep `mapi start` running;
2. restart or reload Codex;
3. run `codex mcp list` or use `/mcp`;
4. confirm that the MAPI tool list is visible;
5. call `bootstrap_agent_context` with the intended `project_key`;
6. search before writing and inspect full records before relying on them.

### Generic MCP client on Linux

Use the same endpoint and HTTP transport:

```json
{
  "mcpServers": {
    "mapi": {
      "url": "http://127.0.0.1:8015/mcp/",
      "transport": "http"
    }
  }
}
```

## Linux/VPS: connect ChatGPT web to remote MAPI

This is the supported MAPI path when ChatGPT must reach the server over the network.

Before configuring ChatGPT, complete the `vps-remote-auth` installation and HTTPS reverse-proxy steps in [Installation](INSTALLATION.md). You need a working endpoint such as:

```text
https://mapi.example.com/mcp/
```

The MAPI origin itself should still listen only on `127.0.0.1:8015`.

### 1. Verify the MAPI remote-auth state

On the VPS:

```bash
mapi doctor
mapi remote status
```

MAPI remote auth supports authorization code + PKCE, Dynamic Client Registration and refresh tokens. The advertised OAuth scopes include `offline_access` so compatible clients can maintain authorization without storing the owner's plaintext password.

### 2. Confirm ChatGPT plan and developer-mode availability

OpenAI's current ChatGPT documentation should be treated as authoritative because plan and UI availability can change.

As checked on 2026-08-22:

- custom MCP apps are configured in ChatGPT web;
- full MCP including write/modify actions is available to Business and Enterprise/Edu workspaces in the current beta;
- Pro can connect custom MCP apps with read/fetch permissions in developer mode, but full write MCP is not currently available there;
- ChatGPT connects to remote MCP servers, not directly to localhost.

Current OpenAI guidance:

- <https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta>

### 3. Enable developer mode in ChatGPT

The exact menu depends on the workspace role and plan.

Current OpenAI paths include:

- **Business admin/owner:** User Settings -> Apps -> Advanced settings -> Developer mode, or Workspace settings -> Apps -> Create;
- **Enterprise/Edu:** the workspace administrator grants Connected Data developer access, then an enabled user can turn on Settings -> Apps -> Advanced Settings -> Developer mode.

If the required options are absent, check the current OpenAI documentation rather than changing MAPI networking to work around a plan/UI limitation.

### 4. Create the custom MCP app

In ChatGPT web:

1. open **Settings -> Apps -> Create** or the corresponding workspace **Apps -> Create** screen;
2. provide a name such as `MAPI`;
3. set the MCP endpoint to your public URL, for example `https://mapi.example.com/mcp/`;
4. choose OAuth authentication when prompted;
5. select **Scan Tools**;
6. complete the MAPI owner-login authorization page;
7. wait for the tool scan to finish;
8. select **Create**.

MAPI supports Dynamic Client Registration for the ChatGPT callback flow, so the normal path does not require the user to manually invent an OAuth Client ID or callback URL.

### 5. Test the app in a new chat

Open a new ChatGPT conversation and select the draft/custom MAPI app from the tools/apps menu.

On a fresh remote MAPI instance, bootstrap begins the guided onboarding flow. The user chooses the assistant's personal name, preferred user name, optional work context, autonomy preference, memory behavior and optional exclusions before the profile is committed to durable memory.

After onboarding, the normal memory workflow is:

1. bootstrap the intended project;
2. search existing memories;
3. read the selected record and its links;
4. use an explicit durable write only when authorized;
5. use proposal/review flow for uncertain agent-generated material;
6. inspect timeline/audit evidence for consequential changes.

### 6. Do not add a second authentication layer in front of MAPI OAuth

For `vps-remote-auth`, the TLS reverse proxy only forwards traffic. Do not add Basic Auth or trusted identity headers in front of the MAPI OAuth endpoints. Doing so can break the OAuth flow and creates two competing identity boundaries.

Do not share the single owner login. For non-interactive automation, use MAPI's explicit revocable service-token mechanism instead of reusing the interactive owner password.

## Read and write contract

The ordinary project-memory sequence is:

- bootstrap: `bootstrap_agent_context`;
- search: `find_memories`;
- inspect: `get_memory` and `get_memory_links`;
- explicit durable write: `save_memory`;
- uncertain material: `propose_memory`;
- lifecycle changes: preview, retain the returned preview hash, then apply only with the required authorization;
- audit: inspect current state, links, timeline and run records.

A client must not infer that a mutation succeeded from a timeout or disconnected response.

## Profiles and privilege boundaries

`reader` is read-only. `agent` enables ordinary explicit memory/proposal workflows. `maintainer` enables controlled maintenance. `admin` exposes dangerous local/operator capabilities and is separately gated.

In the supported single-owner `vps-remote-auth` deployment, the authenticated owner maps to the admin profile by design. That deployment assumes the remote owner is the machine/operator owner. Do not treat it as a multi-user SaaS authorization model.

## Troubleshooting connection failures

### The local client cannot see MAPI

Check:

```text
mapi doctor
```

Confirm that `mapi start` is still running and that the client URL is exactly:

```text
http://127.0.0.1:8015/mcp/
```

Then run the protocol smoke from the source checkout.

### ChatGPT cannot scan the remote tools

Check, in this order:

1. DNS resolves to the intended VPS;
2. HTTPS certificate is valid;
3. reverse proxy forwards to `127.0.0.1:8015` and preserves streaming;
4. `mapi remote status` reports a valid enabled configuration;
5. the public MCP URL ends with `/mcp/`;
6. the ChatGPT plan/workspace currently permits the required custom MCP capability;
7. OAuth authorization completes successfully.

Do not solve a scan failure by exposing port `8015` publicly or disabling authentication.
