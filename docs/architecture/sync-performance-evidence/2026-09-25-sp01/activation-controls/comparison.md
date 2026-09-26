# Paired synchronization comparison

Unchanged SP01 runtime with profiler off/on; three balanced activation controls at 4 CPUs and 50 rows/s.

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 3792.262 | 3713.210 |
