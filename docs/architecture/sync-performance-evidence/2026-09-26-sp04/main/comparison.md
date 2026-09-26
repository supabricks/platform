# Paired synchronization comparison

Scoped planning inventories reduce directory traversal while preserving sync correctness and safety boundaries

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 3725.838 | 2245.806 |
| 8 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 4508.390 | 3039.302 |
| 16 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 3588.871 | 2270.216 |
| 16 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 4528.923 | 3026.764 |
