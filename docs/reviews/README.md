# Reference-grade review series

This directory holds implementation reviews aimed at turning the current
prototype into a reference-grade prototype: something a new engineer can clone,
run, trust, diagnose, and extend without rediscovering hidden traps.

Reference-grade does not mean expanding the product scope. Deferred M2 work
such as the gateway, TLS, IAM, and HA remains deferred unless a review document
explicitly argues otherwise. The bar here is that every current promise is
clear, tested, and hard to misuse.

| Doc | Focus |
|---|---|
| [001-implementation-gaps.md](001-implementation-gaps.md) | Initial issue register from source, docs, build, and local cluster review. |
| [002-data-safety-review.md](002-data-safety-review.md) | Branch correctness, cleanup/finalizers, suspend/wake durability, restore, and remaining data-safety gaps. |
| [003-api-contract-review.md](003-api-contract-review.md) | MCP schema, result payloads, error semantics, validation, UI assumptions, and client compatibility. |
| [004-kubernetes-hardening-review.md](004-kubernetes-hardening-review.md) | Helm rendering, pod security, RBAC, service-account tokens, network boundaries, image provenance, secrets, and probes. |
