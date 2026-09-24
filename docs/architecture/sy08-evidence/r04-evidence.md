# Qualified local release

Version: `v0.1.0-alpha.36` · source: `8ea59d8c5c70f798165c1f1c3b981a2f51d666d0`

Console source: `ec6c1b99d50288b14a70255dfb05b2af9287a827`

Sail source: `9544c9253e981a82c5f9e493c43ce98a4d9d41b7` (native build on each target)

| Target | Chromium | Checks across reports | Manifest SHA-256 |
| --- | --- | ---: | --- |
| linux-x86_64 | 153.0.8010.12 | 318 | `a81619398391c900be51a7441d61e78c5854d3258e66c46d5d5a62cbd534398b` |
| macos-arm64 | 153.0.8010.12 | 245 | `473eb0868599fc7552076ad003419e18deaec7b9dbc0858632dc70344993077a` |

Unity Catalog: `17280eababcc4ae31c53522d930ef2fe92d1c331`; bundled JRE and backend contract recorded per target. Catalog gates include native lifecycle, two-project browser workflow, retained reads and moved-root recovery against each installed archive.

The historical macOS notebook predecessor is prepared before isolation. Candidate upgrade, rebuilt/restored kernels and the resolver run under Seatbelt; the JSON evidence records child outbound denial and predecessor process shutdown.


Governed profile: Linux TLS console, real on-prem identity, native data and isolated execution passed on the same Linux archive. macOS has no governed qualification.


## Ingestion measurements

Synthetic fixtures; throughput covers the worker interval, not upload, inspection or total user latency. RSS is sampled and can miss short peaks. Compressed Parquet source throughput is not decoded throughput.

| Target | Format | Source bytes | Rows | Worker seconds | Source MiB/s | Sampled peak MiB |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| linux-x86_64 | csv | 104857600 | 102400 | 1.037 | 96.432 | 141.8 |
| linux-x86_64 | jsonl | 4317184 | 8192 | 0.993 | 4.146 | 92.8 |
| linux-x86_64 | json | 10485760 | 8192 | 0.889 | 11.249 | 116.1 |
| linux-x86_64 | parquet | 71801 | 32768 | 2.370 | 0.029 | 105.2 |
| macos-arm64 | csv | 104857600 | 102400 | 2.153 | 46.447 | 136.6 |
| macos-arm64 | jsonl | 4317184 | 8192 | 0.458 | 8.989 | 70.2 |
| macos-arm64 | json | 10485760 | 8192 | 0.426 | 23.474 | 106.1 |
| macos-arm64 | parquet | 71801 | 32768 | 1.609 | 0.043 | 80.5 |

## Logical data portability

Both targets verified and transactionally imported the same Linux-produced `.sbdata` companion: `b1bf45bb9b5ded63090b34069011722d2d4161f9c49d06f1300d29b3b7d10133`. The fixture includes a primary key, decimal amounts, NULL and binary bytes. Native-cell qualification additionally covers the type matrix, concurrent snapshots, schema rejection, atomic rollback and SIGKILL before/after COMMIT. The profile is PG17 typed table data; it does not copy roles, grants, executable dump SQL or Delta epochs.

## Project portability

Both targets consumed the same `.sbproj`: `6d170633774f0e5759fff65880ccb711081a5b704df1bb59036f0d36de94f5c7`. Synthetic fixture: two CSV rows, two migrations, one SQL query and one notebook, with two native wheel closures. Prepare time is first deployment in the clean candidate installation; start time is candidate kernel readiness. RSS samples daemon descendants; disk peak samples allocated bytes across owned data roots every 0.5 seconds. These samples can miss short peaks; RSS can double-count shared pages. Archive/install/harness disk is excluded.

| Target | Package bytes | Expanded bytes | Prepare seconds | Kernel start seconds | Sampled RSS bytes | Sampled data disk bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| linux-x86_64 | 198371866 | 198329453 | 47.650 | 12.594 | 2664787968 | 5955653632 |
| macos-arm64 | 198371866 | 198329453 | 43.318 | 10.815 | 2262843392 | 5086552064 |

The JSON companion records fixture, archive, asset, worker, notice and report hashes. These are local engineering archives. Public hosting/signing/notarization, redistribution audit, physical power-loss tests, Safari/Firefox and other OS targets are separate gates.
