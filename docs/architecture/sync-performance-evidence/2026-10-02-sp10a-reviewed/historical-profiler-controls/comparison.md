# Paired synchronization comparison

Extract SQLite journal contract only; identical native binary, dependencies, profiler, durability, batching, direct process access and serial worker reuse

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1855.752 | 1956.468 |
| 8 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2240.713 | 2221.627 |
| 16 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1782.969 | 1925.421 |
| 16 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2256.865 | 2248.735 |
