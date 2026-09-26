# SP03b supplemental component observer controls

Declared before these supplemental measurements. The production runtime and
benchmark harness remain frozen at `5382e80032bb4fc70706f112db26f8415849a961`.
The original 30-component and 72-full-stack protocol remains unchanged.

The original six component activation controls disable native sync instrumentation
but retain the bounded read-only reader. They measure native instrumentation cost;
they do not isolate that reader's observation overhead. To satisfy the capture
milestone's observer-disabled throughput check explicitly, add six component
trials after the declared full-stack series finishes.

Run three fresh pairs of grouped WAL/FULL at saturated input, 10 seconds each,
with native profiling disabled in both arms. Toggle only the benchmark's read-only
reader (`capture_groups.py --reader` versus no `--reader`). Balance pair order:
reader on/off, off/on, on/off. Keep SP02 groups (32 transactions, 1 MiB, 10 ms),
pruning, package, filesystem and checkpoint settings unchanged. Use the existing
`--no-profile` flag; do not add a runtime or measurement hook.

Require the same five-minute quiet-host admission before each trial. Retain and
replace whole contended pairs. A runtime failure remains an outcome; it cannot
be replaced for performance. Verify every retained payload/link and pruned-prefix
count after stopping, using the existing independent correctness check. No active
read observer or native profiler is present in the reader-off arm. Minimal clocks,
transaction counts and group bookkeeping still remain in the driver.

Report achieved transactions/s, CPU and physical bytes with individual results
and three-pair medians. Disabled sync counters are unavailable, not zero. These
controls establish component observation overhead only; they do not measure
PostgreSQL input capacity, publication lag or end-to-end throughput. Retain them
separately from the original factorial and profiling controls. Do not select or
change production settings from these runs.
