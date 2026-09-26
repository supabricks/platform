# Fixed SP03a component screen

Nine ten-second trials, three repeats each, with unchanged SP02 32-transaction,
1 MiB, 10 ms grouping and DELETE/FULL durability. Saturated/reader, 500
transactions/s/reader and 500 transactions/s/no-reader cases. The fixed protocol
was committed at da548e7 before measurement; no setting was selected from these
results. All attempts and host observations are retained. Private command logs
are excluded. This is a synthetic capture component check, not end-to-end capacity.

Recompute with `python3 ../component_analysis.py .`.
