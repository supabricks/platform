# Paired synchronization comparison

Reuse only imported worker runtime within the same authorized generation; unchanged batching, journal, durability and single publication writer

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1858.224 | 1919.224 |
| 8 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2235.715 | 2218.861 |
| 16 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1891.452 | 1979.713 |
| 16 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2251.994 | 2267.002 |
