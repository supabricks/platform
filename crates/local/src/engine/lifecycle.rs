use super::*;
use crate::operations::WorkTicket;
use supabricks_core::lsn::Lsn;
impl Cell {
    pub(super) fn capture_suspend(
        &mut self,
        store: &mut Store,
        ticket: &WorkTicket,
        b: &BranchRecord,
    ) -> Result<bool> {
        if !b.timeline_created {
            return Ok(true);
        }
        let boundary = if let Some(lsn) = store.suspend_lsn(b.branch.id, b.revision)? {
            lsn
        } else {
            let record = store
                .native_processes()?
                .into_iter()
                .find(|p| p.role == Self::compute_role(b));
            let alive = record
                .as_ref()
                .map(|p| {
                    supervisor::os::identity(p.pid)
                        .map(|id| id.is_some_and(|id| !id.zombie && id.start == p.start_identity))
                })
                .transpose()?
                .unwrap_or(false);
            let (code, body, field) = if alive {
                let token = self
                    .key
                    .mint_admin_jwt(60)
                    .map_err(|_| conflict("compute token failed"))?;
                let (code, body) = http::Http::default().json(
                    b.ports
                        .ok_or_else(|| conflict("missing compute ports"))?
                        .external_http,
                    "POST",
                    "/terminate",
                    &[("Authorization", &format!("Bearer {token}"))],
                    None,
                )?;
                (code, body, "lsn")
            } else {
                // A dead compute_ctl can leave a live Postgres descendant.
                // Fence the entire group before reading its final WAL boundary.
                if !self.stop_compute(store, b)? {
                    return Ok(false);
                }
                // The owner may have died after termination but before its receipt
                // was journaled. Recover the durable boundary from the safekeeper,
                // after the old compute group has been fenced by Cell::open.
                let token = self
                    .key
                    .mint_storage_jwt(StorageScope::Safekeeper)
                    .map_err(|_| conflict("storage token failed"))?;
                let (code, body) = http::Http::default().json(
                    self.port("sk_http"),
                    "GET",
                    &format!(
                        "/v1/tenant/{}/timeline/{}",
                        b.branch.tenant_id, b.branch.timeline_id
                    ),
                    &[("Authorization", &format!("Bearer {token}"))],
                    None,
                )?;
                (code, body, "commit_lsn")
            };
            if code != 200 && code != 201 {
                return Ok(false);
            }
            let lsn = body[field]
                .as_str()
                .and_then(|s| s.parse::<Lsn>().ok())
                .ok_or_else(|| conflict("missing durable suspension boundary"))?;
            store.capture_suspend_lsn(ticket, lsn)?;
            lsn
        };
        if !self.ensure_timeline(store, b)? {
            return Ok(false);
        }
        let (code, detail) = self.pageserver()?.detail(b)?;
        Ok(code == 200
            && detail["last_record_lsn"]
                .as_str()
                .and_then(|s| s.parse::<Lsn>().ok())
                .is_some_and(|lsn| lsn >= boundary))
    }
}
