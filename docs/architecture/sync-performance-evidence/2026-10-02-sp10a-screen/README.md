# SP10a functional screening evidence

Source runtime revision 37a523303232c8bc8b697c3c110305c7d1d14581. Immutable candidate
release 8b0aaff1f1e1fb5e7fad0b32145bd5fd198b0b50b21c48f6a78486332e3d8e51 overlays
only the Python journal contract/policy/SQLite implementation and their bytecode.
Native executable, profiler and dependencies are unchanged from accepted SP09a.
Package proof includes installation verification and before/after hashes.

100 existing analytics tests, four new contract tests and 76 harness tests pass.
The installed durable journal, full lifecycle, short observer-off and observer-on
screens pass. Every installed fixture has zero leaked/remaining descendants and
successful cleanup. These short smoke runs have no quiet-host performance or
freshness qualification claim. Do not treat component throughput as whole-pipeline
capacity or compare smoke timings as a runtime gain.

The frozen campaign is separate and pending. See ../../sync-performance-sp10a.md
for the 96-trial + 12-component/lifecycle measurement contract. Raw private logs
and scratch are excluded. Public receipts retain local provenance paths.
