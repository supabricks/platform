# Paired synchronization comparison

Bounded pre-mutation BUSY recovery reduces unnecessary resync while preserving correctness; throughput need not improve.

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 2/3 | 3/3 · 3/3 | 3888.404 | 3627.911 |
| 8 | 1000 | 0/3 · 0/3 | 0/3 · 0/3 | Unavailable | Unavailable |
| 16 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 3636.734 | 3634.075 |
| 16 | 1000 | 0/3 · 0/3 | 0/3 · 0/3 | Unavailable | Unavailable |
