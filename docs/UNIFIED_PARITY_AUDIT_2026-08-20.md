# Unified MAPI 0.3.0a1 parity audit - 2026-08-20

## Scope

This document closes the first consolidation of the former public Windows **Aurora** line and Linux/VPS **Polaris** line into one codebase.

Reference states used during consolidation:

- legacy Polaris reference commit: `3bf75f1966b3`;
- legacy Aurora reference commit: `5aceef2070f5`;
- Unified MAPI release line: `0.3.0a1`.

The canonical public repository for Unified MAPI is `cabo0m/mapi-unified`. The legacy repositories are frozen references and are not development targets for the Unified line.

## Canonical architecture

```text
MAPI Core
|-- mapi_core/          memory, retrieval, self-model, lifecycle, governance, onboarding
|-- mapi_capabilities/  Files, guarded writes, Git, fixed command recipes
`-- mapi_platform/
    |-- windows/        Aurora adapter
    `-- linux/          Polaris adapter
```

`mapi_core` is distribution-neutral. Windows defaults to distribution name **Aurora** and Linux/VPS to **Polaris** through `mapi_platform.identity`. Historical database names such as `0035_polaris_onboarding` and `polaris_onboarding` remain unchanged solely for migration compatibility.

## Capabilities inherited from Polaris

Unified retains the reusable Polaris core, including durable memory, hybrid retrieval, Agent Self Model, lifecycle and supersession, governance, Sandman, semantic retrieval, research ingest, model-worker integration, onboarding, memory self-healing, doctor/recovery, runtime freshness, OAuth2 + PKCE, dynamic client registration and offline refresh.

## Capabilities inherited from Aurora

Unified retains Aurora capabilities that were useful beyond Windows itself: project-bound Files, guarded file writes and rollback, Git read/stage/commit/rollback, fixed command recipes, Windows installation and Task Scheduler integration.

## Platform-specific behavior

- **Aurora / Windows:** PowerShell-backed R3 shell, Task Scheduler, Windows bundle/install path and artifact-based freshness.
- **Polaris / Linux/VPS:** bash/sh-backed R3 shell, systemd service/timer integration and VPS deployment path.

Both distributions run the same memory schema, workshops, access policy and project capabilities.

## Remote authentication

Unified provides owner OAuth2 authorization code + PKCE and explicit revocable service bearer tokens. Legacy Codex bearer authentication remains retired. Trusted-proxy authentication is not restored.

## Migration from legacy Polaris

Polaris databases upgrade in place through the normal Unified migration chain. Compatibility is covered by an automated migration test from the previous Polaris migration tail.

## Migration from legacy Aurora

Aurora database import is deliberately explicit and guarded:

1. preview the legacy database;
2. review the preview hash;
3. apply only against a fresh Unified target;
4. create a verified target backup;
5. remap memory IDs and dependent relations;
6. preserve incompatible historical rows in an import ledger/archive.

Ordinary durable memories are imported. Sensitive health/financial memories are quarantined for review. Credential, private-key and never-store payloads are not copied into the active target. Legacy OAuth/service tokens, embeddings and ephemeral runtime state are not imported.

Aurora project capability configuration is migrated separately with the guarded config translator. It carries Files, Git and fixed command recipes into the Unified environment while leaving runtime identity, database location, semantic settings, authentication and legacy Admin grants under explicit new-instance control.

## Deliberately retired or not restored

The following behavior is intentionally absent from Unified:

- legacy trusted-proxy authentication;
- legacy Codex bearer authentication;
- copying live auth tokens between installations;
- copying ephemeral runtime leases/state;
- automatic inheritance of old Aurora Admin grants;
- reactivation of historical mutation rollback ledgers as if they belonged to the new runtime.

These are removals by design, not parity gaps.

## Historical compatibility names

Some names remain because changing them would break existing databases or callers. They are compatibility artifacts, not product identity:

- historical migration/table names containing `polaris`;
- Windows-only `run_powershell` as a compatibility alias for canonical `run_shell`.

New user-facing Core surfaces are distribution-neutral.

## Release gates

The Unified line is accepted only when all of these are green on the exact release tree:

- full pytest suite;
- compileall;
- public repository audit;
- wheel-content gate;
- Windows bundle smoke on a fresh virtual environment;
- legacy Aurora database import smoke;
- Polaris in-place migration test.

## Conclusion

Aurora and Polaris are now deployment identities around one Unified MAPI codebase rather than manually synchronized forks. Further feature development belongs in the Unified repository first; platform adapters should contain only platform-specific behavior.
