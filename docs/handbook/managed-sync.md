# Managed sync in the console and agents

[Contract and remaining gates](../architecture/sy06-sync-surfaces.md) · [Continuous sync](continuous-sync.md)

In the local console, open **Database workspace → Analytics** and select the
source branch. **Managed analytical sync** contains the policy and recent runs.
Inspect prerequisites before creating a policy. Inspection is read-only; detailed
source qualification happens when the worker starts. A failed qualification does
not replace the last complete publication.

Choose snapshot for a full copy, triggered for bounded incremental catch-up, or
continuous for supervised incremental publication. Snapshot/triggered intervals
are UTC seconds (60–2592000); 0 in the console means manual. Continuous defaults
to desired freshness 5000 ms and a minimum batch interval of 500 ms. Accept the
compute/resource disclosure before enabling it. The panel preserves existing
export budgets when changing settings.

Pause stops new publication at the safe boundary described by the mode. Capture
can remain active and retain WAL while paused. Check the returned state before
resuming or changing modes. Cancel applies to one admitted run and can require
resync if a private batch was fenced. Delete stops the policy and retires capture;
it does not remove retained publications used by readers.

If a response is lost, inspect policy/run state and use **Retry exact sync
request**. The original key and revision identify the accepted intent. Dismiss
that retry only after checking the outcome; a fresh key is a new request.

## Full resync

Use **Review full resync**, then approve its stated full-copy and cleanup effect.
The policy pauses and retires its capture. Once cleanup finishes, explicitly
resume. Until a new bootstrap is complete, readers can keep the preceding
authorized epoch. Equivalent CLI commands:

```bash
supabricks sync inspect --branch main
supabricks sync review-resync POLICY_ID
supabricks sync resync POLICY_ID --revision REVISION --review-hash HASH --key resync-1
supabricks sync show POLICY_ID
supabricks sync resume POLICY_ID --revision RETURNED_REVISION --key resume-1
```

Agents use MCP `managed_snapshots` with a `command` from the
[shared schema](../../schemas/sync-command-v1.schema.json). Inspect/list/get/runs
are read-only. Creation, settings and lifecycle changes require explicit intent,
stable keys and the current policy revision. Results are returned under `value`.

## Governed sync policies

On the signed-in Data page, select a branch and enter an enabled service principal
when creating a policy. A realm administrator must grant:

- Manager: project membership and branch Read, Manage sync and Read sync.
- Service: project membership and branch Read and Execute sync.
- Sharing/review: the existing administrator, Read sync and Share permissions;
  readers still need UC SELECT and governed execution authority.

The browser session authorizes the command; the stored policy uses the service.
Signing out does not stop that service. Revoke/disable the service or remove its
source authority to fence work and new readers. Producer authorization changes
also fence saved policies conservatively. Review grants, retire any affected UC
publication, and delete/recreate the policy explicitly. Existing snapshots are
retained for recovery but cannot be reopened using revoked authority.

Select a successful run's epoch for sharing review; creating a sync policy never
grants or publishes UC access automatically. An incremental sharing review reports
the extra immutable copy and its byte count. Only the selected epoch's active
files are shared; later sync results require another explicit publication.
Views are bounded to 256 MiB, 4096 files and 128 tables. Triggered/continuous
controls require matching native workers. Older runtimes show an unavailable message.

Source checks (disposable native roots only):

```bash
python e2e/native/sync_surfaces.py --binary target/release/supabricks \
  --bundle ENGINE --helpers HELPERS --python PYTHON --worker python/analytics/export.py \
  --catalog-runtime UC_RUNTIME --report /tmp/sy06-native.json
# From console/, with paths resolved relative to that directory:
node scripts/qualify.mjs --binary ../target/release/supabricks \
  --bundle ENGINE --helpers HELPERS --python PYTHON --worker ../python/analytics/export.py \
  --slice sync --sync true --report /tmp/sy06-browser.json
```

The governed browser suite uses disposable Keycloak, PostgreSQL, UC and the pinned
execution configuration prepared by `e2e/native/execution/prepare.py`:

```bash
python e2e/native/governed-console/qualify.py --binary target/release/supabricks \
  --release BASELINE_RELEASE --uc-runtime UC_RUNTIME --execution-config EXECUTION_CONFIG \
  --console console --sync --python PYTHON --worker python/analytics/export.py \
  --report /tmp/sy06-governed-browser.json
SUPABRICKS_UC093_RUNTIME=UC_RUNTIME SUPABRICKS_UC094_CONFIG=EXECUTION_CONFIG \
  cargo test --release --locked -p supabricks-local --lib \
  isolated_execution_real_uc -- --ignored --test-threads=1
```
