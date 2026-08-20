# Installation

## Tested systems

- Windows 11 with Python 3.12.10;
- Ubuntu 24.04 under WSL2 with the Python version recorded in `PUBLIC_RELEASE_AUDIT.md`.

macOS is expected to work for the model-free core but is not verified.

## Prerequisites

- Git for source checkout;
- Python 3.11 or 3.12;
- a writable local data directory.

No model provider, API key, GPU or model download is required.
The core package includes lightweight `tzdata` so named IANA timezones remain
available when the operating system does not provide a timezone database.

## Source and development installation

```bash
git clone https://github.com/cabo0m/mapi-unified.git
cd mapi-agent-memory
python -m venv .venv
```

Activate the environment, then:

```bash
python -m pip install --upgrade pip
pip install -e .
```

For tests and lint:

```bash
pip install -e ".[dev]"
```

## Initialize a fresh instance

```bash
mapi-init
mapi-server
```

`mapi-init` is the supported day-zero path. The interactive wizard defaults to a local instance. It stores persistent runtime state outside the source checkout at `~/.mapi-agent-memory`, generates the runtime `.env`, creates data/backup/log directories, applies migrations, records only explicit neutral self-model bootstrap evidence, creates a verified SQLite-consistent first backup and runs final doctor checks. It never seeds demo data.

For automation, use `--non-interactive` plus explicit flags. For example:

```bash
mapi-init --non-interactive --mode local --agent-name MyAgent
```

For a server using the built-in single-owner OAuth login:

```bash
mapi-init --mode vps-remote-auth --public-url https://mapi.example.com --service-name polaris
```

The interactive wizard securely asks for the owner login/password and stores only a salted password hash. It does not ask the VPS operator to invent a personal assistant identity. For ChatGPT, Dynamic Client Registration supplies the OAuth client ID and callback automatically, so the customer does not enter either value manually. On the first MCP bootstrap, Polaris starts a one-question-at-a-time onboarding in which the customer chooses the assistant name, says how they want to be addressed, provides optional work context, chooses an assistant autonomy level and memory policy, can define memory exclusions, and may create a first project. The collected answers stay in draft onboarding state until a final summary is confirmed, so the customer can revise fields before durable memory is created. Optional static OAuth client/redirect settings remain available only as a compatibility fallback. Use `vps-proxy` instead only when an external proxy is intentionally responsible for authentication.

The VPS bootstrap generates `generated/<service-name>.service`, `generated/<service-name>-maintenance.service`, `generated/<service-name>-maintenance.timer`, and `generated/reverse-proxy-security-template.txt` inside the instance root. The default service name is `mapi.service`; use `--service-name polaris` (or another safe systemd name) when multiple instances or existing services share a host. On interactive Linux it offers to install the selected runtime and maintenance units immediately using `sudo` when required. For non-interactive provisioning add `--install-service`; without that flag no privileged change is attempted. The persistent maintenance timer runs nightly on the VPS itself. It requires no vendor account, SSH session, or retained VPS password after deployment. It performs verified online SQLite backup before mutation, deterministic metadata hygiene and unambiguous structural self-healing; ambiguous semantic branches are left for the connected assistant model and, only when necessary, explicit user consent. Scheduled maintenance never deletes memory content. Firewall and DNS remain separate infrastructure boundaries. In `vps-remote-auth`, the proxy is TLS/forwarding only; authentication stays inside MAPI OAuth.

After service installation MAPI waits for `127.0.0.1:<port>` and probes the MCP URL. Only after the start/probe phase does it run the final doctor report. The final JSON contains `connection.recommended_mcp_url`, `initial_backup`, and the selected system service. Public reachability is reported separately, so a missing proxy cannot be mistaken for a working public endpoint.

Use `mapi-init --resume` only to finish the same initialization. Resume is idempotent and fails if identity, project namespace, profile, port or remote-auth configuration differs from the existing `.env`. Configuration changes are deliberately not an init side effect.

After the runtime starts, run `python scripts/smoke_mcp.py`. Run `mapi-demo` separately when you want fictional product-demo data. If you choose a non-default instance root, pass `--root <path>` to runtime CLI commands such as `mapi-server`, `mapi-doctor`, `mapi-migrate`, `mapi-recover` and `mapi-seed-demo`.

## Optional extras

```bash
pip install -e ".[semantic]"
pip install -e ".[gemini]"
```

Install extras only when the feature will be configured and tested.
The semantic extra may install model libraries, but core installation and core
CI do not install or import them.

## Upgrade

1. Stop the runtime.
2. Back up the SQLite database and verify the backup.
3. Update source and environment.
4. Run `mapi-migrate`.
5. Run `mapi-doctor`.
6. Start the runtime and perform an MCP smoke test.

Migrations are forward-only. Roll back code and restore the verified pre-upgrade database if an upgrade fails.

## Uninstall and local data deletion

Stop MAPI, uninstall the package with `pip uninstall mapi-agent-memory`, then remove the virtual environment. Data is retained separately; delete the configured data directory only after confirming backups and the exact path.
