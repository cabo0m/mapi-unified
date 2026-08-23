# Public directory submission package

This neutral package can be adapted for MCP catalogues, GitHub awesome lists, open-source documentation and community posts. It is preparation material, not evidence that a submission has been made.

## Core fields

- Name: **MAPI**
- Repository: `https://github.com/cabo0m/mapi-unified`
- Category: **Memory & Knowledge**
- Runtime: **Python**
- Transport: **HTTP MCP**
- License: **Apache License 2.0**
- Publisher: **Michał Chlewicki / MorenaTech**

Short description:

> Persistent, auditable project memory for Codex, ChatGPT and other MCP-compatible AI clients.

More cautious alternative:

> Persistent, auditable project memory for Codex and other MCP-compatible AI clients.

## Long description

MAPI is a self-hosted MCP memory server for project decisions, corrections, rules, progress and next steps. It resolves current state while preserving historical records and lineage, separates explicit writes from proposals, and keeps project boundaries visible. Records can carry provenance and confidence metadata. Conflict review, guarded lifecycle operations, preview hashes, audit evidence and rollback support controlled maintenance without treating an opaque vector index as the source of truth. The model-free local core runs on Python and SQLite; optional semantic and provider integrations are disabled by default.

## Tags

`mcp`, `mcp-server`, `agent-memory`, `persistent-memory`, `project-memory`, `codex`, `chatgpt`, `ai-agents`, `python`, `sqlite`, `self-hosted`, `local-first`, `audit`

## Limitations

- self-hosted and local-first; no hosted SaaS;
- Python 3.11 or 3.12;
- SQLite single-writer characteristics;
- ChatGPT web can reach Windows Aurora through OpenAI Secure MCP Tunnel, or through a separately secured HTTPS endpoint such as authenticated ngrok; VPS deployments use remote HTTPS + Aurora OAuth;
- remote authentication is experimental and outside the quickstart;
- Docker and macOS are not verified in this candidate.

## Community post copy

MAPI is an open-source, self-hosted project memory server for MCP clients. It helps assistants carry decisions, corrections, rules, progress and next steps across sessions while retaining provenance and auditable history. A model-free demo shows SQLite being superseded by PostgreSQL, with PostgreSQL resolved as current and SQLite preserved as history. The project is a developer preview, runs locally on Python and SQLite, and does not offer hosted SaaS.
