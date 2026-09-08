//! Durable coordinator contracts; all methods require the daemon's Store lock.
use super::{
    Result, Store,
    error::{conflict, invalid, missing},
    now_ms,
};
use crate::ingest::*;
use rusqlite::{OptionalExtension, params};
use serde_json::to_string;
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Read,
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt},
    path::PathBuf,
};
use supabricks_core::resource::ProjectId;

fn directory(path: &std::path::Path) -> Result<()> {
    if !path.try_exists()? {
        fs::DirBuilder::new().mode(0o700).create(path)?;
    }
    let m = fs::symlink_metadata(path)?;
    if !m.is_dir() || m.uid() != unsafe { libc::geteuid() } || m.mode() & 0o077 != 0 {
        return Err(invalid("ingestion directory must be private and owned"));
    }
    Ok(())
}
impl Store {
    /// Installation/data-lineage identity survives releases, branches and restore.
    pub fn ingest_origin(&self) -> Result<String> {
        Ok(self
            .db
            .query_row("SELECT origin FROM ingest_identity WHERE id=1", [], |r| {
                r.get(0)
            })?)
    }
    /// Allocate an unusable receiving slot. Only server IDs determine paths.
    pub fn acquire_source(&mut self, project: ProjectId, name: &str) -> Result<Source> {
        self.project(project)?;
        if name.is_empty() || name.len() > 255 || name.chars().any(char::is_control) {
            return Err(invalid("invalid source display name"));
        }
        let reserved: i64=self.db.query_row("SELECT coalesce(sum(CASE WHEN state IN ('receiving','interrupted') THEN 104857600 ELSE bytes END),0) FROM ingest_sources WHERE state IN ('receiving','staged','interrupted')",[],|r|r.get(0))?;
        if reserved as u64 + SOURCE_BYTES > STAGING_BYTES {
            return Err(conflict(
                "staging budget exhausted; dispose unreferenced sources",
            ));
        }
        let id = SourceId::new();
        self.db.execute("INSERT INTO ingest_sources(id,project_id,generation,display_name,state,expires_at_ms) VALUES (?1,?2,?3,?4,'receiving',?5)",params![id.to_string(),project.to_string(),self.generation(),name,now_ms()?+RETENTION_MS])?;
        self.ingest_source(project, id)
    }
    pub fn ingest_source(&self, project: ProjectId, id: SourceId) -> Result<Source> {
        self.db.query_row("SELECT generation,display_name,state,bytes,sha256,expires_at_ms FROM ingest_sources WHERE id=?1 AND project_id=?2",params![id.to_string(),project.to_string()],|r|Ok(Source{id,project_id:project,generation:r.get(0)?,display_name:r.get(1)?,state:r.get(2)?,bytes:r.get::<_,i64>(3)? as u64,sha256:r.get(4)?,expires_at_ms:r.get(5)?})).optional()?.ok_or_else(||missing("source in project"))
    }
    fn source_path(&self, id: SourceId, suffix: &str) -> Result<PathBuf> {
        let parent = self.root().join("ingest");
        directory(&parent)?;
        let sources = parent.join("sources");
        directory(&sources)?;
        Ok(sources.join(format!("{id}.{suffix}")))
    }
    /// Pass this newly created descriptor to the bounded acquisition worker.
    /// It may write at most SOURCE_BYTES; seal_source checks again before use.
    pub fn source_writer(&self, project: ProjectId, id: SourceId) -> Result<fs::File> {
        let s = self.ingest_source(project, id)?;
        if s.state != "receiving"
            || s.generation != self.generation()
            || s.expires_at_ms <= now_ms()?
        {
            return Err(conflict("source acquisition is stale"));
        }
        Ok(fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(self.source_path(id, "part")?)?)
    }
    /// Called only after the acquisition process is fenced and its descriptor closed.
    /// Publishes immutable bytes before making their reference usable in SQLite.
    pub fn seal_source(&mut self, project: ProjectId, id: SourceId) -> Result<Source> {
        let s = self.ingest_source(project, id)?;
        if s.state != "receiving"
            || s.generation != self.generation()
            || s.expires_at_ms <= now_ms()?
        {
            return Err(conflict("source acquisition is stale"));
        }
        let path = self.source_path(id, "part")?;
        let mut f = fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&path)?;
        let private = f.metadata()?;
        if !private.is_file()
            || private.nlink() != 1
            || private.uid() != unsafe { libc::geteuid() }
            || private.mode() & 0o077 != 0
        {
            return Err(invalid("invalid private source payload"));
        }
        let meta = f.metadata()?;
        if meta.len() > SOURCE_BYTES {
            return Err(invalid("source exceeds 100 MiB"));
        }
        let mut hash = Sha256::new();
        let mut bytes = 0;
        let mut buf = [0; 65536];
        loop {
            let n = f.read(&mut buf)?;
            if n == 0 {
                break;
            }
            bytes += n as u64;
            if bytes > SOURCE_BYTES {
                return Err(invalid("source exceeds 100 MiB"));
            }
            hash.update(&buf[..n]);
        }
        let after = f.metadata()?;
        if bytes != meta.len()
            || after.len() != meta.len()
            || after.mtime() != meta.mtime()
            || after.mtime_nsec() != meta.mtime_nsec()
        {
            return Err(conflict("source changed during sealing"));
        }
        f.sync_all()?;
        drop(f);
        let target = self.source_path(id, "source")?;
        // Never replace an existing immutable payload (including a crash orphan).
        fs::hard_link(&path, &target)?;
        fs::remove_file(&path)?;
        fs::File::open(target.parent().unwrap())?.sync_all()?;
        self.db.execute(
            "UPDATE ingest_sources SET state='staged',bytes=?2,sha256=?3 WHERE id=?1",
            params![id.to_string(), bytes as i64, hex::encode(hash.finalize())],
        )?;
        self.ingest_source(project, id)
    }
    pub fn create_ingest(&mut self, key: &str, load: Load) -> Result<Job> {
        let fingerprint = load.validate()?;
        if key.is_empty() || key.len() > 128 || key.chars().any(char::is_control) {
            return Err(invalid("invalid ingestion idempotency key"));
        }
        let request = to_string(&load)?;
        if let Some((id,prior))=self.db.query_row("SELECT id,request FROM ingest_jobs WHERE project_id=?1 AND branch_id=?2 AND request_key=?3",params![load.project_id.to_string(),load.branch_id.to_string(),key],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?))).optional()? {
            if prior!=request {return Err(conflict("idempotency key already binds different ingestion input"));}
            return self.ingest_job(load.project_id,JobId(super::parse(&id)?));
        }
        self.accepting_work(load.branch_id)?;
        let branch = self.branch(load.branch_id)?;
        if branch.branch.project_id != load.project_id || branch.revision != load.branch_revision {
            return Err(conflict("stale branch or wrong project"));
        }
        let source = self.ingest_source(load.project_id, load.source_id)?;
        if source.state != "staged"
            || source.sha256.as_ref() != Some(&load.source_sha256)
            || source.expires_at_ms <= now_ms()?
        {
            return Err(conflict("source is unavailable, expired or changed"));
        }
        if self
            .db
            .prepare("SELECT 1 FROM ingest_jobs WHERE state IN ('queued','loading','reconciling')")?
            .exists([])?
        {
            return Err(conflict("one active import per cell"));
        }
        let id = JobId::new();
        self.db.execute("INSERT INTO ingest_jobs(id,project_id,branch_id,branch_revision,source_id,request_key,request,fingerprint,state,generation,updated_at_ms) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,'queued',?9,?10)",params![id.to_string(),load.project_id.to_string(),load.branch_id.to_string(),load.branch_revision,load.source_id.to_string(),key,request,fingerprint,self.generation(),now_ms()?])?;
        self.ingest_job(load.project_id, id)
    }
    pub fn ingest_job(&self, project: ProjectId, id: JobId) -> Result<Job> {
        let row=self.db.query_row("SELECT request,state,attempt,generation,worker,cancel_requested,parsed_rows,copied_rows,committed_rows,retryable,source_released FROM ingest_jobs WHERE id=?1 AND project_id=?2",params![id.to_string(),project.to_string()],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get(2)?,r.get(3)?,r.get::<_,Option<String>>(4)?,r.get(5)?,r.get::<_,i64>(6)? as u64,r.get::<_,i64>(7)? as u64,r.get::<_,Option<i64>>(8)?.map(|n|n as u64),r.get(9)?,r.get(10)?))).optional()?.ok_or_else(||missing("ingestion job in project"))?;
        Ok(Job {
            id,
            load: serde_json::from_str(&row.0)?,
            state: serde_json::from_value(serde_json::json!(row.1))?,
            attempt: row.2,
            generation: row.3,
            worker: row.4.map(|s| serde_json::from_str(&s)).transpose()?,
            cancel_requested: row.5,
            parsed_rows: row.6,
            copied_rows: row.7,
            committed_rows: row.8,
            retryable: row.9,
            source_released: row.10,
        })
    }
    /// Register a gated child before opening its execution gate. A crash between
    /// native registration and job update is safe: recovery fences every ingest role.
    pub fn start_ingest(
        &mut self,
        project: ProjectId,
        id: JobId,
        worker: &crate::supervisor::OwnedProcess,
    ) -> Result<Job> {
        let j = self.ingest_job(project, id)?;
        if j.state != State::Queued
            || worker.role != format!("ingest-{id}")
            || worker.branch != Some((j.load.branch_id, j.load.branch_revision))
        {
            return Err(conflict("invalid ingestion worker ticket"));
        }
        self.record_native_process(worker)?;
        self.db.execute("UPDATE ingest_jobs SET state='loading',attempt=attempt+1,generation=?2,worker=?3,updated_at_ms=?4 WHERE id=?1",params![id.to_string(),self.generation(),to_string(worker)?,now_ms()?])?;
        self.ingest_job(project, id)
    }
    pub fn ingest_progress(
        &mut self,
        project: ProjectId,
        id: JobId,
        worker: &crate::supervisor::OwnedProcess,
        parsed: u64,
        copied: u64,
    ) -> Result<()> {
        let j = self.ingest_job(project, id)?;
        if j.state != State::Loading
            || j.worker.as_ref() != Some(worker)
            || worker.generation != self.generation()
            || parsed < j.parsed_rows
            || copied < j.copied_rows
            || copied > parsed
            || parsed > i64::MAX as u64
        {
            return Err(conflict("stale or non-monotonic ingestion progress"));
        }
        self.db.execute(
            "UPDATE ingest_jobs SET parsed_rows=?2,copied_rows=?3,updated_at_ms=?4 WHERE id=?1",
            params![id.to_string(), parsed as i64, copied as i64, now_ms()?],
        )?;
        Ok(())
    }
    pub fn cancel_ingest(&mut self, project: ProjectId, id: JobId) -> Result<Job> {
        let j = self.ingest_job(project, id)?;
        if matches!(j.state, State::Queued | State::Loading | State::Reconciling) {
            self.db.execute("UPDATE ingest_jobs SET cancel_requested=1,state=CASE WHEN state='queued' THEN 'cancelled' ELSE 'reconciling' END,source_released=CASE WHEN state='queued' THEN 1 ELSE 0 END,updated_at_ms=?2 WHERE id=?1",params![id.to_string(),now_ms()?])?;
        }
        self.ingest_job(project, id)
    }
    /// Called after supervisor::stop and forget_native_process, never on EOF alone.
    pub fn fence_ingest(&mut self, project: ProjectId, id: JobId) -> Result<()> {
        let j = self.ingest_job(project, id)?;
        if self
            .native_processes()?
            .iter()
            .any(|p| p.role == format!("ingest-{id}"))
        {
            return Err(conflict(
                "ingestion process must be stopped and forgotten first",
            ));
        }
        if matches!(j.state, State::Loading | State::Reconciling) {
            self.db.execute("UPDATE ingest_jobs SET state='reconciling',worker=NULL,updated_at_ms=?2 WHERE id=?1",params![id.to_string(),now_ms()?])?;
        }
        Ok(())
    }
    pub fn reconcile_ingest(
        &mut self,
        project: ProjectId,
        id: JobId,
        outcome: Reconciliation,
    ) -> Result<Job> {
        let j = self.ingest_job(project, id)?;
        if j.state != State::Reconciling
            || j.worker.is_some()
            || self
                .native_processes()?
                .iter()
                .any(|p| p.role == format!("ingest-{id}"))
        {
            return Err(conflict("reconciliation requires a fenced import"));
        }
        match outcome {
            Reconciliation::Unknown => {}
            Reconciliation::Committed {
                receipt,
                target_oid,
            } => {
                if !receipt.matches(&self.ingest_origin()?, id, &j.load)?
                    || receipt.table_oid != target_oid
                {
                    return Err(conflict(
                        "receipt or target identity differs; manual reconciliation required",
                    ));
                }
                self.db.execute("UPDATE ingest_jobs SET state='succeeded',committed_rows=?2,receipt=?3,source_released=1,retryable=0,updated_at_ms=?4 WHERE id=?1",params![id.to_string(),receipt.committed_rows as i64,to_string(&receipt)?,now_ms()?])?;
            }
            Reconciliation::Absent { target_exists } => {
                if target_exists {
                    return Err(conflict(
                        "target exists without matching receipt; manual reconciliation required",
                    ));
                }
                self.db.execute("UPDATE ingest_jobs SET state=?2,retryable=?3,source_released=?4,updated_at_ms=?5 WHERE id=?1",params![id.to_string(),if j.cancel_requested {"cancelled"} else {"failed"},!j.cancel_requested,j.cancel_requested,now_ms()?])?;
            }
        }
        self.ingest_job(project, id)
    }
    pub fn retry_ingest(&mut self, project: ProjectId, id: JobId) -> Result<Job> {
        let j = self.ingest_job(project, id)?;
        if j.state != State::Failed || !j.retryable || j.source_released {
            return Err(conflict("job cannot be retried"));
        }
        let s = self.ingest_source(project, j.load.source_id)?;
        if s.state != "staged" || s.expires_at_ms <= now_ms()? {
            return Err(conflict("retained source expired"));
        }
        self.accepting_work(j.load.branch_id)?;
        if self.branch(j.load.branch_id)?.revision != j.load.branch_revision {
            return Err(conflict("branch revision changed"));
        }
        if self
            .db
            .prepare("SELECT 1 FROM ingest_jobs WHERE state IN ('queued','loading','reconciling')")?
            .exists([])?
        {
            return Err(conflict("another import is active"));
        }
        self.db.execute("UPDATE ingest_jobs SET state='queued',retryable=0,parsed_rows=0,copied_rows=0,generation=?2,updated_at_ms=?3 WHERE id=?1",params![id.to_string(),self.generation(),now_ms()?])?;
        self.ingest_job(project, id)
    }
    pub(crate) fn interrupt_ingest(&mut self) -> Result<()> {
        if self
            .native_processes()?
            .iter()
            .any(|p| p.role.starts_with("ingest-"))
        {
            return Err(conflict("fence ingestion workers before recovery"));
        }
        let tx = self.db.transaction()?;
        tx.execute(
            "UPDATE ingest_sources SET state='interrupted' WHERE state='receiving'",
            [],
        )?;
        tx.execute(
            "UPDATE ingest_jobs SET state='failed',retryable=1 WHERE state='queued'",
            [],
        )?;
        tx.execute("UPDATE ingest_jobs SET state='reconciling',worker=NULL WHERE state='loading' OR (state='reconciling' AND worker IS NOT NULL)",[])?;
        tx.commit()?;
        Ok(())
    }
    /// Bounded daemon maintenance. Failed jobs retain their source until expiry;
    /// successful/cancelled jobs release it only after every reference is gone.
    pub(crate) fn cleanup_ingest(&mut self) -> Result<()> {
        let rows = {
            let mut q = self.db.prepare("SELECT s.id,s.project_id,s.expires_at_ms FROM ingest_sources s WHERE s.payload_deleted=0 AND s.state IN ('staged','interrupted','disposed','expired') AND NOT EXISTS (SELECT 1 FROM ingest_jobs j WHERE j.source_id=s.id AND (j.state IN ('queued','loading','reconciling') OR j.worker IS NOT NULL)) AND (s.state IN ('disposed','expired') OR s.expires_at_ms<=?1 OR (EXISTS(SELECT 1 FROM ingest_jobs j WHERE j.source_id=s.id) AND NOT EXISTS(SELECT 1 FROM ingest_jobs j WHERE j.source_id=s.id AND j.source_released=0))) LIMIT 16")?;
            q.query_map([now_ms()?], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, i64>(2)?,
                ))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?
        };
        for (id, project, expiry) in rows {
            self.dispose_source(
                super::parse(&project)?,
                SourceId(super::parse(&id)?),
                expiry <= now_ms()?,
            )?;
        }
        Ok(())
    }
    /// Active references always win over expiry. Persist disposal first; retry
    /// deletion after a crash. Store ownership excludes backups and other writers.
    pub fn dispose_source(
        &mut self,
        project: ProjectId,
        id: SourceId,
        expired: bool,
    ) -> Result<()> {
        let s = self.ingest_source(project, id)?;
        if s.state == "receiving" {
            return Err(conflict("fence source acquisition before disposal"));
        }
        if expired && s.expires_at_ms > now_ms()? {
            return Err(conflict("source has not expired"));
        }
        if self.db.prepare("SELECT 1 FROM ingest_jobs WHERE source_id=?1 AND (state IN ('queued','loading','reconciling') OR worker IS NOT NULL)")?.exists([id.to_string()])? {return Err(conflict("source is referenced by active ingestion"));}
        let tx = self.db.transaction()?;
        tx.execute(
            "UPDATE ingest_jobs SET source_released=1,retryable=0 WHERE source_id=?1",
            [id.to_string()],
        )?;
        tx.execute(
            "UPDATE ingest_sources SET state=?2 WHERE id=?1",
            params![id.to_string(), if expired { "expired" } else { "disposed" }],
        )?;
        tx.commit()?;
        for suffix in ["part", "source"] {
            let path = self.source_path(id, suffix)?;
            match fs::symlink_metadata(&path) {
                Ok(m) if m.is_file() => fs::remove_file(&path)?,
                Ok(_) => return Err(invalid("unexpected source payload type")),
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => return Err(e.into()),
            }
            fs::File::open(path.parent().unwrap())?.sync_all()?;
        }
        self.db.execute(
            "UPDATE ingest_sources SET payload_deleted=1 WHERE id=?1",
            [id.to_string()],
        )?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::project::ProjectConfig;
    #[test]
    fn expiry_releases_interrupted_reservations_and_resumes_disposal() {
        let temp = tempfile::tempdir().unwrap();
        let mut s = Store::open(&temp.path().join("data")).unwrap();
        let p = ProjectConfig {
            format_version: 1,
            id: ProjectId::new(),
            name: "expiry".into(),
        };
        s.register_project(&p).unwrap();
        let source = s.acquire_source(p.id, "abandoned").unwrap();
        drop(s.source_writer(p.id, source.id).unwrap());
        s.interrupt_ingest().unwrap();
        assert_eq!(
            s.ingest_source(p.id, source.id).unwrap().state,
            "interrupted"
        );
        s.cleanup_ingest().unwrap();
        assert_eq!(
            s.ingest_source(p.id, source.id).unwrap().state,
            "interrupted"
        );
        s.db.execute("UPDATE ingest_sources SET expires_at_ms=0", [])
            .unwrap();
        s.cleanup_ingest().unwrap();
        assert_eq!(s.ingest_source(p.id, source.id).unwrap().state, "expired");
        assert!(!s.source_path(source.id, "part").unwrap().exists());
        // Simulate a crash after disposal state committed but before file unlink.
        s.db.execute(
            "UPDATE ingest_sources SET state='disposed',payload_deleted=0",
            [],
        )
        .unwrap();
        fs::write(s.source_path(source.id, "source").unwrap(), b"orphan").unwrap();
        s.cleanup_ingest().unwrap();
        assert!(!s.source_path(source.id, "source").unwrap().exists());
        s.cleanup_ingest().unwrap();
    }
}
