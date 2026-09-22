use super::*;
use crate::{
    authorization::Subject,
    catalog::publication::Publication,
    identity::{self, AdminCommand as IdentityAdmin, Channel},
};
use serde_json::Value;
use std::{
    fs,
    path::PathBuf,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

fn principal(store: &mut Store) -> (Context, String) {
    let id = identity::id();
    store
        .db
        .execute(
            "INSERT INTO identity_principals(id,kind,label) VALUES (?1,'service','equal label')",
            [&id],
        )
        .unwrap();
    let token = store
        .identity_admin(IdentityAdmin::IssueService {
            principal: id.clone(),
            scopes: vec![
                identity::SELF_SCOPE.into(),
                crate::authorization::CONTROL_SCOPE.into(),
            ],
            ttl_seconds: 3600,
        })
        .unwrap()["token"]
        .as_str()
        .unwrap()
        .to_owned();
    let ctx: Context = serde_json::from_value(
        store
            .identity_auth(identity::AuthCommand::Authenticate {
                token: token.clone(),
                channel: Channel::Service,
                csrf: None,
            })
            .unwrap(),
    )
    .unwrap();
    (ctx, identity::hash(&token))
}
fn run(store: &mut Store, broker: &Broker, command: AdminCommand) -> Result<Value> {
    store.catalog_governance_admin(command, broker.fresh())?()?(store)
}
fn apply(store: &mut Store, broker: &Broker, changes: Vec<Change>) -> Result<Value> {
    let plan = run(store, broker, AdminCommand::Plan { changes })?;
    run(
        store,
        broker,
        AdminCommand::Apply {
            plan: plan["id"].as_str().unwrap().into(),
            key: identity::id(),
        },
    )
}
fn read(
    store: &mut Store,
    broker: &Broker,
    who: &(Context, String),
    search: &str,
) -> Result<Value> {
    store.catalog_governance_read(
        who.0.clone(),
        who.1.clone(),
        ReadCommand::List {
            search: search.into(),
        },
        broker.fresh(),
    )?()?(store)
}
fn change(p: &Publication, subject: Subject, present: bool) -> Change {
    Change {
        publication: p.id.to_string(),
        subject,
        publication_revision: p.revision.unwrap(),
        tables: vec![p.tables[0].id.to_string()],
        present,
    }
}
fn seed(store: &mut Store, p: &mut Publication) {
    store.begin_catalog_publication(p).unwrap();
    for t in &mut p.tables {
        t.state = "verified".into();
    }
    store.commit_catalog_publication(p).unwrap();
}
struct ManagedChild(Child);
impl Drop for ManagedChild {
    fn drop(&mut self) {
        unsafe {
            libc::kill(self.0.id() as i32, libc::SIGTERM);
        }
        let deadline = Instant::now() + Duration::from_secs(5);
        while self.0.try_wait().ok().flatten().is_none() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(20));
        }
        if self.0.try_wait().ok().flatten().is_none() {
            let _ = self.0.kill();
        }
        let _ = self.0.wait();
    }
}
struct Uc {
    _child: ManagedChild,
    broker: Broker,
}
fn real_uc(store: &Store, p: &mut Publication) -> Uc {
    let runtime = PathBuf::from(
        std::env::var("SUPABRICKS_UC093_RUNTIME")
            .expect("set SUPABRICKS_UC093_RUNTIME to the qualified pinned UC runtime"),
    );
    let runtime = crate::catalog::runtime::verify(&runtime).unwrap();
    let root = store.root().join("uc-test");
    let conf = root.join("etc/conf");
    fs::create_dir_all(&conf).unwrap();
    fs::create_dir_all(root.join("etc/db")).unwrap();
    fs::copy(
        runtime
            .root
            .join("configuration-template/hibernate.properties"),
        conf.join("hibernate.properties"),
    )
    .unwrap();
    fs::write(conf.join("server.properties"),"server.env=prod\nserver.authorization=enable\nserver.allowed-issuers=internal\nserver.audiences=supabricks-local-owner\nserver.access-token-timeout=PT1M\n").unwrap();
    fs::copy(
        runtime
            .root
            .join("configuration-template/server.log4j2.properties"),
        conf.join("server.log4j2.properties"),
    )
    .unwrap();
    let (front, back) = crate::catalog::runtime::ports(store).unwrap();
    let port = front.local_addr().unwrap().port();
    drop(front);
    drop(back);
    let endpoint = format!("http://127.0.0.1:{port}");
    let log = fs::File::create(root.join("server.log")).unwrap();
    let child = Command::new(runtime.root.join("java/bin/java"))
        .args([
            "-Xms64m",
            "-Xmx256m",
            "-XX:ActiveProcessorCount=2",
            "-Djava.net.preferIPv4Stack=true",
            "-cp",
            &runtime.classpath,
            "io.unitycatalog.server.UnityCatalogServer",
            "--port",
            &port.to_string(),
        ])
        .current_dir(&root)
        .stdout(log.try_clone().unwrap())
        .stderr(log)
        .stdin(Stdio::null())
        .spawn()
        .unwrap();
    let child = ManagedChild(child);
    let started = Instant::now();
    while !conf.join("token.txt").exists() {
        assert!(started.elapsed() < Duration::from_secs(45));
        std::thread::sleep(Duration::from_millis(100));
    }
    let mut uc = Uc {
        _child: child,
        broker: Broker::fixture(
            p.namespace.provider_id.clone(),
            p.namespace.metastore_id.clone(),
            endpoint,
            &conf,
        ),
    };
    let summary = loop {
        if let Ok(v) = uc
            .broker
            .fresh()
            .test_request("GET", false, "metastore_summary", None)
        {
            break v;
        }
        assert!(started.elapsed() < Duration::from_secs(45));
        std::thread::sleep(Duration::from_millis(100));
    };
    p.namespace.metastore_id = summary["metastore_id"].as_str().unwrap().into();
    uc.broker.metastore = p.namespace.metastore_id.clone();
    let catalog = uc
        .broker
        .fresh()
        .test_request(
            "POST",
            false,
            "catalogs",
            Some(json!({"name":p.namespace.catalog})),
        )
        .unwrap();
    p.namespace.catalog_id = Some(catalog["id"].as_str().unwrap().into());
    let schema = uc
        .broker
        .fresh()
        .test_request(
            "POST",
            false,
            "schemas",
            Some(json!({"catalog_name":p.namespace.catalog,"name":p.namespace.schema})),
        )
        .unwrap();
    p.namespace.schema_id = Some(schema["schema_id"].as_str().unwrap().into());
    for t in &mut p.tables {
        let created = uc
            .broker
            .fresh()
            .test_request("POST", false, "tables", Some(t.body.clone()))
            .unwrap();
        t.id = created["table_id"].as_str().unwrap().parse().unwrap();
    }
    uc
}

#[test]
fn catalog_governance_subjects_use_realm_and_uuid_never_labels() {
    let a = identity::id();
    let b = identity::id();
    let r = identity::id();
    assert_ne!(gov::subject(&r, &a).unwrap(), gov::subject(&r, &b).unwrap());
    assert_ne!(gov::subject(&r, &a).unwrap(), gov::subject(&b, &a).unwrap());
    assert!(gov::subject(&r, "admin").is_err());
}

#[test]
#[ignore = "requires the source-built pinned UC Java runtime; catalog-probe runs this"]
fn catalog_governance_real_uc_enforces_principals_grants_drift_and_incarnation_fences() {
    let (_root, mut store, _owner, mut publication) = crate::catalog::publication::tests::setup();
    let uc = real_uc(&store, &mut publication);
    let broker = &uc.broker;
    seed(&mut store, &mut publication);
    let alice = principal(&mut store);
    let bob = principal(&mut store);
    assert!(read(&mut store, broker, &alice, "").is_err());
    for who in [&alice, &bob] {
        run(
            &mut store,
            broker,
            AdminCommand::MapPrincipal {
                principal: who.0.actor_id.clone(),
            },
        )
        .unwrap();
    }
    apply(&mut store, broker, vec![]).unwrap();
    assert_eq!(
        read(&mut store, broker, &alice, "").unwrap()["items"],
        json!([])
    );
    let group = store
        .identity_admin(IdentityAdmin::Group {
            label: "readers".into(),
        })
        .unwrap()["group_id"]
        .as_str()
        .unwrap()
        .to_owned();
    store
        .identity_admin(IdentityAdmin::Membership {
            group: group.clone(),
            principal: alice.0.actor_id.clone(),
            present: true,
        })
        .unwrap();
    apply(
        &mut store,
        broker,
        vec![
            change(
                &publication,
                Subject::Principal(alice.0.actor_id.clone()),
                true,
            ),
            change(&publication, Subject::Group(group.clone()), true),
        ],
    )
    .unwrap();
    let visible = read(&mut store, broker, &alice, "").unwrap();
    assert_eq!(visible["items"].as_array().unwrap().len(), 1);
    assert!(!visible.to_string().contains("storage_location"));
    assert!(!visible.to_string().contains("file:"));
    assert_eq!(
        read(&mut store, broker, &bob, "").unwrap()["items"],
        json!([])
    );
    assert_eq!(
        read(&mut store, broker, &bob, "sb_test").unwrap()["items"],
        json!([])
    );
    let snap = snapshot(&store.db, broker, &[]).unwrap();
    let alice_uc = snap
        .principals
        .iter()
        .find(|p| p.principal == alice.0.actor_id)
        .unwrap();
    let bob_uc = snap
        .principals
        .iter()
        .find(|p| p.principal == bob.0.actor_id)
        .unwrap();
    let table = snap
        .tables
        .iter()
        .find(|t| t.object.id == publication.tables[0].id.to_string())
        .unwrap()
        .clone();
    assert_eq!(
        broker
            .fresh()
            .test_user_request(
                alice_uc,
                "GET",
                &format!("tables/{}", table.object.name),
                None
            )
            .unwrap(),
        200
    );
    assert!(matches!(
        broker
            .fresh()
            .test_user_request(
                bob_uc,
                "GET",
                &format!("tables/{}", table.object.name),
                None
            )
            .unwrap(),
        403 | 404
    ));
    assert_eq!(
        broker
            .fresh()
            .test_user_request(
                alice_uc,
                "POST",
                "catalogs",
                Some(json!({"name":"no_admin"}))
            )
            .unwrap(),
        403
    );
    // Recreated platform identity with the same display label gets a distinct UC
    // subject and does not inherit the original identity's grants.
    let recreated = principal(&mut store);
    run(
        &mut store,
        broker,
        AdminCommand::MapPrincipal {
            principal: recreated.0.actor_id.clone(),
        },
    )
    .unwrap();
    apply(&mut store, broker, vec![]).unwrap();
    assert_eq!(
        read(&mut store, broker, &recreated, "").unwrap()["items"],
        json!([])
    );
    // Removing one origin retains the overlapping group grant.
    apply(
        &mut store,
        broker,
        vec![change(
            &publication,
            Subject::Principal(alice.0.actor_id.clone()),
            false,
        )],
    )
    .unwrap();
    assert_eq!(
        read(&mut store, broker, &alice, "").unwrap()["items"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    let stale = run(&mut store, broker, AdminCommand::Plan { changes: vec![] }).unwrap();
    store
        .identity_admin(IdentityAdmin::Membership {
            group: group.clone(),
            principal: alice.0.actor_id.clone(),
            present: false,
        })
        .unwrap();
    assert!(
        run(
            &mut store,
            broker,
            AdminCommand::Apply {
                plan: stale["id"].as_str().unwrap().into(),
                key: identity::id()
            }
        )
        .is_err()
    );
    assert!(read(&mut store, broker, &alice, "").is_err());
    apply(&mut store, broker, vec![]).unwrap();
    assert_eq!(
        read(&mut store, broker, &alice, "").unwrap()["items"],
        json!([])
    );
    // An out-of-band grant is drift, never an implicit sharing decision.
    broker
        .fresh()
        .test_request(
            "PATCH",
            false,
            &format!("permissions/table/{}", table.object.name),
            Some(json!({"changes":[{"principal":bob_uc.subject,"add":["SELECT"],"remove":[]}]})),
        )
        .unwrap();
    assert!(read(&mut store, broker, &bob, "").is_err());
    apply(&mut store, broker, vec![]).unwrap();
    assert_eq!(
        read(&mut store, broker, &bob, "").unwrap()["items"],
        json!([])
    );
    // Audit failure precedes every UC grant side effect.
    let planned = run(
        &mut store,
        broker,
        AdminCommand::Plan {
            changes: vec![change(
                &publication,
                Subject::Principal(bob.0.actor_id.clone()),
                true,
            )],
        },
    )
    .unwrap();
    store.db.execute_batch("CREATE TRIGGER fail_grant_audit BEFORE INSERT ON catalog_grant_audit BEGIN SELECT RAISE(ABORT,'unavailable'); END;").unwrap();
    assert!(
        run(
            &mut store,
            broker,
            AdminCommand::Apply {
                plan: planned["id"].as_str().unwrap().into(),
                key: identity::id()
            }
        )
        .is_err()
    );
    store
        .db
        .execute_batch("DROP TRIGGER fail_grant_audit")
        .unwrap();
    assert_eq!(
        read(&mut store, broker, &bob, "").unwrap()["items"],
        json!([])
    );
    // A stale UC plan cannot overwrite a concurrent administrator's change.
    broker
        .fresh()
        .test_request(
            "PATCH",
            false,
            &format!("permissions/table/{}", table.object.name),
            Some(json!({"changes":[{"principal":bob_uc.subject,"add":["SELECT"],"remove":[]}]})),
        )
        .unwrap();
    assert!(
        run(
            &mut store,
            broker,
            AdminCommand::Apply {
                plan: planned["id"].as_str().unwrap().into(),
                key: identity::id()
            }
        )
        .is_err()
    );
    assert!(read(&mut store, broker, &bob, "").is_err());
    apply(&mut store, broker, vec![]).unwrap();
    assert_eq!(
        read(&mut store, broker, &bob, "").unwrap()["items"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    // Completed remote work is fenced against local revocation before delivery.
    let job = store
        .catalog_governance_read(
            bob.0.clone(),
            bob.1.clone(),
            ReadCommand::List { search: "".into() },
            broker.fresh(),
        )
        .unwrap();
    let finish = job().unwrap();
    store
        .identity_admin(IdentityAdmin::Disable {
            principal: bob.0.actor_id.clone(),
            disabled: true,
        })
        .unwrap();
    assert!(finish(&mut store).is_err());
    // Restart never reopens grants without a fresh observed reconciliation.
    store.recover_catalog_governance().unwrap();
    assert!(read(&mut store, broker, &alice, "").is_err());
    apply(&mut store, broker, vec![]).unwrap();
    // UC soft-deletes users and refuses same-subject recreation at this pin.
    broker
        .fresh()
        .test_request(
            "DELETE",
            true,
            &format!("scim2/Users/{}", alice_uc.uc_id),
            None,
        )
        .unwrap();
    assert!(
        broker
            .fresh()
            .create_principal(&alice_uc.principal, &alice_uc.subject)
            .is_err()
    );
    assert!(read(&mut store, broker, &alice, "").is_err());
}

#[test]
fn catalog_governance_overlap_revision_recreation_and_unmapped_group_members_fail_closed() {
    let (_root, mut store, _owner, mut p) = crate::catalog::publication::tests::setup();
    seed(&mut store, &mut p);
    let broker = Broker::snapshot_fixture(
        p.namespace.provider_id.clone(),
        p.namespace.metastore_id.clone(),
    );
    let alice = principal(&mut store);
    let bob = principal(&mut store);
    let realm: String = store
        .db
        .query_row("SELECT id FROM identity_realm", [], |r| r.get(0))
        .unwrap();
    let subject = gov::subject(&realm, &alice.0.actor_id).unwrap();
    store
        .db
        .execute(
            "INSERT INTO catalog_principals VALUES (?1,?2,?3,?4,'ready')",
            params![alice.0.actor_id, subject, identity::id(), broker.provider],
        )
        .unwrap();
    let group = store
        .identity_admin(IdentityAdmin::Group {
            label: "readers".into(),
        })
        .unwrap()["group_id"]
        .as_str()
        .unwrap()
        .to_owned();
    store
        .identity_admin(IdentityAdmin::Membership {
            group: group.clone(),
            principal: alice.0.actor_id.clone(),
            present: true,
        })
        .unwrap();
    let grants = vec![
        change(&p, Subject::Principal(alice.0.actor_id.clone()), true),
        change(&p, Subject::Group(group.clone()), true),
    ];
    let plan = snapshot(&store.db, &broker, &grants).unwrap();
    assert_eq!(
        plan.desired
            .values()
            .map(|g| g.values().map(BTreeSet::len).sum::<usize>())
            .sum::<usize>(),
        3
    );
    for origin in &plan.origins {
        store
            .db
            .execute(
                "INSERT INTO catalog_grant_origins VALUES (?1,?2,?3,?4)",
                params![
                    origin.publication,
                    origin.subject,
                    origin.revision,
                    serde_json::to_string(&origin.tables).unwrap()
                ],
            )
            .unwrap();
    }
    let removing = snapshot(
        &store.db,
        &broker,
        &[change(
            &p,
            Subject::Principal(alice.0.actor_id.clone()),
            false,
        )],
    )
    .unwrap();
    assert_eq!(plan.desired, removing.desired);
    let before_revision = revision(&store.db).unwrap();
    store
        .identity_admin(IdentityAdmin::Membership {
            group: group.clone(),
            principal: bob.0.actor_id.clone(),
            present: true,
        })
        .unwrap();
    assert!(revision(&store.db).unwrap() > before_revision);
    assert!(snapshot(&store.db, &broker, &[]).is_err());
    store
        .identity_admin(IdentityAdmin::Membership {
            group,
            principal: bob.0.actor_id.clone(),
            present: false,
        })
        .unwrap();
    let mut stale = change(&p, Subject::Principal(bob.0.actor_id.clone()), true);
    stale.publication_revision += 1;
    assert!(snapshot(&store.db, &broker, &[stale]).is_err());
    let mut stale = change(&p, Subject::Principal(bob.0.actor_id.clone()), true);
    stale.tables = vec![identity::id()];
    assert!(snapshot(&store.db, &broker, &[stale]).is_err());
    store
        .identity_admin(IdentityAdmin::Disable {
            principal: alice.0.actor_id.clone(),
            disabled: true,
        })
        .unwrap();
    assert!(
        snapshot(&store.db, &broker, &[])
            .unwrap()
            .desired
            .values()
            .all(BTreeMap::is_empty)
    );
    // Existing grants cannot drift to a newly published revision, even with an unchanged alias.
    p.revision = Some(p.revision.unwrap() + 1);
    store.save_catalog_publication(&p).unwrap();
    assert!(snapshot(&store.db, &broker, &[]).is_err());
    assert!(
        serde_json::from_value::<ReadCommand>(
            json!({"action":"list","search":"","actor_id":"admin"})
        )
        .is_err()
    );
}

struct FaultProxy {
    endpoint: String,
    control: std::sync::Arc<std::sync::Mutex<(bool, usize, usize, bool)>>,
    stop: std::sync::Arc<std::sync::atomic::AtomicBool>,
    worker: Option<std::thread::JoinHandle<()>>,
}
impl FaultProxy {
    fn start(upstream: String) -> Self {
        use std::sync::{
            Arc, Mutex,
            atomic::{AtomicBool, Ordering},
        };
        let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
        let endpoint = format!("http://{}", server.server_addr());
        let stop = Arc::new(AtomicBool::new(false));
        let control = Arc::new(Mutex::new((false, 0usize, 0usize, false)));
        let (done, mode) = (stop.clone(), control.clone());
        let worker = std::thread::spawn(move || {
            let client = reqwest::blocking::Client::builder()
                .no_proxy()
                .timeout(Duration::from_secs(3))
                .build()
                .unwrap();
            while !done.load(Ordering::Relaxed) {
                let Some(mut request) = server.recv_timeout(Duration::from_millis(50)).unwrap()
                else {
                    continue;
                };
                let mut status = mode.lock().unwrap();
                if status.0 {
                    drop(status);
                    request
                        .respond(tiny_http::Response::from_string("{}").with_status_code(503))
                        .unwrap();
                    continue;
                }
                let patch = request.method().as_str() == "PATCH";
                if patch {
                    status.2 += 1;
                }
                let create = request.method().as_str() == "POST"
                    && request.url().ends_with("scim2/Users")
                    && status.3;
                if create {
                    status.3 = false;
                }
                let fail = create || (patch && status.1 > 0 && status.1 == status.2);
                drop(status);
                let mut body = Vec::new();
                request.as_reader().read_to_end(&mut body).unwrap();
                let mut forward = client.request(
                    request.method().as_str().parse().unwrap(),
                    format!("{upstream}{}", request.url()),
                );
                for h in request.headers() {
                    let name = h.field.as_str().as_str();
                    if name.eq_ignore_ascii_case("authorization")
                        || name.eq_ignore_ascii_case("content-type")
                        || name.to_ascii_lowercase().starts_with("x-supabricks-")
                    {
                        forward = forward.header(name, h.value.as_str());
                    }
                }
                let response = forward.body(body).send().unwrap();
                let code = response.status().as_u16();
                let bytes = response.bytes().unwrap();
                // Lose the reply after UC committed the second mutation.
                let reply = if fail {
                    tiny_http::Response::from_data(b"{}".to_vec()).with_status_code(503)
                } else {
                    tiny_http::Response::from_data(bytes.to_vec()).with_status_code(code)
                };
                let _ = request.respond(reply);
            }
        });
        Self {
            endpoint,
            control,
            stop,
            worker: Some(worker),
        }
    }
}
impl Drop for FaultProxy {
    fn drop(&mut self) {
        self.stop.store(true, std::sync::atomic::Ordering::Relaxed);
        self.worker.take().unwrap().join().unwrap();
    }
}

#[test]
#[ignore = "requires the source-built pinned UC Java runtime; catalog-probe runs this"]
fn catalog_governance_real_uc_partial_apply_outage_recovery_and_object_recreation() {
    let (_root, mut store, _owner, mut p) = crate::catalog::publication::tests::setup();
    let uc = real_uc(&store, &mut p);
    seed(&mut store, &mut p);
    let proxy = FaultProxy::start(uc.broker.test_endpoint().into());
    let broker = uc.broker.with_endpoint(proxy.endpoint.clone());
    let alice = principal(&mut store);
    proxy.control.lock().unwrap().3 = true;
    assert!(
        run(
            &mut store,
            &broker,
            AdminCommand::MapPrincipal {
                principal: alice.0.actor_id.clone()
            }
        )
        .is_err()
    );
    assert_eq!(
        store.catalog_governance_status().unwrap()["principals"][0]["state"],
        "pending"
    );
    let users = uc
        .broker
        .fresh()
        .test_request("GET", true, "scim2/Users", None)
        .unwrap();
    let created = users["Resources"]
        .as_array()
        .unwrap()
        .iter()
        .find(|u| u["externalId"] == alice.0.actor_id)
        .unwrap();
    assert!(
        run(
            &mut store,
            &broker,
            AdminCommand::ResolvePrincipal {
                principal: alice.0.actor_id.clone(),
                expected_uc_id: identity::id()
            }
        )
        .is_err()
    );
    run(
        &mut store,
        &broker,
        AdminCommand::ResolvePrincipal {
            principal: alice.0.actor_id.clone(),
            expected_uc_id: created["id"].as_str().unwrap().into(),
        },
    )
    .unwrap();
    apply(&mut store, &broker, vec![]).unwrap();
    proxy.control.lock().unwrap().1 = 2;
    assert!(
        apply(
            &mut store,
            &broker,
            vec![change(
                &p,
                Subject::Principal(alice.0.actor_id.clone()),
                true
            )]
        )
        .is_err()
    );
    assert_eq!(proxy.control.lock().unwrap().2, 2);
    assert!(read(&mut store, &broker, &alice, "").is_err());
    let snapshot = snapshot(&store.db, &broker, &[]).unwrap();
    assert_ne!(
        uc.broker.fresh().observe(&snapshot).unwrap(),
        snapshot.desired
    );
    // Intent and fail-closed state survive a real Store reopen.
    let root = store.root().to_owned();
    drop(store);
    let mut store = Store::open(&root).unwrap();
    store.recover_catalog_governance().unwrap();
    proxy.control.lock().unwrap().1 = 0;
    apply(&mut store, &broker, vec![]).unwrap();
    assert_eq!(
        read(&mut store, &broker, &alice, "").unwrap()["items"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    proxy.control.lock().unwrap().0 = true;
    assert!(read(&mut store, &broker, &alice, "").is_err());
    proxy.control.lock().unwrap().0 = false;
    assert!(read(&mut store, &broker, &alice, "").is_err());
    apply(&mut store, &broker, vec![]).unwrap();
    // Losing the durable completion audit leaves the already-applied UC revoke
    // unresolved. A fresh restart/reconciliation is required before new reads.
    let plan = run(
        &mut store,
        &broker,
        AdminCommand::Plan {
            changes: vec![change(
                &p,
                Subject::Principal(alice.0.actor_id.clone()),
                false,
            )],
        },
    )
    .unwrap();
    let job = store
        .catalog_governance_admin(
            AdminCommand::Apply {
                plan: plan["id"].as_str().unwrap().into(),
                key: identity::id(),
            },
            broker.fresh(),
        )
        .unwrap();
    let finish = job().unwrap();
    store.db.execute_batch("CREATE TRIGGER fail_completion_audit BEFORE INSERT ON catalog_grant_audit BEGIN SELECT RAISE(ABORT,'unavailable'); END;").unwrap();
    assert!(finish(&mut store).is_err());
    assert!(read(&mut store, &broker, &alice, "").is_err());
    store
        .db
        .execute_batch("DROP TRIGGER fail_completion_audit")
        .unwrap();
    store.recover_catalog_governance().unwrap();
    apply(&mut store, &broker, vec![]).unwrap();
    assert_eq!(
        read(&mut store, &broker, &alice, "").unwrap()["items"],
        json!([])
    );
    apply(
        &mut store,
        &broker,
        vec![change(
            &p,
            Subject::Principal(alice.0.actor_id.clone()),
            true,
        )],
    )
    .unwrap();
    let table = snapshot
        .tables
        .iter()
        .find(|t| t.object.id == p.tables[0].id.to_string())
        .unwrap();
    let plan = run(&mut store, &broker, AdminCommand::Plan { changes: vec![] }).unwrap();
    uc.broker
        .fresh()
        .test_request(
            "DELETE",
            false,
            &format!("tables/{}", table.object.name),
            None,
        )
        .unwrap();
    let recreated = uc
        .broker
        .fresh()
        .test_request("POST", false, "tables", Some(p.tables[0].body.clone()))
        .unwrap();
    assert_ne!(recreated["table_id"], p.tables[0].id.to_string());
    assert!(
        run(
            &mut store,
            &broker,
            AdminCommand::Apply {
                plan: plan["id"].as_str().unwrap().into(),
                key: identity::id()
            }
        )
        .is_err()
    );
    assert!(read(&mut store, &broker, &alice, "").is_err());
    // The actual UC request, not just the local journal, rejects this new object.
    assert!(matches!(
        uc.broker
            .fresh()
            .test_user_request(
                &snapshot.principals[0],
                "GET",
                &format!("tables/{}", table.object.name),
                None
            )
            .unwrap(),
        403 | 404
    ));
}

fn execution_admit(store: &mut Store, who: &Context, deployment: &str, code: &str) -> String {
    use crate::authorization::Command as C;
    let revision: i64 = store
        .db
        .query_row(
            "SELECT revision FROM authorization_policy WHERE deployment=?1",
            [deployment],
            |r| r.get(0),
        )
        .unwrap();
    let contents=json!({"nbformat":4,"nbformat_minor":5,"metadata":{},"cells":[{"cell_type":"code","source":[code],"metadata":{},"outputs":[],"execution_count":null}]}).to_string();
    let saved = store
        .authorized_command(
            who,
            C::SaveSource {
                deployment: deployment.into(),
                asset: identity::id(),
                kind: "notebook".into(),
                contents,
                expected_head: None,
                expected_policy: revision,
                key: identity::id(),
            },
        )
        .unwrap();
    store
        .authorized_command(
            who,
            C::AdmitExecution {
                deployment: deployment.into(),
                source_revision: saved["revision"].as_str().unwrap().into(),
                effective_principal: None,
                expected_policy: revision,
                key: identity::id(),
            },
        )
        .unwrap()["id"]
        .as_str()
        .unwrap()
        .into()
}
fn execution_fixture(store: &mut Store, who: &Context, deployment: &str) {
    use crate::authorization::{AdminCommand as A, Grant, Role};
    for action in [false, true] {
        let revision = store
            .db
            .query_row(
                "SELECT revision FROM authorization_policy WHERE deployment=?1",
                [deployment],
                |r| r.get(0),
            )
            .unwrap();
        store
            .authorization_admin(if action {
                A::SetGrant {
                    deployment: deployment.into(),
                    subject: Subject::Principal(who.actor_id.clone()),
                    grant: Grant::Execute,
                    effective_principal: None,
                    source_revision: None,
                    present: true,
                    expected_policy: revision,
                    key: identity::id(),
                }
            } else {
                A::SetRole {
                    deployment: deployment.into(),
                    subject: Subject::Principal(who.actor_id.clone()),
                    role: Some(Role::Editor),
                    expected_policy: revision,
                    key: identity::id(),
                }
            })
            .unwrap();
    }
}
#[test]
#[ignore = "requires Linux Docker/gVisor, verified UC09.4 inputs and the pinned real UC runtime"]
fn isolated_execution_real_uc_two_users_and_independent_expiry() {
    use crate::execution::{self as exec, Command as C};
    use sha2::{Digest, Sha256};
    let (_root, mut store, _owner, mut publication) = crate::catalog::publication::tests::setup();
    let config =
        PathBuf::from(std::env::var("SUPABRICKS_UC094_CONFIG").expect("verified execution config"));
    crate::supervisor::write_private(
        &store.root().join("execution-runtime.json"),
        &fs::read(&config).unwrap(),
    )
    .unwrap();
    let settings: Value = serde_json::from_slice(&fs::read(config).unwrap()).unwrap();
    // Replace only fixture bytes with an actual Arrow/Delta producer, then bind
    // their exact hashes in the same immutable publication descriptor.
    let snapshot = store
        .snapshot(publication.project_id, publication.epoch_id)
        .unwrap();
    let mut descriptor = snapshot.publication.descriptor.unwrap();
    let generation = store
        .root()
        .join("analytics/generations")
        .join(snapshot.publication.export_id.to_string());
    fs::remove_dir_all(generation.join("101")).unwrap();
    assert!(Command::new(PathBuf::from(settings["release"].as_str().unwrap()).join("python/analytics/python")).args(["-c","import sys;import pyarrow as pa;from deltalake import write_deltalake;write_deltalake(sys.argv[1],pa.table({'id':pa.array([1,2,3],type=pa.int32())}))"]).arg(generation.join("101")).status().unwrap().success());
    let mut files = descriptor["manifest"]["files"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|v| !v["path"].as_str().unwrap().starts_with("101/"))
        .cloned()
        .collect::<Vec<_>>();
    for directory in [generation.join("101"), generation.join("101/_delta_log")] {
        for entry in fs::read_dir(directory).unwrap() {
            let path = entry.unwrap().path();
            if path.is_file() {
                let bytes = fs::read(&path).unwrap();
                files.push(json!({"path":path.strip_prefix(&generation).unwrap().to_str().unwrap(),"bytes":bytes.len(),"sha256":hex::encode(Sha256::digest(bytes))}));
            }
        }
    }
    descriptor["manifest"]["files"] = json!(files);
    descriptor["manifest_sha256"] = json!(hex::encode(Sha256::digest(
        descriptor["manifest"].to_string().as_bytes()
    )));
    publication.manifest_hash = descriptor["manifest_sha256"].as_str().unwrap().into();
    store
        .db
        .execute(
            "UPDATE publications SET descriptor=?1 WHERE epoch_id=?2",
            params![descriptor.to_string(), publication.epoch_id.to_string()],
        )
        .unwrap();
    let uc = real_uc(&store, &mut publication);
    seed(&mut store, &mut publication);
    let alice = principal(&mut store);
    let bob = principal(&mut store);
    let deployment = publication.deployment_id.to_string();
    for who in [&alice, &bob] {
        execution_fixture(&mut store, &who.0, &deployment);
        run(
            &mut store,
            &uc.broker,
            AdminCommand::MapPrincipal {
                principal: who.0.actor_id.clone(),
            },
        )
        .unwrap();
    }
    apply(
        &mut store,
        &uc.broker,
        vec![change(
            &publication,
            Subject::Principal(alice.0.actor_id.clone()),
            true,
        )],
    )
    .unwrap();
    let table = publication.tables[0].id.to_string();
    let datasets = vec![exec::Dataset {
        publication: publication.id.to_string(),
        publication_revision: publication.revision.unwrap(),
        table: table.clone(),
    }];
    let denied_id = execution_admit(
        &mut store,
        &bob.0,
        &deployment,
        "raise RuntimeError('must never execute')",
    );
    let manager = exec::Manager::default();
    assert!(
        store
            .execution_job(
                bob.0.clone(),
                bob.1.clone(),
                deployment.clone(),
                C::Start {
                    id: denied_id,
                    datasets: datasets.clone()
                },
                uc.broker.fresh(),
                manager.clone()
            )
            .unwrap()()
        .unwrap()(&mut store)
        .is_err()
    );
    let canary = store.root().join("host-canary");
    fs::write(&canary, b"host credential must not cross").unwrap();
    let socket_path = store.root().join("host-control.sock");
    let _socket = std::os::unix::net::UnixListener::bind(&socket_path).unwrap();
    let host_process = ManagedChild(
        Command::new("sleep")
            .arg("120")
            .env("HOST_CREDENTIAL", "host-canary")
            .spawn()
            .unwrap(),
    );
    let source = include_str!("../../../../e2e/native/execution/kernel_checks.py")
        .replace("TABLE_UUID", &table)
        .replace("HOST_CANARY_JSON", &json!(canary).to_string())
        .replace("HOST_SOCKET_JSON", &json!(socket_path).to_string())
        .replace("HOST_PROCESS_PID", &host_process.0.id().to_string())
        .replace(
            "HOST_UC_PORT",
            uc.broker.test_endpoint().rsplit(':').next().unwrap(),
        );
    let a = execution_admit(&mut store, &alice.0, &deployment, &source);
    let b = execution_admit(
        &mut store,
        &bob.0,
        &deployment,
        &format!(
            "import time\nfrom pathlib import Path\nassert not Path('/admission/data/{table}').exists()\nassert not Path('/scratch/private-cache').exists()\nassert spark.sql('select 7 AS n').collect()[0].n==7\nprint('BOB_ISOLATED',flush=True)\ntime.sleep(18)\n"
        ),
    );
    let mut jobs = Vec::new();
    for (who, id, data) in [(&alice, &a, datasets.clone()), (&bob, &b, vec![])] {
        let job = store
            .execution_job(
                who.0.clone(),
                who.1.clone(),
                deployment.clone(),
                C::Start {
                    id: id.clone(),
                    datasets: data,
                },
                uc.broker.fresh(),
                manager.clone(),
            )
            .unwrap();
        jobs.push(std::thread::spawn(job));
    }
    let commits = jobs
        .into_iter()
        .map(|job| job.join().unwrap().unwrap())
        .collect::<Vec<_>>();
    for commit in commits {
        commit(&mut store).unwrap();
    }
    let start = Instant::now();
    let mut renew = Instant::now();
    let mut complete = std::collections::BTreeSet::new();
    while complete.len() != 2 {
        manager.tick(&mut store, false).unwrap();
        if renew.elapsed() > Duration::from_secs(3) {
            for (who, id, marker) in [
                (&alice, &a, "UC094_CHECKS_PASSED"),
                (&bob, &b, "BOB_ISOLATED"),
            ] {
                if complete.contains(id) {
                    continue;
                }
                let result = store
                    .execution_job(
                        who.0.clone(),
                        who.1.clone(),
                        deployment.clone(),
                        C::Poll { id: id.clone() },
                        uc.broker.fresh(),
                        manager.clone(),
                    )
                    .unwrap()()
                .unwrap()(&mut store)
                .unwrap();
                assert_ne!(result["state"], "failed", "{result}");
                if result["state"] == "finished" {
                    assert_eq!(result["result"]["exit_code"], 0, "{result}");
                    assert!(
                        result["result"]["output"]
                            .as_str()
                            .unwrap()
                            .contains(marker),
                        "{result}"
                    );
                    complete.insert(id.clone());
                }
            }
            renew = Instant::now();
        }
        assert!(start.elapsed() < Duration::from_secs(100));
        std::thread::sleep(Duration::from_millis(50));
    }
    drop(manager);
    // Exercise real open descriptors, cached bytes and a concurrent Sail query.
    // Losing the writer/renewal worker and failing audit persistence must stop
    // the same sandbox as an acknowledged platform disable.
    for mode in ["renewal_loss", "audit_failure", "platform_disable"] {
        let manager = exec::Manager::default();
        let open_file = format!(
            "import time,threading\nf=open('/admission/data/{table}/_delta_log/00000000000000000000.json','rb')\ncached=f.read()\ndef read_forever():\n while True:\n  f.seek(0);assert f.read()==cached\n  open('/scratch/open-ready','w').write(str(time.monotonic()))\n  time.sleep(.05)\nthreading.Thread(target=read_forever,daemon=True).start()\nspark.sql('SELECT sum(id) FROM range(10000000000)').collect()\n"
        );
        let id = execution_admit(&mut store, &alice.0, &deployment, &open_file);
        store
            .execution_job(
                alice.0.clone(),
                alice.1.clone(),
                deployment.clone(),
                C::Start {
                    id: id.clone(),
                    datasets: datasets.clone(),
                },
                uc.broker.fresh(),
                manager.clone(),
            )
            .unwrap()()
        .unwrap()(&mut store)
        .unwrap();
        let name = format!("sb-exec-{id}");
        let ready = Instant::now();
        let last_access = loop {
            let check = Command::new("docker")
                .args([
                    "exec",
                    &name,
                    "/tools/runsc",
                    "--root=/work/runsc",
                    "exec",
                    "lease",
                    "/bin/cat",
                    "/scratch/open-ready",
                ])
                .output()
                .unwrap();
            if check.status.success() && !check.stdout.is_empty() {
                break crate::identity::now();
            }
            assert!(ready.elapsed() < Duration::from_secs(25));
            std::thread::sleep(Duration::from_millis(200));
        };
        if mode == "platform_disable" {
            store
                .identity_admin(crate::identity::AdminCommand::Disable {
                    principal: alice.0.actor_id.clone(),
                    disabled: true,
                })
                .unwrap();
        } else if mode == "audit_failure" {
            store.db.execute_batch("CREATE TRIGGER deny_renewal_audit BEFORE INSERT ON authorization_audit BEGIN SELECT RAISE(ABORT,'disk full'); END;").unwrap();
            let poll = store
                .execution_job(
                    alice.0.clone(),
                    alice.1.clone(),
                    deployment.clone(),
                    C::Poll { id: id.clone() },
                    uc.broker.fresh(),
                    manager.clone(),
                )
                .unwrap();
            assert!(poll().unwrap()(&mut store).is_err());
        }
        let acknowledged = crate::identity::now();
        let began = Instant::now();
        if mode == "platform_disable" {
            manager.tick(&mut store, false).unwrap();
        }
        // No cooperative worker or writer is required to close the descriptors.
        let manager = if mode == "renewal_loss" {
            drop(manager);
            None
        } else {
            Some(manager)
        };
        loop {
            if let Some(manager) = &manager {
                let _ = manager.tick(&mut store, false);
            }
            let status = Command::new("docker")
                .args(["inspect", &name])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .unwrap();
            if !status.success() {
                break;
            }
            assert!(began.elapsed() < Duration::from_secs(35));
            std::thread::sleep(Duration::from_millis(200));
        }
        eprintln!(
            "UC096_REVOCATION {}",
            json!({"mode":mode,"last_success_observed_ms":last_access,"acknowledged_deny_ms":acknowledged,"closed_ms":crate::identity::now(),"closure_seconds":began.elapsed().as_secs_f64(),"bound_seconds":60})
        );
        if mode == "audit_failure" {
            store
                .db
                .execute_batch("DROP TRIGGER deny_renewal_audit")
                .unwrap();
        }
        store.recover_executions().unwrap();
        assert!(store.execution_live(&id).is_err());
    }
}
