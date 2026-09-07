# P06 qualification

P06 adds the native application CLI, project-bound socket API, stdio MCP bridge,
orders app and coding-agent setup adapter. The older operator API is unchanged.
Engineering qualification uses PG17.8 from E01 and the P03 native helper bundle.
This is not a public installer release or an analytical engine milestone.

## Automated evidence

The portable application contract tests exercise the real daemon socket and CLI:
create retry restores its original ID/ports across SIGKILL, idempotency conflicts
remain errors, branch/operation lookups stay project-scoped, worktree selection
is independent, changed project identity invalidates a live client, stale
revisions/default deletion fail, SQL writes need explicit branch selection,
unknown local versions/arguments fail, and the MCP schema is separately pinned.
They also check the 32-active-branch admission limit, retries at that limit, and
shutdown waiting for ownership release after the daemon socket disappears.

The native `e2e/native/workflow.py` runs an independent CLI and generic stdio MCP
client against the actual cell, with no private SQL or state access for workflow
operations. It covers the shipped orders HTTP app, migration isolation, text SQL
results, read-only enforcement, multiple-statement rejection, bounded rows/bytes/
frames/time, rollback of over-limit writes, worker admission, responsive daemon
status, suspension/wake, persistent worktree selection and stable connections
across down/up, then named child deletion with parent data intact. Both native CI
architectures run this alongside P03, P04 and P05 qualification.

Local Linux qualification on 2026-09-07 passed all **61 workspace tests**, local
all-target Clippy with warnings denied, formatting and the portable dependency
gate. The native reports passed P03 (11 checks; disk-full is exercised in Linux
CI), P04 (12), P05 (9) and P06 (6). Logs/reports were retained privately because
native diagnostics can contain local paths and application data.

## Installed coding-agent usability check — blocked

Observed 2026-09-07 UTC on Linux with **codex-cli 0.153.4**, authenticated through
its existing ChatGPT session. This was a disposable project and installation.
Per-invocation `mcp_servers` overrides configured the exact adapter-generated
binary/argv. `codex mcp get --json` verified that the installed CLI resolved the
intended transport. No saved registration, trust or approval settings changed.
Adapter retry/conflict behavior is additionally covered by `agents/test-setup.py`.

A real `codex exec` session completed MCP initialization and called `capabilities`
and `list_branches`. It verified the intended worktree and empty inventory. Its
attempt to call `create_database` was rejected by the host before reaching the
server: `MCP tool call requires approval, but approval policy is never`. Its shell
and file-editing path also failed because the host sandbox could not initialize
loopback (`bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`).

No database or application source was created by that session; it did not obtain
credentials. Branch migration, app execution, restart and cleanup by a real agent
remain **unverified**. The automated generic-client workflow passing does not
replace this usability exit criterion. The setup adapter intentionally does not
relax host approval or sandbox policy to turn this into a passing result.

To complete the remaining usability check, run the [smoke task](../../agents/smoke-task.md)
in an interactive trusted local coding-agent session where its normal approval
flow and shell sandbox work. Record its version, operation IDs, app output,
parent-isolation and restart checks, and any workflow friction. The database itself
continues to require no model account or API key.
