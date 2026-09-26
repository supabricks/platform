# Paired synchronization comparison

Same unchanged runtime instrumentation off/on; common checkpoint probe activation control.

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 3636.430 | 3612.060 |
| 8 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 4577.678 | 4610.349 |
| 16 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 4542.164 | 4594.736 |
