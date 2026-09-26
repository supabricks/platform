# Paired synchronization comparison

Final candidate instrumentation activation at the formerly failing load; not an additional runtime variant

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 8 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 4549.422 | 4554.581 |
| 16 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 4589.127 | 4521.714 |
