# SP08 shutdown correction: functional validation

Native source 54deabbf6ed105def5c3323b28a0df7b6e54bcc9 passes the full capture
lifecycle gate, including worker SIGKILL and daemon/compute restart, without
duplicates, gaps, or an unwanted resync. All ten capture checks pass; cleanup
reports zero leaked/remaining descendants. All 207 local Rust tests pass, with
four ignored.

This is functional validation outside the performance denominator; no quiet-host
or performance qualification claim. The original failed candidate is preserved
in ../2026-10-01-sp08-shutdown/. A fresh full campaign runs under
build/sp08-20261001/campaign-02 using the unchanged frozen controller and common
predecessor, the new candidate package, and the original protocol. CI and full
continuous lifecycle/performance results remain pending.
