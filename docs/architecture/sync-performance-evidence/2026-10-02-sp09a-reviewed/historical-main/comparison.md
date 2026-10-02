# Paired synchronization comparison

Reuse only imported worker runtime within the same authorized generation; unchanged batching, journal, durability and single publication writer

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1962.982 | 1922.498 |
| 8 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2686.003 | 2211.443 |
| 16 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1934.656 | 1941.783 |
| 16 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2686.576 | 2222.964 |
