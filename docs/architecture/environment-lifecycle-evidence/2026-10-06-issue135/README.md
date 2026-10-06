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
That run passed with 4.430 seconds in collection; it did not cross the six-second
lease interval. A one-million-file retired-tree diagnostic is running to exercise
that boundary. Neither passing run establishes resolution of the historical race.

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

Local validation passes **219 unit tests**, with four existing ignored tests,
including the three new collection/owner regressions. The initial CI compile
exposed module visibility and job-type export omissions; those are corrected in
the tested source. The hosted lifecycle candidate and negative stress control
remain pending; the first failed CI result is retained.

The one-million-file retired-tree control reproduces the exact missing-handle
404 after **18.853 seconds** of synchronous collection:
https://github.com/supabricks/platform/actions/runs/37512841876 .
This demonstrates the heartbeat-starvation mechanism without changing the
runtime or lease interval. The asynchronous candidate is being tested with the
same stress. Console collection uses the existing 120-second long-operation
response budget; notebook ownership still renews through the unchanged six-second
lease, and other environment admission requests keep their existing deadline.
