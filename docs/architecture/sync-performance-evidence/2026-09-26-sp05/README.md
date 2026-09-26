# SP05 maintenance assessment evidence

This is an **offline assessment**, not a new benchmark or runtime slice.
[Decision and interpretation](../../sync-performance-sp05.md): defer maintenance
policy tuning under the plan's explicit conditional path. No speedup is claimed.

`analyze_maintenance.py` verifies the immutable SP04 manifests and reads all 24
existing main trials: 12 accepted SP04 candidate trials and 12 SP03b predecessors.
It preserves individual results and three-repeat median/range summaries. No
profiler, runtime, configuration, workload or measurement harness was changed.
The existing raw trial/profile/host/cleanup records remain in the sibling
[SP04 archive](../2026-09-26-sp04/README.md), without duplicating or relabeling them
as SP05 trials. Its root manifest hash is bound inside `maintenance-summary.json`.
The root manifest also binds the SP04 controls and signed qualification receipts.

Reproduce with the Python standard library from this directory:

```sh
python3 -B analyze_maintenance.py > /tmp/sp05-maintenance-reproduced.json
cmp maintenance-summary.json /tmp/sp05-maintenance-reproduced.json
sha256sum -c SHA256SUMS
```

Capture and daemon deltas use their first/last snapshots inside the measured
window and omit the edges. Inclusive checkpoint/prune and publication stages can
overlap; elapsed percentages must not be summed, interpreted as CPU percentages,
or turned into a predicted throughput improvement. Maxima cover full recorded
worker lifetimes. Apply summaries select completed successful workers started
during load; their lifetimes can cross the window, with excluded workers listed.
No compaction calls were observed; zero counts mean unexercised rotation rather
than free compaction. Short runs cannot establish sustained storage plateaus.

The two signed maintenance gates prove functional rollover, pins, reclamation,
restart and backup recovery on the accepted runtime. They are not sustained
performance trials. Issue #116's observed transient-reader WAL high-water mark
remains an open follow-up. Neither it nor #119 is resolved by this deferral.

`SHA256SUMS` binds every local evidence file except itself. The analysis validates
the referenced SP04 root and main manifests before emitting results.
