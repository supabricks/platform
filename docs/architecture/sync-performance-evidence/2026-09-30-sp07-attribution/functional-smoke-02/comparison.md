# Paired synchronization comparison

Host I/O observer off/on with identical runtime and existing worker profiling; attribution only, not runtime speedup or replacement for SP06 misses

Trial medians and ranges; failed trials have no complete latency. Individual pairs and all metrics are in comparison.json.

| CPUs | Offered rows/s | Predecessor complete / fresh | Candidate complete / fresh | Predecessor p95 median (ms) | Candidate p95 median (ms) |
| --- | --- | --- | --- | --- | --- |
| 8 | 50 | 1/1 · 1/1 | 1/1 · 1/1 | 2067.594 | 2029.927 |
