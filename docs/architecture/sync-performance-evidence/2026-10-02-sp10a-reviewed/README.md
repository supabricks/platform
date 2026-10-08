# SP10a reviewed evidence

Decision: retain the SQLite journal contract. All 96 performance/control trials
and 12 component/lifecycle fixtures pass, with no replacements. See
[the review](../../sync-performance-sp10a.md) for results and limitations.

Comparison folders retain sanitized structured archives, original hashes and
SHA256SUMS. Fixture receipts retain original and exported hashes. Host samples
are compressed; private runtime scratch and logs are excluded. The outer manifest
records exported files before this README and the final checksum list.

`review-results.json` retains individual results and paired changes. Reproduce
against original local evidence with:

```
build/sp06-controller-venv/bin/python review_results.py build/sp10a-20261002
```

This reads frozen harness-01 and completed campaign-01 without starting workloads.
Every trial is reconstructed from raw evidence with the frozen comparison loader;
all fixture receipts, cleanup and independent correctness outcomes are verified.
The source validation JSON retains its historical checkpoint status; final campaign
status and this review supersede that checkpoint.

Three repeats are regression screens, not statistical equivalence. Historical
four-client overload remains source-limited. Profiler CPU and low-load freshness
costs are explicit; observer controls cannot qualify p95. Stage percentiles are
separate distributions, not additive causal shares. No performance gain is claimed.
