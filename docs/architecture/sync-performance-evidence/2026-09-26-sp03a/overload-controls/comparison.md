# Paired synchronization comparison

Same runtime instrumentation off/on overload controls; unchanged SP02 probes.

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 8 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 4606.892 | 4522.246 |
| 16 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 4535.508 | 4603.596 |
