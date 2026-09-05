use super::error::{conflict, invalid, missing};
use super::{Result, Store, constraint, now_ms, parse};
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use std::time::Duration;
use supabricks_core::{lsn::Lsn, resource::*};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProcessRecord {
    pub endpoint_id: EndpointId,
    pub role: String,
    pub generation: i64,
    pub revision: i64,
    pub pid: u32,
    pub process_group: u32,
    pub start_identity: String,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct TableMapping {
    pub source_oid: u32,
    pub table_name: String,
    pub object_path: String,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Epoch {
    pub id: EpochId,
    pub branch_id: BranchId,
    pub source_lsn: Lsn,
    pub tables: Vec<TableMapping>,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Lease {
    pub id: LeaseId,
    pub branch_id: BranchId,
    pub epoch_id: Option<EpochId>,
    pub holder: String,
    pub generation: i64,
    pub expires_at_ms: i64,
}

impl Store {
    /// Records are evidence to inspect on restart, not permission to kill a PID.
    /// P03 supplies and verifies OS start identity and process-group ownership.
    pub fn record_process(&mut self, record: &ProcessRecord) -> Result<()> {
        if record.generation != self.generation
            || record.pid == 0
            || record.process_group == 0
            || record.start_identity.is_empty()
            || record.role.is_empty()
        {
            return Err(invalid("invalid process ownership record"));
        }
        let (revision,desired): (i64,String) = self.db.query_row("SELECT b.revision,b.desired FROM branches b JOIN endpoints e ON e.branch_id=b.id WHERE e.id=?1", [record.endpoint_id.to_string()], |r| Ok((r.get(0)?,r.get(1)?)))?;
        if revision != record.revision || desired != "running" {
            return Err(conflict("process belongs to a stale resource revision"));
        }
        // Never replace surviving ownership evidence implicitly after restart.
        if let Some(existing) = self
            .processes()?
            .into_iter()
            .find(|p| p.endpoint_id == record.endpoint_id && p.role == record.role)
        {
            return if existing == *record {
                Ok(())
            } else {
                Err(conflict(
                    "reconcile the existing process before replacing its record",
                ))
            };
        }
        self.db.execute(
            "INSERT INTO processes VALUES (?1,?2,?3,?4,?5,?6,?7)",
            params![
                record.endpoint_id.to_string(),
                record.role,
                record.generation,
                record.revision,
                record.pid,
                record.process_group,
                record.start_identity
            ],
        )?;
        Ok(())
    }
    pub fn processes(&self) -> Result<Vec<ProcessRecord>> {
        let mut query = self.db.prepare("SELECT endpoint_id,role,generation,revision,pid,process_group,start_identity FROM processes ORDER BY endpoint_id,role")?;
        let rows = query
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                    r.get(5)?,
                    r.get(6)?,
                ))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        rows.into_iter()
            .map(
                |(id, role, generation, revision, pid, process_group, start_identity)| {
                    Ok(ProcessRecord {
                        endpoint_id: parse(&id)?,
                        role,
                        generation,
                        revision,
                        pid,
                        process_group,
                        start_identity,
                    })
                },
            )
            .collect()
    }
    /// Caller must first verify that this exact owned process is stopped.
    pub fn forget_process(&mut self, record: &ProcessRecord) -> Result<()> {
        let changed = self.db.execute("DELETE FROM processes WHERE endpoint_id=?1 AND role=?2 AND generation=?3 AND revision=?4 AND pid=?5 AND process_group=?6 AND start_identity=?7", params![record.endpoint_id.to_string(),record.role,record.generation,record.revision,record.pid,record.process_group,record.start_identity])?;
        if changed != 1
            && self
                .db
                .prepare("SELECT 1 FROM processes WHERE endpoint_id=?1 AND role=?2")?
                .exists(params![record.endpoint_id.to_string(), record.role])?
        {
            return Err(conflict("process identity changed"));
        }
        Ok(())
    }
    /// Epoch metadata is immutable and idempotent; analytics publication and
    /// retention policies are later slices, not implied by inserting this row.
    pub fn put_epoch(&mut self, epoch: &Epoch) -> Result<()> {
        let mut epoch = epoch.clone();
        epoch.tables.sort_by_key(|table| table.source_oid);
        match self.epoch(epoch.id) {
            Ok(existing) if existing == epoch => return Ok(()),
            Ok(_) => return Err(conflict("epoch identity reused with different mappings")),
            Err(super::Error::Operation(supabricks_core::error::OperationError::NotFound(_))) => {}
            Err(error) => return Err(error),
        }
        if self.branch(epoch.branch_id)?.endpoint.desired_state == DesiredState::Deleted {
            return Err(conflict("cannot add an epoch to a deleted branch"));
        }
        let tx = self.db.transaction()?;
        tx.execute(
            "INSERT INTO epochs VALUES (?1,?2,?3)",
            params![
                epoch.id.to_string(),
                epoch.branch_id.to_string(),
                epoch.source_lsn.to_string()
            ],
        )
        .map_err(constraint)?;
        for table in &epoch.tables {
            if table.table_name.is_empty() || table.object_path.is_empty() {
                return Err(invalid("table mappings require a name and object path"));
            }
            tx.execute(
                "INSERT INTO table_mappings VALUES (?1,?2,?3,?4)",
                params![
                    epoch.id.to_string(),
                    table.source_oid,
                    table.table_name,
                    table.object_path
                ],
            )
            .map_err(constraint)?;
        }
        tx.commit()?;
        Ok(())
    }
    pub fn epoch(&self, id: EpochId) -> Result<Epoch> {
        let (branch, lsn): (String, String) = self
            .db
            .query_row(
                "SELECT branch_id,source_lsn FROM epochs WHERE id=?1",
                [id.to_string()],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?
            .ok_or_else(|| missing("epoch"))?;
        let mut query = self.db.prepare("SELECT source_oid,table_name,object_path FROM table_mappings WHERE epoch_id=?1 ORDER BY source_oid")?;
        let tables = query
            .query_map([id.to_string()], |r| {
                Ok(TableMapping {
                    source_oid: r.get(0)?,
                    table_name: r.get(1)?,
                    object_path: r.get(2)?,
                })
            })?
            .collect::<std::result::Result<_, _>>()?;
        Ok(Epoch {
            id,
            branch_id: parse(&branch)?,
            source_lsn: parse(&lsn)?,
            tables,
        })
    }
    pub fn acquire_lease(
        &mut self,
        branch: BranchId,
        epoch: Option<EpochId>,
        holder: &str,
        ttl: Duration,
    ) -> Result<Lease> {
        if self.branch(branch)?.endpoint.desired_state != DesiredState::Running {
            return Err(conflict("branch is not accepting new work"));
        }
        if holder.is_empty() {
            return Err(invalid("lease holder is required"));
        }
        let lease = Lease {
            id: LeaseId::new(),
            branch_id: branch,
            epoch_id: epoch,
            holder: holder.into(),
            generation: self.generation,
            expires_at_ms: expiry(ttl)?,
        };
        self.db
            .execute(
                "INSERT INTO leases VALUES (?1,?2,?3,?4,?5,?6)",
                params![
                    lease.id.to_string(),
                    branch.to_string(),
                    epoch.map(|id| id.to_string()),
                    holder,
                    self.generation,
                    lease.expires_at_ms
                ],
            )
            .map_err(constraint)?;
        Ok(lease)
    }
    pub fn leases(&self, branch: BranchId) -> Result<Vec<Lease>> {
        let mut query = self.db.prepare("SELECT id,epoch_id,holder,generation,expires_at_ms FROM leases WHERE branch_id=?1 AND expires_at_ms>?2 ORDER BY id")?;
        let rows = query
            .query_map(params![branch.to_string(), now_ms()?], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, Option<String>>(1)?,
                    r.get(2)?,
                    r.get(3)?,
                    r.get(4)?,
                ))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        rows.into_iter()
            .map(|(id, epoch, holder, generation, expires_at_ms)| {
                Ok(Lease {
                    id: parse(&id)?,
                    branch_id: branch,
                    epoch_id: epoch.as_deref().map(parse).transpose()?,
                    holder,
                    generation,
                    expires_at_ms,
                })
            })
            .collect()
    }
    pub fn renew_lease(&mut self, lease: &Lease, ttl: Duration) -> Result<Lease> {
        if lease.generation != self.generation {
            return Err(conflict("lease belongs to a previous daemon generation"));
        }
        let expires_at_ms = expiry(ttl)?;
        let changed = self.db.execute("UPDATE leases SET expires_at_ms=?1 WHERE id=?2 AND holder=?3 AND generation=?4 AND expires_at_ms>?5", params![expires_at_ms,lease.id.to_string(),lease.holder,self.generation,now_ms()?])?;
        if changed != 1 {
            return Err(conflict("lease expired or ownership changed"));
        }
        Ok(Lease {
            expires_at_ms,
            ..lease.clone()
        })
    }
    pub fn release_lease(&mut self, lease: &Lease) -> Result<()> {
        let changed = self.db.execute(
            "DELETE FROM leases WHERE id=?1 AND holder=?2 AND generation=?3",
            params![lease.id.to_string(), lease.holder, lease.generation],
        )?;
        if changed != 1
            && self
                .db
                .prepare("SELECT 1 FROM leases WHERE id=?1")?
                .exists([lease.id.to_string()])?
        {
            return Err(conflict("lease ownership changed"));
        }
        Ok(())
    }
}
fn expiry(ttl: Duration) -> Result<i64> {
    if ttl < Duration::from_millis(1) || ttl > Duration::from_secs(86400) {
        return Err(invalid(
            "lease duration must be at least one millisecond and at most one day",
        ));
    }
    now_ms()?
        .checked_add(
            i64::try_from(ttl.as_millis()).map_err(|_| invalid("lease duration overflow"))?,
        )
        .ok_or_else(|| invalid("lease expiry overflow"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        operations::{Mutation, Ports},
        project::ProjectConfig,
    };
    #[test]
    fn expired_leases_cannot_be_renewed_or_block_suspension() {
        let root = tempfile::tempdir().unwrap();
        let mut store = Store::open(&root.path().join("private")).unwrap();
        let project = ProjectConfig {
            format_version: 1,
            id: ProjectId::new(),
            name: "lease".into(),
        };
        store.register_project(&project).unwrap();
        let op = store
            .submit(
                project.id,
                "create",
                Mutation::CreateBranch {
                    name: "main".into(),
                    parent_id: None,
                    ports: Ports {
                        sql: 5400,
                        external_http: 5401,
                        internal_http: 5402,
                    },
                },
            )
            .unwrap();
        assert!(
            store
                .acquire_lease(op.branch_id, None, "tiny", Duration::from_nanos(1))
                .is_err()
        );
        let lease = store
            .acquire_lease(op.branch_id, None, "query", Duration::from_secs(1))
            .unwrap();
        let renewed = store.renew_lease(&lease, Duration::from_secs(60)).unwrap();
        assert!(renewed.expires_at_ms > lease.expires_at_ms);
        store
            .db
            .execute(
                "UPDATE leases SET expires_at_ms=0 WHERE id=?1",
                [lease.id.to_string()],
            )
            .unwrap();
        assert!(store.renew_lease(&lease, Duration::from_secs(1)).is_err());
        assert!(store.leases(op.branch_id).unwrap().is_empty());
        store
            .submit(
                project.id,
                "suspend",
                Mutation::SetState {
                    branch_id: op.branch_id,
                    expected_revision: 1,
                    desired: DesiredState::Suspended,
                },
            )
            .unwrap();
    }
}
