# SP11 steady-baseline launch evidence

The harness is frozen at `b3fe029`; the immutable runtime remains the separately
qualified #157 SQLite-owner package. `steady-config-01.json` replaces the local
repository root with `<platform>`; `launch.json` records the original config hash.
The campaign has six 15-minute and two 60-minute fixtures, eight clients and
1,250 offered changed rows/s. Each fresh fixture has 60 seconds of warmup.

All 92 harness tests pass, including seven SP11 gate tests. The ten-second screen
verifies event export, analysis, exact equality, reopen and cleanup only. It
missed offered input and is explicitly excluded from capacity qualification.
`validation.json` retains hashes of all original screen artifacts; compact
cleanup/history/reopen/window receipts are included here. No production benchmark
result or completed SP11 gate is claimed by launch.

The service owns a continuous quiet-host monitor and stops on a failed or
contaminated fixture. Runtime, workload and instrumentation remain frozen across
repeats. Other SP11 maintenance, pressure, scaling, interference and recovery
phases remain required; see the [protocol](../../sync-performance-sp11.md).
