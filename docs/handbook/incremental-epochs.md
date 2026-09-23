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
Matching [SY07 binaries/workers](../architecture/sy07-sync-hardening.md) add
published-spool pruning and periodic compaction into a new generation. Explicit
history collection can reclaim old generations while the capture remains live;
pins and catalog references still prevent deletion. Finite storage and durable
run-journal limits remain. [SY04](triggered-sync.md) adds triggered policies,
[SY05](continuous-sync.md) continuous scheduling, and SY06 governed/catalog integration.
