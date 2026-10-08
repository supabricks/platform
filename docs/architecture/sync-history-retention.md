# Bounded synchronization execution history

Issue [#156](https://github.com/supabricks/platform/issues/156) stopped SP10c's
first sustained fixture after all 324 comparison fixtures completed. The SQLite
owner source committed 1,021.246 changed rows/s against 1,250 offered, but the
fixture failed with `triggered_batch_admission_failed` and has no accepted
freshness or final correctness result. Its stopped database contains exactly
1,024 successful incremental runs; admission counts all retained runs against a
1,024 limit. A deterministic store test reproduces the same failure before the fix.
The source slowdown has not been causally attributed to this admission failure.

Original evidence remains immutable in
`build/sp10c-20261003/sequence-02/sustained/sqlite-owner`. The sequence was not
restarted or reset. RocksDB and direct SQLite sustained arms did not run.

## Retention contract

Admission maintains a bounded recent execution history, separate from durable
analytical publications. At 768 incremental records, each cleanup can remove up
to 128 eligible records while protecting the most recent 512 records. Eligibility
requires a successful managed batch, a successful parent, a published descriptor
whose capture/project/branch provenance matches, and no active parent dependency,
current head, pending GC or owned native process. A remaining request workspace,
including a dangling symlink, prevents expiration until engine cleanup finishes.

Only receipts structurally bound to that private parent's apply command can expire
with a batch. A user receipt referencing the batch protects it, even when its key
looks internal. Public cancellation resolves its target before any retention.
Active-parent requests remain replayable. After an internal receipt expires, a
terminal or missing parent cannot admit new work using the old request.

Successful automatic continuous parents also rotate in batches of at most 128,
with the same 768 high-water mark and 512 recent-record protection, once all their
children and private receipts expire. Manual/scheduled parents, failures,
cancellations, active work and parents named by public request receipts remain.
This avoids merely moving the automatic workload from the 1,024-child limit to
the existing 4,096-receipt or 10,000-parent limit.

Cleanup uses a savepoint: a failure rolls back both receipts and execution records.
It never unlinks a runtime file, stops a process, changes a replay cursor, advances
an acknowledgment, collects a snapshot, or removes publication/catalog/audit data.
Recent execution status can expire; retained snapshots remain addressable and
reader leases continue to work. Existing public request and protected-history
limits still fail closed if they fill. Known run/receipt exhaustion now surfaces
as `incremental_run_budget_exhausted` or `incremental_receipt_budget_exhausted`,
rather than only the generic admission error.

Published descriptors retain the validated capture identity used to resolve the
governing policy after execution records expire. This preserves service authority
checks and revocation for historical published artifacts. Security audit events
are preserved independently of execution history.

This bounds eligible operational history, **not total installation storage**.
Publication descriptors, retained snapshots, public receipts and audit records
keep their existing lifetimes and budgets. Explicit analytical history collection
and SP11 storage-pressure qualification remain necessary.

## Catalog compatibility

Catalog 30 adds four expression indexes for request-to-run/owner lookup. It does
not change capture spool or Delta formats. Existing catalog 29 roots require the
normal explicit stopped upgrade with a verified backup; ordinary startup refuses
to migrate them. Upgrade/restore support recognizes 30, and an older runtime must
not open the upgraded database. Retained catalog 29 release evidence stays tied to
its original source and archive. Current assembly and evidence collectors declare
30; this alone does not qualify a new release.

## Validation and qualification

Regression coverage includes actual admission/publication past 1,024 successful
batches; repeated history rotation past 11,000 publications; preserved snapshots,
reader pins, public retries and conflicts across restart; protected active/failed
work and pending cleanup; transactional rollback; safe exhaustion; retained
governance/audit and revocation; and explicit catalog 29 to 30 migration.

Source validation passed 215 unit tests (four ignored), 31 recovery tests, three
package tests and 26 evidence-validator tests. The [validation receipt and frozen
qualification inputs](sync-performance-evidence/2026-10-04-sync-history/validation.json)
identify the exact source and local evidence hashes. All four installed checks
(backend, lifecycle, observer off/on) passed with clean descendant cleanup.
The [supervised qualification](sync-performance-evidence/2026-10-04-sync-history/launch.json)
stopped on publication drain after crossing the old limit; see the
[#157 maintenance investigation](sync-performance-sp10c.md#sustained-capture-maintenance-correction-157).
The [October 5 review](sync-performance-evidence/2026-10-05-sp10c-review/README.md)
now verifies twelve separate history-correction trials with no regression-screen
failures. The corrected #156+#157 SQLite-owner soak completes 1,126 publications,
retains 742 incremental runs/requests/automatic parents, passes foreign-key and
final table-equality checks, and reopens the journal cleanly. Keep this correction
for reliability, without a short-trial speedup claim.

The original qualification protocol used a new immutable SQLite owner package with only the
native binary and its catalog declaration changed. Preserve the failed original
run. First repeat the 30-minute workload with unchanged input, resources and
observer, verifying more than 1,024 publications, bounded execution history,
complete table equality, journal reopen and descendant cleanup. Then compare
predecessor/candidate at 8 and 16 CPUs and 1,250 offered rows/s with three fresh
pairs per cell, unchanged five-minute measurement windows and the same profiler setting in both arms. No speedup or completed sustained qualification is
claimed before receipt review. Apply the same native correction to
each experimental engine arm before any new engine comparison.
