//! Local-owner snapshot, triggered and supervised continuous policies.
use crate::{
    api::Binding,
    store::{ExportLimits, Result, Store, error::invalid},
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use supabricks_core::resource::{BranchId, DeploymentId, OperationId, ProjectId};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Schedule {
    pub interval_seconds: u64,
    pub timezone: String,
    pub missed_run: String,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Continuous {
    pub freshness_ms: u64,
    pub batch_interval_ms: u64,
}
impl Default for Continuous {
    fn default() -> Self {
        Self {
            freshness_ms: 5000,
            batch_interval_ms: 500,
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub mode: String,
    pub strategy: String,
    pub schedule: Option<Schedule>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub continuous: Option<Continuous>,
    #[serde(default)]
    pub limits: ExportLimits,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            mode: "snapshot".into(),
            strategy: "full".into(),
            schedule: None,
            continuous: None,
            limits: Default::default(),
        }
    }
}
impl Config {
    pub fn incremental(&self) -> bool {
        matches!(self.mode.as_str(), "triggered" | "continuous") && self.strategy == "incremental"
    }
    pub fn continuous(&self) -> bool {
        self.mode == "continuous"
    }
    pub fn continuous_config(&self) -> Continuous {
        self.continuous.clone().unwrap_or_default()
    }
    pub fn validate(&self) -> Result<()> {
        if !((self.mode == "snapshot" && self.strategy == "full") || self.incremental()) {
            return Err(invalid(
                "supported policies are snapshot/full, triggered/incremental and continuous/incremental",
            ));
        }
        if self.continuous() {
            let c = self.continuous_config();
            if self.schedule.is_some()
                || !(1000..=300000).contains(&c.freshness_ms)
                || !(200..=60000).contains(&c.batch_interval_ms)
                || c.batch_interval_ms > c.freshness_ms
            {
                return Err(invalid(
                    "continuous requires no schedule, freshness 1000–300000 ms and batch interval 200–60000 ms no greater than freshness",
                ));
            }
        } else if self.continuous.is_some() {
            return Err(invalid("continuous settings require continuous mode"));
        }
        self.limits.validate()?;
        if let Some(s) = &self.schedule {
            if !(60..=2_592_000).contains(&s.interval_seconds)
                || s.timezone != "UTC"
                || s.missed_run != "coalesce"
            {
                return Err(invalid(
                    "schedule requires a 60–2592000 second UTC interval and missed_run=coalesce",
                ));
            }
        }
        Ok(())
    }
    pub(crate) fn next_due(&self, now: i64) -> Option<i64> {
        self.schedule
            .as_ref()
            .map(|s| now.saturating_add(s.interval_seconds as i64 * 1000))
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Policy {
    pub id: OperationId,
    #[serde(default)]
    pub capture_id: Option<OperationId>,
    #[serde(default)]
    pub pause_requested: bool,
    #[serde(default)]
    pub observation: Option<Value>,
    pub project_id: ProjectId,
    pub deployment_id: DeploymentId,
    pub branch_id: BranchId,
    pub installation_id: String,
    pub tenant_id: String,
    pub timeline_id: String,
    pub database: String,
    /// A01 discovers and validates the complete application-table set at each cut.
    pub membership: String,
    pub authority: String,
    pub revision: i64,
    pub state: String,
    pub config: Config,
    pub created_at_ms: i64,
    pub next_due_at_ms: Option<i64>,
    pub last_success_at_ms: Option<i64>,
    pub last_epoch_id: Option<String>,
    pub error: Option<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Run {
    pub id: OperationId,
    pub policy_id: OperationId,
    pub policy_revision: i64,
    pub config: Config,
    pub project_id: ProjectId,
    pub branch_id: BranchId,
    pub trigger: String,
    #[serde(default)]
    pub capture_id: Option<OperationId>,
    #[serde(default)]
    pub target_lsn: Option<String>,
    #[serde(default)]
    pub apply_id: Option<OperationId>,
    #[serde(default)]
    pub batches: u32,
    #[serde(default)]
    pub deadline_ms: Option<i64>,
    pub state: String,
    pub admitted_at_ms: i64,
    pub scheduled_for_ms: Option<i64>,
    pub finished_at_ms: Option<i64>,
    pub refresh_id: Option<OperationId>,
    pub epoch_id: Option<String>,
    pub source_lsn: Option<String>,
    pub error: Option<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Create {
        branch: String,
        key: String,
        config: Config,
    },
    Update {
        id: OperationId,
        expected_revision: i64,
        key: String,
        config: Config,
    },
    Pause {
        id: OperationId,
        expected_revision: i64,
        key: String,
    },
    Resume {
        id: OperationId,
        expected_revision: i64,
        key: String,
    },
    Delete {
        id: OperationId,
        expected_revision: i64,
        key: String,
    },
    RunNow {
        id: OperationId,
        expected_revision: i64,
        key: String,
    },
    Cancel {
        id: OperationId,
        key: String,
    },
    Get {
        id: OperationId,
    },
    List,
    Runs {
        id: OperationId,
        #[serde(default = "history_limit")]
        limit: usize,
    },
    Run {
        id: OperationId,
    },
}
fn history_limit() -> usize {
    50
}
impl Command {
    pub(crate) fn key(&self) -> Option<&str> {
        match self {
            Self::Create { key, .. }
            | Self::Update { key, .. }
            | Self::Pause { key, .. }
            | Self::Resume { key, .. }
            | Self::Delete { key, .. }
            | Self::RunNow { key, .. }
            | Self::Cancel { key, .. } => Some(key),
            _ => None,
        }
    }
}
pub fn handle(store: &mut Store, binding: &Binding, command: Command) -> Result<Value> {
    let deployment = store.binding_context(binding)?.deployment_id;
    store.sync_command(
        binding.project_id,
        deployment,
        command,
        chrono::Utc::now().timestamp_millis(),
    )
}

/// One daemon writer, one native export at a time. No browser/worktree lifetime dependency.
pub(crate) fn tick(store: &mut Store, cell: Option<&crate::engine::Cell>) -> Result<()> {
    let now = chrono::Utc::now().timestamp_millis();
    store.reconcile_sync(now)?;
    store.schedule_sync(now)?;
    if cell.is_some() {
        store.schedule_continuous(now)?;
        store.tick_triggered(now)?;
    }
    if cell.is_none()
        || !store.active_exports()?.is_empty()
        || !store.pending_refreshes()?.is_empty()
    {
        return Ok(());
    }
    if let Some(run) = store.next_sync_run()? {
        // Durable starting intent precedes export admission. Its unique key repairs
        // a crash after the export commits but before the link/refresh marker does.
        store.start_sync_run(run.id)?;
        let binding = Binding {
            project_id: run.project_id,
            worktree: store.root().to_owned(),
        };
        let result = crate::api::handle(
            store,
            cell,
            &binding,
            crate::api::Action::Export {
                branch: run.branch_id.to_string(),
                key: format!("internal:sync:{}", run.id),
                limits: run.config.limits,
            },
        );
        match result {
            Ok(value) => {
                let id = serde_json::from_value(value["id"].clone())?;
                store.link_sync_refresh(run.id, id)?;
            }
            Err(_) => store.fail_sync_run(run.id, "snapshot_admission_failed", now)?,
        }
    }
    Ok(())
}
