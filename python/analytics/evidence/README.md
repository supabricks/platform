# A00 recorded native qualification

Both reports come from [native-baseline run 34152360809](https://github.com/supabricks/platform/actions/runs/34152360809)
on September 7, 2026, for A01 PR #12 head `b3583d6` (the added Psycopg dependency). Actions checked out the clean
synthetic PR merge tree `e2e015562ddbeee2f39efa43ac3502baf860d253`.
The original JSON artifacts are retained verbatim. Each report binds the exact
fixture and environment files by SHA-256; `components/validate.py` rejects
mismatched inputs, versions, targets, dirty source or failed worker exits.

| Observation | Linux x86_64 | macOS arm64 |
| --- | ---: | ---: |
| Native runner | Ubuntu 24.04 / glibc 2.39 | macOS 15.7.9 |
| CPython | 3.12.13 | 3.12.13 |
| Correctness groups | 15 passed | 15 passed |
| Process launch → first SQL, including imports/setup | 1.738 s | 2.929 s |
| Server start → first SQL, excluding imports/setup | 0.313 s | 0.619 s |
| Sampled peak fixture process-tree RSS | 614.7 MiB | 357.5 MiB |
| Main-process RSS after workload | 387.0 MiB | 246.2 MiB |
| Fixture process wall time | 2.636 s | 4.619 s |
| Worker exit code | 0 | 0 |

The process-tree peak includes the separate reader, Python, Spark client, Sail,
Arrow and delta-rs. This is not a comparison of isolated engine efficiency:
runners and OS memory accounting differ, caches may be warm, and sampling can
miss transient peaks. See the [measurement boundaries](../README.md#evidence-and-measurement-boundaries).

When changing a locked dependency or hashed fixture input, let both native jobs
produce new passing artifacts, replace the affected reports, update this note,
and rerun component validation. A component check failing on stale evidence is
intentional. Never update a report's hashes or package versions by hand to make
it pass. The source revision in a report identifies the run that produced it,
not the later commit that checks the report into the repository.
