# Paired synchronization comparison

Extract SQLite journal contract only; identical native binary, dependencies, profiler, durability, batching, direct process access and serial worker reuse

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1921.109 | 1907.382 |
| 8 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2259.233 | 2225.624 |
| 16 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1891.333 | 1901.688 |
| 16 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2242.329 | 2253.495 |
