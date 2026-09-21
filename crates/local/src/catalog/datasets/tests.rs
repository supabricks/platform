use super::*;
use crate::{
    catalog::publication::tests::setup,
    deployments::{Command as Deployment, Source},
    project_apply::{Command as Apply, Options},
    projects,
};
use std::{fs, path::Path};
use supabricks_core::resource::ProjectId;

fn consumer(store: &mut Store, path: &Path, p: &Publication) -> Binding {
    fs::create_dir(path).unwrap();
    fs::write(
        path.join("supabricks.toml"),
        format!(
            r#"format_version=2
id="{}"
name="consumer"
[package]
version="0.1.0"
include=[]
notebook_outputs="strip"
[targets.local]
mode="development"
default=true
[resources.dataset.sales]
kind="catalog_dataset"
requirement="sales.orders.v1"
[resources.dataset.sales.provenance]
deployment_id="{}"
provider_id="{}"
publication_id="{}"
"#,
            ProjectId::new(),
            p.deployment_id,
            p.namespace.provider_id,
            p.id
        ),
    )
    .unwrap();
    let source = Source::read(path).unwrap();
    let ctx: crate::deployments::Context = serde_json::from_value(
        store
            .project_command(
                &source,
                Deployment::Create {
                    key: "create".into(),
                    target: None,
                },
            )
            .unwrap(),
    )
    .unwrap();
    ctx.binding(path)
}
fn publish(store: &mut Store, p: &mut Publication) {
    store.begin_catalog_publication(p).unwrap();
    for t in &mut p.tables {
        t.state = "verified".into();
    }
    store.commit_catalog_publication(p).unwrap();
}
fn options(p: &Publication) -> Options {
    Options {
        datasets: BTreeMap::from([(
            "dataset.sales".into(),
            Target {
                deployment_id: p.deployment_id,
                provider_id: p.namespace.provider_id.clone(),
                publication_id: p.id,
            },
        )]),
        ..Default::default()
    }
}
fn activate(store: &mut Store, mut o: project_apply::Operation) {
    for s in &o.plan.steps {
        if s.action == "unbind" {
            continue;
        }
        o.resources.insert(
            s.logical.clone(),
            Resource {
                kind: s.kind.clone(),
                origin: o.id,
                branch: None,
                file: None,
                database: None,
                environment: None,
                generation: None,
                receipt: Some(json!(selected(s).unwrap())),
            },
        );
    }
    store.activate_deployment(&mut o).unwrap();
}
#[test]
fn packages_never_adopt_provenance_and_destination_plan_is_explicit() {
    let (dir, mut store, _, mut p) = setup();
    publish(&mut store, &mut p);
    let binding = consumer(&mut store, &dir.path().join("consumer"), &p);
    let report = projects::inspect(&binding.worktree, None).unwrap();
    assert!(report.capabilities.contains(&CAPABILITY.into()));
    assert_eq!(report.unresolved_bindings[0].kind, "catalog_dataset");
    let pack = projects::package::prepare(&binding.worktree, None).unwrap();
    assert_eq!(
        pack.report.inspection.unresolved_bindings[0].kind,
        "catalog_dataset"
    );
    let unresolved = project_apply::plan(&store, &binding, Options::default()).unwrap();
    assert_eq!(unresolved.steps[0].action, "unresolved");
    assert!(
        project_apply::handle(
            &mut store,
            &binding,
            Apply::Apply {
                plan: unresolved,
                key: "no".into()
            }
        )
        .is_err()
    );
    let plan = project_apply::plan(&store, &binding, options(&p)).unwrap();
    assert_eq!(plan.steps[0].action, "bind");
    let d = selected(&plan.steps[0]).unwrap();
    assert!(!json!(d).to_string().contains("storage_location"));
    assert!(!json!(d).to_string().contains("file://"));
    let op: project_apply::Operation = serde_json::from_value(
        project_apply::handle(
            &mut store,
            &binding,
            Apply::Apply {
                plan: plan.clone(),
                key: "bind".into(),
            },
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(store.dataset_references(p.id).unwrap()["apply"], 1);
    activate(&mut store, op);
    assert_eq!(
        store.dataset_references(p.id).unwrap(),
        json!({"apply":0,"binding":1,"session":0})
    );
    assert_eq!(
        project_apply::plan(&store, &binding, Options::default())
            .unwrap()
            .steps[0]
            .action,
        "retain"
    );
    let second = consumer(&mut store, &dir.path().join("other"), &p);
    assert_eq!(
        project_apply::plan(&store, &second, Options::default())
            .unwrap()
            .steps[0]
            .action,
        "unresolved"
    );
    assert!(
        project_apply::handle(
            &mut store,
            &second,
            Apply::Apply {
                plan,
                key: "foreign-plan".into()
            }
        )
        .is_err()
    );
}
#[test]
fn cancellation_and_unbind_release_only_consumer_references_and_survive_restart() {
    let (dir, mut store, _, mut p) = setup();
    publish(&mut store, &mut p);
    let binding = consumer(&mut store, &dir.path().join("consumer"), &p);
    let plan = project_apply::plan(&store, &binding, options(&p)).unwrap();
    let mut op: project_apply::Operation = serde_json::from_value(
        project_apply::handle(
            &mut store,
            &binding,
            Apply::Apply {
                plan,
                key: "cancel".into(),
            },
        )
        .unwrap(),
    )
    .unwrap();
    op.state = "cancelled".into();
    store.save_apply(&op).unwrap();
    assert_eq!(store.dataset_references(p.id).unwrap()["apply"], 0);
    let plan = project_apply::plan(&store, &binding, options(&p)).unwrap();
    let op = serde_json::from_value(
        project_apply::handle(
            &mut store,
            &binding,
            Apply::Apply {
                plan,
                key: "bind".into(),
            },
        )
        .unwrap(),
    )
    .unwrap();
    activate(&mut store, op);
    let root = store.root().to_owned();
    drop(store);
    let mut store = Store::open(&root).unwrap();
    assert_eq!(store.dataset_references(p.id).unwrap()["binding"], 1);
    p = store
        .retire_catalog_publication(p.deployment_id, p.id, "withdraw", 1)
        .unwrap();
    assert!(store.catalog_references(&p).unwrap());
    assert!(project_apply::plan(&store, &binding, Options::default()).is_err());
    // Decommissioning the consuming project's declarations is a reviewed unbind.
    let file = binding.worktree.join("supabricks.toml");
    let text = fs::read_to_string(&file).unwrap();
    fs::write(
        file,
        text.split("[resources.dataset.sales]").next().unwrap(),
    )
    .unwrap();
    let plan = project_apply::plan(&store, &binding, Options::default()).unwrap();
    assert_eq!(plan.steps[0].action, "unbind");
    let op = serde_json::from_value(
        project_apply::handle(
            &mut store,
            &binding,
            Apply::Apply {
                plan,
                key: "unbind".into(),
            },
        )
        .unwrap(),
    )
    .unwrap();
    activate(&mut store, op);
    assert_eq!(store.dataset_references(p.id).unwrap()["binding"], 0);
    assert!(!store.catalog_references(&p).unwrap());
    assert_eq!(
        store.snapshot(p.project_id, p.epoch_id).unwrap().state,
        "available"
    );
    assert_eq!(
        store
            .catalog_publication(p.deployment_id, p.id)
            .unwrap()
            .state,
        "retiring"
    );
}
#[test]
fn fingerprints_stale_plans_and_foreign_provider_mappings_fail_closed() {
    let (dir, mut store, _, mut p) = setup();
    publish(&mut store, &mut p);
    let binding = consumer(&mut store, &dir.path().join("consumer"), &p);
    let mut bad = options(&p);
    bad.datasets.get_mut("dataset.sales").unwrap().provider_id = ProjectId::new().to_string();
    assert!(project_apply::plan(&store, &binding, bad).is_err());
    let plan = project_apply::plan(&store, &binding, options(&p)).unwrap();
    let file = binding.worktree.join("supabricks.toml");
    let text = fs::read_to_string(&file).unwrap();
    fs::write(
        &file,
        text.replace(
            "requirement=\"sales.orders.v1\"",
            &format!(
                "requirement=\"sales.orders.v1\"\nexpected_schema_sha256=\"{}\"",
                "0".repeat(64)
            ),
        ),
    )
    .unwrap();
    assert!(project_apply::plan(&store, &binding, options(&p)).is_err());
    assert!(
        project_apply::handle(
            &mut store,
            &binding,
            Apply::Apply {
                plan,
                key: "stale".into()
            }
        )
        .is_err()
    );
    assert_eq!(store.dataset_references(p.id).unwrap()["apply"], 0);
}

#[test]
fn reader_admission_rolls_back_all_pins_and_recovery_releases_only_reader_retention() {
    let (dir, mut store, _, mut p) = setup();
    publish(&mut store, &mut p);
    let binding = consumer(&mut store, &dir.path().join("consumer"), &p);
    let plan = project_apply::plan(&store, &binding, options(&p)).unwrap();
    let op = serde_json::from_value(
        project_apply::handle(
            &mut store,
            &binding,
            Apply::Apply {
                plan,
                key: "bind".into(),
            },
        )
        .unwrap(),
    )
    .unwrap();
    activate(&mut store, op);
    let target = options(&p).datasets.remove("dataset.sales").unwrap();
    let d = describe(&store, &target, true).unwrap();
    let inputs = BTreeMap::from([("dataset.a".into(), d.clone()), ("dataset.b".into(), d)]);
    let root = store.root().to_owned();
    // A failure after the first extra pin must roll back both admission and all pins.
    let db = rusqlite::Connection::open(root.join("state.sqlite3")).unwrap();
    db.execute_batch("CREATE TRIGGER fail_second_dataset BEFORE INSERT ON catalog_publication_refs WHEN NEW.reference_key LIKE 'session:%:dataset.b' BEGIN SELECT RAISE(ABORT,'injected pin failure'); END;").unwrap();
    let admit = |store: &mut Store| {
        store.admit_session_inputs(
            p.project_id,
            p.branch_id,
            "reader",
            json!({"catalog":true}),
            Some(p.epoch_id),
            None,
            60_000,
            None,
            inputs.clone(),
        )
    };
    assert!(admit(&mut store).is_err());
    assert!(store.active_analytical_sessions().unwrap().is_empty());
    assert_eq!(
        store.dataset_references(p.id).unwrap(),
        json!({"binding":1,"apply":0,"session":0})
    );
    db.execute_batch("DROP TRIGGER fail_second_dataset")
        .unwrap();
    drop(db);
    let session = admit(&mut store).unwrap();
    assert_eq!(admit(&mut store).unwrap().id, session.id);
    assert!(store.finish_session_inputs(&session).is_err());
    drop(store);
    let mut store = Store::open(&root).unwrap();
    assert_eq!(store.dataset_references(p.id).unwrap()["session"], 2);
    assert_eq!(
        store
            .analytical_session(p.project_id, session.id)
            .unwrap()
            .datasets,
        inputs
    );
    assert_eq!(store.dataset_holders(p.id).unwrap().len(), 3);
    assert_eq!(
        store.dataset_other_references(p.epoch_id).unwrap()["primary_sessions"],
        1
    );
    crate::sessions::Sessions::recover(&mut store).unwrap();
    assert_eq!(
        store.dataset_references(p.id).unwrap(),
        json!({"binding":1,"apply":0,"session":0})
    );
    assert_eq!(
        store
            .analytical_session(p.project_id, session.id)
            .unwrap()
            .state,
        "failed"
    );
    assert_eq!(
        store.dataset_other_references(p.epoch_id).unwrap()["primary_sessions"],
        0
    );
}

#[test]
fn schema_fingerprint_ignores_physical_order_and_detects_logical_schema_changes() {
    let (_dir, _store, _, mut p) = setup();
    let expected = project_apply::digest(&logical_tables(&p)).unwrap();
    p.tables.reverse();
    for table in &mut p.tables {
        table.body["name"] = json!("another_physical_table");
        table.body["catalog_name"] = json!("another_destination");
    }
    assert_eq!(
        project_apply::digest(&logical_tables(&p)).unwrap(),
        expected
    );
    p.tables[0].body["columns"][0]["name"] = json!("renamed_column");
    assert_ne!(
        project_apply::digest(&logical_tables(&p)).unwrap(),
        expected
    );
}
