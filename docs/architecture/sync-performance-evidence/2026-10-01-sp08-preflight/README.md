# SP08 preflight evidence

Implementation candidate, not performance acceptance. Native lifecycle and paired
measurements remain pending. Local Rust tests: 207 pass, four ignored. Benchmark
accounting tests: 71 pass. Both packages replace only the native binary in the
immutable accepted SP04 package; all shared payload hashes and executable bits
were verified. Dependencies and Python worker instrumentation are unchanged.

The common predecessor extracts capture observation at its original point and
adds the same observation profiler span as the candidate. A separate bridge and
activation controls must qualify this before the runtime comparison. Compiler
and source identities are in provenance.json; these are unsigned diagnostic
packages, not release artifacts.

Raw ongoing campaign: build/sp08-20261001/campaign. Do not count these preflight
receipts as benchmark trials. See the [protocol](../../sync-performance-sp08.md).
