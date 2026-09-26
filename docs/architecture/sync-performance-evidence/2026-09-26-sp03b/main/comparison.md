# Paired synchronization comparison

Capture-only WAL/FULL with bounded checkpoints improves durable capture reader concurrency over SP03a batching; no source capacity claim.

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 3712.740 | 3498.586 |
| 8 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 4538.615 | 4524.161 |
| 16 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 3635.017 | 3376.941 |
| 16 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 4559.767 | 4465.064 |
