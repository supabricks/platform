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

Installed capacity evidence is pending. Passing this slice will not qualify
SF100 throughput, PostgreSQL loading, the 103 queries or the upper 128 GiB bound.
