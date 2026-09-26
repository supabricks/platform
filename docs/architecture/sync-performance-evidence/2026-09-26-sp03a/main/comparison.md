# Paired synchronization comparison

Qualifying existing SQLite dependencies and adding explicit installation inspection preserves sync behavior and performance; no sync-path or dependency change.

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 3623.791 | 3610.160 |
| 8 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 4583.562 | 4582.938 |
| 16 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 3613.346 | 3613.005 |
| 16 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 4575.429 | 4514.604 |
