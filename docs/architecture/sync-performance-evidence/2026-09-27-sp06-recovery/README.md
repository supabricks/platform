# SP06 campaign recovery, 2026-09-27

The previous SP06 source-screen controller exhausted three host-contention
replacement attempts (nine trials, no accepted blocks), then stopped while other
host work continued. PR #121 records that outcome and the then-observed clean
teardown. On resumption, the `/tmp/sp06-evidence`, `/tmp/sp04-candidate-01` and
frozen harness/checkpoint paths were absent. The cause of deletion is unknown.

**Those nine raw trials and their host/cleanup receipts are unavailable.** Their
prior summary is historical context, not independently reproducible evidence.
No rate, exclusion receipt or cleanup result has been fabricated or counted in
this campaign. This is tracked in [#124](https://github.com/supabricks/platform/issues/124).

The fresh campaign uses persistent local storage under
`build/sp06-recovery-20260927/`, outside `/tmp`. Frozen harnesses are restored by
commit, and the historical accepted runtime is reconstructed using archived
inventory/overlay proofs, a retained diagnostic binary and upstream CI artifacts.
Package identity and every payload hash must match before measurement. Package
recovery and a short functional smoke are prerequisites, not capacity trials.

The frozen SP06 source harness and protocol are unchanged. A new campaign has its
own predeclared order, quiet intervals and bounded contention retry budget. It
must not resume or rewrite the missing campaign's manifest. Archive structured
attempts before deferring work, even when a controller stops short of completion.
The archiver's explicit `--allow-incomplete` mode takes the controller lock,
retains recorded and unrecorded public trial evidence, and preserves the original
state. It does not qualify capacity or establish cleanup. Private logs and scratch
are excluded. Analyze capacity only from complete, validated campaigns.

## Verified package recovery

`package-recovery.json` binds all 26,026 verified payload files and permissions to
historical manifest `87e08c546e1316759457443b51f048f0cc7774e109b09d7176c7bf1e65cf7d40`.
The native binary and rebuilt profiling library match their historical hashes.
The diagnostic inventory is still unsigned; this is recovery of a previously
measured payload, not new release qualification or a runtime change.

The upstream payload is the Linux artifact from
[run 35895998663](https://github.com/supabricks/platform/actions/runs/35895998663),
not the later qualified SY08 release. Its historical merge was `8f6ff846`.
Two later alpha.36 artifacts were inspected and rejected as reconstruction bases
because their dependencies/metadata differed. Neither entered a benchmark.

To reproduce, use a new persistent workspace containing `exact-base-release/`
with that run's `release-linux-x86_64` archive extracted as `supabricks/`; restore
`sp04-harness/` at `e10d515` and `harness/` at `be4701c`. Compile the former's
`profile_io.c` with `cc -O2 -shared -fPIC -Wall -Wextra -Werror ... -ldl -pthread`
as `profile_io.so`. Provide the retained diagnostic binary at the repository's
`target/release/supabricks`; the recipe requires its exact historical hash.
Run `restore_package.py --workspace WORKSPACE --repo REPOSITORY`. It reconstructs
the expected manifest from the committed SP01 inventory and later overlay proofs,
checks the upstream payload, applies known sources and verifies every final file.
It refuses an existing destination or any unknown mismatch.

The 55 accounting and archival tests pass in the pinned qualifier image; output
is retained in `accounting-tests.txt`. Tests ran before capacity measurements.

The recovered payload passed a 5-second warmup / 10-second source-only functional
smoke with exact two-table validation, profiling and clean teardown. `smoke/`
retains public receipts and numeric timings. This smoke is excluded from all
source-capacity selection and performance claims.

## Fresh campaign launch

The source screen runs from frozen `be4701c` using the restored SP04 package,
4/8/16 clients, 8/16 logical CPUs, three repeats, 60-second warmup and 300-second
measurement. The original seed `20260926`, 10,000 offered changed rows/s ceiling,
16 GiB/no-swap/no-quota envelope and five-minute quiet interval are unchanged.
It is a new campaign, not a resumption of missing evidence.

`continue_campaign.py` is the campaign-local continuation recipe (copy it into
the persistent workspace before running). It waits for the launched screen's
controller lock, validates and archives the completed screen, then runs the
predeclared source controls/envelope and historical comparisons. A separately
named full-stack profile is gated on capacity and source-observer checks. It
stops on controller failures rather than silently resetting budgets or rerunning
failed measurements. Its status is in `campaign-status.json`; raw phase logs and
outputs remain under the workspace, with public archives under `archives/`.
Completion still requires review of attribution, observer costs, CI and the final
report; the continuation recipe does not mark the PR ready or claim a speedup.
