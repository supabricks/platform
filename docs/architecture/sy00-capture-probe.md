# SY00 — Managed analytical capture and bootstrap probe

[Implementation plan](../plans/analytical-sync-implementation.md) ·
[Delivery ledger](../plans/status.md) · [Probe instructions](../../e2e/native/sync/README.md)

Status: SY00 probe complete, 2026-09-22. Both native targets passed the
[exact-alpha.35 qualification matrix](sy00-evidence/README.md), with 33 live checks
and 12 unit tests per target. This does not
implement a managed sync policy, durable consumer, or triggered/continuous mode.

## Decision

Proceed with a **PostgreSQL `pgoutput` capture adapter**, isolated physical-branch
bootstrap and **versioned Delta table roots selected by an atomic group epoch
map**. Reuse Sail. No custom PostgreSQL WAL hook or new analytical engine is
required to proceed to SY01. SY02 must implement and qualify durable capture and
its resource/lifecycle controls before incremental modes can ship.

This is a bounded feasibility decision. The prototype uses the SQL logical-slot
interface, `pgoutput` protocol v1 and an in-memory reference model. It does not
qualify a streaming replication transport, persistent spool or production decoder.
The first incremental eligibility profile is ordinary logged, public-schema tables
with an integer primary key and qualified integer/numeric/text columns. Broader
A01 snapshot eligibility remains independent.

## Exact inputs and evidence

The probe runs a new disposable native cell through the installed Supabricks
binary, engine bundle and helpers. It neither attaches to nor modifies the user's
running installation data. The native cell includes the real pageserver,
safekeeper, SeaweedFS and PostgreSQL processes, rather than a standalone system
Postgres substituted for the shipped engine.

- PostgreSQL **17.8**, source `56692dfb680281a963c7470fc7f0fec7f65ecfd4`.
- Neon **`1c6fa095261112aae239beef5a221b484703d49a`**.
- Locked analytical runtime: delta-rs 1.6.3, Arrow 25.0.1 and the existing Sail
  environment. The storage probe runs in the release's own Python launcher.
- CI downloads the exact alpha.35 archives from
  [release run 35700396118](https://github.com/supabricks/platform/actions/runs/35700396118).
  [prepare.py](../../e2e/native/sync/prepare.py) pins both archive hashes and
  verifies the extracted release inventory. Local installed-source runs are
  labeled by their actual manifest/binary hashes; a matching version string does
  not make them the qualified archive.
- Reports include source revision/dirty state, probe-file hashes, engine manifest,
  input hashes, individual checks, measurement scope and limitations. Only the
  sanitized report is uploaded. Credentials, raw feed and private state/logs are
  not CI artifacts.

The dedicated [workflow](../../.github/workflows/sync-probe.yml) runs both Linux
x86_64 and macOS arm64. [qualify.py](../../e2e/native/sync/qualify.py) rejects missing,
failed or duplicate checks, an unexpected engine and exceeded probe budgets.
Unit tests cover truncated/unknown messages, schema mismatch, invalid commit
boundaries, replay, key/TOAST handling, atomic failure and incomplete evidence.

## Source settings and operational constraints

The current compute template already enables logical capture. Measured values:

| Setting | Shipped value | Consequence |
| --- | --- | --- |
| `wal_level` | `logical` | No runtime setting change needed for the probe |
| `max_replication_slots` / `max_wal_senders` | 10 / 10 | Managed admission must reserve capacity; these are shared engine limits |
| `fsync` | on | Probe uses the native durability profile |
| `max_slot_wal_keep_size` | -1 | **Unbounded by this setting today. SY02 must add bounded retention and gap detection.** |
| `logical_decoding_work_mem` | 65536 KiB | Decoder can spill; client transaction limits alone do not bound source work |
| `max_prepared_transactions` | 0 | Initial profile excludes prepared/two-phase transactions |
| `wal_sender_timeout` | 5000 ms | A future streaming client needs timely feedback and reconnect tests |

The probe uses a local administrative identity to install its publication, slot
and DDL fence. An ordinary `NOREPLICATION` role is denied SQL decoding with 42501.
This is **not** evidence of a least-privilege governed service identity. Capture
can expose source data outside ordinary SQL read permissions; SY06 must enforce
explicit source enrollment and qualified whole-table authority. No RLS/column-mask
support is claimed. Replication is an internal capability, never a browser token.

## Bootstrap protocol selected by the probe

Let `S` be the consistent point returned when the managed slot is established,
`F` the exact ancestor LSN of the subsequently frozen child, and `E` a decoded
transaction's commit **end** LSN. All three belong to the same source lineage.

1. Install a qualified schema/membership fence and establish a `pgoutput` slot
   before capturing the physical boundary. Record `S` and require `S <= F`.
   Slot establishment is bounded and may wait for older transactions; failure
   leaves no successful bootstrap claim.
2. Capture a flushed, storage-ingested source LSN through the existing branch
   machinery, and fork a child at exactly `F`. Read all group members there in
   one repeatable-read read-only transaction. Its bulk scans use child compute.
3. Retain and decode source changes beginning at the slot. Group changes by
   complete committed transactions. Ignore transactions with `E <= F` because
   their effects are already in the frozen baseline. Apply transactions with
   `E > F` in commit order. Never filter by row-event LSN or XID allocation order.
4. Verify that baseline plus tail equals source state at a controlled final cut.
   Preserve the source identity and group schema in every subsequent checkpoint.
   A new branch, restored incarnation or unrelated timeline needs a new bootstrap.

This uses a **physical branch boundary plus a pre-established logical stream**;
it does not import a PostgreSQL exported snapshot identifier into another
timeline. The probe proves the relevant behavior on the shipped engine with:

- Changes committed between `S` and `F`, excluded from replay without loss.
- Two-table transactions open across `F`, committed afterward and applied together.
- Aborted transactions and rolled-back savepoints, absent from the resulting data.
- Transactions with XIDs allocated in the opposite order from their commits.
- A frozen reader that retains its original data while the parent changes.
- Source table scan counters unchanged during the child bootstrap scan. Setup and
  final source-oracle scans are outside that measurement window.

The fixture creates an ordinary disposable child to exercise the same physical
branch primitive. Production SY02 must use hidden owned children and enforce
A01's existing credential, worker and cleanup isolation. The probe does not itself
add an internal-branch API or replace A01.

## Capture adapter boundary

The following is the implementation contract for SY02, not an exported API today:

| Operation / value | Required semantics |
| --- | --- |
| `inspect(source, group)` | Report eligibility, source identity, settings, authority and budgets before enrollment |
| `establish(source, generation)` | Create owned capture resources; return a consistent lower boundary with recovery identity |
| `read(cursor, budget)` | Produce complete committed transaction envelopes or explicitly bounded pending segments |
| Transaction envelope | Source generation, XID for correlation, commit LSN, end LSN, relation/schema revision, ordered row changes and fence events |
| `ack(durable_cursor)` | Advance only a contiguous prefix reproducible from the durable spool; receiving or applying in memory is insufficient |
| `status()` | Observed source/captured positions, history availability, retained WAL, heartbeat and source identity |
| `close/delete()` | Fence the worker; retain or release only owned resources under explicit lifecycle policy |

PostgreSQL's [logical decoding documentation](https://www.postgresql.org/docs/17/logicaldecoding-explanation.html)
explains why clients must tolerate replay and manage retention. Protocol fields
follow the [official pgoutput message specification](https://www.postgresql.org/docs/17/protocol-logicalrep-message-formats.html).
The probe rejects unknown messages and preserves textual numeric precision.
Production transport/spool work must preserve that rejection behavior, but must
not copy the probe's `fetchall`/in-memory model into an unbounded consumer.

## Eligibility and schema matrix

| Case | Probe evidence / initial decision |
| --- | --- |
| Ordinary logged table, non-null integer primary key | Selected initial profile; keyed insert/update/delete and key movement tested |
| `numeric(38,8)`, text including Unicode, NULL | Exact textual decode and typed Delta conversion; decimal value beyond float precision tested |
| External/unchanged TOAST value | Preserve old column when pgoutput marks it unchanged; 16,000-byte text survives update |
| No primary key / replica identity | Reject incremental enrollment; source rejects published keyless deletes, so discovery must precede publication creation |
| Alternate replica identity / composite or noninteger keys | Not qualified in SY00; add explicit fixtures before expanding |
| Partitions, unlogged/temporary tables, generated columns, foreign tables, custom collation/types | Not admitted by initial profile; existing snapshot rules remain separate |
| Two-phase and in-progress transaction streaming | Not qualified; v1 complete-commit protocol only, prepared transactions disabled in tested source |
| ADD column; CREATE/DROP empty group candidate | Transactional DDL fence blocks the whole group even with no subsequent row change |
| Changed relation metadata | Decoder rejects it; never guesses a column mapping |
| TRUNCATE, origin/custom type/unknown protocol messages | Decoder rejects unsupported events; do not advertise transparent support |
| DDL fence tampering / hostile source superuser | Outside this local-owner probe; governed worker/service authority requires separate qualification |

The experimental event triggers emit a transactional logical message for relevant
public-schema table/index DDL, including drops. Engine-owned `health_check` is
excluded. An initial broad fence also caught compute startup's internal DDL;
filtering the qualified scope fixed that false invalidation. SY02 must inventory
engine objects by verified identity and define fence ownership, migration,
revocation and whole-group enrollment. Do not treat name filtering as a general
security boundary, or a successful row feed as evidence of complete DDL capture.

## Incremental storage decision

Use versioned Delta roots per managed table and a new **v2 group epoch descriptor**
that selects an explicit Delta version for each member at a source commit cut.
Unchanged members reuse previous versions. All user-facing reads resolve this map.
Per-table latest pointers remain internal and must never define group consistency.

The storage probe converts the actual bootstrap and captured-tail reference state
into Delta, applies only changed/new rows and deletes, and reads both old and new
versions through a fresh independent delta-rs reader object. After the first table
changes, the durable old group map still resolves both old tables. After both
finish, an atomic map swap selects the new pair. Old versions remain readable.

This establishes representation feasibility, not the production publication
journal's crash proof. SY03 must implement SQLite epoch/cursor atomicity, process
crash failpoints, file/log verification and independent reader-process tests.
Sail's qualified explicit-version binding remains A03; no new live/latest-table
binding is introduced by this probe. A02 v1 generation files are never modified.

Delta merge/delete can rewrite a substantial fraction of a small table despite a
small input change set. Report new Parquet bytes relative to original data and
changed-row count; do not market incremental input as zero-copy output. SY03/07
need file sizing, compaction and shared-file reference accounting before sustained
continuous workloads. Do not vacuum files held by any historical reader, notebook,
catalog publication/binding or recovery checkpoint.

## Recovery and branch findings

- The source slot survives explicit suspend/wake. Capture requires compute to be
  available; continuous mode cannot promise freshness while the source sleeps.
- Unacknowledged committed changes survive a real `compute_ctl` SIGKILL and
  replacement compute. The oracle replays them without duplicate visible effects.
- The tested fresh child does not inherit an active logical consumer slot, and its
  source identity differs. Do not reuse a parent's checkpoint even if numerical
  LSNs overlap.
- Probe acknowledgment uses `pg_replication_slot_advance` only after checking the
  disposable reference model. It is **not a durable acknowledgment implementation**.
  Spool/ack crash windows, history loss, full host restart, physical power loss,
  backup/restore, replication failover and governed revocation remain later gates.

## Initial engineering envelope and budgets

These are acceptance targets for development, not a production freshness SLA.
The probe workload is two tables of about 1,000 rows each, one writer, integer
keys, bounded text and decimal values. It measures 30 sequential single-row writes
with capture enabled in both phases, with and without active consumption; this
is not a controlled comparison against `wal_level=replica` or a throughput ceiling.

| Boundary | Initial target / policy |
| --- | --- |
| SY00 warm commit acknowledgment → SQL decode/reference apply | p95 <= 1,000 ms over 30 measured commits |
| SY00 changed-row apply to each fixture Delta table | <= 5,000 ms; bootstrap and Python startup measured separately |
| SY02 message / complete transaction / decoded batch | 1 MiB / 4 MiB / 16 MiB initial admitted limits; bound source and transport before allocation |
| SY02 spool / retained source WAL | 512 MiB each per source group proposal; aggregate installation admission required; warn at 80%, fence before exhausting allocated capacity |
| SY04/05 initial workload qualification | Two tables up to 10,000 rows each; 50 changed rows/s sustained and 200-row bursts; publish measured OLTP impact and resource maxima |
| SY05 initial continuous target | p95 commit-to-published-epoch <= 5 s within that envelope; no partial transaction publication to meet the timer |
| SY05 backpressure | Visible lagging state and bounded backlog; unavailable retained history becomes resync-required, never a skipped interval |

The larger SY04/05 envelope is a **future test requirement**, not demonstrated
capacity. The current probe records cold bootstrap time, source write latency,
capture/application latency, columnar apply time/bytes and harness CPU/RSS.
It does not measure total runtime peak memory or end-to-end continuous lag.
Threshold changes require a recorded reason and requalification; they must not
be relaxed solely to make a failing run green.

## Handoff

SY01 can implement policy/run persistence and clearly labeled scheduled snapshots.
SY02 is gated on durable spool/ack semantics, bounded WAL retention, stable source
incarnation identity, explicit schema-fence ownership, qualified credentials and
safe hidden-bootstrap cleanup. The selected capture/format approach is sufficient
to begin that work, not evidence that those production controls already exist.
Reverse lakehouse-to-Postgres synchronization remains outside SY00.
