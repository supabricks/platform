# Local checks and GitHub CI

Use `just check-plan [base]` and `just check [base]` for local feedback. The
default base is `HEAD`, so it includes staged, unstaged and untracked changes.
Pass the parent branch to include committed work. Run a focused regression for
the changed behavior, then let GitHub own full integration and OS coverage.
No automatic local full-stack build, benchmark, quiet-host wait or duplicate
pre-push qualification is needed. Full `verify-static`, `portable-check` and
`verify-runtime` commands remain available for diagnosis.

## Automatic PR checks

`scripts/checks.py` owns the shared impact map. GitHub compares the actual tested
merge tree with the PR base; main pushes compare against the previous commit.
It reads a Git diff, including both sides of renames, without the API's file-list
pagination limits. A missing diff selects all checks. Unknown code/configuration
paths also select all checks. Changes to the selector and common build inputs
select everything so the policy cannot avoid its own validation.

| Change | Required checks that perform work |
|---|---|
| Local PostgreSQL/daemon Rust | Linux/macOS portable suites; formatting and portable contracts |
| Shared Rust core | Portable suites, operator unit tests and Kubernetes E2E |
| Operator/chart/installer | Operator tests and Kubernetes E2E |
| Console submodule | Console typecheck and build |
| Operator UI | UI tests/build, operator tests and Kubernetes E2E |
| Analytics/ingest or analytical build inputs | Linux/macOS analytical tests; relevant component contracts |
| Component inventory | Inventory contracts and analytical baseline |
| Prose only | Scope selection; no compilation or runtime tests |

Native-cell, catalog, identity, performance-accounting and TPC-DS workflows keep
their focused regression responsibilities. Source-based native checks still
cover actual PG behavior on both OSes; installed release qualification is a
separate stage. Core/local Linux tests run once in portable, not again in unit.
OS-independent portable dependency and upgrade-control tests run once on Linux.

The six protected contexts remain `unit`, `e2e`, `portable (ubuntu-latest)`,
`portable (macos-15)`, `components`, and `analytical-probe`. Required workflows
always start; unrelated jobs skip at job level. A selector failure fails a
required check. Portable jobs have fixed per-OS names so a skipped job still
reports the protected context without relying on matrix expansion. The
analytical aggregate accepts a skipped matrix only when
scope selection succeeded and explicitly marked analytics unrelated. Selected
matrices must succeed on both targets. Cancellation does not manufacture a
failed analytical aggregate.

GitHub distinguishes [skipped jobs from path-filtered required workflows](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks):
the latter can leave required checks pending. Branch protection is unchanged.

## Full native release qualification

`native-release` runs for affected changes pushed to main and by explicit
workflow dispatch on a chosen ref. For a release-sensitive PR, dispatch it
before merging. Ordinary PR iterations no longer rebuild and qualify every
release subsystem. Main/dispatch still builds the exact archives and runs the
existing Linux/macOS recovery, catalog, notebooks, ingest, project, governed and
sync suites. No individual assertion or resource threshold was removed.

Run it from Actions → native-release → Run workflow, or:

```sh
gh workflow run native-release.yml --ref <branch-or-tag>
```

Existing runs retain their original workflow and evidence; this policy does not
turn a failed qualification into a pass. A green PR is not proof that its
distributed archive passed release qualification. Qualify the exact intended
archive before publishing it.

The IAM00 and SY00 workflows automatically run their cheap protocol/evidence
contracts when their inputs change. Replaying their historical alpha archives is
manual. The old notebook frontend candidate comparison is also manual; current
installed notebooks remain in native-release. These feasibility experiments
are retained for investigation, not rerun as current-product qualification.

## Measuring the reduction

The immediate reduction is selected work: a local-only Rust PR no longer runs
the Kubernetes gate, the standalone analytics matrix or the full release matrix.
A handbook-only PR runs neither native builds nor required runtime suites. The
scope job records its selection in the job summary. Delta cache verification
and exact build identities remain as described in
[the CI repair ledger](../architecture/pg-ci-repairs.md).

Compare queue time, runner minutes and total duration on comparable PRs before
claiming a measured speedup. Cold Delta cache misses, native-cell/catalog
runtime checks and the full main release matrix still cost time. This change
does not claim those costs disappeared.
