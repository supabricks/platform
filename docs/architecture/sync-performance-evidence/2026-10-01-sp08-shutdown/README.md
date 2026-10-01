# SP08 initial candidate: shutdown regression

The first quiet native component pair retained here has a passing predecessor
and a candidate lifecycle failure at daemon restart. Both fixtures cleaned up
without leaks. This is a retained runtime failure, not a contended trial or an
accepted performance comparison. The main matrices never started.

The candidate passed bootstrap, idle no-change behavior, source atomicity,
sustained load, and pause/resume before failing shutdown/restart. Its earlier
latency observations are partial diagnostic evidence, not performance acceptance.
Linux and macOS native-cell CI independently failed the capture restart check
with the same replication_stream_failed:cleanup_pending terminal state.

The correction prevents early receipt ingestion during source teardown.
Issue: [#133](https://github.com/supabricks/platform/issues/133). The original
campaign and packages remain unchanged under build/sp08-20261001/. A corrected
candidate requires a newly frozen package and fresh campaign.

Structured receipts are copied byte-for-byte. Host JSONL streams are losslessly
gzip-compressed. Private logs stay local; the disposable failed container was
removed after clean descendant teardown.
