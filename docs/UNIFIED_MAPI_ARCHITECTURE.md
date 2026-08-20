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
