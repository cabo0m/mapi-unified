# Public release audit

Status: **PUBLISHED — TECHNICAL AND LICENSING GATES PASSED**

Public repository: **https://github.com/cabo0m/mapi-unified**

| Field | Result |
|---|---|
| Source commit | `72a3a06780e3c250241aaed476db610df1af9ec3` |
| Previous public root commit | `4f83d13b65e5a5acdd8a2d4517d56b12c0497d09` |
| Final public root commit | produced by the final single-root amend; exact value is recorded by `git rev-parse HEAD` and the final release report |
| Export date | 2026-07-30 |
| Finalization date | 2026-07-31 |
| Included file count | 277 |
| Excluded categories | private data, history, deployment configuration, and runtime artifacts; see the export manifest |
| Public capability count | 156 registered actions; profile policy narrows what each client sees |
| Workshop count | 12 registered; 10 visible in the default `agent` profile |
| Migration tail | `0032_retire_bridge_mailbox` |
| License | Apache License 2.0 |
| Public author | Michał Chlewicki |
| Public contact | `info@morenatech.work` |
| Windows clean install | passed on Windows 11 with CPython 3.12.10; non-editable install, migration, repeatable seed, doctor, server, and real MCP smoke |
| Linux clean install | passed on Ubuntu 24.04.3 LTS under WSL2 with CPython 3.12.3; isolated offline wheel install, migration, repeatable seed, doctor, server, real MCP smoke, and 11 focused tests |
| Full pytest | the clean full suite covers both model-free core and optional semantic tests; CI runs the two groups separately |
| Static and compilation checks | Ruff passed; Python compilation passed |
| Model-free startup | passed on Windows and Linux with semantic and Gemini extras disabled |
| Privacy and secret scan | passed for tracked files; narrow reviewed scanner/fixture exceptions remain explicit |
| Git metadata scan | audit v3 checks every reachable commit and blob, author/committer data, branches, tags, notes, commit messages, and local reflogs; it accepts either no remote for a release candidate or exactly one canonical public `origin` after publication |
| English-only documentation | passed for 30 public documentation/configuration/CLI files and the effective workshop catalogue |
| Binary inventory | passed; no unapproved binary, database, archive, log, or model artifact |
| Dependency inventory | documented in `DEPENDENCIES.md` |
| Publication blockers | none |

The final manifest SHA-256 is
`014142be5e7c0f733fef3a59765a4bba452aa1fdde42ae28eb2d5db5ef8872c9`.
The exact Apache License 2.0 text has normalized SHA-256
`cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`.

The root commit field cannot literally embed its own final SHA: changing the
document changes the commit object and therefore changes that SHA. The
repository records the authoritative value through `git rev-parse HEAD`; the
exact immutable result is also included in the final release report.

The audit recognizes two explicit repository states:

- `release_candidate`: no remotes;
- `published`: exactly one `origin` resolving to
  `https://github.com/cabo0m/mapi-unified.git` or its canonical SSH form.

Arbitrary owners, repositories, protocols, credentials, query strings,
fragments, extra remotes, extra push URLs, and target-changing `insteadOf`
rewrites remain forbidden. Transport-generated remote-tracking reflogs are
local Git state and are not part of the published history; local branch and
HEAD reflogs remain scanned.
