# Supabricks documentation

This is the documentation home for the Supabricks stack. Start with
[what we have built](stack.md) for the product, architecture, repository ownership
and supported deployment profiles. The public technical documentation lives in
this repository; access to the private RFC repository is not required to understand,
build or operate the stack.

## Start here

| I want to… | Read |
| --- | --- |
| Understand the whole stack | [Stack overview](stack.md) |
| Install and try it locally | [Local walkthrough](handbook/local-demo.md), then [create a project](handbook/project-creation.md) |
| Operate a shared Linux server | [Governed server guide](handbook/governed-server.md) |
| Find a user or operator workflow | [Handbook](handbook/README.md) |
| See what shipped and what remains | [Delivery status and release history](plans/status.md) |
| Find implementation plans, including IAM00 and UC09 | [Implementation plan index](plans/README.md) |
| Run local triggered incremental analytics | [Triggered sync workflow](handbook/triggered-sync.md) |
| Run local continuous incremental analytics | [Continuous sync workflow](handbook/continuous-sync.md) |
| Understand the synchronization roadmap | [Managed synchronization plan](plans/analytical-sync-implementation.md) |
| Understand a subsystem or its acceptance evidence | [Architecture and evidence index](architecture/README.md) |
| Build the stack from source | [Native build and installation](../install/native/README.md), [component inputs](../components/README.md) |
| Find the code owner or source pin | [Stack component map](stack.md#components-and-source-ownership) |
| Understand the earlier Kubernetes prototype | [Kubernetes profile](handbook/kubernetes-profile.md) |

## How these documents fit together

- **Stack overview:** a concise account of the current product and its boundaries.
- **Handbook:** instructions for using, building and operating it.
- **Plans:** original delivery slices and acceptance criteria. The
  [delivery ledger](plans/status.md) owns current completion status; dated plan
  baselines are historical, even when their prose uses future tense.
- **Architecture:** subsystem contracts, design decisions and qualification
  records. Evidence is specific to the source, profile and archive tested.
- **Reviews and research:** [implementation reviews](reviews/README.md) and
  [packaging research](research/project-packaging-industry.md) retain the reasoning
  behind changes. They are not a substitute for current delivery status.

The [private RFC repository](https://github.com/supabricks/rfcs) retains product
direction and historical decisions. Component repositories retain their own
upstream documentation; this documentation home explains their integration into
Supabricks. Existing deep links and machine-readable evidence stay at their
original paths.

## Keeping the documentation current

When a slice lands, update its plan and the delivery ledger, link its contract and
evidence from the architecture index, and update the affected handbook workflow.
Change the stack overview when product behavior, ownership or support changes.
Keep historical failures and qualification limits visible. A successful source
build or local smoke test does not qualify a new release archive.
