# PostgreSQL PR stack: CI repairs and build reuse

The October 2026 PG253/PG248/PG247 stack exposed product portability bugs,
qualification assumptions and duplicated CI work. These changes are awaiting
native Linux/macOS CI; focused local checks are not release qualification.

## Correctness and qualification

- [#359](https://github.com/supabricks/platform/issues/359): macOS process-group
  enumeration must distinguish a zero result with errno from an empty group.
  Preserve the error so shutdown retries instead of claiming the group is gone.
- [#362](https://github.com/supabricks/platform/issues/362): canonicalize the
  metrics directory before opening SQLite, accommodating macOS `/var` aliases.
  The database file itself still rejects symlinks, including SQLite's NOFOLLOW.
- [#363](https://github.com/supabricks/platform/issues/363): historical release
  fixtures assumed an in-place engine upgrade that the product does not support.
  The actual signed candidate must reject a changed engine inventory before
  changing the stopped source, current symlink, backup or upgrade journal.
  Positive catalog/platform migration checks use a separately signed
  `.platformcontrol` archive with the exact predecessor engine and current
  platform payload. Reports retain candidate, predecessor and control identities
  and the scope limitation. This does **not** qualify an in-place upgrade to the
  distributed candidate's engine. Clean-install checks still use that candidate.
- [#108](https://github.com/supabricks/platform/issues/108) and
  [#187](https://github.com/supabricks/platform/issues/187): explicitly restart
  the catalog after the port-collision scenario, giving the independent crash
  scenario a fresh budget. Keep the separate three-attempt exhaustion assertion
  and product limit unchanged. Retain failure state in the public report.
- [#364](https://github.com/supabricks/platform/issues/364): a separate external
  test JVM exited after the missing-metastore recovery check. The retained CI
  report lacks Java diagnostics; its cause remains unproven. An isolated offline
  startup of the qualified runtime passed in 5.686 seconds. Failure reports now
  include the existing bounded, redacted Java diagnostics. Do not count this
  observation as a reproduced or fixed native failure.
- [#341](https://github.com/supabricks/platform/issues/341): Neon commit
  `b358a828` scopes upstream cloud workflows to their upstream target branches
  and guards upstream-only labeling. Public native builds remain enabled for
  `sb/main`. The qualified engine payload and platform pin remain unchanged.

## CI cost and dependency structure

[#356](https://github.com/supabricks/platform/issues/356) remains open for timing
evidence and further cold-build consolidation. Native cell, analytical baseline
and native release now use one composite Delta build action. Its artifact cache
key covers target, runner image, Python, source lock, Cargo lock, compiler pin,
build dependencies, patch, builder, verifier and action definition. Every hit
and miss verifies the source contract and complete wheel inventory. There are
no fallback keys. Corrupt entries fail qualification rather than being trusted.

Cell and analytical baseline build or restore Delta inside each target job:
Linux no longer waits for the macOS source matrix. The combined analytical gate
still requires both platforms. Release assembly retains its existing matrix
barriers. Cold timeouts include the former separate source build's allowance;
this does not add a delay to cache hits.

GitHub cache scope still applies: sibling PRs cannot necessarily share entries,
and concurrent first misses can still build twice. Runner image changes and
source changes deliberately miss. The action records the cache key and hit in
the job summary. Compare source-build time, cache restore time, queue time and
end-to-end duration on unchanged and changed inputs before claiming a measured
speedup. No new wall-clock improvement has yet been measured.

The Linux workspace unit job excludes core/local, which the portable job
already tests on Linux and macOS. Remaining workspace crates retain their tests;
full local `verify-static` is unchanged. Cell and baseline also cancel stale
runs for the same ref. Release qualification, rollback, sync, catalog and
cross-platform gates remain in place.

## Focused checks

Passed locally: five metrics tests, two supervisor tests, eleven existing
upgrade/recovery tests, three upgrade-control tests, one cache-key invalidation
test, two Delta inventory tests and four catalog lifecycle helper tests.
The isolated external Java startup also passed. Changed workflow files pass
actionlint. No full native rebuild or duplicate local CI run was requested.
macOS syscall behavior, native signed upgrade controls and corrected catalog
scenarios still require the pushed PRs' CI results.
