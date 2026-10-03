# Paired synchronization comparison

Move validated bounded SQLite reads into capture-owned private IPC; identical dependencies, profiler, durability, append groups and serial worker reuse

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1926.374 | 1920.025 |
| 8 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2249.243 | 2245.462 |
| 16 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1926.346 | 1963.783 |
| 16 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2250.452 | 2214.762 |
