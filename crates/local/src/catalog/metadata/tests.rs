use super::*;
use crate::{
    api::Binding,
    catalog::{adapter::Adapter, http::Probe},
    deployments::{Context, Source},
    operations::{Mutation, Ports},
    project::ProjectConfig,
    store::Store,
    supervisor,
};
use std::time::Duration;

fn fixture() -> (tempfile::TempDir, Store, Context, Binding, BranchId) {
    let dir = tempfile::tempdir().unwrap();
    let work = dir.path().join("project");
    std::fs::create_dir(&work).unwrap();
    let project = ProjectConfig::initialize(&work, "same-name").unwrap();
    let mut store = Store::open(&dir.path().join("data")).unwrap();
    let context = store
        .resolve_deployment(&Source {
            definition_id: project.id,
            worktree: work.canonicalize().unwrap(),
        })
        .unwrap();
    let binding = context.binding(&work.canonicalize().unwrap());
    let branch = store
        .submit(
            project.id,
            "create",
            Mutation::CreateDatabase {
                name: "main".into(),
                ports: Ports {
                    sql: 51000,
                    external_http: 51001,
                    internal_http: 51002,
                },
            },
        )
        .unwrap()
        .branch_id;
    (dir, store, context, binding, branch)
}
fn asset(context: &Context, branch: BranchId) -> Asset {
    Asset {
        id: OperationId::new(),
        project_id: context.runtime_project_id,
        deployment_id: context.deployment_id,
        branch_id: branch,
        resource_key: "table:123".into(),
        provider_id: format!("pg:{branch}"),
        provider: "postgres".into(),
        kind: "postgres_table".into(),
        incarnation: "original".into(),
        uc_object_id: None,
        alias: "public.orders".into(),
        schema: "public".into(),
        name: "orders".into(),
        columns: vec![Column {
            name: "id".into(),
            data_type: "integer".into(),
            nullable: false,
            ordinal: 1,
        }],
        version: "a".repeat(64),
        publication_revision: None,
        epoch_id: None,
        source_revision: Some(1),
        observed_at_ms: 0,
        snapshot_at_ms: None,
        state: "active".into(),
    }
}
fn namespace(context: &Context) -> Namespace {
    Namespace {
        deployment_id: context.deployment_id,
        project_id: context.runtime_project_id,
        provider_id: ProjectId::new().to_string(),
        metastore_id: "00000000-0000-4000-8000-000000000001".into(),
        catalog: "sb_test".into(),
        schema: "analytics".into(),
        catalog_id: None,
        schema_id: None,
        state: "creating_catalog".into(),
    }
}
#[test]
fn ownership_identity_incarnation_and_incomplete_observations_are_fenced() {
    let (_dir, mut store, context, _binding, branch) = fixture();
    let a = asset(&context, branch);
    let first = store
        .observe_catalog_assets(&context, branch, vec![a.clone()], true)
        .unwrap()
        .remove(0);
    let mut renamed = a.clone();
    renamed.id = OperationId::new();
    renamed.name = "renamed".into();
    renamed.version = "b".repeat(64);
    let updated = store
        .observe_catalog_assets(&context, branch, vec![renamed], true)
        .unwrap()
        .remove(0);
    assert_eq!(first.id, updated.id);
    assert_ne!(first.version, updated.version);
    assert!(store.catalog_asset(DeploymentId::new(), first.id).is_err());
    let mut replacement = a.clone();
    replacement.incarnation = "recreated".into();
    replacement.id = OperationId::new();
    let new = store
        .observe_catalog_assets(&context, branch, vec![replacement], true)
        .unwrap()
        .remove(0);
    assert_ne!(new.id, first.id);
    assert_eq!(
        store
            .catalog_asset(context.deployment_id, first.id)
            .unwrap()
            .state,
        "stale"
    );
    let mut bad = a;
    bad.project_id = ProjectId::new();
    assert!(
        store
            .observe_catalog_assets(&context, branch, vec![bad], true)
            .is_err()
    );
    assert_eq!(
        store
            .catalog_asset(context.deployment_id, new.id)
            .unwrap()
            .state,
        "active",
        "failed observation must roll back invalidation"
    );
}
#[test]
fn interrupted_namespace_creation_never_adopts_by_name() {
    let (dir, mut store, context, _, _) = fixture();
    let mut n = namespace(&context);
    store.save_catalog_namespace(&n).unwrap();
    drop(store);
    let mut store = Store::open(&dir.path().join("data")).unwrap();
    Service::recover(&mut store).unwrap();
    assert_eq!(
        store
            .catalog_namespace(context.deployment_id, &n.provider_id)
            .unwrap()
            .unwrap()
            .state,
        "indeterminate"
    );
    n.catalog_id = Some(ProjectId::new().to_string());
    n.state = "catalog_ready".into();
    store.save_catalog_namespace(&n).unwrap();
    store.recover_catalog_metadata().unwrap();
    assert_eq!(
        store
            .catalog_namespace(context.deployment_id, &n.provider_id)
            .unwrap()
            .unwrap()
            .catalog_id,
        n.catalog_id
    );
    let mut forged = n;
    forged.project_id = ProjectId::new();
    assert!(store.save_catalog_namespace(&forged).is_err());
}
#[test]
fn pagination_fences_source_changes_and_other_deployments() {
    let (_dir, _store, context, _, branch) = fixture();
    let a = asset(&context, branch);
    let mut b = a.clone();
    b.id = OperationId::new();
    let page = super::service::page(&context, branch, vec![a.clone(), b.clone()], None, 1).unwrap();
    let cursor = page["next"].as_str().unwrap();
    assert_eq!(
        super::service::page(
            &context,
            branch,
            vec![a.clone(), b.clone()],
            Some(cursor),
            1
        )
        .unwrap()["assets"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    b.version = "changed".into();
    assert!(matches!(
        super::service::page(&context, branch, vec![a, b], Some(cursor), 1)
            .unwrap_err()
            .code,
        Code::StaleVersion
    ));
    assert!(super::service::page(&context, BranchId::new(), vec![], Some(cursor), 1).is_err());
}
#[test]
fn analytical_resolution_rejects_case_and_quoting_collisions() {
    let (_dir, _store, context, _, branch) = fixture();
    let a = asset(&context, branch);
    let mut b = a.clone();
    b.name = "Orders".into();
    assert!(matches!(
        sources::validate_source(&[a.clone(), b]).unwrap_err().code,
        Code::QuotingCollision
    ));
    let mut b = a.clone();
    b.columns.push(Column {
        name: "ID".into(),
        ..b.columns[0].clone()
    });
    assert!(sources::validate_source(&[b]).is_err());
    assert!(sources::validate_source(&[a]).is_ok());
}
fn adapter_fixture(
    replies: Vec<(u16, Value)>,
) -> (tempfile::TempDir, Adapter, std::thread::JoinHandle<()>) {
    let dir = tempfile::tempdir().unwrap();
    let token = dir.path().join("token");
    supervisor::write_private(&token, b"private-catalog-test-token").unwrap();
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let endpoint = format!("http://{}", server.server_addr());
    let thread = std::thread::spawn(move || {
        for (index, (code, body)) in replies.into_iter().enumerate() {
            let r = server
                .recv_timeout(Duration::from_secs(4))
                .unwrap()
                .expect("expected bounded HTTP request");
            if index == 0 {
                assert!(!r.headers().iter().any(|h| h.field.equiv("Authorization")));
            } else {
                assert!(r.headers().iter().any(|h| h.field.equiv("Authorization")));
            }
            if r.method() == &tiny_http::Method::Post {
                assert!(r.headers().iter().any(
                    |h| h.field.equiv("Content-Type") && h.value.as_str() == "application/json"
                ));
            }
            r.respond(tiny_http::Response::from_string(body.to_string()).with_status_code(code))
                .unwrap();
        }
    });
    (
        dir,
        Adapter {
            probe: Probe {
                endpoint,
                token_file: token,
                ca_file: None,
                expected_metastore: Some("00000000-0000-4000-8000-000000000001".into()),
            },
            provider_id: "test".into(),
        },
        thread,
    )
}
fn health() -> Vec<(u16, Value)> {
    vec![
        (401, json!({})),
        (200, json!({"catalogs":[]})),
        (
            200,
            json!({"metastore_id":"00000000-0000-4000-8000-000000000001"}),
        ),
    ]
}
#[test]
fn remote_properties_cannot_establish_ownership_and_uuid_replacement_is_stale() {
    let (_root, _store, context, _, _) = fixture();
    let mut n = namespace(&context);
    let mut replies = health();
    replies.push((200,json!({"name":n.catalog,"id":ProjectId::new(),"properties":{"supabricks_project":n.project_id}})));
    let (_dir, a, t) = adapter_fixture(replies);
    assert!(matches!(
        a.namespace(n.clone()).unwrap_err().code,
        Code::NameCollision
    ));
    t.join().unwrap();
    n.catalog_id = Some(ProjectId::new().to_string());
    n.state = "ready".into();
    let mut replies = health();
    replies.push((200, json!({"name":n.catalog,"id":ProjectId::new()})));
    let (_dir, a, t) = adapter_fixture(replies);
    assert!(matches!(
        a.namespace(n).unwrap_err().code,
        Code::IdentityChanged
    ));
    t.join().unwrap();
}
#[test]
fn remote_write_failure_is_not_retried_or_exposed() {
    let (_root, _store, context, _, _) = fixture();
    let n = namespace(&context);
    let mut replies = health();
    replies.extend([
        (404, json!({})),
        (500, json!({"message":"secret-server-detail"})),
    ]);
    let (_dir, a, t) = adapter_fixture(replies);
    let error = a.namespace(n).unwrap_err();
    assert!(matches!(error.code, Code::AmbiguousMutation));
    assert!(
        !serde_json::to_string(&error)
            .unwrap()
            .contains("secret-server-detail")
    );
    t.join().unwrap();
}

#[test]
fn projectless_home_and_caller_supplied_ownership_are_rejected() {
    let (_dir, mut store, _context, _, _) = fixture();
    let home = store.root().join("console-home");
    std::fs::create_dir(&home).unwrap();
    let config = ProjectConfig::initialize(&home, "internal-home").unwrap();
    let context = store
        .resolve_deployment(&Source {
            definition_id: config.id,
            worktree: home.canonicalize().unwrap(),
        })
        .unwrap();
    let manager = crate::catalog::Manager::recover(&mut store);
    let mut service = Service::recover(&mut store).unwrap();
    let error = service
        .handle(
            &mut store,
            &manager,
            &context.binding(&home),
            Command::EnsureNamespace {},
            None,
        )
        .unwrap_err();
    assert!(error.to_string().contains("select a project"));
    for field in ["project_id", "deployment_id", "provider_id", "catalog_id"] {
        let mut v = json!({"action":"ensure_namespace"});
        v[field] = json!(ProjectId::new());
        assert!(serde_json::from_value::<Command>(v).is_err());
    }
}

#[test]
fn schema_uuid_uses_the_oss_schema_info_contract() {
    let (_root, _store, context, _, _) = fixture();
    let mut n = namespace(&context);
    let catalog = ProjectId::new().to_string();
    let schema = ProjectId::new().to_string();
    n.catalog_id = Some(catalog.clone());
    n.state = "creating_schema".into();
    let mut replies = health();
    replies.extend([
        (200, json!({"name":n.catalog,"id":catalog})),
        (404, json!({})),
        (
            200,
            json!({"name":n.schema,"catalog_name":n.catalog,"schema_id":schema}),
        ),
    ]);
    let (_dir, a, t) = adapter_fixture(replies);
    let ready = a.namespace(n).unwrap();
    assert_eq!(ready.schema_id.as_deref(), Some(schema.as_str()));
    assert_eq!(ready.state, "ready");
    t.join().unwrap();
    let mut replies = health();
    replies.extend([
        (200, json!({"name":ready.catalog,"id":catalog})),
        (
            200,
            json!({"name":ready.schema,"catalog_name":ready.catalog,"schema_id":schema}),
        ),
    ]);
    let (_dir, a, t) = adapter_fixture(replies);
    assert_eq!(a.namespace(ready).unwrap().state, "ready");
    t.join().unwrap();
}

#[test]
fn surviving_catalog_state_cannot_initialize_a_replacement_control_database() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("data");
    let store = Store::open(&root).unwrap();
    drop(store);
    std::fs::create_dir(root.join("catalog")).unwrap();
    std::fs::remove_file(root.join("state.sqlite3")).unwrap();
    assert!(Store::open(&root).is_err());
}
