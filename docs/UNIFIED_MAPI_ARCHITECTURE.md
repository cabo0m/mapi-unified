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
