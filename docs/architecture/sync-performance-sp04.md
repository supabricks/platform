# SP04 — Bounded filesystem work during planning

Status: implementation and qualification in progress. No performance decision yet.
Predecessor: SP03b, merged as `f02dca8`; accepted diagnostic runtime `5382e80`.
Tracks [#91](https://github.com/supabricks/platform/issues/91).

## Change and safety contract

The daemon already admits one incremental writer per installation, stops the
previous worker before retry, and pins both generations against garbage collection.
SP04 adds a nonblocking exclusive flock on the generation directory inode, held
from initialization's return through the worker receipt. No lock file is added.
Initialization/compaction creates a separate temporary generation before this lock;
its existing admission, reservation, durability and path checks remain unchanged.

Only read-only planning reuses accounting. A bounded admission inventory records
all file sizes/identities and directory identities. Every 32-row Arrow batch checks
the deadline, live lease and ancestor identities, directory metadata and actual
free space. A second complete inventory must match before returning the plan.
Namespace changes fail closed between batches; in-place file changes fail before
persisting a plan or allocating Delta output. Same-user arbitrary writes cannot
be prevented by an advisory lock; they are outside cooperative ownership and the
exit reconciliation rejects observed changes. The inventory is never reused for
writes, compaction, recovery, another lease or another planning call. Standalone
planning without a lease retains per-batch full checks. A conflicting writer fails
closed. All existing output reservations, retained-generation checks, post-write
checks, file hashing, fsync and publication checks remain in place.

No change to Arrow batch size, merge settings, worker lifecycle, parallelism,
spool/checkpoint policy, caps, SQL or dependencies. The new directory/file mode
checks reject group/world-writable planning trees.

## Predeclared measurement protocol

Freeze implementation, common profiler and drivers before trials. Package both
arms as immutable unsigned overlays of the accepted SP03b diagnostic package;
retain before/after hashes and checked-hash bytecode proofs. Only the candidate
changes planning/worker code. Signed installed Linux/macOS qualification is separate.

1. Matched component planning: fresh and aged two-table generations, each with
   10,000 rows per table; fresh writes each table once, aged writes 100 rows per
   commit (100 versions). Touch 1 or 1,024 existing keys per table. Three randomized
   pairs per age/key cell (24 measurements). Both arms receive identical file
   trees, transaction payloads, touched keys and selected Delta versions. Compare
   canonical complete plans, scan batches (including empty batches), planning
   wall/CPU time, directory-check time, full traversal and Python stat counts.
   Build fixtures before quiet admission; no fixture writes in measured intervals.
2. Predecessor and candidate profiler off/on controls: three pairs each at 4/50,
   8/1000 and 16/1000 CPUs/offered changed rows per second (36 full-stack trials).
3. Mandatory main comparison: three fresh pairs each at 4/50, 16/50, 8/1000 and
   16/1000 (24 full-stack trials). Four clients, two 10,000-row tables, two changes
   per transaction, five-second baseline/warmup, 45-second measured load, 120-second
   drain, 16 GiB, full SMT siblings, no CPU quota or swap. Retain the same source
   capacity limitation; 1,000 offered is not 1,000 achieved.

Use seeded randomized/counterbalanced ordering, a 300-second quiet interval,
continuous host observations, immutable receipts and complete cleanup checks.
Retain all outcomes; repeat both arms of a contended pair. Functional failures
remain failures, never silently rerun as performance successes. Preserve original
attempts if a candidate correction requires refreezing.

The common private profiler counts Python `stat`, `lstat`, `fstat`, `statvfs`,
`scandir` calls and `Path.rglob` invocations by nested span. These are Python API
calls, not a claim of all kernel metadata operations inside Arrow/Delta/native
libraries. Parent/child counts overlap and must not be summed. Both arms use
identical counting hooks; activation controls quantify their overhead. Attribute
unchanged inventory, retention and durability work outside planning separately.

Acceptance: complete correctness, deadline/disk/unsafe-path and lock/recovery
checks; retained old readers through growth/rotation; reduced matched planning
cost with traversal count independent of batch count. Seek >=80% reduction in
planning directory-check time on aged matched trees, without claiming the same
percentage for complete workers or end-to-end lag. Report fresh-tree regressions
and resource costs. Keep/revise/reject/defer only after the measurements finish.
