# SY00 qualification evidence

[Contract and decisions](../sy00-capture-probe.md)

Both native targets passed [sync-probe run 35770762748](https://github.com/supabricks/platform/actions/runs/35770762748)
on 2026-09-22, testing merge `73f16d4f77e96844edea3630b29d0a1e2c556123`
from implementation head `62b991cfcbced01a20eb8d410ff89fd969069d9c`.
Each report is the unmodified CI artifact and includes all probe-file hashes.
Inputs are independently hash-verified alpha.35 archives from release run
35700396118; archive receipts and release/engine identities are in each report.
The 12 decoder/model/evidence rejection tests also passed on both targets.

| Target | Real probe checks | Warm commit acknowledgment → decode/model p95 | Whole probe, including inventory verification |
| --- | --- | --- | --- |
| [linux-x86_64](linux-x86_64.json) | 33 | 14.928 ms | 20.646 s |
| [macos-arm64](macos-arm64.json) | 33 | 20.974 ms | 34.387 s |

These latency observations cover 30 sequential one-row commits on a small
synthetic fixture, not end-to-end columnar publication, sustained throughput or a
production SLO. Reports identify columnar apply measurements separately. Harness
RSS excludes most native runtime processes. No governed, power-loss, failover,
streaming transport or durable-spool qualification is claimed.

To validate a retained report from the repository root:

```sh
python e2e/native/sync/qualify.py docs/architecture/sy00-evidence/linux-x86_64.json
python e2e/native/sync/qualify.py docs/architecture/sy00-evidence/macos-arm64.json
```

Earlier local iterations caught a statistics-flush issue in the scan-isolation
measurement and an overbroad DDL fence that included engine startup maintenance.
The harness now explicitly flushes/clears source statistics at the measurement
boundary and scopes its experimental fence. Neither failed iteration is release
or SY00 acceptance evidence. The retained CI runs passed after those fixes.
