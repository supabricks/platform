# Paired synchronization comparison

Move validated bounded SQLite reads into capture-owned private IPC; identical dependencies, profiler, durability, append groups and serial worker reuse

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 8 | 1250 | 3/3 · 3/3 | 3/3 · 3/3 | 3001.736 | 3002.198 |
| 16 | 1250 | 3/3 · 3/3 | 3/3 · 3/3 | 3024.916 | 3015.008 |
