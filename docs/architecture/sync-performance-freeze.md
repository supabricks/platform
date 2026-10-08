# SP workstream freeze — 2026-10-06

The user directed that SP remain frozen while the [TPC-DS end-to-end work](../plans/tpcds-end-to-end-qualification.md)
proceeds. Review the E2E results before resuming SP. This changes sequencing; it
does not waive failed gates, accept pending candidates, or close performance issues.

Frozen branch: `feat/sp11-sustained-qualification` at
`8b68cd206edd5de2b1f820c90c39e7a76aedf3aa`; [PR #167](https://github.com/supabricks/platform/pull/167)
remains draft and unmerged. All CI checks for that revision passed, including
exact Linux/macOS release, console and environment lifecycle checks in
[run 37522400869](https://github.com/supabricks/platform/actions/runs/37522400869).
EQ starts on a separate branch based on this tested engineering candidate.

Latest SP11 launch 05 stopped after its first 8-CPU one-hour fixture. Overall
input 1,204.238 rows/s and publication p95 3,753.594 ms passed their whole-run
gates. Five of 57 throughput windows failed, starting at minutes 52–57; the
worst published rate was 779.127 rows/s. Worst-window p95 was 4,619.012 ms.
Correctness, foreign keys, reopen, drain, memory, backlog, spool and sampling
passed. No detected build overlap; zero leaked or remaining descendants.
**0/8 accepted, one executed, seven not run.** No SP service is running.

[Issue #169](https://github.com/supabricks/platform/issues/169#issuecomment-6026445728)
retains the failure and verified receipt hashes. Preserve the complete original
attempt at `/data2/supabricks-performance/issue169-20261006/steady-05/` and
all prior repository/local evidence. The native package identity is
`99590cc04aafdee52350c41d008fe375db44373354f07883f587d9425d3ba2fa`;
[launch inputs](sync-performance-evidence/2026-10-06-issue169/launch-05.json)
remain immutable. The original system disk's failures remain separate failures.

On resumption, outstanding work includes #169 attribution/correction and measured
acceptance, remaining SP11 maintenance/reader/capacity/workload/recovery gates,
SP10c's two missing sustained comparison arms, and SP12 installed performance
qualification/documentation. EQ findings may inform that backlog but do not
retroactively qualify SP. Do not delete SP evidence to admit larger EQ scales.
