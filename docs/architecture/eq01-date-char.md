# EQ01 — native DATE and fixed-width CHAR (#171)

Status: implemented; local worker, installed correctness and nine scalar controls
pass. The control results are descriptive, with source-commit stalls tracked in
[#176](https://github.com/supabricks/platform/issues/176). Linux/macOS exact
archive CI remains a merge gate. [SP stays frozen](sync-performance-freeze.md).
This follows the [composite-key slice](eq01-composite-keys.md) and addresses
[#171](https://github.com/supabricks/platform/issues/171), including the Sail
comparison dependency [#174](https://github.com/supabricks/platform/issues/174).

## Storage and recovery contract

Continuous/triggered capture admits DATE and CHAR(n) payload columns under the
existing whole-source profile. Primary-key components remain non-null integers.
Column OID, typmod, nullability and schema identity are retained. Existing row,
transaction, table, memory and retained-storage bounds still apply; a new type
is not an exemption from those bounds. Schema changes still require re-enrollment.

DATE stays Arrow date32 / Delta DATE. Supported finite values are 0001-01-01
through 9999-12-31, matching the existing Python bootstrap reader. Infinite dates,
BC dates and years beyond 9999 are rejected, preserving the last published epoch.
Capture pins the replication connection's DateStyle to ISO/YMD independently of
database/client settings. Durable plans encode dates as ISO text and restore
actual date objects before applying Delta mutations; analytical columns are not
converted to STRING. Exact decimal values retain their existing precision/scale.

CHAR(n) retains PostgreSQL's original padded string, including Unicode, spaces,
tabs and nulls. Width counts Unicode characters, not UTF-8 bytes. The incremental
decoder verifies the declared width; bootstrap counts full `octet_length` in its
server-side row budget. Arrow and Delta fields carry Spark's standard
`__CHAR_VARCHAR_TYPE_STRING=char(n)` metadata through apply and replay. There is
no trimming or source-schema substitution. Supported typmods match PostgreSQL's
1–10,485,760 character range, subject to existing smaller row/message budgets.
Do not downgrade an enrolled DATE/CHAR capture to older workers.

## Analytical semantics

Sail reads imported CHAR metadata before expression optimization. CHAR/CHAR and
CHAR/foldable-string comparisons pad operands to their common character width;
Unicode, joins, ordering, null-safe equality and aliases are covered. Explicit
casts remove CHAR comparison semantics, including casts projected through a
subquery. Ordinary STRING columns and LIKE retain their string behavior. This
change covers imported, already padded columns; it does not establish generic
Sail CHAR DDL/write enforcement. Spark Connect DataFrame schema responses and
metadata round trips remain a separate [#175 follow-up](https://github.com/supabricks/platform/issues/175):
the raw CHAR marker is preserved in storage and managed SQL, but currently omitted
from the client-facing DataFrame schema. Do not infer arbitrary DataFrame
round-trip support from these SQL tests.

The analytical API follows the pinned Apache Spark 4.2.0 behavior, which differs
from PostgreSQL in some expressions. For a CHAR(4) value `x   `:

| Expression | PostgreSQL 17 | Spark 4.2.0 / qualified Sail |
| --- | --- | --- |
| `c = 'x'` | true | true |
| `length(c)` | 1 | 4 |
| cast to TEXT / STRING, then compare with `'x'` | true | false |
| `c IN ('x', NULL)` | true | null |
| `c BETWEEN 'x' AND 'x'` | true | false |
| CHAR(4) `c IN (SELECT d)` for equal CHAR(8) value | true | false |

The final three are observed behavior of the pinned Spark reference, not a claim
about every Spark version or all SQL implementations. An untyped NULL prevents
Spark's analysis-time IN-list padding; CHAR IN-subqueries do not receive binary
join padding. BETWEEN is expanded after padding analysis and uses unpadded bounds. The fixtures retain these differences rather than trimming values
to conceal them. Tests compare all analytical result rows and data types with
independently executed Spark. PostgreSQL equality is asserted for raw values and
queries with shared semantics, including character literal comparisons and joins.
[PostgreSQL character semantics](https://www.postgresql.org/docs/17/datatype-character.html)
and [Spark's pinned padding rule](https://github.com/apache/spark/blob/32f7299601108917fb01920a54e084595b7b3bf8/sql/catalyst/src/main/scala/org/apache/spark/sql/catalyst/analysis/ApplyCharTypePaddingHelper.scala)
provide the implementation context.

## Qualification and separate slice measurements

The worker suite passes 142 tests. New tests cover finite-date boundaries, invalid
encodings, nulls, leap dates, exact decimals, key movement, retained historical
versions, Unicode and padded CHAR metadata, and saved-plan replay after a real
Delta commit with journal rereads disabled. Installed fixtures exercise native
PostgreSQL, bootstrap, I/U/D, old/latest Sail readers, capture SIGKILL and daemon
restart. The DATE fixture changes the database DateStyle and verifies that an
infinite date blocks publication without replacing the previous epoch.

The CHAR fixture also bootstraps the original 24 TPC-DS schemas without type or
key adaptations. These tables are empty schema probes; populated typed fixtures
exercise actual data flow. This is not the generated SF1 load or execution of the
103 TPC-DS statements. Those remain EQ02 and later work.

Independent Apache Spark JVM reference inputs/results are committed in
`e2e/tpcds/type_cases.py` and `type_reference.json`. The reference records its
version, Java runtime, generator/input hashes and all 276 runtime JAR hashes;
`reference-requirements.txt` pins the reference distributions. It runs outside the
product Python runtime. The installed DATE/CHAR gates assert these golden results
on both release targets; evidence collection rejects missing suites/checks or
changed worker identities, including bootstrap export code.

The immutable DATE-only overlay matches commit `f3c4f11`; the CHAR overlay adds
four worker changes and source-built Sail `0ff69f29` ([engine PR](https://github.com/supabricks/sail/pull/1)).
The unchanged scalar control runs three fresh cells per arm, rotating order over
three repetitions. Each uses two 10,000-row integer tables, twelve 64-row
transactions, 8 CPUs, 16 GiB, no swap/network, and a 200 ms publication observer.
No builds or correctness fixtures overlap the campaign, and no quiet-period
sleeps or replacement trials occur. Host physical storage is shared.

| Arm | Mean observed publication lag | Mean source transaction/commit |
| --- | ---: | ---: |
| #170 baseline | 857.82 ms | 9.07 ms |
| DATE only | 881.88 ms | 9.66 ms |
| DATE + CHAR + corrected Sail | 843.77 ms | 15.13 ms |

All nine controls pass exact source/Delta equality and cleanup, with 36 samples
per arm. DATE adds an observed 24.06 ms / 2.81% publication lag; the CHAR slice
reduces that mean by 38.12 ms / 4.32%. These differences do not establish causation
or a speedup. The CHAR arm has 141.58 ms and 100.04 ms source-commit stalls at
transaction 8 in repetitions 1 and 2; repetition 3 has no comparable stall. Its
mean commit time is 56.63% above the DATE arm, and all samples are retained in
[#176](https://github.com/supabricks/platform/issues/176) for investigation.
The control uses integer payloads and measures publication availability, not
Sail query execution or the scaling cost of typed payloads.

The demonstrated contributions are native DATE correctness and complete native
schema admission with CHAR query semantics. [Retained evidence](tpcds-evidence/2026-10-06-issue171/README.md)
includes every control, cleanup receipt, package proof, independent reference
identity, and failed development attempt. These are unsigned engineering overlays,
not signed exact-archive qualification. The complete SF1 load and full TPC-DS
query suite remain pending. No sustained performance envelope or SP advancement
is claimed.
