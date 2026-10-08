# Paired synchronization comparison

Ingest the same durable capture receipt before admission rather than after it; unchanged reporting, batching, polling, durability and single publisher

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1828.749 | 1863.133 |
| 8 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2706.681 | 2684.834 |
| 16 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 1879.594 | 1898.158 |
| 16 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2710.908 | 2735.023 |
