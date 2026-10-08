# Paired synchronization comparison

Ingest the same durable capture receipt before admission rather than after it; unchanged reporting, batching, polling, durability and single publisher

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 8 | 1250 | 3/3 · 3/3 | 3/3 · 3/3 | 3430.844 | 3423.543 |
| 16 | 1250 | 3/3 · 3/3 | 3/3 · 3/3 | 3447.005 | 3441.340 |
