//! Read-only, identity-fenced accounting of the owned compute_ctl process tree.
#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "macos")]
mod macos;
use super::*;
use crate::supervisor::{OwnedProcess, os as identity};
#[cfg(target_os = "linux")]
use linux as os;
#[cfg(target_os = "macos")]
use macos as os;
use std::collections::HashSet;
const MAX_PROCESSES: usize = 512;
const BUDGET: Duration = Duration::from_secs(1);
type ProbeResult<T> = std::result::Result<T, &'static str>;
#[derive(Clone, Debug, Serialize, Deserialize)]
pub(super) struct Observation {
    pub status: String,
    pub at_ms: i64,
    pub raw: Option<Counters>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub(super) struct Counters {
    root_identity: String,
    elapsed_ns: u64,
    cpu_ns: u64,
    rss_sum_bytes: u64,
    process_count: u32,
    collection_ns: u64,
}
#[derive(Clone, Debug)]
struct Process {
    pid: u32,
    parent: u32,
    uid: u32,
    start: String,
    cpu_ns: u64,
    reaped_cpu_ns: u64,
    rss_bytes: u64,
}
impl Process {
    fn same(&self, other: &Self) -> bool {
        self.pid == other.pid
            && self.parent == other.parent
            && self.uid == other.uid
            && self.start == other.start
    }
}
fn check(deadline: Instant) -> ProbeResult<()> {
    if Instant::now() >= deadline {
        Err("probe_budget_exceeded")
    } else {
        Ok(())
    }
}
fn add(a: u64, b: u64) -> ProbeResult<u64> {
    a.checked_add(b).ok_or("counter_overflow")
}
fn verify_root(root: &OwnedProcess) -> ProbeResult<()> {
    let id = identity::identity(root.pid)
        .map_err(|_| "probe_unavailable")?
        .ok_or("compute_identity_changed")?;
    if id.zombie || id.start != root.start_identity || id.uid != unsafe { libc::geteuid() } {
        return Err("compute_identity_changed");
    }
    if !identity::has_token(&id, &root.token).map_err(|_| "probe_unavailable")? {
        return Err("compute_identity_changed");
    }
    Ok(())
}
fn walk(
    pid: u32,
    parent: Option<u32>,
    uid: u32,
    deadline: Instant,
    seen: &mut HashSet<u32>,
) -> ProbeResult<(u64, u64)> {
    check(deadline)?;
    if seen.len() >= MAX_PROCESSES || !seen.insert(pid) {
        return Err("process_tree_limit");
    }
    let before = os::read(pid)?;
    if before.uid != uid || parent.is_some_and(|p| before.parent != p) {
        return Err("process_tree_changed");
    }
    let children = os::children(pid, deadline)?;
    let mut cpu = add(before.cpu_ns, before.reaped_cpu_ns)?;
    let mut rss = before.rss_bytes;
    for child in &children {
        let (child_cpu, child_rss) = walk(*child, Some(pid), uid, deadline, seen)?;
        cpu = add(cpu, child_cpu)?;
        rss = add(rss, child_rss)?;
    }
    // A wait/reparent/reuse during the traversal must not transfer CPU twice.
    let after = os::read(pid)?;
    if !before.same(&after)
        || before.reaped_cpu_ns != after.reaped_cpu_ns
        || children != os::children(pid, deadline)?
    {
        return Err("process_tree_changed");
    }
    check(deadline)?;
    Ok((cpu, rss))
}
pub(super) fn collect(root: Option<OwnedProcess>, started: Instant) -> Observation {
    let begin = Instant::now();
    let deadline = begin + BUDGET;
    let attempt = || -> ProbeResult<Counters> {
        let root = root.as_ref().ok_or("compute_identity_unavailable")?;
        verify_root(root)?;
        let mut seen = HashSet::new();
        let (cpu_ns, rss_sum_bytes) = walk(
            root.pid,
            None,
            unsafe { libc::geteuid() },
            deadline,
            &mut seen,
        )?;
        verify_root(root)?;
        check(deadline)?;
        Ok(Counters {
            root_identity: format!("{}:{}", root.pid, root.start_identity),
            elapsed_ns: started.elapsed().as_nanos() as u64,
            cpu_ns,
            rss_sum_bytes,
            process_count: seen.len() as u32,
            collection_ns: begin.elapsed().as_nanos() as u64,
        })
    };
    let mut result = attempt();
    // One bounded retry permits a backend exit to finish; never spin on churn.
    if matches!(result, Err("process_tree_changed")) && Instant::now() < deadline {
        result = attempt();
    }
    Observation {
        status: result
            .as_ref()
            .map(|_| "available")
            .unwrap_or_else(|e| e)
            .into(),
        at_ms: crate::identity::now(),
        raw: result.ok(),
    }
}
pub(super) fn empty(status: &str) -> Value {
    json!({"status":status,"observed_at_ms":null,"cpu_cores":null,"cpu_status":status,"cpu_interval_ms":null,"rss_sum_bytes":null,"process_count":null,"collection_ms":null})
}
pub(super) fn point(sample: &Sample, previous: Option<&Sample>) -> Value {
    let Some(observation) = &sample.compute else {
        return empty(if sample.status == "available" {
            "not_collected"
        } else {
            &sample.status
        });
    };
    let mut value = empty(&observation.status);
    value["observed_at_ms"] = json!(observation.at_ms);
    let Some(raw) = &observation.raw else {
        return value;
    };
    value["rss_sum_bytes"] = json!(raw.rss_sum_bytes);
    value["process_count"] = json!(raw.process_count);
    value["collection_ms"] = json!(raw.collection_ns as f64 / 1_000_000.);
    value["cpu_status"] = json!("no_baseline");
    let Some(previous) = previous else {
        return value;
    };
    let Some(old) = previous.compute.as_ref().and_then(|c| c.raw.as_ref()) else {
        return value;
    };
    let elapsed = raw.elapsed_ns.checked_sub(old.elapsed_ns).unwrap_or(0);
    if sample.generation != previous.generation
        || sample.revision != previous.revision
        || raw.root_identity != old.root_identity
        || elapsed == 0
        || elapsed > 90_000_000_000
    {
        value["cpu_status"] = json!("gap_or_restart");
        return value;
    }
    let Some(cpu) = raw.cpu_ns.checked_sub(old.cpu_ns) else {
        value["cpu_status"] = json!("counter_regression");
        return value;
    };
    value["cpu_cores"] = json!(cpu as f64 / elapsed as f64);
    value["cpu_interval_ms"] = json!(elapsed as f64 / 1_000_000.);
    value["cpu_status"] = json!("available");
    value
}
#[cfg(test)]
mod tests;
