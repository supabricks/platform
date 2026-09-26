# Paired synchronization comparison

FULL/DELETE durable groups reduce sync work per source transaction while preserving complete ordered replay and bounded feedback; fresh SP01 predecessor controls host drift.

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 3697.001 | 3697.623 |
| 8 | 1000 | 0/3 · 0/3 | 3/3 · 3/3 | Unavailable | 4568.230 |
| 16 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 3718.995 | 3664.476 |
| 16 | 1000 | 0/3 · 0/3 | 3/3 · 3/3 | Unavailable | 4596.328 |
