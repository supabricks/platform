# Paired synchronization comparison

Extract SQLite journal contract only; identical native binary, dependencies, profiler, durability, batching, direct process access and serial worker reuse

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 8 | 1250 | 3/3 · 3/3 | 3/3 · 3/3 | 3015.022 | 3009.265 |
| 16 | 1250 | 3/3 · 3/3 | 3/3 · 3/3 | 3015.233 | 3022.302 |
