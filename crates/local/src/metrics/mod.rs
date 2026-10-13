//! Bounded, installation-local PostgreSQL history. Never wakes a compute.
mod history;
mod latency;
mod resources;
mod sql;
#[cfg(test)]
mod tests;
use crate::{
    api::Binding,
    console::workspace::Target,
    engine::Cell,
    store::{
        Result, Store,
        error::{conflict, invalid},
    },
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};
use supabricks_core::resource::{BranchId, DesiredState, ProjectId};
const INTERVAL_MS: i64 = 30_000;
const RETENTION_MS: i64 = 7 * 24 * 3600 * 1000;
const MAX_BRANCHES: usize = 64;
const WORKERS: usize = 2;
#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize)]
pub enum Range {
    #[default]
    #[serde(rename = "1h")]
    Hour,
    #[serde(rename = "6h")]
    SixHours,
    #[serde(rename = "24h")]
    Day,
    #[serde(rename = "7d")]
    Week,
}
impl Range {
    fn bounds(self) -> (i64, i64) {
        match self {
            Self::Hour => (3600_000, INTERVAL_MS),
            Self::SixHours => (6 * 3600_000, INTERVAL_MS),
            Self::Day => (24 * 3600_000, 60_000),
            Self::Week => (RETENTION_MS, 600_000),
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    History {
        target: Target,
        #[serde(default)]
        range: Range,
    },
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct Raw {
    // OID and timestamps distinguish replacements and stats resets.
    database_oid: String,
    postmaster_start: String,
    database_reset: Option<String>,
    wal_reset: Option<String>,
    commits: u64,
    rollbacks: u64,
    hits: u64,
    reads: u64,
    wal_bytes: u64,
    connections_active: u64,
    connections_idle: u64,
    connections_other: u64,
    database_size_bytes: u64,
    #[serde(default)]
    latency: Option<latency::Snapshot>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct Sample {
    at_ms: i64,
    generation: i64,
    revision: i64,
    status: String,
    raw: Option<Raw>,
    #[serde(default)]
    compute: Option<resources::Observation>,
}
impl Sample {
    fn point(&self, previous: Option<&Self>) -> Value {
        let mut value = json!({"observed_at_ms":self.at_ms,"status":self.status,"connections_active":null,"connections_idle":null,"connections_other":null,"database_size_bytes":null,"commits_per_second":null,"rollbacks_per_second":null,"wal_bytes_per_second":null,"cache_hit_ratio":null,"rate_interval_ms":null,"rates_status":"no_baseline"});
        value["compute"] = resources::point(self, previous);
        value["latency"] = latency::point(self, previous);
        let Some(raw) = &self.raw else {
            value["rates_status"] = json!("sample_unavailable");
            return value;
        };
        value["connections_active"] = json!(raw.connections_active);
        value["connections_idle"] = json!(raw.connections_idle);
        value["connections_other"] = json!(raw.connections_other);
        value["database_size_bytes"] = json!(raw.database_size_bytes);
        let Some(prev) = previous else { return value };
        let elapsed = self.at_ms - prev.at_ms;
        let Some(old) = &prev.raw else {
            value["rates_status"] = json!("gap");
            return value;
        };
        if elapsed <= 0
            || elapsed > INTERVAL_MS * 3
            || self.generation != prev.generation
            || self.revision != prev.revision
            || raw.postmaster_start != old.postmaster_start
            || raw.database_oid != old.database_oid
        {
            value["rates_status"] = json!("gap_or_restart");
            return value;
        }
        // Conservatively fence the entire interval on either reset/regression.
        if raw.database_reset != old.database_reset
            || raw.wal_reset != old.wal_reset
            || raw.commits < old.commits
            || raw.rollbacks < old.rollbacks
            || raw.hits < old.hits
            || raw.reads < old.reads
            || raw.wal_bytes < old.wal_bytes
        {
            value["rates_status"] = json!("counter_reset");
            return value;
        }
        let seconds = elapsed as f64 / 1000.;
        value["commits_per_second"] = json!((raw.commits - old.commits) as f64 / seconds);
        value["rollbacks_per_second"] = json!((raw.rollbacks - old.rollbacks) as f64 / seconds);
        value["wal_bytes_per_second"] = json!((raw.wal_bytes - old.wal_bytes) as f64 / seconds);
        let hits = (raw.hits - old.hits) as f64;
        let reads = (raw.reads - old.reads) as f64;
        if hits + reads > 0. {
            value["cache_hit_ratio"] = json!(hits / (hits + reads));
        }
        value["rate_interval_ms"] = json!(elapsed);
        value["rates_status"] = json!("available");
        value
    }
}
struct Job {
    project: ProjectId,
    target: Target,
    generation: i64,
    thread: thread::JoinHandle<()>,
    receiver: mpsc::Receiver<(i64, Option<Raw>, resources::Observation)>,
}
pub struct Service {
    history: Option<history::History>,
    jobs: Vec<Job>,
    due: HashMap<BranchId, Instant>,
    next_scan: Instant,
    storage_error: bool,
    started: Instant,
}
impl Service {
    pub fn open(store: &Store) -> Self {
        let history = history::History::open(store.root()).ok();
        Self {
            storage_error: history.is_none(),
            started: Instant::now(),
            history,
            jobs: vec![],
            due: HashMap::new(),
            next_scan: Instant::now(),
        }
    }
    pub fn active(&self) -> usize {
        self.jobs.len()
    }
    fn save(&mut self, project: ProjectId, branch: BranchId, sample: Sample) {
        if let Some(history) = &mut self.history {
            self.storage_error = history.append(project, branch, &sample).is_err();
        }
    }
    pub fn tick(&mut self, store: &Store, cell: Option<&Cell>, stopping: bool) {
        for i in (0..self.jobs.len()).rev() {
            if !self.jobs[i].thread.is_finished() {
                continue;
            }
            let job = self.jobs.swap_remove(i);
            let _ = job.thread.join();
            // Work can finish after suspend/delete/governance changes. Discard it.
            if !store.branch(job.target.branch).is_ok_and(|b| {
                b.revision == job.target.revision
                    && b.endpoint.desired_state == DesiredState::Running
            }) || store.governed_branch(job.target.branch).unwrap_or(true)
            {
                continue;
            }
            let (at_ms, raw, compute) = job
                .receiver
                .try_recv()
                .map(|(at, raw, compute)| (at, raw, Some(compute)))
                .unwrap_or_else(|_| (crate::identity::now(), None, None));
            self.save(
                job.project,
                job.target.branch,
                Sample {
                    at_ms,
                    generation: job.generation,
                    revision: job.target.revision,
                    status: if raw.is_some() {
                        "available"
                    } else {
                        "collection_failed"
                    }
                    .into(),
                    raw,
                    compute,
                },
            );
        }
        if stopping || self.history.is_none() || Instant::now() < self.next_scan {
            return;
        }
        self.next_scan = Instant::now() + Duration::from_secs(1);
        if let Some(history) = &mut self.history {
            if history.prune(crate::identity::now()).is_err() {
                self.storage_error = true;
            }
        }
        let Ok(branches) = eligible(store) else {
            return;
        };
        self.due
            .retain(|id, _| branches.iter().any(|b| b.branch.id == *id));
        let now = Instant::now();
        let mut branches = branches;
        // Oldest due first; slow branches cannot monopolize the two workers.
        branches.sort_by_key(|b| self.due.get(&b.branch.id).copied().unwrap_or(now));
        for branch in branches {
            let id = branch.branch.id;
            if self.due.get(&id).is_some_and(|due| *due > now)
                || self.jobs.iter().any(|j| j.target.branch == id)
            {
                continue;
            }
            let status = if branch.endpoint.desired_state == DesiredState::Suspended {
                Some("suspended")
            } else if cell.is_none() {
                Some("engine_disabled")
            } else if branch.revision != branch.observed_revision
                || !cell
                    .unwrap()
                    .connection_ready(store, &branch)
                    .unwrap_or(false)
            {
                Some("not_ready")
            } else {
                None
            };
            if status.is_none() && self.active() >= WORKERS {
                continue;
            }
            self.due
                .insert(id, now + Duration::from_millis(INTERVAL_MS as u64));
            if let Some(status) = status {
                self.save(
                    branch.branch.project_id,
                    id,
                    Sample {
                        at_ms: crate::identity::now(),
                        generation: store.generation(),
                        revision: branch.revision,
                        status: status.into(),
                        raw: None,
                        compute: None,
                    },
                );
                continue;
            }
            let Some(ports) = branch.ports else { continue };
            let Ok(password) = store.endpoint_password(branch.endpoint.id) else {
                continue;
            };
            let root = store.native_processes().ok().and_then(|records| {
                records.into_iter().find(|p| {
                    p.role == format!("compute-{}", branch.endpoint.id)
                        && p.generation == store.generation()
                        && p.branch == Some((id, branch.revision))
                        && p.root == store.root()
                })
            });
            let started = self.started;
            let (sender, receiver) = mpsc::channel();
            match thread::Builder::new()
                .name("postgres-metrics".into())
                .spawn(move || {
                    let raw = sql::collect(ports.sql, password).ok();
                    let at_ms = crate::identity::now();
                    // OS observation remains useful if SQL is overloaded/unavailable.
                    let compute = resources::collect(root, started);
                    let _ = sender.send((at_ms, raw, compute));
                }) {
                Ok(thread) => self.jobs.push(Job {
                    project: branch.branch.project_id,
                    target: Target {
                        branch: id,
                        revision: branch.revision,
                    },
                    generation: store.generation(),
                    thread,
                    receiver,
                }),
                Err(_) => self.save(
                    branch.branch.project_id,
                    id,
                    Sample {
                        at_ms: crate::identity::now(),
                        generation: store.generation(),
                        revision: branch.revision,
                        status: "worker_unavailable".into(),
                        raw: None,
                        compute: None,
                    },
                ),
            }
        }
    }
    pub fn handle(&self, store: &Store, binding: &Binding, command: Command) -> Result<Value> {
        let Command::History { target, range } = command;
        target.validate(store, binding)?;
        if store.governed_branch(target.branch)? {
            return Err(invalid(
                "metrics requires local operator authority; governed monitoring is not enabled",
            ));
        }
        let history = self.history.as_ref().ok_or_else(|| {
            conflict("metrics storage unavailable; inspect the local metrics database and restart")
        })?;
        let mut result = history.read(
            binding.project_id,
            target.branch,
            range,
            crate::identity::now(),
        )?;
        result["target"] = json!(target);
        result["collection_admitted"] = json!(
            eligible(store)?
                .iter()
                .any(|b| b.branch.id == target.branch)
        );
        result["storage_healthy"] = json!(!self.storage_error);
        Ok(result)
    }
}
fn eligible(store: &Store) -> Result<Vec<crate::store::BranchRecord>> {
    let mut branches = Vec::new();
    for b in store.branches()? {
        if b.endpoint.desired_state != DesiredState::Deleted
            && !b.expired
            && !store.is_export(b.branch.id)?
            && !store.governed_branch(b.branch.id)?
        {
            branches.push(b)
        }
        if branches.len() == MAX_BRANCHES {
            break;
        }
    }
    Ok(branches)
}
