# Triggered incremental synchronization (SY04)

[Contract and limits](../architecture/sy04-triggered-sync.md) · [Capture recovery](durable-capture.md)

Use a matching SY04 binary and analytical runtime on a running local-owner native
source. Creating this policy enables background capture and a frozen bootstrap;
capture stays active between runs and can keep compute awake. The source must
match the existing integer-primary-key capture profile.

```bash
supabricks sync create --branch main --mode triggered --key orders-triggered
supabricks sync run POLICY_ID --revision 1 --key first-run
supabricks sync status RUN_ID
```

`--mode triggered` defaults to `--strategy incremental`. Wait for `succeeded`.
The policy also reports `capture_id`, so capture status and cleanup remain
accessible if bootstrap fails. Run status reports `capture_id`, the eventual fixed `target_lsn`, last published
`source_lsn`, `epoch_id`, batch count, deadline and errors. Target acquisition waits
for a transactional source barrier after bootstrap; later commits are reserved for
a future run. Large runs may publish intermediate complete epochs before reaching
the target. Existing sessions keep their pinned inputs; open a new session to read
the latest published epoch.

Retry the same intent with the same key. Use a new key for a new run; overlapping
manual requests are rejected. An idle run completes without rewriting table data.

```bash
supabricks sync update POLICY_ID --revision 1 --mode triggered --every-seconds 300 --key schedule
supabricks sync runs POLICY_ID
supabricks sync show POLICY_ID
```

Schedules use UTC intervals and coalesce missed ticks after sleep/restart. Updates
replace the full configuration; include `--mode triggered` when editing it. Read
the returned revision for subsequent commands. Omit `--every-seconds` to remove a
schedule. `--timeout-ms` bounds the entire run, including bootstrap/target waiting;
`--max-bytes` bounds the bootstrap export only.

```bash
supabricks sync pause POLICY_ID --revision REVISION --key pause
supabricks sync resume POLICY_ID --revision REVISION --key resume
supabricks sync cancel RUN_ID --key cancel
supabricks sync delete POLICY_ID --revision REVISION --key delete
```

An idle pause keeps capture buffering under its quotas. Cancelling or revising an
unfinished batch can require resync, preserving the last complete epoch. A final
publication that already committed cannot be cancelled. Policy deletion also cleans
up owned capture resources; retained readers can keep analytical files alive.

To resync, explicitly delete the failed capture, wait for `deleted`, then enroll
again against the active policy's current revision:

```bash
supabricks sync capture delete CAPTURE_ID --key retire-capture
supabricks sync capture status CAPTURE_ID
supabricks sync capture start POLICY_ID --revision REVISION --key new-baseline
supabricks sync run POLICY_ID --revision REVISION --key after-resync
```

Finite spool/root budgets apply; pruning and compaction are not implemented yet.
Restored policies require explicit resume and capture re-enrollment. Console and
governed controls and incremental catalog publication remain later slices.
[SY05 continuous mode](continuous-sync.md) can reuse a drained triggered capture
when the policy switches between the two incremental modes.
