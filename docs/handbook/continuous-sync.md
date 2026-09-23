# Continuous incremental synchronization (SY05)

[Contract, measurements and limits](../architecture/sy05-continuous-sync.md) · [Triggered runs](triggered-sync.md)

Use a matching SY05 binary and analytical runtime with a running, eligible
local-owner source. Enabling continuous mode starts capture, an isolated bootstrap
and automatic incremental publication. It can keep source compute awake.

```bash
supabricks sync create --branch main --mode continuous --key orders-continuous
supabricks sync show POLICY_ID
supabricks sync runs POLICY_ID
```

`--mode continuous` defaults to `--strategy incremental`. The daemon supervises
runs without a browser. `continuous_status` shows initialization, catch-up, observed
health, lag, resource pressure and errors. The default desired freshness is 5 s,
with at least 500 ms between run admissions. These are configurable targets:

```bash
supabricks sync update POLICY_ID --revision REVISION --mode continuous \
  --freshness-ms 5000 --batch-interval-ms 500 --key settings
```

Updates replace the full configuration. Pause and wait for a safe boundary before
changing settings during active work; revising an unfinished batch can require
resync. There is no manual Run now or schedule in continuous mode.

```bash
supabricks sync pause POLICY_ID --revision REVISION --key pause
supabricks sync show POLICY_ID
```

Pause can return `pause_requested=true`. Wait for policy `state=paused`; the current
batch finishes and no new one starts. Capture continues buffering under its quotas
and holds its compute lease. Do not assume pausing application makes compute sleep.

```bash
supabricks sync resume POLICY_ID --revision REVISION --key resume
```

Resume uses the retained checkpoint. A paused policy can also switch to triggered
mode without a new baseline, then resume for explicit runs:

```bash
supabricks sync update POLICY_ID --revision REVISION --mode triggered --key triggered
supabricks sync resume POLICY_ID --revision REVISION --key resume-triggered
```

Use each returned revision for the next command. Retry an identical intent with
the same key. Open a new analytical session to select a newer epoch; existing
sessions keep their pinned inputs.

Healthy means no known pending data at a recent observed source barrier. It is
not a promise that a transaction committed just now is already queryable. Unknown
lag is null. Source/capture outages become unavailable; schema/history failures
block publication and retain the last complete epoch. Inspect capture status and
use the explicit [resync workflow](triggered-sync.md) when required. After capture
re-enrollment, explicitly resume the continuous policy to clear a failed-run
admission error; it then bootstraps and catches up automatically.

```bash
supabricks sync capture status CAPTURE_ID
supabricks sync delete POLICY_ID --revision REVISION --key delete
```

With matching SY07 binaries/workers, published spool prefixes are reclaimed and
Delta data periodically moves to a compacted generation. Old epochs remain pinned
until their readers/catalog references close and you explicitly collect history:

```bash
supabricks analytics gc --branch main --keep 1
```

Finite spool, retained-root and installation-history limits still apply; durable
run/retry journals are not silently expired. See [SY07 maintenance and recovery](../architecture/sy07-sync-hardening.md)
for limits and failure behavior. SY06 supplies shared console/governed controls;
SY08 still owns exact installed-release qualification.
