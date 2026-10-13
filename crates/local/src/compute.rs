//! Shared-host PostgreSQL profiles, idle lifecycle scheduling and storage sampling.
//! CPU and RAM are tuning budgets, not reservations or hard process limits.
use crate::store::{Result, Store, error::invalid};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    sync::mpsc,
    time::{Duration, Instant},
};
use supabricks_core::resource::{BranchId, DesiredState};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    pub size: String,
    pub idle_timeout_seconds: Option<u32>,
}
impl Default for Settings {
    fn default() -> Self {
        Self {
            size: "m".into(),
            idle_timeout_seconds: None,
        }
    }
}
impl Settings {
    pub(crate) fn validate(&self) -> Result<()> {
        profile(&self.size)?;
        if self
            .idle_timeout_seconds
            .is_some_and(|s| !(60..=86400).contains(&s))
        {
            return Err(invalid(
                "idle_timeout_seconds must be null (never) or 60–86400",
            ));
        }
        Ok(())
    }
}
#[derive(Clone, Copy, Debug, Serialize)]
pub struct Profile {
    pub id: &'static str,
    pub label: &'static str,
    pub vcpu: u32,
    pub memory_bytes: u64,
    pub max_connections: usize,
}
pub const PROFILES: [Profile; 3] = [
    Profile {
        id: "s",
        label: "Small",
        vcpu: 1,
        memory_bytes: 1024 * 1024 * 1024,
        max_connections: 32,
    },
    Profile {
        id: "m",
        label: "Medium",
        vcpu: 2,
        memory_bytes: 2 * 1024 * 1024 * 1024,
        max_connections: 64,
    },
    Profile {
        id: "l",
        label: "Large",
        vcpu: 4,
        memory_bytes: 4 * 1024 * 1024 * 1024,
        max_connections: 128,
    },
];
pub fn profile(id: &str) -> Result<Profile> {
    PROFILES
        .iter()
        .copied()
        .find(|p| p.id == id)
        .ok_or_else(|| invalid("unknown compute size; use s, m or l"))
}
impl Profile {
    pub(crate) fn apply(self, settings: &mut Vec<Value>) {
        let values = [
            ("max_connections", (self.max_connections + 16).to_string()),
            ("superuser_reserved_connections", "8".into()),
            (
                "shared_buffers",
                format!("{}MB", self.memory_bytes / 4 / 1024 / 1024),
            ),
            (
                "effective_cache_size",
                format!("{}MB", self.memory_bytes * 3 / 4 / 1024 / 1024),
            ),
            ("work_mem", "4MB".into()),
            (
                "maintenance_work_mem",
                format!("{}MB", self.memory_bytes / 16 / 1024 / 1024),
            ),
            ("max_parallel_workers", self.vcpu.to_string()),
            (
                "max_parallel_workers_per_gather",
                self.vcpu.saturating_sub(1).to_string(),
            ),
        ];
        for (name, value) in values {
            settings.retain(|s| s["name"] != name);
            settings.push(json!({"name":name,"value":value,"vartype":"string"}));
        }
    }
}

/// Timers are monotonic and owner-local: every restart or busy period earns a
/// full new idle interval. The journal rechecks blockers atomically at expiry.
#[derive(Default)]
pub(crate) struct Service {
    idle: HashMap<BranchId, (Instant, i64, Settings, i64)>,
    samples: Option<mpsc::Receiver<Vec<Sample>>>,
    next_sample: Option<Instant>,
}
pub(crate) struct Sample {
    pub branch: BranchId,
    pub sampled_at_ms: i64,
    pub logical: Option<u64>,
    pub physical: Option<u64>,
    pub accurate: bool,
    pub available: bool,
}
impl Sample {
    fn from_detail(branch: BranchId, detail: Option<Value>) -> Self {
        let value = detail.unwrap_or(Value::Null);
        Self {
            branch,
            sampled_at_ms: chrono::Utc::now().timestamp_millis(),
            logical: value["current_logical_size"].as_u64(),
            physical: value["current_physical_size"].as_u64(),
            accurate: value["current_logical_size_is_accurate"]
                .as_bool()
                .unwrap_or(false),
            available: value["state"] == "Active",
        }
    }
}
impl Service {
    fn tick_idle(&mut self, store: &mut Store, now: Instant) -> Result<()> {
        let branches = store.branches()?;
        self.idle
            .retain(|id, _| branches.iter().any(|b| b.branch.id == *id));
        for branch in &branches {
            let id = branch.branch.id;
            let settings = store.compute_settings(id)?;
            let activity = store.compute_activity(id)?;
            let timeout = settings.idle_timeout_seconds;
            if timeout.is_none()
                || branch.endpoint.desired_state != DesiredState::Running
                || branch.revision != branch.observed_revision
                || store.is_export(id)?
                || !store.compute_idle(id)?
            {
                self.idle.remove(&id);
                continue;
            }
            let entry =
                self.idle
                    .entry(id)
                    .or_insert((now, branch.revision, settings.clone(), activity));
            if entry.1 != branch.revision || entry.2 != settings || entry.3 != activity {
                *entry = (now, branch.revision, settings, activity);
            }
            if now.duration_since(entry.0) >= Duration::from_secs(u64::from(timeout.unwrap())) {
                let key = format!("internal:idle-suspend:{id}:{}", branch.revision);
                store.submit(
                    branch.branch.project_id,
                    &key,
                    crate::operations::Mutation::AutoSuspend {
                        branch_id: id,
                        expected_revision: branch.revision,
                    },
                )?;
                self.idle.remove(&id);
            }
        }
        Ok(())
    }
    pub(crate) fn tick(&mut self, store: &mut Store, cell: &crate::engine::Cell) -> Result<()> {
        if let Some(rx) = &self.samples {
            match rx.try_recv() {
                Ok(samples) => {
                    for sample in samples {
                        store.save_storage_sample(sample)?;
                    }
                    self.samples = None;
                }
                Err(mpsc::TryRecvError::Disconnected) => self.samples = None,
                Err(mpsc::TryRecvError::Empty) => (),
            }
        }
        let now = Instant::now();
        self.tick_idle(store, now)?;
        let branches = store.branches()?;
        if self.samples.is_none() && self.next_sample.is_none_or(|due| now >= due) {
            let ps = cell.pageserver()?;
            let targets: Vec<_> = branches
                .into_iter()
                .filter(|b| b.timeline_created && b.endpoint.desired_state != DesiredState::Deleted)
                .filter(|b| !store.is_export(b.branch.id).unwrap_or(true))
                .collect();
            let (tx, rx) = mpsc::channel();
            std::thread::Builder::new()
                .name("branch-storage".into())
                .spawn(move || {
                    let samples = targets
                        .into_iter()
                        .map(|b| {
                            let detail = ps
                                .detail(&b)
                                .ok()
                                .filter(|(code, _)| *code == 200)
                                .map(|(_, v)| v);
                            Sample::from_detail(b.branch.id, detail)
                        })
                        .collect();
                    let _ = tx.send(samples);
                })?;
            self.samples = Some(rx);
            self.next_sample = Some(now + Duration::from_secs(30));
        }
        Ok(())
    }
}

fn host_memory() -> Option<u64> {
    #[cfg(target_os = "linux")]
    {
        let text = std::fs::read_to_string("/proc/meminfo").ok()?;
        let host = text
            .lines()
            .find_map(|line| line.strip_prefix("MemTotal:"))?
            .split_whitespace()
            .next()?
            .parse::<u64>()
            .ok()?
            .checked_mul(1024)?;
        // On unified cgroups the process may have a smaller allowance than the host.
        let cgroup = std::fs::read_to_string("/proc/self/cgroup")
            .ok()
            .and_then(|s| {
                s.lines()
                    .find_map(|l| l.strip_prefix("0::").map(str::to_owned))
            })
            .and_then(|path| {
                std::fs::read_to_string(
                    std::path::Path::new("/sys/fs/cgroup")
                        .join(path.trim_start_matches('/'))
                        .join("memory.max"),
                )
                .ok()
            })
            .and_then(|s| s.trim().parse::<u64>().ok());
        Some(cgroup.map_or(host, |n| n.min(host)))
    }
    #[cfg(target_os = "macos")]
    {
        let output = std::process::Command::new("/usr/sbin/sysctl")
            .args(["-n", "hw.memsize"])
            .output()
            .ok()?;
        if !output.status.success() {
            return None;
        }
        String::from_utf8(output.stdout).ok()?.trim().parse().ok()
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    {
        None
    }
}
pub(crate) fn validate_host(p: Profile) -> Result<()> {
    let cores = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(0);
    if cores < p.vcpu as usize || host_memory().is_none_or(|bytes| bytes < p.memory_bytes) {
        return Err(crate::store::error::conflict(
            "compute size exceeds detected host CPU or memory; use a smaller size",
        ));
    }
    Ok(())
}
pub(crate) fn catalog() -> Value {
    json!({"items":PROFILES.iter().map(|p| {
        let mut value = json!(p);
        value["available"] = json!(validate_host(*p).is_ok());
        value["resource_model"] = json!("shared_host_tuning");
        value
    }).collect::<Vec<_>>(),"default":"m","installation_connection_limit":256,
        "cpu_memory_isolation":false,"resize_requires_restart":true})
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::compute::tests::{configure_idle, finish, fixture};
    #[test]
    fn compute_idle_timer_observes_brief_connections_and_restarts_its_interval() {
        let (_root, mut store, project, id) = fixture();
        configure_idle(&mut store, project, id, Some(60));
        let now = Instant::now();
        let mut service = Service::default();
        service.tick_idle(&mut store, now).unwrap();
        let lease = store.accept_connection(id).unwrap();
        store.release_connection(lease).unwrap();
        service
            .tick_idle(&mut store, now + Duration::from_secs(59))
            .unwrap();
        service
            .tick_idle(&mut store, now + Duration::from_secs(118))
            .unwrap();
        assert_eq!(
            store.branch(id).unwrap().endpoint.desired_state,
            DesiredState::Running
        );
        service
            .tick_idle(&mut store, now + Duration::from_secs(119))
            .unwrap();
        assert_eq!(
            store.branch(id).unwrap().endpoint.desired_state,
            DesiredState::Suspended
        );
        assert_eq!(
            store.compute_view(id).unwrap()["last_suspend_reason"],
            "idle_timeout"
        );
        let pending = store.pending().unwrap();
        assert_eq!(pending.len(), 1);
        finish(&mut store, &pending[0]);
        let lease = store.accept_connection(id).unwrap();
        store.wake_for_connection(id).unwrap();
        assert_eq!(
            store.branch(id).unwrap().endpoint.desired_state,
            DesiredState::Running
        );
        store.release_connection(lease).unwrap();
    }
    #[test]
    fn compute_never_and_new_owner_do_not_suspend_early() {
        let (_root, mut store, project, id) = fixture();
        let now = Instant::now();
        let mut service = Service::default();
        service.tick_idle(&mut store, now).unwrap();
        service
            .tick_idle(&mut store, now + Duration::from_secs(86401))
            .unwrap();
        assert_eq!(store.branch(id).unwrap().revision, 1);
        configure_idle(&mut store, project, id, Some(60));
        service.tick_idle(&mut store, now).unwrap();
        let mut restarted = Service::default();
        restarted
            .tick_idle(&mut store, now + Duration::from_secs(59))
            .unwrap();
        restarted
            .tick_idle(&mut store, now + Duration::from_secs(60))
            .unwrap();
        assert_eq!(store.branch(id).unwrap().revision, 1);
    }
    #[test]
    fn compute_profile_replaces_settings_without_duplicates_and_validates_timeout() {
        assert!(
            validate_host(Profile {
                vcpu: u32::MAX,
                ..PROFILES[0]
            })
            .is_err()
        );
        assert!(
            validate_host(Profile {
                memory_bytes: u64::MAX,
                ..PROFILES[0]
            })
            .is_err()
        );
        let mut settings = vec![json!({"name":"max_connections","value":"100"})];
        profile("s").unwrap().apply(&mut settings);
        profile("m").unwrap().apply(&mut settings);
        assert_eq!(
            settings
                .iter()
                .filter(|s| s["name"] == "max_connections")
                .count(),
            1
        );
        assert_eq!(
            settings
                .iter()
                .find(|s| s["name"] == "max_connections")
                .unwrap()["value"],
            "80"
        );
        assert!(
            Settings {
                size: "s".into(),
                idle_timeout_seconds: Some(0)
            }
            .validate()
            .is_err()
        );
        assert!(
            Settings {
                size: "s".into(),
                idle_timeout_seconds: Some(86401)
            }
            .validate()
            .is_err()
        );
    }
}
