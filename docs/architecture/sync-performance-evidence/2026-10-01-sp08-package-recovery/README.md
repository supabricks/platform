# SP08 package verification recovery

All six corrected-candidate/common-predecessor lifecycle fixtures passed, forming
three quiet matched pairs. Linux and macOS native-cell and installed release-sync
CI also pass. The following common-refactor control completed the accepted-runtime
arm (1,248.659 rows/s, p95 3,708.37 ms), then stopped before daemon startup in the
rebuilt common arm: installation verify rejects an unknown manifest field.
There is no completed performance pair. The stopped comparison is retained here
without changing its running_trial terminal state; cleanup shows no descendants.

Issue #134: overlay provenance belongs in its external proof, because the runtime
release schema rejects unknown fields. Corrected packages use byte-identical native
binaries and shared payloads, omit the extra manifest field, and pass the actual
installation verifier. Both package creation and campaign startup now require that
verification. All 73 benchmark-accounting/package tests pass. Old packages and
failed campaigns remain unchanged. A fresh campaign will repeat qualification.

Three release CI failures remain under investigation: environment lifecycle #135,
macOS notebook recovery #136, and governed data #137. Their relation to SP08 has
not been established. No performance or complete-release acceptance is claimed.

Structured component receipts are unchanged; host streams are losslessly compressed.
The stopped control archive is copied verbatim, including its checksum inventory.
