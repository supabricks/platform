# Owner replacement correction: reviewed qualification

Decision: **keep for reliability**, and use the corrected SQLite owner as the
SP10c comparison baseline. All 108 fixtures passed, with zero runtime failures,
contention replacements or cleanup leaks. Raw receipt hashes were verified and
comparison metrics reconstructed with frozen harness `0682839`. No speedup or
full-release qualification is claimed.

At qualified 1,250 rows/s input, predecessor → candidate median source rates were
1,249.597 → 1,249.589 (8 CPUs) and 1,249.842 → 1,249.871 (16 CPUs).
Median p95 publication latency was 3,013.058 → 3,004.872 ms and
3,011.085 → 3,008.505 ms. Paired median CPU changes were +0.391% and 0.000%;
RSS changes were −0.347% and −0.375%. No main investigation screen fired.
Historical four-client overload remained source-limited and is not capacity proof.

Isolated 32/512/4,096-record read latency paired median changes were
+4.78%/+0.56%/+0.41%; corresponding CPU changes were +6.64%/+0.81%/−0.54%.
The 32-record median latency increased from 1.112 to 1.165 ms. These costs are
retained rather than subtracted from pipeline measurements. Each of three fresh
fixtures contains 16 correlated requests per size. Profiler activation added
7.60%/8.70% paired median CPU at 8/16 CPUs; corrected-owner observer activation
added 1.37%/1.65%. Stage percentiles are not additive causal shares.

Three pairs per cell do not prove statistical equivalence or sustained capacity.
The observer bridge, same-interface engine comparison, comparison with direct
SQLite, and sustained runs still precede any RocksDB adoption decision.

Four matrix directories retain sanitized structured archives and original inner
checksum manifests. Fixture receipts retain original and exported hashes;
host samples are compressed. Private runtime scratch and logs are excluded.
`owner-fix-review-results.json` retains all trial results and screening metrics;
`decision.json` records review separately from mechanical validation. Reconstruct
against the original local evidence with:

```
build/sp06-controller-venv/bin/python review_owner_fix.py build/sp10c-20261003
```

The script reads `harness-06` and `owner-fix-campaign-01`. The outer `SHA256SUMS`
covers every exported file except itself. The export manifest records the initial
export before this README and decision were added.
