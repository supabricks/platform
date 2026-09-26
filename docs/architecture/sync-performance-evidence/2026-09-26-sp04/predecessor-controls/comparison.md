# Paired synchronization comparison

Scoped planning inventories reduce directory traversal while preserving sync correctness and safety boundaries

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 3669.768 | 3677.945 |
| 8 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 4554.427 | 4531.584 |
| 16 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 4539.237 | 4583.822 |
