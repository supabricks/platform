# sspc engineering handbook

This is the operational knowledge that isn't in the RFCs: how the system is
actually built, how to work on it without stepping on the landmines we already
found, what to do when it breaks, and what is deliberately NOT built yet.
The RFCs (`https://github.com/supabricks/rfcs/blob/main/design/`) record *decisions*; this handbook records *reality*.

| Doc | Question it answers |
|---|---|
| [architecture.md](architecture.md) | How does it actually work, end to end? |
| [dev-loop.md](dev-loop.md) | How do I build, test, and deploy a change — and what will bite me? |
| [runbook.md](runbook.md) | It broke / I need to break it — what do I do and what should I see? |
| [backlog.md](backlog.md) | Why isn't X built? Is that a gap or a decision? |
| [Local runtime plan](../plans/local-runtime-implementation.md) | What are we building for the native Supabricks product, and in what order? |
| [Repository map](../plans/repository-map.md) | Which organization repo owns each component and which sources have been inspected? |
| [Component baseline](../../components/README.md) | Which sources are selected, what has been tested, and how do I validate the inventory? |

## Your first week

1. Read [architecture.md](architecture.md) (20 minutes) with `crates/operator/src/` open next to it.
2. Run `install/up.sh` on your laptop. It should end with a working
   UI at http://localhost:30080/ and a smoke-tested MCP server. If it doesn't,
   that's a bug — file it.
3. Run the gates: `./e2e/run.sh` (~2 min) then `./e2e/chaos.sh` (~3 min, it
   reboots the kind node on purpose). Both must pass before and after any
   change you make.
4. Do one fire drill from [runbook.md](runbook.md) by hand, watching
   `kubectl -n sspc-cell logs deploy/sspc-operator -f` while you do it.
5. Read [backlog.md](backlog.md) **before** building anything new. Several
   "obvious missing features" (gateway, TLS, IAM, HA) are deliberately
   deferred with rationale — building them now is scope creep, not initiative.

## Ground rules we learned the hard way

- **Verify behavior, not strings.** Never gate a deploy on grep'ing a binary
  or a log; assert what the system *does*. A grep-gated deploy chain once ran
  old code for a day while every diagnostic said the fix was in.
- **The e2e gate is the spec.** If a promise matters, it has a step in
  `e2e/run.sh` or `e2e/chaos.sh`. A change that weakens a step needs an RFC
  addendum, not a quiet edit.
- **CI is the arbiter.** Green on your laptop is an anecdote; green on the PR
  is the fact (`.github/workflows/ci.yml` runs unit + installer + e2e + chaos).
- **One writer.** The operator is single-replica by design (no leader
  election yet — see backlog). Never scale the Deployment to 2.
- **File naming.** Ecosystem-mandated names keep their canonical casing
  (`Chart.yaml`, `Cargo.toml`, `Dockerfile`, `README.md`); everything we
  name ourselves is lowercase-kebab (`hardening.md`, `check-hardening.sh`,
  `rfcs/business/vision.md`).
