# Paired synchronization comparison

Ingest the same durable capture receipt before admission rather than after it; unchanged reporting, batching, polling, durability and single publisher

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 4 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 2221.452 | 1871.280 |
| 8 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 2994.492 | 2727.365 |
| 16 | 50 | 3/3 · 3/3 | 3/3 · 3/3 | 2264.288 | 1894.364 |
| 16 | 1000 | 3/3 · 3/3 | 3/3 · 3/3 | 3022.478 | 2722.384 |
