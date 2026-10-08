# EQ02: bounded analytical session resources (#197)

The default analytical worker's resource envelope is too small for nine original
SF1 statements. Raising its memory pool alone does not resolve all nine: q72 also
needs a different join plan. The explicit `analytical` session profile combines a
larger bounded envelope with cost-based join reordering. The existing `compact`
default remains available.

Full managed-session qualification is in progress in [PR #207](https://github.com/supabricks/platform/pull/207).
The diagnostic results below establish the profile choice; they are not a claim
that every statement is correct. Newly exposed q39 DOUBLE result differences are
tracked in [#206](https://github.com/supabricks/platform/issues/206). SP stays frozen.

## Isolating the limits

All probes use the installed Sail source `528b49dac7beafa6d5a0e1f2e538efcbbc4aca2f`,
the retained SF1 epoch, the nine unchanged statements, eight CPUs, and a 16 GiB
container with no swap or external network. Each statement runs in a fresh
process; the parent samples RSS and owned spill files every 100 ms. Plans,
results, schemas, failures, and profile declarations are retained. Completed
results are compared with the existing independent Spark 4.2.0 reference.

| Query pool | Join reordering | Statements completing | Sampled peak worker RSS | Sampled peak spill |
| --- | --- | ---: | ---: | ---: |
| 256 MiB | Disabled | 0/9 | 640 MiB | 70 MiB |
| 1 GiB | Disabled | 8/9 | 1,373 MiB | 88 MiB |
| 2 GiB | Disabled | 8/9 | 2,387 MiB | 0 MiB |
| 256 MiB | Enabled | 5/9 | 480 MiB | 10 MiB |
| 1 GiB | Enabled | 9/9 | 1,590 MiB | 183 MiB |

The compact probes retain the original 256 MiB spill, 1 GiB RSS, and 16 MiB
per-file limits. The 1 GiB pool probes declare 4 GiB total spill, 4 GiB RSS, and
1 GiB per file. The 2 GiB pool probe declares 8 GiB spill and 6 GiB RSS, with the
same 1 GiB file bound. Thread settings and automatic execution parallelism remain
unchanged. These are controlled diagnostic envelopes, not limits changed during
a qualification campaign.

Without join reordering, q72 builds a chain of hash-join intermediates. Its plan
includes repeated `CollectLeft` inputs with severe cardinality underestimates;
the input reservation consumes almost the entire pool at both 1 and 2 GiB.
Reordering resolves q72 at the original 256 MiB pool. It also resolves q21, q39a,
q39b, and q66 there, but q47, q67, q75, and q78 still exhaust memory. The combined
profile resolves those remaining execution failures. Spill files exceed the old
16 MiB file limit, and peak RSS exceeds the old 1 GiB watchdog ceiling.

The first full managed-session run completed the nine originally failing queries
but introduced a q64 memory failure. That 1 GiB candidate is rejected. A follow-up
probe with a 2 GiB pool, 8 GiB spill, and 6 GiB RSS completes all ten statements,
including q64; the replacement full-suite qualification is pending. Sort-merge
preference alone is also being measured as a separate diagnostic alternative.

## Product behavior

Use `analytics open --resource-profile analytical`,
`analytics sql --resource-profile analytical --sql ...`, or
`spark shell --resource-profile analytical`. The API field is `resource_profile`.
Omitting it selects `compact`. A profile cannot be changed on an existing session;
close it and open another. The selected profile and resolved worker limits appear
in session status and epoch metadata.

The analytical profile has a 2 GiB fair query pool, 8 GiB spill budget, 6 GiB
sampled RSS ceiling, and 1 GiB per-file ceiling. It enables Sail cost-based join
reordering. This is a local bounded profile, not automatic sizing for arbitrary
TPC-DS scales or concurrent tenants. The container's 16 GiB hard limit is
separate from Sail's tracked operator pool and the worker's sampled RSS limit.

Admission permits two compact sessions or one analytical session per installation.
The reservation persists through waiting, startup, use, and closing until the
supervisor proves the worker dead. The profile is durable and part of request
idempotency; legacy records deserialize as compact. A capacity rejection happens
before an implicit refresh can be created. Console and notebook entry points
retain their current compact default in this slice.

## Evidence boundary

The native binary and Python session worker are built/collected from a clean
committed platform tree. The engineering package binds both bytes to its build
receipt and checks the worker against that exact Git revision. The retained-load
verifier permits only those explicit platform changes plus the already qualified
Sail artifact; unrelated payload and metadata changes remain rejected. A normal
stopped backup/upgrade applies the package. The original successful load and
independent Spark reference are retained.

The rejected first candidate source commit is `8fa1d72d009d9fba7ae35a5580604584d2d98e0b`. The candidate
runtime identity is `55337e0cd5805eb07292eb4b4ce1d398700b1e98cde0d4baeba3ccd3719f3cf9`.
The baseline runtime identity is
`1208de89e46af8ae20ab62f3378ccf92082072564c53dc792449a70277021cee`.
This remains unsigned engineering evidence, distinct from release archive CI.

Tests cover durable admission, idempotency, closing/recovery reservations, legacy
records, fixed worker limits, invalid profiles, and rejection of uncommitted or
tampered worker/native artifacts. The existing native library tests and analytical
session tests also pass. The full qualification compares all 24 tables and all
103 original statements, records resource samples, and preserves strict typed
comparison. No numeric tolerance or query rewrite is introduced.
