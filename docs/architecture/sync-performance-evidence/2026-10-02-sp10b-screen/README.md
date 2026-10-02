# SP10b installed screens

Candidate source a37bf26 / native b8ce4f2 passes the owner read component,
full lifecycle (same-process reuse, worker kill, pause/restart/schema fences,
atomic/pinned epochs) and five-second observer off/on screens. The predecessor
also passes the identical read component through direct SQLite. Every successful
fixture reports zero leaked/remaining descendants and exit zero.

Initial candidate b8ce4f2 passes its read component but fails lifecycle on the
strict native journal receipt schema; issue #146 records the diagnosis/correction.
That failure and its clean teardown are retained. No measurement used that candidate.

All 119 current analytics tests, 334 native tests (four intentionally ignored),
and 76 benchmark harness tests pass. Source-validation records log hashes and the
queue-pressure test correction. Package proofs retain each replacement and validate
all shared payloads. Profiler/dependencies are unchanged; the candidate has native
sync-profile capability. Runtime candidate-02, config-02 and harness-02 are frozen.

These are functional screens under potentially contended host conditions, not
performance qualification. Do not use their timing as slice improvement evidence.
See ../../sync-performance-sp10b.md for the separately declared measured campaign.
Local workspace paths are replaced by stable labels; no private runtime logs or
scratch are exported. SHA256SUMS covers every exported file except itself.
