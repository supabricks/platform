# SP10b reviewed evidence

Decision: accept the measured private owner interface as the baseline for SP10c's
fair engine comparison. No performance improvement is claimed. All 108 fixtures
passed, with no contention replacements, runtime failures or cleanup leaks. See
[the review](../../sync-performance-sp10b.md) for paired results, costs and limits.

The four comparison folders retain sanitized structured archives and their checksum
lists. Fixture receipts preserve original and exported hashes; host samples are
compressed. Private runtime scratch/logs are excluded. `review-results.json`
contains all individual trials and component/control results. Reconstruct against
the original local frozen evidence with:

```
build/sp06-controller-venv/bin/python review_results.py build/sp10b-20261002
```

The script reads harness-02/campaign-01, verifies raw hashes, recomputes comparison
metrics with the frozen loader, checks fixture correctness/cleanup and produces
screen flags. Its mechanical status remains `all_receipts_verified_review_required`;
`decision.json` records the subsequent review, distinct from automatic execution.

Component fixtures contain 16 correlated measured requests at each of three sizes;
only the three fresh fixtures per arm are separate replicates. Latency medians are
medians of per-fixture medians, and percentages are medians of paired changes.
Independent percentiles cannot be added/subtracted as causal shares. The historical
four-client overload does not attain offered input; qualified eight-client input
does. Component read costs are not PostgreSQL-to-Delta throughput. Profiler and
observer activation costs remain explicit, without subtraction from runtime costs.

`ci-review.json` retains sanitized failure excerpts, original artifact hashes and
links: 44 checks passed, including every required check and both installed sync
gates, but two installed release jobs failed on aabbb16. Issues #110/#148 remain
unresolved; no all-green release claim. Issue #147 tracks read-transport costs.
Screen/receipt-schema correction evidence remains in ../2026-10-02-sp10b-screen/.

The export manifest describes files at export time. The outer SHA256SUMS covers
all final files except itself, including the separately written decision/CI review.
