# IAM00 / UC09.0 feasibility probe

Developer-only **Linux x86_64** experiment. Does not change Docker configuration,
install a runtime globally, or connect to an existing Supabricks cell. Not a
multi-user product launch and not a production IdP deployment.

Read the [decision and threat model](../../../docs/architecture/iam00-governance-probe.md)
and [UC09 plan](../../../docs/plans/uc09-governed-implementation.md).

## Reproduce

Requires Docker with permission to launch disposable privileged containers,
Python 3.12+, GitHub CLI with read access to the pinned Actions artifact, at least
8 GiB available memory and 8 GiB available disk. Two workload containers each have
2 CPUs, 2 GiB memory (no swap), 512 tasks, and 512 MiB scratch; native control
services and Keycloak run separately. The workflow qualifies Ubuntu 24.04.

```sh
python3 -m venv /tmp/iam00-venv
/tmp/iam00-venv/bin/pip install -r e2e/native/catalog/requirements.txt
gh run download 35585995786 --repo supabricks/platform \
  --name release-linux-x86_64 --dir /tmp/iam00-artifact
/tmp/iam00-venv/bin/python e2e/native/iam/prepare.py \
  --archive /tmp/iam00-artifact/supabricks-v0.1.0-alpha.34-linux-x86_64.tar.gz \
  --output /tmp/iam00-inputs
/tmp/iam00-venv/bin/python e2e/native/iam/run.py \
  --release /tmp/iam00-inputs/supabricks --tools /tmp/iam00-inputs/gvisor \
  --report /tmp/iam00-report.json
/tmp/iam00-venv/bin/python e2e/native/iam/qualify.py /tmp/iam00-report.json
python3 -m unittest discover -s e2e/native/iam -p 'test_*.py'
```

The preparation output must be new. Archive hashes, product file inventory, full
gVisor distribution and source-built UC/Sail commits are checked before running.
Images are referenced by digest. If the Actions artifact expires, copy the exact
hash-matching archive from retained release evidence; do not substitute a newer
build or claim qualification from a different archive. No credentials go in the
report. Only the JSON report is uploaded by CI.

## What runs where

- Host: throwaway native PG cell, pinned UC fork, identity broker prototype and
  test orchestration. All native services use their existing private/loopback
  configuration. The UC reader here is a capability test, not an isolation test.
- Disposable Keycloak: loopback-only host port, synthetic users, randomly generated
  secrets, code/PKCE user flow and client-credentials service flow. `start-dev` and
  bootstrap admin CLI credentials are **fixture-only**. The fixture includes an
  intentionally dangerous external `admin` claim to characterize UC federation.
- Two disposable trusted launch containers: pinned Ubuntu rootfs, pinned gVisor,
  no network and no host Docker socket. Each has a host cgroup budget. Privilege is
  needed for nested runsc namespaces/mounts on developer hosts without rootless
  namespaces; it is **not** a privilege given to notebook code.
- Inside each gVisor sandbox: UID 1000, empty capabilities, no-new-privileges,
  read-only product/code/admitted data, virtual process/network namespaces, private
  quota-limited scratch and tmpfs. Exact packaged Jupyter server, kernel and
  bootstrap; source-built Sail with its own loopback Spark Connect endpoint.
  The disposable one-use launch shim replaces the future governed host admission
  adapter. A base venv uses the archive's read-only site packages for this probe;
  custom environment building and arbitrary package installation remain UC09.4.

`runsc --ignore-cgroups` refers only to the nested runtime: the enclosing,
per-execution Docker cgroup enforces the recorded memory/CPU/task limits. This
experiment does not approve ignoring resource limits in the product adapter.

## Evidence and cleanup

The runner prints its private `/tmp/sb-iam00-*` directory. Logs, synthetic tokens,
configuration, and discarded test cells remain there for diagnosis; do not upload
that directory. Services and containers are stopped in `finally` blocks; the
report verifies descendant shutdown. Scratch mounts disappear with their outer
containers. Normal failures write a `FAIL` report and exit nonzero. A SIGKILL of
the entire host runner cannot execute Python cleanup; on a persistent developer
host inspect only containers labeled `supabricks.iam00=true` and the printed
owned cell before cleanup. CI runners are disposable.

The committed capability report is checked for complete coverage, source hashes,
known limitations, component pins, resource/revocation bounds and cleanup. Negative
validator tests reject missing checks, stale source, false governance claims,
changed pins, slow revocation and failed cleanup. Passing the probe chooses a
candidate architecture; it does not ship the future server profile.
