# Local incremental epochs (SY03)

[Capture setup](durable-capture.md) · [Contract and limits](../architecture/sy03-incremental-epochs.md)

Use the SY03 binary and analytical runtime together on a local-owner native cell.
Enroll a manual snapshot policy using the capture workflow, then wait for capture
status `capturing` with a verified bootstrap boundary.

```bash
supabricks sync apply CAPTURE_ID --key baseline-1
supabricks sync applied RUN_ID
```

The first run publishes the frozen baseline. Wait for `succeeded`; its `epoch_id`
is available through the existing snapshot/session APIs. Later explicit runs apply
complete captured transactions to that baseline:

```bash
supabricks sync apply CAPTURE_ID --key changes-1
supabricks sync applied RUN_ID
```

Inspect `target_lsn`, `applied_lsn`, state and error. The target is sampled when
admitted; the bounded batch can stop before it. Submit a new key for another batch.
Retry the same intent with the same key; status comes from `sync applied`, since
an admission retry returns the original receipt. Existing Sail readers stay pinned
to their old epoch; open a new session to read the new head. Publication exposes
all table versions together. A no-change batch may publish the same map again.

```bash
supabricks sync cancel-apply RUN_ID --key cancel-1
```

Cancellation fences the capture because some private table commits may already
exist. On `resync_required`, delete that capture, wait for `deleted`, and enroll a
new baseline. Publishing a full snapshot from another workflow also requires new
enrollment before further incremental application. Restore does not automatically
resume copied capture/apply intent.

Capture deletion removes source resources and the spool; retained epochs still
keep their shared analytical root. Close sessions/unpin snapshots and use the
existing snapshot retention workflow before the final root can be collected.
Finite spool and Delta budgets still apply: SY03 has no background compaction or
spool pruning. Triggered/continuous scheduling and Unity Catalog publication of
these versioned roots are not enabled in this slice.
