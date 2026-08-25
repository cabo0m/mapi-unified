# Configuration

The authoritative neutral template is [`.env.example`](../.env.example). `mapi-init` generates the effective private `.env` for a new instance, normally under `~/.mapi-agent-memory`. Runtime commands load that file automatically; explicit process environment variables take precedence.

| Variable | Default | Required | Security impact |
|---|---|---:|---|
| `MAPI_INSTANCE_ROOT` | `~/.mapi-agent-memory` | no | Discovery root for the generated instance `.env` |
| `MAPI_ROOT` | instance root | no | Runtime root used for relative paths and generated state |
| `MAPI_REPOSITORY_ROOT` | detected source checkout | no | Git/freshness source root when instance data lives outside the checkout |
| `MAPI_DATA_DIR` | `<root>/data` | no | Contains private agent data |
| `MAPI_DB_PATH` | `./data/mapi.db` | no | Direct read access exposes memories |
| `MAPI_RUNTIME_HOST` | `127.0.0.1` | no | Non-loopback binds require auth and TLS |
| `MAPI_RUNTIME_PORT` | `8015` | no | Ensure the port is not publicly exposed |
| `MAPI_SYSTEMD_SERVICE_NAME` | `mapi.service` | no | Persisted VPS systemd unit name; choose a unique name on shared hosts |
| `MCP_SURFACE_PROFILE` | `agent` | no | Higher profiles expose more mutations |
| `MAPI_ADMIN_TOOLS_ENABLED` | `false` | no | Must be true before `admin` is effective |
| `MAPI_FILES_ENABLED` | `false` | no | Enables guarded file browsing under configured roots |
| `MAPI_FILE_ROOTS` | empty | when files enabled | Allowed read roots; use one parent workspace root where practical |
| `MAPI_FILE_READ_MAX_BYTES` | `262144` | no | Per-file text read limit |
| `MAPI_FILE_WRITE_ENABLED` | `false` | no | Separately enables guarded file writes |
| `MAPI_FILE_WRITE_ROOTS` | empty | when writes enabled | Writable roots; must also be listed in read roots |
| `MAPI_FILE_WRITE_MAX_BYTES` | `262144` | no | Per-file guarded write limit |
| `MAPI_FILE_PROJECT_ROOTS_JSON` | `{}` | no | Optional project-to-root bindings; `*` means every project key |
| `MAPI_FILE_PROJECT_WRITE_ROOTS_JSON` | `{}` | when writes enabled | Project-to-write-root bindings; `*` means every project key |
| `MAPI_REMOTE_OWNER_LOGIN` | `owner` | vps-remote-auth | Login shown by the built-in OAuth owner page |
| `MAPI_REMOTE_OWNER_PASSWORD_HASH` | generated PBKDF2 hash | vps-remote-auth | Password verifier only; never store the plaintext owner password |
| `MAPI_OWNER_KEY` | `owner` | no | Single-instance identity namespace |
| `MAPI_AGENT_SUBJECT_KEY` | `agent` | no | Stable subject key for Agent Self Model |
| `MAPI_AGENT_DISPLAY_NAME` | `Agent` | no | Display label only; not authorization |
| `MAPI_AGENT_PROJECT_KEY` | `agent-self` | no | Dedicated self-evidence namespace; keep separate from customer projects |
| `MAPI_SEMANTIC_ENABLED` | `false` | no | May trigger optional model use |
| `MAPI_EMBEDDING_MODEL` | MiniLM example | no | First use may download model files |
| `GEMINI_API_KEY` | empty | no | Secret; never commit |
| `MAPI_GEMINI_ENABLED` | `false` | no | Enables external provider eligibility |
| `MAPI_LOCAL_MODEL_ENABLED` | `false` | no | Enables local provider eligibility |
| `MAPI_LOCAL_MODEL_URL` | loopback | no | Do not point at untrusted endpoints |
| `MAPI_LOG_DIR` | `<root>/logs` | no | Runtime log destination; protect as operational data |
| `MAPI_LOG_LEVEL` | `INFO` | no | Debug logs may contain operational context |
| `MAPI_BACKUP_DIR` | `./backups` | no | Protect like the primary database |
| `MAPI_REQUEST_TIMEOUT_SECONDS` | `30` | no | Limits optional outbound provider waits |
| `MAPI_RECOVERY_COMMAND_JSON` | empty | no | Explicit local JSON argv for `mapi-recover --execute`; never shell-evaluated |

Environment variables are process configuration, not authorization. A payload field can never grant a higher profile.

## First-run configuration

Every initialized instance has a deterministic MCP URL: `http://127.0.0.1:<port>/mcp/` locally, or `<public HTTPS origin>/mcp/` for VPS modes. `mapi-init` and `mapi-server` print the recommended URL. A reported public URL is considered verified only when the endpoint probe succeeds. First-run creates a verified `mapi-initial-*.db` backup before the final doctor report; `--resume` verifies and reuses that artifact instead of creating duplicates.

`mapi-init` owns instance creation. Its generated file is private state and must not be committed. The default instance root is outside the source checkout. Local bootstrap may explicitly enable the admin surface with `--profile admin`. In `vps-remote-auth` mode the deployment is intentionally single-owner: the authenticated owner uses profile `admin`, `MAPI_ADMIN_TOOLS_ENABLED=true`, and there is no second remote login path. Resume never acts as an implicit configuration editor.

The generated self-model records are operational evidence from the operator's explicit configuration: one identity record and one namespace-separation guardrail. They are not demo memories or inferred personality traits.

## Project file access

The simplest setup is to expose one parent workspace directory instead of registering every project directory separately. Edit the initialized instance environment file, normally `%USERPROFILE%\.mapi-agent-memory\.env` on Windows or `~/.mapi-agent-memory/.env` on Linux, then restart MAPI.

For read-only access, two settings are enough.

Windows:

```dotenv
MAPI_FILES_ENABLED=true
MAPI_FILE_ROOTS=C:\Projects
```

Linux:

```dotenv
MAPI_FILES_ENABLED=true
MAPI_FILE_ROOTS=/home/name/projects
```

Every child directory under that root is then available through the guarded file capability. Protected locations and secret-like files such as `.git`, `.ssh`, `.env`, private keys and credential files remain blocked even when their parent directory is allowed.

For a trusted single-user instance that should also edit files, enable guarded writes and use the explicit wildcard project binding `*`. This means "all project keys in this MAPI instance" and avoids maintaining one JSON entry per project.

Windows:

```dotenv
MAPI_FILES_ENABLED=true
MAPI_FILE_ROOTS=C:\Projects
MAPI_FILE_WRITE_ENABLED=true
MAPI_FILE_WRITE_ROOTS=C:\Projects
MAPI_FILE_PROJECT_ROOTS_JSON={"*":["C:/Projects"]}
MAPI_FILE_PROJECT_WRITE_ROOTS_JSON={"*":["C:/Projects"]}
```

Linux:

```dotenv
MAPI_FILES_ENABLED=true
MAPI_FILE_ROOTS=/home/name/projects
MAPI_FILE_WRITE_ENABLED=true
MAPI_FILE_WRITE_ROOTS=/home/name/projects
MAPI_FILE_PROJECT_ROOTS_JSON={"*":["/home/name/projects"]}
MAPI_FILE_PROJECT_WRITE_ROOTS_JSON={"*":["/home/name/projects"]}
```

Use forward slashes inside the JSON value on Windows to avoid backslash escaping. If more than one root is needed, separate `MAPI_FILE_ROOTS` and `MAPI_FILE_WRITE_ROOTS` entries with `;` on Windows or `:` on Linux. Exact project bindings can still be used instead of `*` when stronger project isolation is required.

After restart, open the `files` workshop and run `list_file_roots` for a quick verification. File writes remain guarded by the existing preview/apply/audit/rollback contract; adding a root does not bypass protected-path checks.

## Remote authentication status

Remote authentication is separate from the local quickstart. The supported public model is intentionally simple: one owner, one OAuth login path, one resulting `admin` profile. A fresh `vps-remote-auth` instance does not preassign a human-facing assistant identity: Polaris is the product/runtime label, while the personal assistant name is chosen by the user during the persistent `polaris_onboarding.v2` flow. The onboarding is ordered, one question at a time and resumable across chats. It includes an assistant autonomy preference and a final review step. Answers remain draft onboarding state until the user confirms the summary; revisions before confirmation update only the draft. The confirmed profile is then committed into the existing self/user memory model rather than a parallel knowledge store. Polaris/MAPI renders the owner login directly at `/authorize` and verifies the owner password against a salted PBKDF2 hash stored in the private instance `.env`; there is no intermediate login session or challenge record. Dynamic Client Registration is enabled for HTTPS callbacks under `https://chatgpt.com/connector/oauth/`, so ChatGPT can obtain its client ID and register its callback automatically. Static OAuth client/redirect values are optional compatibility fallback only. The plaintext password is never stored. The reverse proxy only provides HTTPS and forwarding. Ordinary payload fields cannot raise privileges. Legacy Codex bearer issuance is retired and ignored by the active remote-auth provider.

## Automatic memory self-healing

VPS installations created with service installation enabled include a local maintenance pair named from the runtime service, for example `polaris-maintenance.service` and `polaris-maintenance.timer`. The timer is persistent and runs nightly on the customer's server. It does not call back to the service provider and does not require the provider to retain VPS credentials after handoff.

The unattended path is deliberately bounded. It may create a verified SQLite backup, normalize deterministic memory metadata, complete an unambiguous half-supersession, remove dangling or cross-boundary lineage metadata, run integrity diagnostics, and maintain an internal self-healing queue. It does not delete memory content, silently choose between competing semantic truths, auto-apply retention, or merge ambiguous memories. Multiple replacement heads and lineage cycles are instead exposed to the connected assistant model as bounded evidence. The model may prepare a canonical repair proposal; applying a semantic choice requires explicit user consent and another verified backup. Non-canonical content is archived as history rather than deleted. Healthy runs are silent.

Maintenance reports are stored under the instance log directory in `maintenance/`; maintenance backups use the `mapi-maintenance-*.db` prefix and are pruned independently from other backups.

## Agent Self Model

`MAPI_AGENT_SUBJECT_KEY`, `MAPI_AGENT_DISPLAY_NAME` and `MAPI_AGENT_PROJECT_KEY` configure the neutral, read-only self-model surfaces. The self-model is evidence-first: ordinary project notes are not promoted into identity merely because their text sounds personal. Explicit self evidence should live in the dedicated self project and use identity/autobiographical layers or explicit subject/self-model tags.

The self-model can feed Context Engine and source-bound Gravity. Those readers never create durable truth, relations, importance changes or recall changes on their own.

## Controlled Self Narrative

The deterministic self narrative uses only allowlisted claims derived from Agent Self Model evidence. With `provider=gemini`, the external model may only select known claim IDs through a strict JSON schema. It cannot return source memory IDs or prose; final text is rendered locally from approved claims. Provider calls are stateless (`store=false`) and use no tools or background execution.
