# Paired synchronization comparison

Correct control JSON replacement races without changing SQLite storage, source workload, SQL observer or profiler; measure validation overhead separately before the RocksDB comparison.

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1885.936 | 1894.943 |
| 8 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2229.894 | 2252.095 |
| 16 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1852.198 | 1919.825 |
| 16 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2236.008 | 2246.470 |
