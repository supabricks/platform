# Supabricks repository and component map

*Inspected: 2026-09-05. This records observed repository state, not inferred
capabilities from repository descriptions. Implementation sequence:
[local-runtime-implementation.md](local-runtime-implementation.md).*

## Organization inventory

The authenticated organization listing contains five repositories. There is no
`ui`, `website`, `scintilla`, or separate analytical-engine repository yet.

| Repository | Visibility / working branch | Current contents | Role in the local product |
|---|---|---|---|
| [platform](https://github.com/supabricks/platform) | Public / `main` | Extracted SSPC operator, MCP, chart, installer, UI, tests and handbook | Main implementation repository: CLI/daemon, local runtime, materializer, analytical integration, packaging, tests, agent tooling |
| [neon](https://github.com/supabricks/neon) | Public / `sb/main` | Neon source plus Supabricks engine ledger | Pageserver, safekeeper, broker, `compute_ctl`, `neon` PG extension, engine build recipes and bundles |
| [postgres](https://github.com/supabricks/postgres) | Public / `sb/REL_16_STABLE`, `sb/REL_17_STABLE`, `sb/REL_18_STABLE` | Imported Neon patch series on PG source branches; `main` is docs only | PG source and inherited/incremental integration patches, regression tests and patch ledger |
| [rfcs](https://github.com/supabricks/rfcs) | Private / `main` | `design/`, `product/`, `business/`, `research/`; ledger templates | Architecture decisions and product direction; not a build dependency |
| [sspc](https://github.com/supabricks/sspc) | Private / `main` | Pre-split history | Historical reference only; new product implementation goes to `platform` |

The SSPC README labels the repo archived/read-only, but GitHub's archive flag was
false in the organization listing. Treat it as retired by project convention.

## Observed source baselines

| Repository / ref | Commit inspected | Meaning |
|---|---|---|
| `platform/main` | `070903f1db5d7510572af3845c7efbf9d8bc36f4` | Current operator-based implementation |
| `rfcs/main` | `668dcc08c33b6595b719630b30c2ccf0a6b54499` | Requirements plus vendor-neutral addenda |
| `neon/sb/main` | `d348114f6afc35bbe10e044003128674a6b2b79b` | One ledger-only commit ahead of upstream baseline `8f60b04` |
| `postgres/main` | `2a82f07b1050361eb75d953a7feedf67aed1281f` | README and `docs/postgres-changes.md`, no engine source |
| `postgres/sb/REL_16_STABLE` | `59027122a97c2d91c1acabd5aa512b4bf72f6281` | Identical to the frozen Neon PG16 import at inspection |
| `postgres/sb/REL_17_STABLE` | `56692dfb680281a963c7470fc7f0fec7f65ecfd4` | Supabricks PG17 source branch |
| `postgres/sb/REL_18_STABLE` | `a616eefea763784218a67f81395ba95bc2cf7c4d` | Supabricks PG18 source branch; not wired into Neon build |

These are investigation pins, not the selected release combination. Record the
qualified combination in the platform component lock after testing.

E01 subsequently selected Neon `1c6fa095261112aae239beef5a221b484703d49a`
and PG17.8 `56692dfb680281a963c7470fc7f0fec7f65ecfd4` on feature branches.
[Neon PR #1](https://github.com/supabricks/neon/pull/1) owns native packaging and
extension compatibility changes; platform owns the lock and its
[Linux](../../components/provenance/native-linux.json) and
[macOS](../../components/provenance/native-macos.json) probe evidence.
Postgres [PR #1](https://github.com/supabricks/postgres/pull/1) completes the PC
rationale inventory; [PR #2](https://github.com/supabricks/postgres/pull/2) fixes
the PG17 source workflow and audits a pinned ledger revision. These CI-only
source changes do not alter the selected runtime gitlink.
The tables below preserve the initial inspection, including the original
gitlinks. They are not the current selected combination.

### The Postgres pin mismatch to resolve first

At the inspected Neon ref, the actual gitlinks are:

| Path in `neon` | Actual gitlink | Version label in `vendor/revisions.json` |
|---|---|---|
| `vendor/postgres-v14` | `2155cb165d05f617eb2c8ad7e43367189b627703` | 14.18 |
| `vendor/postgres-v15` | `2aaab3bb4a13557aae05bb2ae0ef0a132d0c4f85` | 15.13 |
| `vendor/postgres-v16` | `a42351fcd41ea01edede1daed65f651e838988fc` | 16.9 |
| `vendor/postgres-v17` | `1e01fcea2a6b38180021aa83e0051d95286d9096` | 17.5 |

The `.gitmodules` URLs are relative `../postgres.git`; in the Supabricks fork
they resolve to `supabricks/postgres`. Their branch hints still use
`REL_*_STABLE_neon`, not `sb/*`. Ordinary submodule checkout uses the gitlink SHA;
the branch hint does not upgrade it. The PG16 pinned commit is accessible through
the Supabricks Postgres repository API, but clean Git checkout/build still needs
qualification. Check out only the selected major for the first bundle.

The Postgres README says submodules point at tags from `sb/*`. Inspection shows
that desired relationship has not landed. The Neon ledger itself records the
repointing as future work. Read actual gitlinks when producing artifacts.
[Neon submodules](https://github.com/supabricks/neon/blob/sb/main/.gitmodules),
[version record](https://github.com/supabricks/neon/blob/sb/main/vendor/revisions.json),
[Postgres branch policy](https://github.com/supabricks/postgres/blob/main/README.md).

The platform's chart uses pinned upstream image digests, not a Supabricks-built
engine bundle. The Neon ledger documents a source/image baseline difference;
do not assume a native source build reproduces the currently running image.
Keep both identities in qualification reports.
[Chart pins](../../chart/values.yaml),
[engine ledger](https://github.com/supabricks/neon/blob/sb/main/docs/supabricks/engine-changes.md).

## Build and governance findings

- No GitHub releases were listed for `platform` or `neon` at inspection.
- `neon`'s inherited macOS workflow references Neon infrastructure and
  `CI_ACCESS_TOKEN`; it is not a standalone Supabricks distribution pipeline.
- The inherited PG16 build workflow triggers on `REL_16_STABLE_neon`, so it does
  not validate normal pushes to `sb/REL_16_STABLE` as configured.
- EC-0001 describes source-built images and adding PG18, but the observed
  Supabricks Neon commit only creates its ledger. A ledger entry is not evidence
  that the build exists. Stage native PG16 packaging before the wider PG18 work.
- The PC ledger contains 27 inherited groups whose detailed fields are TODO.
  Complete the relevant inherited audit before adding PG behavior patches.
- RFC 003 describes `Engine-Change: PC-NNNN`; the live Postgres ledger says
  `Postgres-Change: PC-NNNN`. Normalize this explicitly when adding enforcement;
  use the live ledger convention in new PG work, and amend the older RFC wording.
  Neon uses `Engine-Change: EC-NNNN`. Platform-only changes need neither trailer.
- The live PC ledger is on docs-only `main`; source-branch CI must fetch a pinned
  ledger revision or consume an exported audit manifest. Do not expect the file
  to be present automatically on every `sb/*` source branch.

Sources: [Neon build workflow](https://github.com/supabricks/neon/blob/sb/main/.github/workflows/build-macos.yml),
[PG16 build workflow](https://github.com/supabricks/postgres/blob/sb/REL_16_STABLE/.github/workflows/build.yml),
[PC ledger](https://github.com/supabricks/postgres/blob/main/docs/postgres-changes.md).

## Platform source reuse map

All paths below are relative to `supabricks/platform`, not the old `sspc/platform/`.

| Existing source | Useful behavior | Treatment |
|---|---|---|
| `crates/operator/src/spec.rs`, assets and golden fixture | Explicit compute identity and known-good spec rendering | Extract portable parts; parameterize local paths/ports and PG major |
| `crates/operator/src/keys.rs` | Ed25519/JWKS/JWT machinery | Reuse; local private-file persistence replaces Kubernetes Secrets |
| `crates/operator/src/storcon.rs` | HTTP timeouts, error classification, timeline operations | Keep controller client for K8s; add a direct-pageserver adapter, not a URL-only substitution |
| `crates/operator/src/reconcile.rs` | Ingestion wait, branch identity, deletion safety | Extract pure decisions; implement local durable operations instead of copying K8s reconciliation |
| `crates/operator/src/lifecycle.rs` | Graceful termination, TTL, fresh-wake lessons | Adapt policy; local connection/work leases replace SQL idle polling |
| `crates/operator/src/mcp.rs` and schema fixture | Structured errors, schemas, discoverability | Preserve operator API; implement project-scoped local API over shared domain methods |
| `crates/operator/src/ports.rs` | Collision handling lessons | Replace hard NodePort range with persisted local listener allocation |
| `e2e/run.sh`, `chaos.sh`, `restore.sh` | Behavioral acceptance scenarios | Retain K8s gates; port scenarios into native tests with isolated data roots |
| `chart/`, `install/up.sh`, `install/down.sh` | Existing Kubernetes deployment | Keep their meaning; add `install/native/` and new CLI rather than repurposing them |
| `docs/handbook/`, `docs/reviews/`, `spikes/NOTES.md` | Implementation constraints and historical measurements | Reference and extend; distinguish documented history from rerun results |

There is no analytical materializer in the inspected `platform/main` despite
the repository description mentioning it. The local analytics probe from the
September 5 investigation is now imported into
[spikes/local-analytics](../../spikes/local-analytics/README.md) in the P00 working
branch. It is a component fixture, not an existing OLTAP implementation.

## UI, website and scope

[Platform PR #1](https://github.com/supabricks/platform/pull/1) is open: it removes
the Carbon UI and plans a future console in a separate repository. Its unit and
e2e checks were successful at inspection, but it was not merged. This plan does
not require the Carbon bundle or merge that PR. Avoid changes that recreate its
dependencies; coordinate any edits to the same README/CI/handbook files.

The RFC repo's August 28 addendum also moves RFC 015's Git-like data merge/revert
ideas out of the Supabricks roadmap. Do not schedule them under the local branch
milestone; branching already exists, data merging is a separate project.

Propose `platform/website/` for the initial static `supabricks.io` site because
no website repo exists and install docs should track the release. It is distinct
from the future runtime console. If a website/console repo is created later,
move frontend source there and consume versioned assets/contracts; native
runtime builds must not depend on private business documents or website access.

## Ownership rule

**Product behavior and assembled releases live in `platform`.** A source patch
to a pageserver, safekeeper, broker, compute_ctl or the Neon PG extension belongs
in `neon`. A PG core patch belongs in the relevant `postgres/sb/*` branch.
Off-the-shelf Sail, delta-rs, SeaweedFS and Process Compose remain upstream
dependencies pinned by the platform release; create no new fork merely to rename
or repackage them. Cross-repository integration happens through immutable source
and artifact identities.
