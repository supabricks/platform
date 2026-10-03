# Paired synchronization comparison

Move validated bounded SQLite reads into capture-owned private IPC; identical dependencies, profiler, durability, append groups and serial worker reuse

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1901.843 | 1923.498 |
| 8 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2230.075 | 2219.133 |
| 16 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1966.373 | 1943.805 |
| 16 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2247.878 | 2229.079 |
