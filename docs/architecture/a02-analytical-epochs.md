# A02: atomic analytical epochs

A02 publishes an A01 export as one immutable snapshot of all application tables.
Readers see the previous complete epoch until one SQLite transaction publishes
the next complete epoch. Analytical sessions and query execution remain A03.

## Developer commands

Use the A01 [worker setup](a01-frozen-exports.md#developer-setup), then:

```sh
supabricks analytics export --branch main --project /path/to/project
supabricks analytics status EXPORT_ID --project /path/to/project
# After the export reaches complete:
supabricks analytics publish EXPORT_ID --project /path/to/project
supabricks analytics publication EXPORT_ID --project /path/to/project
supabricks analytics snapshot --branch main --project /path/to/project
supabricks analytics epochs --branch main --limit 20 --project /path/to/project
supabricks analytics epoch EPOCH_ID --project /path/to/project
```

`publish` is idempotent for an export ID and returns a durable operation record.
`publication` tracks `requested → files_complete → published`, or
`failed/cancelled`. A01's export status continues describing file export and
compute cleanup; it is not proof of publication. History returns summaries,
newest first, with `next_before`; use `epochs --before ORDINAL` to continue.
Fetch an individual epoch to obtain its full descriptor. Internal table mapping
names encode `[schema, table]` as JSON to avoid ambiguous dotted identifiers;
the descriptor carries the explicit schema and table fields plus Delta versions.

The descriptor includes an installation ID, project/branch/tenant/timeline and
Postgres database identity, exact source LSN, local ordinal and epoch ID, engine
versions, schema mappings, Delta version/path per table, file sizes/checksums,
export observation time (when present in the source manifest), and preparation
time. Older A01 manifests have no observation timestamp; A02 does not invent
one. SQLite separately records publication time. Ordinals
order local publications; LSNs are never treated as global identities.

## Publication protocol

1. Journal the export ID, new epoch ID, source branch revision and original
   export admission order. Admit one publication per branch at a time.
2. Check the captured source identity, ordinary-table metadata, exact file set,
   Delta log continuity, sizes and SHA-256 checksums. Verification reads at most
   4 MiB per daemon tick in 1 MiB chunks. It never loads table data into RAM.
3. Sync files and the export manifest. Atomically write and sync `snapshot.json`
   and its directories, then journal `files_complete` with the descriptor.
4. Rename `analytics/staging/EXPORT_ID` to
   `analytics/generations/EXPORT_ID` on the same filesystem and sync both parents.
5. In one FULL-synchronous SQLite transaction, insert the immutable epoch,
   every table mapping and snapshot record, update the branch's current pointer,
   and mark the publication published.

Directory existence alone never makes an epoch visible. A retry after rename
uses the journaled descriptor in the generation directory. A crash before the
SQLite commit leaves the old pointer; a crash after commit leaves the new one.
Publication rejects a changed source revision and an export older than the
current source export. A failed publication schedules its unreferenced generation
for deletion while preserving the old snapshot.

Verification is bounded to 128 tables, 4096 files and 8192 directory entries.
Descriptors must fit below the 2 MiB IPC limit, including response overhead.
History pages contain at most 100 summaries. Generation files are immutable
through supported platform operations. Startup checks metadata, exact file sets
and file sizes; it does not rehash every published data file. Post-publication
bit rot or modifications by the same OS user are outside crash consistency;
the recorded checksums support independent diagnosis.

## Reader leases and retention

Before accessing a generation, pin the selected epoch:

```sh
supabricks analytics pin EPOCH_ID --ttl-ms 60000 --project /path/to/project
supabricks analytics renew LEASE_ID --ttl-ms 60000 --project /path/to/project
supabricks analytics unpin LEASE_ID --project /path/to/project
supabricks analytics gc --branch main --keep 2 --project /path/to/project
supabricks analytics discard EXPORT_ID --project /path/to/project
```

Pinning an epoch already selected for deletion fails; retry selection of the
current epoch if necessary. A descriptor lookup alone is not a reader lease.
Renew before expiration and stop accessing files after release or expiry.
Leases last 1–86400 seconds, with 1024 live leases per installation. They survive
daemon restart and do not keep Postgres running. A03 will tie them to supervised
query sessions; A02 also supports explicitly managed independent readers.

GC retains the requested newest 1–100 available/unavailable generations, always
the current epoch, and every generation referenced by a live lease. It checks
both analytical leases and earlier generic epoch leases. Force-deleting the
Postgres branch does not revoke analytical references. UUID-based lookup still
allows inspection of snapshots for a deleted source branch.

GC marks eligible epochs `deleting` before unlinking files. New leases cannot
attach after that mark. Deletion is restartable and ends in a `deleted`
tombstone; metadata is retained for diagnosis. Each collection request marks up
to 256 generations, so repeat collection for a larger backlog. There is no
standalone Delta vacuum and no implied cross-generation copy-on-write saving.
GC runs explicitly; retained generations continue consuming disk until collected.

`discard` cancels pending publication or removes a completed unpublished export.
Use A01 `cancel` first if its worker is still active. Published generations use
GC; discard cannot remove the current snapshot or a leased historical epoch.

## Restart and recovery bundle

On startup, a journaled published generation with missing files or inconsistent
metadata becomes `unavailable`. Access to the current snapshot then fails
explicitly; the daemon never substitutes a different epoch silently. Restoring
its exact files and restarting revalidates it. Pending publications and deletion
jobs resume from their durable states. Known unpublished exports remain available
for explicit publication or discard. Untracked filesystem entries are retained,
not adopted or deleted; runtime status reports their count from startup inspection.

A recoverable installation backup is a **complete, stopped-cell backup**:

1. Stop readers and stop Supabricks; confirm its owned processes have exited.
2. Copy the entire data directory, preserving permissions: SQLite and any WAL,
   manifests, every referenced generation, staging directories, runtime configs,
   credentials, engine/storage state, and object-storage data. Never copy only
   the SQLite main file while its daemon is running.
3. Restore the bundle together to the same data-directory location before
   restarting with compatible binaries. External bundle/helper/worker paths
   must still be available, or be reconfigured as documented for the runtime.

Within analytical metadata, generation paths are relative to the data root and
installation identity survives a complete copy. A standalone `snapshot.json`
can describe an independent Delta read, but cannot reconstruct authoritative
publication pointers, credentials or live references. Missing SQLite alongside
analytics data refuses implicit fresh initialization.

The portable suite kills real subprocesses at each verification/descriptor,
rename, SQL commit and deletion boundary. It also injects filesystem ENOSPC and
SQLite statement failures, tests stale publication, leases and unavailable-data
recovery. Native Linux/macOS tests publish and independently read actual Delta
tables, keep older readers across restart/suspension, collect unleased history,
and kill exporters after table writes and around the manifest. These establish
process-crash recovery and checked durability ordering, not a hardware power-cut
qualification or online backup guarantee.
