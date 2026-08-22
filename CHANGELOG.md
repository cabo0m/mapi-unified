# Changelog

## 0.3.0a1 - 2026-08-20

- Rewrite installation and MCP handoff documentation with separate Windows local, Linux local and Linux/VPS remote paths; fix the Unified checkout name and current ChatGPT remote-MCP guidance.
- Add one-time Windows Secure MCP Tunnel configuration with DPAPI-protected credentials, Task Scheduler autostart, startup ordering and process supervision, so Aurora reconnects after sign-in without repeating setup.
- Start the Unified MAPI line: one platform-neutral `mapi_core` shared by Aurora (Windows) and Polaris (Linux/VPS).
- Add shared project Files, guarded writes, Git stage/commit/rollback and fixed command recipes.
- Add Windows Task Scheduler/install bundle and Linux systemd adapters around the same runtime and SQLite schema.
- Add platform-native R3 admin shell, artifact-based freshness for installed wheels and distribution identity outside Core.
- Add explicit revocable service bearer auth alongside owner OAuth/PKCE.
- Add guarded legacy Aurora import with preview hash, target backup, ID remapping, sensitive-memory quarantine and an audit/archive ledger; Polaris databases upgrade in place.
- Add cross-platform wheel and Windows bundle release gates.


## 0.1.0rc2 — 2026-08-06

- repositioned MAPI as persistent, auditable project memory for MCP clients;
- added a complete local quickstart and named Codex and ChatGPT guidance;
- added an isolated, model-free current-state and preserved-history demo;
- expanded standard and lifecycle smoke verification;
- added neutral release, directory, comparison and issue-reporting materials;
- retained the safe local-first defaults and existing lifecycle contracts.

## 0.1.0rc1 — 2026-07-30

- created a standalone sanitized public release candidate;
- added neutral safe-by-default profiles and configuration;
- added Python packaging and stable CLI entry points;
- added deterministic synthetic demo data;
- added generated capability documentation and public repository auditing;
- retained the migration tail at `0032_retire_bridge_mailbox`;
- excluded all private data, history, deployment configuration and runtime artifacts.
- selected Apache License 2.0 and finalized public author/security metadata;
- expanded the implementation guide and release auditing for documentation,
  license, language, and Git metadata.
- separated model-free core CI from optional semantic-search CI;
- added lightweight IANA timezone data for consistent Windows runtime behavior;
- updated the public audit to accept either an unpublished release candidate or
  the single canonical public GitHub origin.
