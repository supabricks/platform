# Paired synchronization comparison

Correct control JSON replacement races without changing SQLite storage, source workload, SQL observer or profiler; measure validation overhead separately before the RocksDB comparison.

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 8 | 1250 | 3/3 · 3/3 | 3/3 · 3/3 | 3013.058 | 3004.872 |
| 16 | 1250 | 3/3 · 3/3 | 3/3 · 3/3 | 3011.085 | 3008.505 |
