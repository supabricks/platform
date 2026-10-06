# Environment collection and notebook owner starvation (#135)

The Linux lifecycle job on PR #167 repeats the `adopt_environment` HTTP 404
notebook-handle failure at source `714d2d2`:
https://github.com/supabricks/platform/actions/runs/37485850182/job/112375761127 .

A hosted diagnostic uses the same exact archive with additional bounded CLI
timings and notebook state reads. It passes, recording **5.210 seconds** in the
first `env gc`, with the live handle ready before and after collection:
https://github.com/supabricks/platform/actions/runs/37509554193 .
The daemon's existing collection removes retired environment trees synchronously.
That work can prevent a queued heartbeat from renewing a six-second notebook
owner lease. A single passing instrumented run does not establish a fix or prove
the cause of every historical failure.

A second diagnostic adds 100,000 empty files only to the retired incompatible
fixture environment. It preserves the installed runtime and lease settings:
https://github.com/supabricks/platform/actions/runs/37510956426 .
Its result and candidate validation are pending.

The correction journals deletion on the catalog owner, excluding active pointers
and leases, then removes files on one background worker. The worker rechecks
owned directory identity before deletion. Completion returns to the catalog
owner before replying to the CLI or console. A second collection is rejected
while one is active. Shutdown and error exits join the worker before releasing
installation ownership; interrupted/failed deletion remains retryable through
its durable `deleting` state. No authentication, heartbeat or kernel lifetime is
extended.

Regressions cover acquisition fencing while IO is pending, live-lease protection,
worker-time directory substitution, retry after failure, continued control access
and completion before owner-lock release. Existing crash-after-tree-removal tests
continue to exercise resumption. Source tests and hosted lifecycle validation
must pass before this is considered complete.
