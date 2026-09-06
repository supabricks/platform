use super::*;
use crate::operations::{BranchPoint, WorkTicket};
use supabricks_core::lsn::Lsn;
fn position(body: &Value, name: &str) -> Result<Lsn> {
    body[name]
        .as_str()
        .and_then(|s| s.parse().ok())
        .ok_or_else(|| conflict("missing or invalid pageserver LSN"))
}
impl Cell {
    pub(super) fn capture_point(
        &mut self,
        store: &mut Store,
        ticket: &WorkTicket,
        child: &BranchRecord,
    ) -> Result<bool> {
        let (point, deadline) = store.pin_request(child.branch.id)?;
        if chrono::Utc::now().timestamp_millis() > deadline {
            return Err(invalid(
                "branch capture/ingestion deadline exceeded; no earlier boundary was substituted",
            ));
        }
        let parent = store.ensure_parent_running(child.branch.id)?;
        if !self.ensure_timeline(store, &parent)? || !self.ensure_compute(store, &parent)? {
            return Ok(false);
        }
        let ps = self.pageserver()?;
        let (code, detail) = ps.detail(&parent)?;
        if code != 200 {
            return Ok(false);
        }
        let ingested = position(&detail, "last_record_lsn")?;
        let boundary = if let Some(lsn) = child.branch.ancestor_lsn {
            lsn
        } else {
            let flush = self.sql.flush(
                parent
                    .ports
                    .ok_or_else(|| conflict("missing parent ports"))?
                    .sql,
                &store.endpoint_password(parent.endpoint.id)?,
            )?;
            let lsn = match point {
                BranchPoint::Head => flush,
                BranchPoint::Lsn { lsn } => {
                    if lsn > flush {
                        return Err(invalid(
                            "requested LSN is ahead of the parent's flush boundary",
                        ));
                    }
                    lsn
                }
                BranchPoint::Time { timestamp } => {
                    if ingested < flush {
                        return Ok(false);
                    }
                    let timestamp = chrono::DateTime::parse_from_rfc3339(&timestamp)
                        .map_err(|_| invalid("invalid branch timestamp"))?
                        .with_timezone(&chrono::Utc)
                        .to_rfc3339_opts(chrono::SecondsFormat::AutoSi, true);
                    let timestamp = percent_encoding::utf8_percent_encode(
                        &timestamp,
                        percent_encoding::NON_ALPHANUMERIC,
                    )
                    .to_string();
                    let path = format!(
                        "tenant/{}/timeline/{}/get_lsn_by_timestamp?timestamp={timestamp}&with_lease=true",
                        parent.branch.tenant_id, parent.branch.timeline_id
                    );
                    let (code, response) = ps.request("GET", &path, None)?;
                    if code == 400 {
                        return Err(invalid("requested timestamp is unavailable"));
                    }
                    if code != 200 {
                        return Ok(false);
                    }
                    if response["kind"] != "present" {
                        return Err(invalid(
                            "requested time is outside the available parent history",
                        ));
                    }
                    position(&response, "lsn")?
                }
            };
            store.pin_lsn(ticket, lsn)?;
            lsn
        };
        if boundary < position(&detail, "min_readable_lsn")?
            || boundary < position(&detail, "initdb_lsn")?
            || parent.branch.ancestor_lsn.is_some_and(|lsn| boundary < lsn)
        {
            return Err(invalid(
                "requested branch point is outside the parent's retained history",
            ));
        }
        if ingested < boundary {
            return Ok(false);
        }
        ps.lease(&parent, boundary)
    }
    pub(super) fn delete_local_files(&mut self, b: &BranchRecord) -> Result<bool> {
        if self.launches.contains_key(&Self::compute_role(b)) {
            return Err(conflict(
                "compute launch remains authorized during teardown",
            ));
        }
        for dir in [
            self.root.join("computes").join(b.endpoint.id.to_string()),
            self.root.join("tmp").join(b.endpoint.id.to_string()),
        ] {
            match fs::remove_dir_all(dir) {
                Ok(()) => {}
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => return Err(e.into()),
            }
        }
        let launch = self
            .root
            .join(format!("launches/{}.json", Self::compute_role(b)));
        match fs::remove_file(launch) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
        for dir in ["computes", "tmp", "launches"] {
            fs::File::open(self.root.join(dir))?.sync_all()?;
        }
        self.attached.remove(&b.branch.id);
        Ok(true)
    }
}
