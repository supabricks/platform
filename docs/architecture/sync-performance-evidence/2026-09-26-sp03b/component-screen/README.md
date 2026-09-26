# Frozen SP03b component screen

All 30 declared trials completed correctly without detected external-build overlap at frozen runtime/harness `5382e80`. Includes 24 batching × journal-mode × offered-rate factorial trials and six profiler activation controls. Ten seconds each, FULL durability, bounded reader and pruning. All retained payloads, predecessor links and the pruned-prefix count were verified after stopping. This measures synthetic capture only, not PostgreSQL-to-analytics capacity.

Recompute: `python3 ../component_analysis.py .`. Profiler-disabled sync counters are unavailable and excluded from sync-cost summaries.
