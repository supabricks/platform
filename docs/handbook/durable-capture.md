# Local durable capture (SY02)

[Analytical workspace](analytical-workspace.md) · [Capture contract](../architecture/sy02-durable-capture.md)

This explicit local-owner command prepares the capture foundation for incremental
analytics. Capture alone does not update a Sail snapshot. Use [SY03 explicit
incremental application](incremental-epochs.md) or SY01 full snapshot runs to
publish analytical results.

With the SY02 native binary and analytical worker installed/configured, create a
manual snapshot policy on a running branch, then enroll its current revision:

```bash
supabricks sync create --branch main --key manual-policy
supabricks sync capture start POLICY_ID --revision 1 --key capture-start
supabricks sync capture status CAPTURE_ID
```

`start` accepts `--spool-bytes` (16–512 MiB) and `--wal-bytes` (32–512 MiB in whole
MiB); both default to 512 MiB. The entire admitted source must match the
[supported profile](../architecture/sy02-durable-capture.md#contract-and-identity).
Only one generation can be enrolled per installation.

Status distinguishes requested/bootstrapping/capturing, paused, unavailable,
resync-required, deleting and deleted states. It reports observation time, slot
start, verified bootstrap boundary, captured commit boundary, observed source WAL,
spool size and retained WAL. These are capture boundaries, not a published epoch
or an end-to-end replication-lag guarantee.

```bash
supabricks sync capture pause CAPTURE_ID --key capture-pause
supabricks sync capture resume CAPTURE_ID --key capture-resume
supabricks sync capture delete CAPTURE_ID --key capture-delete
```

Pause keeps the source slot, monitor and compute lease. Writes can exhaust retained
WAL while paused. Deletion waits for owned source cleanup and removes the journal;
resume a forcibly suspended source if cleanup is pending. A source retention cap
remains configured after deletion. Reuse the same retry key only for the identical
command; use a new key for a new intent.

On `resync_required`, inspect the error, correct the unsupported schema or resource
condition, delete the generation, wait for `deleted`, and explicitly start a new
one. Restore requires the same explicit re-enrollment. Changing or pausing the
attached snapshot policy fences its capture revision. Opening the console or
running a full snapshot does not enable capture.
