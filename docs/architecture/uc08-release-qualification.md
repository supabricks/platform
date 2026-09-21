# UC08: qualify the complete installed local catalog product

Status: implementation and local validation in progress. Alpha.34 is the new
candidate; it is not yet a qualified release. Alpha.24 remains the last fully
qualified predecessor recorded by the packaging workstream.

The existing `native-release` workflow assembles one candidate per target from
reviewed platform, console, Sail and Unity Catalog sources. UC08 adds a
`release-catalog` job on Linux x86_64 and macOS arm64 and makes its evidence a
required input to the existing combined R04 collector. PG, ingestion, console,
notebook, environment, packaging, logical data transfer, recovery and benchmark
gates remain in place.

`qualify_catalog.py` stages the actual signed localhost installer, pipes curl into
Bash and installs into a path containing spaces. It verifies the archive checksum,
installed inventory and UC/JRE build inputs before exercising native catalog
lifecycle/publication/read/binding scenarios, the console's two-project browser
workflow, and stopped/moved-root recovery. These modes use the installed binary,
workers, console assets and catalog closure without replacing files or inventing
a new release manifest. The fixture-only upgrade candidate in the UC07 native
suite is deliberately excluded from this exact-archive mode; the inherited
signed-install upgrade gates and UC07 interruption gate remain separate.

The native suite includes publication interruption/reconciliation, bound reads,
GC protection and package requirement rebinding. Recovery includes an interrupted
restore, moved data root with stable identities and relocated file URLs, revoked
old credentials, retained snapshots, corrupt backend refusal, incompatible
backend refusal and explicit external-provider rebinding. Linux additionally
requires an actual bounded filesystem ENOSPC failure. The installed `CATALOG-DEMO.md`
walkthrough uses browser project creation, import, publication, binding, Sail SQL
and a managed notebook without manual UC setup.

Linux isolates the entire process tree in a loopback-only network namespace and
masks the system JDK; macOS denies external networking, Homebrew and system Java.
The native service asserts the executable is the candidate's private JRE.
Its managed loopback/file profile uses a private hosts-only resolver, including
the OS hostname, so startup logging cannot trigger external DNS when the host
has no matching system hosts entry. This is a deliberately finite name set;
external operator-managed UC and the platform's HTTP resolver are unchanged.
The mechanism is documented in the [JDK release notes](https://www.oracle.com/java/technologies/javase/9-relnotes.html). The
existing Linux baseline traces all descendant network calls. Resource sampling
includes the daemon's complete live descendant tree and separately records
observed UC JVMs and peak JVM RSS. Sampling can miss short-lived peaks and RSS
can double-count shared pages; these are engineering measurements, not capacity
or isolation guarantees.

The catalog job is a reusable workflow. Its standalone dispatch accepts an existing
archive run for harness diagnosis without rebuilding unchanged components. Such
a partial report does not qualify a release; the combined R04 collector still
requires every gate against one exact candidate. The full native-release workflow
calls it with its own archive run.

A fixture runner records descendant cleanup and fails qualification if processes
leak, even if the functional assertions pass. Failed-gate artifacts contain only
bounded structural traceback information, fixed exception categories and typed
outcomes; private logs, notebook output, SQL, credentials and source data are not
uploaded by the new gate.

The R04 collector binds the report to the same archive and manifest identities
as the inherited gates. It validates exact source/build pins, JRE distribution,
backend schema, publication format and capability profile, requires named
coverage and measured UC resources, and rejects missing suites or cleanup
failures. A green required PR check or merge does not substitute for both final
candidate archives passing the complete combined evidence collector.

## Upgrade stabilization

Previously the additive UC closure made pre-UC releases fail the existing exact
component inventory check. The first-catalog transition now compares the existing
PG/storage/analytics inventory exactly and permits adding UC only when there is
no prior catalog state or undeclared catalog component. The stopped backup and
restartable upgrade journal remain mandatory. Existing UC backend/runtime
changes retain the exact payload compatibility fence; this does not authorize arbitrary
engine changes or backend migrations.

Upgrade comparison excludes the UC build report, whose elapsed build duration
changes even when every runtime file is identical. Every executable, JAR, private
JRE and configuration file remains in the comparison. Persisted backup and journal
fingerprints retain the full original inventory, including that report, so old
checkpoints remain verifiable. Native recovery exercises a timing-only rebuild
through all activation boundaries; unit coverage rejects a changed runtime JAR.

The alpha.22 packaging predecessor can fail database preparation before producing
its resource receipt. Its existing one-time explicit reconciliation now accepts
that empty receipt case while still requiring the exact known database error,
no active revision and one retained database. Candidate failures are never
silently retried.

## Evidence and retry ledger

- PR #63 head `98142bb`: Linux native catalog passed; macOS browser drift assertion
  failed because PostgreSQL could change before the browser's original metadata
  observation completed. Console #8 adds that observation barrier; #63 reruns
  against the merged console pin. That barrier also needed to open the collapsed
  schema panel before awaiting its controls (console #9). Both platforms then
  passed run `35565306929`, and #63 merged as `14c35a6`.
- Native release run `35558570208`: both assemblies passed; recovery and environment
  lifecycle rejected the additive catalog inventory; project portability failed
  during upgrade or predecessor preparation; Linux baseline failed in the
  packaged notebook. The original reports remain historical failure evidence.
- Local UC08 native service: all 37 scenarios passed against unchanged alpha.33
  Linux archive. This exercises exact-installed fixture mode and is not alpha.34
  release evidence; host networking was not isolated.
- Local exact-installed recovery initially reached the restored catalog before
  PostgreSQL was ready. The fixture now waits for its restored SQL query; all nine
  local exact-installed recovery scenarios passed on retry. Linux ENOSPC remains
  a CI gate.
- A local alpha.34 curl-installed Linux app workflow passed, including the
  packaged notebook that previously failed in CI. Its network trace then exposed
  DNS attempts from managed JVM hostname lookup (no network traffic escaped).
  The private resolver change passed a bundled-JRE probe with no DNS attempts;
  the repeated full-product trace passed with 6,251 destinations and zero external
  attempts. This is new evidence,
  not a claim that the prior notebook failure's cause is known.
- The new isolated Linux catalog gate passed 37 native, 11 browser and 10 recovery
  scenarios, including ENOSPC, and its report passed the catalog evidence collector.
  It used the pre-DNS-fix candidate and does not replace final release evidence.
- The DNS-fixed service gate subsequently exposed another readiness race: the
  deliberate missing-catalog failure could be observed while PostgreSQL was still
  starting. The fixture now waits for its PostgreSQL query before asserting that
  catalog failure leaves PostgreSQL available. Cleanup recorded zero leaked processes.
- The DNS-fixed local archive passed the complete isolated catalog gate after
  those readiness fixes: 37 service, 11 browser and 10 recovery scenarios, including
  ENOSPC. The detached-daemon census observed 143 browser descendants and no leaks.
  This locally assembled candidate predates the final branch head.
- Final review found assembly overwrote the new walkthrough with the existing
  catalog service handbook at `CATALOG.md`. The demo now ships separately as
  `CATALOG-DEMO.md`, and the collector requires its exact source hash. Earlier
  catalog gate passes establish runtime behavior, not correct demo packaging.
- All required PR checks and both native catalog suites passed at `3c81659`;
  native recovery includes the timing-only UC rebuild through every interrupted
  activation boundary. The complete release pipeline and corrected demo archive
  still require qualification.
- Run `35569003223` exposed an inherited macOS isolation-policy error: a broad
  local-address network allowance also permitted external TCP connections. The
  new service canary rejected it before catalog startup. Release qualification
  now uses the directional outbound restriction already qualified by the native
  catalog probe. Prior macOS reports using the broad rule do not establish offline
  execution. Run `35572457251` was cancelled before qualification to avoid testing
  the same known policy error again.
- The same run's Linux minimal-host workflow again failed while polling notebook
  admission, before cell execution. Structural diagnostics retained the API request
  stage; a four-CPU, 16 GiB traced local reproduction passed. Typed HTTP diagnostics
  and a focused hosted-runner reproduction are needed before assigning a cause.
- Linux environment lifecycle passed the pre-UC upgrade but failed restoring into
  its Unicode destination. A direct bundled-H2 reproduction showed Java's empty
  locale environment cannot open relative files below that path; `C.UTF-8` fixes
  it. Managed catalog and recovery JVMs now receive the target's UTF-8 locale, and
  native catalog recovery includes a Unicode/quote/percent destination. Run
  `35572941407` was cancelled before testing this known issue again.
- macOS completed 10 MB, 100 MB and 1 GB export/query measurements, then the
  benchmark cleanup exceeded the CLI's interactive 90-second operation wait.
  The fixture now submits deletion once and waits on that same durable operation
  for up to five minutes; completion remains required and its duration is recorded.
- Focused run `35575076407` passed the Linux app workflow against the unchanged
  `3c81659` archive. Its macOS job proved external TCP denial (`EPERM`) and passed
  all 37 exact-installed service scenarios with the corrected directional policy.
  The earlier notebook failure's HTTP status was not retained, so its precise
  cause remains unconfirmed. Read-only readiness polling now tolerates HTTP 503
  within its original deadline and records those interruptions; regression tests
  ensure mutations are submitted once and terminal errors/deadlines remain fatal.
  This does not authorize retrying an entire failed qualification run as success.
- The UTF-8 fix passed 50 affected Rust tests and all nine native recovery scenarios,
  including bound reads and upgrade activation from the Unicode destination.
- Alpha.34 cross-platform candidate qualification is pending. Do not mark UC08
  complete until its final combined R04 evidence passes; record any further retry
  and its cause here.
