# Historical release notes: MAPI v0.1.0-rc2

> Archive only. These notes describe the pre-Unified release line and are **not** current installation instructions.

The current public repository is `cabo0m/mapi-unified`. For current setup use [Installation](INSTALLATION.md), [MCP integration](MCP_INTEGRATION.md) and the root [Changelog](../CHANGELOG.md).

## Historical highlights

The 0.1.0-rc2 line:

- positioned MAPI as persistent, auditable project memory for MCP clients;
- added a local quickstart and client integration guidance;
- added an isolated model-free current-state/history demo;
- expanded protocol and lifecycle smoke verification;
- retained local-first defaults and guarded lifecycle contracts.

## Historical compatibility

At that time:

- Python 3.11 and 3.12 were supported;
- the default local endpoint was `http://127.0.0.1:8015/mcp/`;
- the default surface was `agent` with admin separately gated;
- the model-free core did not require semantic/model-provider extras.

The repository, CLI guidance, remote authentication and platform layout have changed since this release. Do not use old clone commands, old repository names or old client instructions from the 0.1.x line for a new installation.
