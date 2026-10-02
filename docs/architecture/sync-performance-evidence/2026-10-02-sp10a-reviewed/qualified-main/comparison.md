# Paired synchronization comparison

Extract SQLite journal contract only; identical native binary, dependencies, profiler, durability, batching, direct process access and serial worker reuse

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 8 | 1250 | 3/3 · 3/3 | 3/3 · 3/3 | 2999.886 | 3019.729 |
| 16 | 1250 | 3/3 · 3/3 | 3/3 · 3/3 | 3011.311 | 3030.460 |
