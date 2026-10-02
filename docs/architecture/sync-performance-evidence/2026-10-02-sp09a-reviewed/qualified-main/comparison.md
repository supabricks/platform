# Paired synchronization comparison

Reuse only imported worker runtime within the same authorized generation; unchanged batching, journal, durability and single publication writer

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 8 | 1250 | 3/3 · 3/3 | 3/3 · 3/3 | 3402.798 | 2995.737 |
| 16 | 1250 | 3/3 · 3/3 | 3/3 · 3/3 | 3411.920 | 3031.190 |
