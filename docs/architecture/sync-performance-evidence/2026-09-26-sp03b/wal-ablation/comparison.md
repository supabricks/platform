# Paired synchronization comparison

Frozen SP03b implementation: isolate grouping interaction within wal journal mode.

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 16 | 1000 | 3/3 · 0/3 | 3/3 · 3/3 | 93671.745 | 4542.314 |
