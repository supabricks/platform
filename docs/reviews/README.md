# Reference-grade review series

[Documentation home](../README.md) · [Current delivery status](../plans/status.md)

These are historical implementation reviews and repair records. Numbered reviews
001–005 describe the earlier Kubernetes/operator prototype; their deferred TLS,
IAM and gateway statements do not describe today's native/governed product.
The notebook reviews below record defects and subsequent repairs. Use the delivery
ledger and exact-archive evidence for current completion claims.

| Doc | Focus |
|---|---|
| [001-implementation-gaps.md](001-implementation-gaps.md) | Initial issue register from source, docs, build, and local cluster review. |
| [002-data-safety-review.md](002-data-safety-review.md) | Branch correctness, cleanup/finalizers, suspend/wake durability, restore, and remaining data-safety gaps. |
| [003-api-contract-review.md](003-api-contract-review.md) | MCP schema, result payloads, error semantics, validation, UI assumptions, and client compatibility. |
| [004-kubernetes-hardening-review.md](004-kubernetes-hardening-review.md) | Helm rendering, pod security, RBAC, service-account tokens, network boundaries, image provenance, secrets, and probes. |
| [005-test-matrix.md](005-test-matrix.md) | Local/CI gates, unit and contract coverage, e2e/chaos/restore harnesses, UI testing, and reproducibility gaps. |
| [Notebook review](n00-n06-notebooks.md) | Holistic review of the original N00–N06 implementation. |
| [Notebook repairs](n00-n06-repairs.md) | Corrections and qualification mapped to that review. |
