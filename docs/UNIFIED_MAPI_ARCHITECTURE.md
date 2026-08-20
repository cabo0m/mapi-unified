# Unified MAPI architecture

This repository is the integration line for one MAPI codebase on Windows and Linux.

## Layers

- `mapi_core/` ? platform-neutral memory, retrieval, identity/self-model, lifecycle, governance primitives, onboarding and neutral provider contracts.
- `mapi_platform/windows/` ? Windows-specific service/scheduler/shell/install adapters.
- `mapi_platform/linux/` ? Linux/VPS-specific systemd, service and deployment adapters.
- `app/` and `mapi/` ? transitional runtime/public compatibility surface while callers are migrated to the canonical packages.

## Dependency rule

`mapi_core` must never import `mapi_platform`, `mapi`, or platform-specific service modules. Platform adapters may import `mapi_core`. Runtime composition may import both.

## Product mapping

- Aurora = MAPI Core + Windows adapter.
- Polaris = MAPI Core + Linux/VPS adapter.

The goal is behavioral identity of the core, not duplicated implementations. New cross-platform features belong in `mapi_core` first.

## Shared host capabilities

Files, Git and fixed command recipes are shared capabilities, not Windows features. They use platform adapters only for the small process-lifecycle differences required by command execution. Their audit ledgers live in the same SQLite database through migrations 0037–0040.

## Windows distribution

The Windows distribution uses `install-windows.ps1`, a private venv under `%LOCALAPPDATA%\MAPI`, the same `~/.mapi-agent-memory` instance layout as the cross-platform runtime, and a Windows Task Scheduler maintenance job. It does not use the legacy Aurora JSON configuration or a separate memory schema.

## Administrative shell

The canonical admin action is `run_shell`. It remains an explicit R3/admin host-level grant. Windows executes it through PowerShell; Linux executes it through bash/sh. `run_powershell` remains a Windows-only compatibility alias and returns `powershell_windows_only` on Linux.

## Remote authentication

Interactive owner access uses OAuth2 authorization code + PKCE with the built-in owner login. For non-interactive automation the operator may explicitly issue a revocable `service` bearer token. Service tokens are admin-profile, stored only as hashes, expire, are rate-limited and can be revoked by fingerprint. The retired legacy Codex bearer path remains disabled, and trusted-proxy identity auth is not restored.

## Distribution gate

The release wheel must contain `mapi_core`, `mapi_capabilities`, both platform adapter trees, the retrieval corpus and all unified CLI entry points. CI runs `scripts/check_unified_wheel.py` on Linux and an isolated Windows bundle install/doctor/uninstall smoke on Windows.

## Distribution identity

`mapi_core` is product-neutral. User-facing distribution identity comes from `mapi_platform.identity`: Aurora on Windows and Polaris on Linux by default, overridable with `MAPI_DISTRIBUTION_NAME`. Historical database identifiers such as migration `0035_polaris_onboarding` and table `polaris_onboarding` remain unchanged for migration compatibility; they are storage history, not product branding.

## Legacy transition

Polaris databases upgrade in place through the normal migration sequence. Legacy Aurora databases use `mapi import-aurora`: preview first, then `--apply --expected-preview-hash <hash>`. The importer requires a fresh Unified target except for neutral `mapi-init` seed memories, takes a verified target backup, remaps memory IDs, translates onboarding, and preserves incompatible operational history in a legacy archive. Remote credentials, embeddings and ephemeral runtime state are deliberately not imported. Health/financial memories are quarantined; credential/private-key/never-store payloads are not copied into the target and remain only in the untouched source database.
