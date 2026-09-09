# I02: local browser ingestion

The alpha.8 console adds a CSV/TSV importer to the PostgreSQL workspace. It uses
I01's source, mapping, job, worker and PostgreSQL receipt contracts. Catalog
version 9, console API 1 and the 34-tool MCP contract are unchanged. No new runtime
dependencies or host Python/Node requirement are introduced.

## Ownership and bounded transfer

An authenticated workspace `ingest/begin` action reserves a server-generated
source UUID in the bound project. The daemon opens its private `0600` `.part`
file; filenames are display metadata only. At most two open upload descriptors
and 32 console source references are admitted across the cell. I00's 512 MiB
staging reservation and 100 MiB per-source ceiling still apply.

The browser sends the `File` directly through XMLHttpRequest, which supplies
upload progress and cancellation without reading the whole file into JavaScript.
`POST /api/upload/{source}` checks the existing exact Host, Origin, session,
console version and CSRF controls before consuming the body. The loopback bridge
forwards at most 24 KiB per control-socket request, hex encoded within the existing
64 KiB envelope. The daemon checks session/project binding, sequential offset,
declared length and the 64 MiB free-space reserve for every write. Neither process
buffers the full upload. Control-socket readiness wakes the daemon immediately;
its regular maintenance cadence remains bounded by 20 ms.

Upload bodies have a 10-second idle limit and a 10-minute overall deadline. A
broken stream attempts disposal immediately. Daemon maintenance closes abandoned
receiving descriptors after 30 idle seconds or 10 minutes total, including a
failed inspection launch after the descriptor closed. Shutdown closes these
descriptors before disposal; recovery stops console/ingestion processes before
interrupting stale sources. Receiving slots cannot be loaded or disposed by the
ordinary CLI while a writer remains open.

## Inspection and approval

After an exact-length upload the daemon syncs and closes the writer, then launches
the owned Python inspection worker on that same private part file. The worker
hashes the bytes, parses bounded sample rows and reports conservative text types.
Only after fencing the worker does the coordinator seal the immutable source.
There is no second copy of the uploaded data.

A session can reinspect its retained source with different delimiter, header and
null options. Each accepted inspection increments an in-memory preview revision
and removes previous preview evidence before launching the worker. Admission
requires that revision, a ready preview, matching parser options and the staged
SHA-256. The loader independently hashes the source again. Column names, scalar
types and nullable flags are explicit user edits; malformed late rows and unsafe
conversions still fail atomically under I01.

The UI shows project, branch, branch revision, schema and new table name together
before approval. Changing parser options requires reinspection; editing mappings
or the destination clears approval. Navigation does not silently retarget an
existing import. Existing tables are never overwritten.

## Durable jobs and reconnect

Accepted jobs belong to the project and appear in browser history, CLI and MCP.
Upload/source access is session-bound; accepted job status, cancellation and
explicit retry use the durable project scope. Reload polls existing jobs and
never submits a load or retry automatically. Session storage contains only the
selected source UUID and an unresolved submission key, never file bytes, mappings,
credentials or SQL. A lost load response leaves submission uncertainty visible;
users inspect recent jobs before beginning another import.

The UI distinguishes bytes sent, inspection, parsed/copied rows and receipt-backed
committed rows. Cancelled/failed jobs never claim copied rows as committed. A
successful import opens a query tab bound to its own branch and table. Failed
jobs offer explicit retry with retained source; source expiry and branch revision
remain enforced by the shared store. Raw error details remain bounded stable
categories; samples are confined to private inspection artifacts and the UI.

A runtime restart revokes console sessions and ephemeral previews. Durable jobs
remain discoverable after a fresh launch. Unaccepted sources may need uploading
again after session loss; they expire under the existing retention policy.

## Qualification

`console/scripts/ingestion.mjs` extends the real Chromium/native console harness:
keyboard selection, drag/drop, approved scalar mapping, source fidelity,
wrong-session/CSRF/host-path rejection, aborted streaming, stale preview,
branch isolation, reload during an owned loader, cancellation, explicit retry and
source disposal. It runs against both installed native archives under network
isolation in the existing release-console jobs. The ingestion harness's bounded
volume fixture also exercises authenticated browser admission with real disk
pressure and injected expiry. Portable tests cover chunk ordering/limits,
session binding, idle cleanup and closed-but-unaccepted receiving descriptors.

The redistributable walkthrough is [examples/console](../../examples/console/).
JSON, JSONL and Parquet remain I03; analytical views remain separate work.
