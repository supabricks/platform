use super::*;
use crate::catalog::{
    adapter::Adapter,
    metadata::{Code, Fault},
};
use std::thread::JoinHandle;
type Outcome = std::result::Result<(), Fault>;
#[derive(Clone, Copy)]
enum Phase {
    Register,
    Verify,
    Delete,
}
struct Active {
    id: OperationId,
    owner: DeploymentId,
    index: usize,
    phase: Phase,
    worker: JoinHandle<Outcome>,
}
#[derive(Default)]
pub struct Service {
    active: Option<Active>,
    pub last_error: Option<String>,
}
impl Service {
    pub fn recover(store: &mut Store) -> Result<Self> {
        for mut p in store.pending_catalog_publications()? {
            // Reverify the complete set after a crash; never turn intent into ownership by alias.
            if p.state == "registering" {
                for t in &mut p.tables {
                    if t.state == "verified" {
                        t.state = "registered".into();
                    }
                }
                store.save_catalog_publication(&p)?;
            }
        }
        Ok(Self::default())
    }
    pub fn tick(&mut self, store: &mut Store, manager: &Manager, stopping: bool) -> Result<bool> {
        if self.active.as_ref().is_some_and(|a| a.worker.is_finished()) {
            let a = self.active.take().unwrap();
            let result = a.worker.join().unwrap_or_else(|_| {
                Err(Fault::new(
                    Code::Unavailable,
                    "publication worker terminated",
                ))
            });
            let mut p = store.catalog_publication(a.owner, a.id)?;
            match result {
                Ok(()) => {
                    p.tables[a.index].state = match a.phase {
                        Phase::Register => "registered",
                        Phase::Verify => "verified",
                        Phase::Delete => "deleted",
                    }
                    .into()
                }
                Err(e) => {
                    p.error = Some(format!(
                        "{}; inspect provider/identity then explicitly resume",
                        e.message
                    ))
                }
            }
            store.save_catalog_publication(&p)?;
        }
        if stopping {
            return Ok(self.active.is_none());
        }
        if self.active.is_some() {
            return Ok(false);
        }
        let adapter = match manager.adapter(store) {
            Ok(a) if manager.status()["mode"] == "local" => a,
            _ => return Ok(false),
        };
        for mut p in store.pending_catalog_publications()? {
            if p.error.is_some() {
                continue;
            }
            if adapter.provider_id != p.namespace.provider_id
                || adapter.probe.expected_metastore.as_ref() != Some(&p.namespace.metastore_id)
            {
                p.error = Some(
                    "catalog provider identity changed; explicit reconciliation required".into(),
                );
                store.save_catalog_publication(&p)?;
                continue;
            }
            if p.state == "registering" && verify_locations(store, &p).is_err() {
                p.error=Some("snapshot unavailable or publication paths relocated; explicit reconciliation required".into());
                store.save_catalog_publication(&p)?;
                continue;
            }
            let step = if p.state == "retiring" {
                if store.catalog_references(&p)? {
                    continue;
                }
                for t in &mut p.tables {
                    if t.state == "planned" {
                        t.state = "deleted".into();
                    }
                }
                if let Some(i) = p.tables.iter().position(|t| t.state != "deleted") {
                    Some((i, Phase::Delete))
                } else {
                    store.finish_catalog_retirement(&mut p)?;
                    None
                }
            } else {
                if let Some(i) = p
                    .tables
                    .iter()
                    .position(|t| matches!(t.state.as_str(), "planned" | "creating"))
                {
                    p.tables[i].state = "creating".into();
                    Some((i, Phase::Register))
                } else if let Some(i) = p.tables.iter().position(|t| t.state == "registered") {
                    Some((i, Phase::Verify))
                } else {
                    if store.commit_catalog_publication(&mut p).is_err() {
                        p.state = "registering".into();
                        p.revision = None;
                        p.error=Some("source/binding revision changed before commit; unpublish this candidate".into());
                        store.save_catalog_publication(&p)?;
                    }
                    None
                }
            };
            if let Some((i, phase)) = step {
                // Intent is durable before spawning any remote side effect.
                store.save_catalog_publication(&p)?;
                let a = adapter.clone();
                let n = p.namespace.clone();
                let t = p.tables[i].clone();
                self.active = Some(Active {
                    id: p.id,
                    owner: p.deployment_id,
                    index: i,
                    phase,
                    worker: std::thread::spawn(move || perform(&a, n, &t, phase)),
                });
                break;
            }
        }
        Ok(false)
    }
}
pub(crate) fn identity(value: &Value, t: &Table) -> Outcome {
    if value["table_id"] != t.id.to_string() {
        return Err(Fault::new(
            Code::IdentityChanged,
            "UC table UUID changed; ownership was not adopted",
        ));
    }
    for k in [
        "catalog_name",
        "schema_name",
        "name",
        "table_type",
        "data_source_format",
        "storage_location",
    ] {
        if value[k] != t.body[k] {
            return Err(Fault::new(
                Code::IdentityChanged,
                "UC table metadata or location changed",
            ));
        }
    }
    let actual = value["columns"]
        .as_array()
        .ok_or_else(|| Fault::new(Code::InvalidResponse, "UC table columns missing"))?;
    let wanted = t.body["columns"].as_array().unwrap();
    if actual.len() != wanted.len() {
        return Err(Fault::new(Code::SchemaDrift, "UC table schema changed"));
    }
    for (a, b) in actual.iter().zip(wanted) {
        for k in ["name", "type_name", "type_text", "nullable", "position"] {
            if a[k] != b[k] {
                return Err(Fault::new(Code::SchemaDrift, "UC table schema changed"));
            }
        }
        if a["type_json"]
            .as_str()
            .and_then(|v| serde_json::from_str::<Value>(v).ok())
            != b["type_json"]
                .as_str()
                .and_then(|v| serde_json::from_str::<Value>(v).ok())
        {
            return Err(Fault::new(Code::SchemaDrift, "UC column type changed"));
        }
    }
    Ok(())
}
fn perform(adapter: &Adapter, n: Namespace, t: &Table, phase: Phase) -> Outcome {
    adapter.namespace(n)?;
    let path = format!(
        "tables/{}.{}.{}",
        t.body["catalog_name"].as_str().unwrap(),
        t.body["schema_name"].as_str().unwrap(),
        t.body["name"].as_str().unwrap()
    );
    let current = adapter.owned_request("GET", &path, None, None);
    match phase {
        Phase::Register => match current {
            Ok(v) => identity(&v, t),
            Err(e) if matches!(e.code, Code::NotFound) => {
                let result = adapter.owned_request(
                    "POST",
                    "tables",
                    Some(t.body.clone()),
                    Some(&t.id.to_string()),
                )?;
                identity(&result, t)
            }
            Err(e) => Err(e),
        },
        Phase::Verify => identity(&current?, t),
        Phase::Delete => match current {
            Err(e) if matches!(e.code, Code::NotFound) => Ok(()),
            Err(e) => Err(e),
            Ok(v) => {
                identity(&v, t)?;
                match adapter.owned_request("DELETE", &path, None, Some(&t.id.to_string())) {
                    Ok(_) => Ok(()),
                    Err(e) if matches!(e.code, Code::NotFound) => Ok(()),
                    Err(e) => Err(e),
                }
            }
        },
    }
}

#[cfg(test)]
mod protocol_tests {
    use super::*;
    #[test]
    fn lost_create_reply_reconciles_uuid_without_duplicate_and_foreign_cleanup_is_refused() {
        let dir = tempfile::tempdir().unwrap();
        let token = dir.path().join("token");
        crate::supervisor::write_private(&token, b"private-publication-test-token").unwrap();
        let n = Namespace {
            deployment_id: DeploymentId::new(),
            project_id: ProjectId::new(),
            provider_id: ProjectId::new().to_string(),
            metastore_id: ProjectId::new().to_string(),
            catalog: "sb_test".into(),
            schema: "analytics".into(),
            catalog_id: Some(ProjectId::new().to_string()),
            schema_id: Some(ProjectId::new().to_string()),
            state: "ready".into(),
        };
        let t = Table {
            id: OperationId::new(),
            source_schema: "public".into(),
            source_name: "items".into(),
            body: json!({"catalog_name":n.catalog,"schema_name":n.schema,"name":"e_test","table_type":"EXTERNAL","data_source_format":"DELTA","storage_location":"file:///private/owned/101","columns":[]}),
            state: "creating".into(),
        };
        let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
        let adapter = Adapter {
            provider_id: n.provider_id.clone(),
            probe: crate::catalog::http::Probe {
                endpoint: format!("http://{}", server.server_addr()),
                token_file: token,
                ca_file: None,
                expected_metastore: Some(n.metastore_id.clone()),
            },
        };
        let ns = n.clone();
        let table = t.clone();
        let foreign = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let flag = foreign.clone();
        let server = std::thread::spawn(move || {
            let mut created = false;
            let mut creates = 0;
            for _ in 0..19 {
                let request = server
                    .recv_timeout(std::time::Duration::from_secs(4))
                    .unwrap()
                    .expect("missing protocol request");
                let path = request.url().to_owned();
                let authenticated = request
                    .headers()
                    .iter()
                    .any(|h| h.field.equiv("Authorization"));
                let (code, body) = if !authenticated {
                    (401, json!({}).to_string())
                } else if path.contains("metastore_summary") {
                    (200, json!({"metastore_id":ns.metastore_id}).to_string())
                } else if path.contains("catalogs?") {
                    (200, json!({"catalogs":[]}).to_string())
                } else if path.contains("catalogs/") {
                    (
                        200,
                        json!({"id":ns.catalog_id,"name":ns.catalog}).to_string(),
                    )
                } else if path.contains("schemas/") {
                    (200,json!({"schema_id":ns.schema_id,"name":ns.schema,"catalog_name":ns.catalog}).to_string())
                } else if request.method() == &tiny_http::Method::Post {
                    creates += 1;
                    created = true;
                    assert!(
                        request
                            .headers()
                            .iter()
                            .any(|h| h.field.equiv("X-Supabricks-Table-Id")
                                && h.value.as_str() == table.id.to_string())
                    );
                    (200, "lost/incomplete create receipt".into())
                } else {
                    assert_eq!(
                        request.method(),
                        &tiny_http::Method::Get,
                        "foreign object must never reach DELETE"
                    );
                    if !created {
                        (404, "{}".into())
                    } else {
                        let mut v = table.body.clone();
                        v["table_id"] = json!(if flag.load(std::sync::atomic::Ordering::SeqCst) {
                            OperationId::new()
                        } else {
                            table.id
                        });
                        (200, v.to_string())
                    }
                };
                request
                    .respond(tiny_http::Response::from_string(body).with_status_code(code))
                    .unwrap();
            }
            assert_eq!(creates, 1);
        });
        assert!(perform(&adapter, n.clone(), &t, Phase::Register).is_err());
        perform(&adapter, n.clone(), &t, Phase::Register).unwrap();
        foreign.store(true, std::sync::atomic::Ordering::SeqCst);
        assert!(matches!(
            perform(&adapter, n, &t, Phase::Delete).unwrap_err().code,
            Code::IdentityChanged
        ));
        server.join().unwrap();
    }
}
