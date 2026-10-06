# Paired synchronization comparison

Correct control JSON replacement races without changing SQLite storage, source workload, SQL observer or profiler; measure validation overhead separately before the RocksDB comparison.

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 8 | 1250 | 3/3 · 3/3 | 3/3 · 3/3 | 2997.729 | 3022.116 |
| 16 | 1250 | 3/3 · 3/3 | 3/3 · 3/3 | 3014.349 | 3026.171 |
