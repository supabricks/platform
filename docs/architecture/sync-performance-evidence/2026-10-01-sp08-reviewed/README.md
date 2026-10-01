# SP08 reviewed evidence

Decision: keep the scheduling change for latency. Merge/release readiness remains
gated by CI issues #135 and #137; the #136 macOS notebook rerun passed without
a targeted fix. See ../../sync-performance-sp08.md for the results and limits.

Final campaign: 84 performance/control trials and six lifecycle fixtures, all
correct/fresh with clean teardown. No contention replacement occurred. The 24
historical main trials include four-client overload attempts that do not achieve
the offered 1,000 rows/s; no throughput improvement is attributed to SP08. The 12
source-qualified main trials all meet the offered 1,250 rows/s tolerance.

Each comparison directory is the controller's unchanged structured archive,
including its own SHA256SUMS. Component host JSONL is losslessly compressed.
review-results.json retains individual outcomes, ranges, paired changes, stage
percentiles and controls. review_results.py revalidates every raw receipt hash
and reconstructs metrics with the frozen controller's load_trial/expected code.
For reproduction in this workspace, run the script from build/sp08-20261001
with build/sp06-controller-venv/bin/python; it expects campaign-03 and harness-03
there. It launches no workloads and makes no changes to the source evidence.

The independent stage distributions cannot be added. Apply-worker totals span
the whole fixture and are comparable only within an identical workload profile.
Three repeats are screening evidence, not a confidence interval or universal
capacity guarantee. Earlier shutdown and package-verification failures remain in
their original dated evidence directories and are not counted as successful runs.
