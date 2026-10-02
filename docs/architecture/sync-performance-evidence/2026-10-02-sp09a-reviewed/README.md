# SP09a reviewed evidence

Decision: keep bounded worker reuse. All 84 accepted performance/control trials
and six lifecycle fixtures completed; raw receipt hashes and frozen-controller
metric reconstruction passed. The interrupted, unmonitored pair is excluded and
repeated in full. See ../../sync-performance-sp09a.md for results and limitations.

Comparison folders retain the sanitized structured archives and their SHA256SUMS.
review-results.json contains individual outcomes and paired changes. Reproduce
with build/sp06-controller-venv/bin/python review_results.py build/sp09a-20261001;
it reads campaign-02, continuation-01 and frozen harness-02 without launching work.
Profiles omit source values, SQL and credentials. Local provenance paths are retained
in configuration/component receipts. Earlier failed campaigns remain documented.

Three repeats per cell are screening evidence, not statistical confidence or a
sustained capacity guarantee. Independent percentiles cannot be added or subtracted
to attribute causal shares. The four-client historical overload does not attain
its offered load; the qualified eight-client workload does. Profiler CPU/memory
costs are reported explicitly rather than subtracted from measured runtime gains.
