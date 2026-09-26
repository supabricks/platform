# Final SP02 component screen

33 randomized ten-second trials, three repetitions of each configuration; all passed independent stopped-file checks and none overlapped observed external builds. `screen.json` retains the seed, frozen source hashes, quiet receipts and attempts. The declaration preceded this screen; the result applies its unchanged selection thresholds.

Runtime source: `838f0b1d4977489526bbd6e77d036569f3df83a9`. Native counters use the unchanged SP01 `profile_io.so`. Each trial uses a fresh on-disk SQLite spool, FULL durability, DELETE journaling, publication-authorized pruning, and optional bounded readers. `single=true` is a grouping-disabled candidate ablation, not the fresh SP01 predecessor in the full matrix.

The synthetic 103-byte complete transactions contain two small row events. This component screen excludes PostgreSQL, decoding, Delta and publication; actual source transactions can differ in width. Native sync totals include pruning. Reader-disabled trials retain the same independent final payload, checksum, chain and prefix-count checks; they do not measure whole-stack observer overhead. Process-crash qualification is distinct from power loss.

Recompute `summary.json` with `python3 ../component_analysis.py .`. Private logs and scratch files are excluded.
