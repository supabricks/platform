# SP06 higher-load comparison and interrupted control recovery

Status: main comparison complete; profiler controls restarted on September 30,
2026. This is a checkpoint, not a completed SP06 qualification. The earlier
[78 accepted trials and source qualification](../2026-09-27-sp06/README.md) remain
unchanged. The main comparison adds 12 accepted trials (90 total so far).

Both arms use the same accepted SP04 runtime `e10d515` and frozen SP06 harness
`be4701c`, eight clients, two 10,000-row tables, two changed rows per transaction,
1,250 offered changed rows/s, 60-second warmup and 300-second measurement. CPU
sets use whole SMT sibling groups; memory is 16 GiB with no swap or CPU quota.
No runtime change or replication-engine speedup is claimed.

## Complete main results

| Logical CPUs | Accepted trials | Input passes | Actual input range (rows/s) | Publication p95 range (ms) |
| --- | --- | --- | --- | --- |
| 8 | 6 | 4 | 737.593–1,249.085 | 3,714.982–4,512.676 |
| 16 | 6 | 6 | 1,249.227–1,249.689 | 3,703.343–3,760.205 |

Each CPU cell has three predecessor/candidate pairs. All 12 accepted trials
passed exact final-table correctness, cleanup, and p95 freshness at achieved
input. Input passes use the existing 95%-of-offered threshold. Two additional
pairs were excluded for detected build overlap; all four trials are retained.

The accepted 8-CPU repeat-2 pair missed even 1,000 rows/s: predecessor 839.428,
candidate 737.593. Neither had detected build overlap. They remain in the
original denominator; no selective performance rerun or contention reclassification
is justified. Source COMMIT p50 grew from about 11 ms in the other accepted runs
to 17.042 / 19.771 ms. Capture COMMIT mean rose from about 7.5–7.8 ms to
12.053 / 13.783 ms. CPU use fell to 1.102 / 1.030 cores. The candidate's
pre-sync source baseline was already only 760.131 rows/s.

This identifies durable commit service time as the immediate limit. The recorded
host I/O pressure and ~17 GiB free space do not establish its root cause or a
causal competing process. Later disk exhaustion does not prove the cause of
these earlier slow trials. See [issue #127](https://github.com/supabricks/platform/issues/127).
There is no blanket 8-core 1,000-row/s qualification.

## Failed controls and recovery

The first profiler-off control trial stopped on host-monitor `ENOSPC`. Free space
fell from 8.407 GiB at phase start to 0.305 GiB in the last complete sample, then
the host JSONL record was truncated. There are zero accepted pairs and no final
trial or cleanup receipt. The original experiment remains `running_trial`.

`qualified-controls-interrupted/` preserves that state, the incomplete matrix,
the byte-exact compressed host stream (including its partial final line), and
analysis source. The export explicitly qualifies neither performance nor cleanup.
Private logs and scratch are excluded. `interruption.txt` preserves the controller
exception with local repo paths redacted. Failure handling is tracked in
[issue #126](https://github.com/supabricks/platform/issues/126).

At investigation, the matrix had recorded its interruption, its exception handler
had requested removal of its unique container, and no benchmark container remained.
This current-state check does not manufacture a historical cleanup receipt. Failed
scratch and the original campaign remain locally preserved. Available disk space
had recovered to 136.9 GiB without deleting files in this continuation.

`qualified-controls-02` is a fresh, separately named campaign, not a resumed or
reset failed manifest. `controls-interruption-review.json` records the restart
basis; `controls-02-preflight.json` records pinned dependencies and a clean frozen
harness. `continue_controls_02.py` is the exact continuation recipe (run from its
original `build/sp06-recovery-20260927/` location, not this archive). It requires
64 GiB free space at phase admission and uses the same frozen runtime, harness,
workload, randomized pairs and quiet-host rules. This admission check is not a
continuous low-disk guard. No benchmark acceptance threshold changed.

Remaining: complete the 12 profiler on/off trials, review activation costs against
this main comparison, and document the final SP06 disposition. Do not infer
complete qualification from this checkpoint or erase the original input misses.

## Reproduction

`SHA256SUMS` binds this checkpoint, including each child archive's checksum list.
Each child list binds its raw structured reports, host observations and analysis
snapshot. Recompute every main trial outcome and this review using the pinned
controller environment:

```bash
build/sp06-controller-venv/bin/python \
  docs/architecture/sync-performance-evidence/2026-09-30-sp06-full-stack/review_main.py \
  --repo . --output /tmp/sp06-main-review.json
```

The command verifies both child archives, recomputes recorded main metrics from
trial/profile/cleanup evidence, and preserves all accepted and excluded attempts.
Its result matches `main-review.json`. Original controller records and source
artifact hashes remain embedded in the exported manifests.
