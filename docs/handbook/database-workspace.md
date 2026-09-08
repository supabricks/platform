# PostgreSQL database workspace

The C02 alpha.5 console adds **Database workspace** to `supabricks console`.
Open it from a project containing `supabricks.toml`. The packaged runtime needs
no Node server, CDN or external service. The [console runbook](local-console.md)
covers installation, launch links and supported browsers.

## Branches and SQL tabs

Choose a **Navigation branch**, then **New SQL tab**. Each tab keeps the branch
UUID and revision captured when it opens. Changing navigation does not move an
existing tab or change the CLI's worktree selection. Up to eight tabs can remain
open. Switching between Overview and Database workspace preserves them in memory;
closing/reloading the page loses unsaved text and results.

**Create or delete a branch** offers a new root database or a child of the
navigation branch. Suspend/resume and delete use the currently observed revision.
Deletion requires typing the branch name and respects existing child/connection
protections. The console shows the durable operation ID, accepted/running state,
step progress and final outcome. A failed or interrupted request is never
resubmitted automatically; refresh and inspect the operation before trying again.

A changed, expired or deleted branch makes a tab stale. Select a current branch
and use **Rebind to navigation branch** explicitly. Rebinding clears old results
and returns the tab to read-only mode. Running queries retain their original
binding; changing navigation cannot redirect a write.

## Explore and query

**Refresh tables** reads the selected branch's schemas, tables and columns, up to
1,000 catalog rows. Expand a table to inspect names, types and nullability. Its
**Preview** opens a read-only query tab and selects at most 200 rows. PostgreSQL
identifiers are quoted by the backend. A larger catalog produces an explicit
limit error; use SQL or a regular PostgreSQL client to narrow the selection.

The SQL editor accepts one PostgreSQL statement per execution. Use **Run SQL**
or Ctrl/Command+Enter. Transactions are read-only unless **Allow writes** is
checked for that tab. Loading a saved query and rebinding both start read-only.
Multi-statement scripts and interactive transactions remain regular PostgreSQL
client workflows.

Defaults are 200 rows and a 10-second statement budget. Controls allow 1–1,000
rows and 100–30,000 ms. The existing 32 KiB SQL, 256 KiB result and 1 MiB backend
frame bounds remain. Oversized results fail rather than silently truncating.
Cells contain exact PostgreSQL text or SQL NULL; bigint and decimal values never
pass through JavaScript numeric conversion. Click a non-null cell to copy its
exact text. The result table renders a small window of rows as you scroll.

**Cancel query** targets the tab's query handle, leaving other tabs and regular
PostgreSQL clients alone. Cancellation is requested first and completion is
reported separately. A write interrupted near COMMIT may already have committed;
inspect the database before retrying. **Check query status** reads an existing
handle and never executes its SQL again. A lost response, expired handle or
runtime restart cannot establish that a write rolled back.

All console and existing CLI/MCP SQL share four worker slots. The daemon keeps
at most 32 recent console handles; terminal handles expire after ten minutes or
are evicted as new queries arrive. Handles belong to their browser session and
daemon generation and do not survive runtime restart. Query text/results are
held in memory, without automatic browser storage or on-disk query history.

**Reveal connection** fetches the selected branch's stable application URI only
on request. Copy it explicitly and use **Hide connection** afterward. Changing
navigation, signing out or reloading clears the displayed URI.

## Save and recover queries

Enter a title and choose **Save query**. Saved query files contain versioned SQL,
title, branch binding and a revision, privately under
`DATA/queries/PROJECT_UUID/QUERY_UUID.json`. There are at most 100 per project.
Saving from a stale tab or overwriting a changed saved revision fails explicitly.
Loading saved text never runs it or enables writes. Use the adjacent delete
control to remove a saved query; unsaved tab text remains until closed/reloaded.

Stopped backups include these files. Restore and runtime restart retain saved
text and branch bindings, but never restore browser sessions or query handles.
See [recovery](recovery.md). A saved query pointing to a changed/deleted branch
requires explicit rebinding before execution. SQL can contain sensitive literals;
only save text you intend to retain in your private data root and backups.

C02 qualifies Chromium on Linux x86_64 and macOS arm64. File ingestion,
analytical workspace views and VS Code integration remain later slices.
