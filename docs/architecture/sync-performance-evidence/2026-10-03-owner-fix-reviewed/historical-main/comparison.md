# Paired synchronization comparison

Correct control JSON replacement races without changing SQLite storage, source workload, SQL observer or profiler; measure validation overhead separately before the RocksDB comparison.

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1926.646 | 1901.300 |
| 8 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2267.401 | 2253.199 |
| 16 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1915.207 | 1960.515 |
| 16 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2262.414 | 2244.616 |
