# EQ03 explicit incremental storage capacity (#213)

Large live datasets previously failed even with ample host disk: planning,
generation reservations and publication validation enforced a fixed 1 GiB
generation quota. Retained roots were limited to 4 GiB. Compaction cannot make a
live set larger than the quota fit.

Create a new triggered or continuous policy with an explicit storage profile:

```sh
supabricks sync create --branch main --mode continuous --storage-profile large
```

| Bound | `compact` (default) | `large` (explicit) |
| --- | ---: | ---: |
| Generation bytes | 1 GiB | 128 GiB |
| Retained incremental roots, installation-wide accounting | 4 GiB | 384 GiB |
| Compaction target file size | 16 MiB | 64 MiB |
| Generation files | 4,096 | 4,096 |
| Retained files / roots | 32,768 / 256 | 32,768 / 256 |
| Worker RSS / per-apply deadline | 768 MiB / 300 seconds | unchanged |
| Epoch metadata | 2 MiB | unchanged |

These are ceilings, not preallocated capacity or a promise that every workload
fits. Reservations still check actual free space with the existing 128 MiB
worker reserve. A workload must budget PostgreSQL, WAL, object history, reference
data and query spill separately. Retention accounting still includes **all**
incremental roots in the installation, including pinned history and unfinished
initializations. A compact policy does not inherit another policy's larger
allowance. Mixing profiles can therefore cause the compact policy to reach its
retention limit; no worker evicts another policy's history to make room.

The profile is stored in the policy, managed run, incremental run, generation
owner marker and epoch manifest. The worker checks it before reading the
journal, and the controller checks the returned profile before accepting or
publishing the epoch. Reused workers bind it into their authority scope.
Historical descriptor validation resolves the recorded profile; absent legacy
fields mean `compact`. Unknown profiles and explicit nulls fail closed.

Profile changes require completed reviewed-resync cleanup of the old capture.
An enrolled capture cannot be resized in place, including while paused. Use the
existing resync review/cleanup flow before updating the policy, or create a new
policy after deleting and cleaning up the old one. This prevents a resumed run
or saved plan from silently acquiring new quotas.

Byte rollover uses half the remaining admitted generation budget above the
compacted baseline. The existing 512-version append / 64-version mixed history
and 2,048-file rollover triggers remain independent. The larger compaction file
target reduces file-count pressure; the hard file, metadata and deadline limits
remain enforced. Query memory, query/session leases, snapshot exports and catalog
export/recovery limits are independent of this profile.

## Qualification

The original installed quota rejection is retained in
[the SF100 preparation receipt](tpcds-evidence/2026-10-08-sf100-admission/baseline.json).
Source tests cover legacy defaults, profile admission, immutable capture
configuration, forged publication profiles, historical inventory validation,
byte rollover, actual disk exhaustion, retained quotas and commit replay.

`e2e/tpcds/storage_capacity.py` creates 131,072 deterministic rows with 8,192-byte
payloads as an actual uncompressed Delta live set exceeding 1 GiB. The baseline
must reject compaction at its original byte quota. The candidate must compact,
commit 1,024 inserts, recover after interruption between the Delta commit and
worker receipt, and compare every row in both old and new versions exactly.
Each phase runs in a fresh installed process and records time, RSS and bytes.
The 768 MiB worker RSS ceiling remains enforced.

`e2e/tpcds/package_storage_candidate.py` builds from a clean committed tree and
packages the native binary plus the five changed incremental worker files into
a verified engineering release. It binds worker bytes to committed source,
retains build/source/package hashes and verifies the original package remains
unchanged. It is an unsigned engineering artifact, not a release signature.

The installed Linux slice passes. [Review and raw receipts](tpcds-evidence/2026-10-08-eq213/review.json)
bind the baseline and candidate to the same 1,077,344,043-byte source descriptor.
The original worker rejects it with `incremental_disk_budget`. The candidate
compacts 131,072 rows into 17 files (841 ms for the measured compaction body),
then commits the 1,024 inserts and successfully replays the interrupted commit.
Fresh readers compare 131,072 source rows, 131,072 compacted-version rows and
132,096 appended-version rows exactly. Apply, replay and exact verification take
3.212, 1.545 and 1.571 seconds; peak worker RSS is 497.37 MiB, below 768 MiB.
These are capacity measurements, not a speedup comparison against a failed run.

`e2e/tpcds/storage_policy.py` also passes against the installed native binary:
CLI selection, profile propagation through continuous capture/publication,
exact Delta contents, pinned Sail reads, forbidden live downgrade, daemon
restart and reopening historical readers. Its isolated 8-CPU/16-GiB container
has networking disabled; cleanup observes 81 descendants and retains zero leaks.
The native fixture uses a small PostgreSQL table; the >1-GiB fixture isolates
incremental-worker capacity rather than loading that payload through PostgreSQL.

Local validation: 228 Rust tests pass (four existing ignored) and all 161 Python
analytical-worker tests pass. Strict Clippy is not clean: the unchanged base and
candidate produce the identical 148 lib/test diagnostics under Rust 1.94, with
zero additions/removals. [#217](https://github.com/supabricks/platform/issues/217)
tracks that repository-wide baseline. Two failed fixture preparations remain in
the evidence: the initial generator omitted bounded writer settings, and the
initial direct-worker fixture omitted the production private umask. Both were
corrected before the retained final baseline/candidate pair.

Reproduction (each phase in a fresh installed Python process):

```sh
python3 e2e/tpcds/package_storage_candidate.py \
  --base <verified-base-release> --destination <fresh-candidate-release> \
  --proof <fresh-package-proof.json>
<base>/python/analytics/python e2e/tpcds/storage_capacity.py \
  --release <base> --root <fresh-seed> --phase generate
```

Clone the seed to distinct baseline/candidate workspaces. Run the baseline
`apply` phase with `--expected reject`. Run candidate `apply`, `replay`, then
`verify`, all with `--expected accept`. Use the candidate's installed Python for
each phase. Run `storage_policy.py --release <candidate> --output <fresh-output>`
under `install/native/catalog_gate.py` inside the pinned offline container to
account for every native descendant. Fixture source hashes, package identities,
raw receipts and `SHA256SUMS` are retained beside the review.

This slice does not qualify SF100 throughput, the 103 queries or the upper
128 GiB bound. Full release CI, including macOS, remains pending.
