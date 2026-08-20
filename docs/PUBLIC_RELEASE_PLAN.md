# Public release plan

## Baseline

- Branch: `main`
- Starting HEAD: `25394f59788e2a068e97607172c7eb482ef5d310`
- Upstream: `origin/main`
- Canonical remote: `https://github.com/cabo0m/mapi-unified.git`
- Starting worktree: clean; local `main` matched `origin/main`.
- Initial `git diff --check`: passed.

## Current onboarding

The repository already provides an editable Python install, migration, fictional seed,
doctor command, local HTTP MCP server and an agent-profile protocol smoke. The existing
README is technically useful but introduces governance before the user problem, does not
name Codex or ChatGPT integration paths, and does not demonstrate current-state resolution
as a standalone product workflow.

## Confirmed documentation inconsistencies

- `docs/KNOWN_LIMITATIONS.md` says the license is unresolved although the repository has
  an Apache License 2.0 file and the public audit validates it.
- The generic MCP configuration is documented, but named Codex, ChatGPT desktop and
  ChatGPT web guidance is absent.
- The quickstart omits clone and checkout commands.
- The seed contains a supersession example, but there is no human-readable, isolated demo
  that fails when current-state resolution is wrong.
- The standard smoke checks the timeline workshop but not a timeline action result.
- Directory submission copy, a fair comparison, issue templates and RC2 draft notes are absent.

## Planned scope and files

- Product/onboarding: `README.md`, `docs/README.md`, `docs/INSTALLATION.md`,
  `docs/MCP_INTEGRATION.md`, `docs/KNOWN_LIMITATIONS.md`, `docs/CONFIGURATION.md`,
  `docs/DEPLOYMENT.md`.
- Distribution/release: `docs/PUBLIC_DIRECTORY_SUBMISSIONS.md`, `docs/COMPARISON.md`,
  `docs/RELEASE_NOTES_0.1.0_RC2.md`, this plan.
- Demo/smoke: `mapi/demo.py`, `mapi/cli.py`, `pyproject.toml`,
  `scripts/demo_project_memory.py`, `scripts/smoke_mcp.py`,
  `scripts/smoke_mcp_lifecycle.py`, and focused tests.
- GitHub intake: issue templates under `.github/ISSUE_TEMPLATE/`.

## Implementation checklist

- [x] Lead README with the user problem, positioning, benefits and honest status.
- [x] Provide complete Windows and Linux onboarding.
- [x] Add named Codex and ChatGPT integration guidance.
- [x] Add an isolated, model-free SQLite-to-PostgreSQL demo.
- [x] Expand safe agent-profile smoke and add isolated lifecycle smoke.
- [x] Correct limitations and remote-deployment boundaries.
- [x] Add neutral directory copy, comparison and RC2 draft notes.
- [x] Add privacy-conscious issue templates.
- [x] Keep generated capability documentation synchronized.

## Test checklist

- [x] Focused demo, seed, current-state, supersession, conflict and public-surface tests.
- [x] Standard MCP smoke against an isolated database.
- [x] Lifecycle smoke against an isolated database with no admin exposure.
- [x] Full `pytest -q`.
- [x] `ruff check .` and `compileall`.
- [x] Capability catalogue drift check.
- [x] Public repository audit and `git diff --check`.
- [x] Clean editable install in a temporary directory, including server, smoke and demo.
- [x] Confirm temporary server termination and remove temporary artifacts.

## Risks

- Lifecycle apply is deliberately more restricted than ordinary agent writes; tests must
  exercise the existing contract without broadening the default profile.
- Client configuration formats can change. Examples must identify where they were verified
  and avoid promising support for every client version or plan.
- SQLite supports one controlled writer; concurrent mutation scaling is out of scope.
- Public audit and manifest checks may require listing every newly tracked public file.

## Out of scope

No hosted service, public deployment, multi-user onboarding, PostgreSQL migration, Docker,
macOS support claim, private data, private runtime changes, directory submission, payment,
tag, GitHub release or push. No broad refactor of the legacy core.

## GitHub metadata recommendations

- Description: `Persistent, auditable project memory for Codex, ChatGPT and other MCP clients.`
- Topics: `mcp`, `mcp-server`, `agent-memory`, `ai-memory`, `persistent-memory`,
  `project-memory`, `codex`, `chatgpt`, `ai-agents`, `python`, `sqlite`, `self-hosted`,
  `local-first`.
- Social preview: no dedicated public social-preview asset was found at the starting HEAD.
  A future image should show MAPI, project memory, lineage and current state; it must remain
  legible at GitHub preview size and must not use any private assistant identity.

## Completion criteria

A new technical user can understand the product quickly, install it from the README, run a
local server, connect an MCP client, execute the isolated demo, inspect current and historical
decisions, understand the security and support boundaries, and file a privacy-safe issue.
All focused and release gates pass, followed by one scoped commit and no push or publication.
