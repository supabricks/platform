use super::*;
use crate::{governed::Capability, identity::Context, sync::GovernedCommand};

fn principal(s: &Store, kind: &str) -> Context {
    let id = uuid::Uuid::new_v4().to_string();
    s.db.execute(
        "INSERT INTO identity_principals(id,kind,label) VALUES (?1,?2,'sync test')",
        params![id, kind],
    )
    .unwrap();
    Context {
        api_version: 1,
        realm_id: s
            .db
            .query_row("SELECT id FROM identity_realm", [], |r| r.get(0))
            .unwrap(),
        actor_id: id.clone(),
        effective_principal_id: id,
        channel: crate::identity::Channel::Service,
        scopes: vec![crate::authorization::CONTROL_SCOPE.into()],
        expires_ms: i64::MAX,
    }
}
fn grant(s: &Store, d: DeploymentId, b: BranchId, ctx: &Context, caps: &[Capability]) {
    s.db.execute(
        "INSERT OR IGNORE INTO authorization_roles VALUES (?1,?2,'viewer')",
        params![d.to_string(), format!("principal:{}", ctx.actor_id)],
    )
    .unwrap();
    for cap in caps {
        s.db.execute(
            "INSERT OR IGNORE INTO data_grants VALUES (?1,?2,?3,?4)",
            params![
                d.to_string(),
                b.to_string(),
                format!("principal:{}", ctx.actor_id),
                cap.name()
            ],
        )
        .unwrap();
    }
}
fn request(command: Command, service: Option<&Context>) -> GovernedCommand {
    GovernedCommand {
        command,
        expected_policy: Some(1),
        service_principal: service.map(|c| c.actor_id.clone()),
    }
}

#[test]
fn service_authority_survives_manager_expiry_but_revocation_fences_export_and_replay() {
    let (_dir, mut s, p, d, b) = setup();
    let mut manager = principal(&s, "user");
    let service = principal(&s, "service");
    grant(
        &s,
        d,
        b,
        &manager,
        &[
            Capability::Read,
            Capability::ManageSync,
            Capability::ReadSync,
        ],
    );
    grant(
        &s,
        d,
        b,
        &service,
        &[Capability::Read, Capability::ExecuteSync],
    );
    s.db.execute(
        "INSERT INTO governed_branches VALUES (?1,'ready')",
        [b.to_string()],
    )
    .unwrap();
    let command = Command::Create {
        branch: b.to_string(),
        key: "service".into(),
        config: config(),
    };
    let policy: Policy = serde_json::from_value(
        s.governed_sync(
            &manager,
            &d.to_string(),
            request(command.clone(), Some(&service)),
        )
        .unwrap(),
    )
    .unwrap();
    assert!(
        s.capture_command(
            p,
            crate::capture::Command::Start {
                policy_id: policy.id,
                expected_revision: policy.revision,
                key: "operator-capture".into(),
                limits: Default::default(),
            }
        )
        .is_err()
    );
    let replay = s
        .governed_sync(&manager, &d.to_string(), request(command, Some(&service)))
        .unwrap();
    assert_eq!(replay["id"], json!(policy.id));
    manager.expires_ms = 0;
    assert!(
        s.governed_sync(&manager, &d.to_string(), request(Command::List, None))
            .is_err()
    );
    s.sync_source(&policy).unwrap();
    let r = run(&mut s, &policy, "background");
    let e = export(&mut s, &r);
    s.data_export_live(e, false).unwrap();
    assert!(s.governed_export(s.export(e).unwrap().child_id).unwrap());
    let before: i64 =
        s.db.query_row("SELECT revision FROM catalog_governance", [], |r| r.get(0))
            .unwrap();
    s.identity_admin(crate::identity::AdminCommand::Revoke {
        principal: service.actor_id.clone(),
    })
    .unwrap();
    assert!(s.sync_source(&policy).is_err());
    assert!(s.data_export_live(e, false).is_err());
    assert!(s.sync_publication_live(e).is_err());
    let after: i64 =
        s.db.query_row("SELECT revision FROM catalog_governance", [], |r| r.get(0))
            .unwrap();
    assert!(after > before);
    let view = s
        .sync_command(p, d, Command::Get { id: policy.id }, 1)
        .unwrap();
    assert_eq!(view["authority_status"], "revoked_or_unavailable");
    let audit: String =
        s.db.query_row(
            "SELECT event FROM security_audit WHERE event->>'$.action'='run.admitted'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!(audit.contains(&service.actor_id));
    assert!(!audit.contains("token"));
}

#[test]
fn governed_sync_requires_separate_source_manage_execute_and_result_grants_and_scopes() {
    let (_dir, mut s, p, d, b) = setup();
    let manager = principal(&s, "user");
    let service = principal(&s, "service");
    grant(&s, d, b, &manager, &[Capability::ReadSync]);
    let create = Command::Create {
        branch: b.to_string(),
        key: "create".into(),
        config: Config::default(),
    };
    assert!(
        s.governed_sync(
            &manager,
            &d.to_string(),
            request(create.clone(), Some(&service))
        )
        .is_err()
    );
    grant(
        &s,
        d,
        b,
        &manager,
        &[Capability::Read, Capability::ManageSync],
    );
    assert!(
        s.governed_sync(
            &manager,
            &d.to_string(),
            request(create.clone(), Some(&service))
        )
        .is_err()
    );
    grant(
        &s,
        d,
        b,
        &service,
        &[Capability::Read, Capability::ExecuteSync],
    );
    assert!(
        s.governed_sync(
            &manager,
            &d.to_string(),
            request(create.clone(), Some(&manager))
        )
        .is_err()
    );
    let policy: Policy = serde_json::from_value(
        s.governed_sync(&manager, &d.to_string(), request(create, Some(&service)))
            .unwrap(),
    )
    .unwrap();
    let other = principal(&s, "user");
    grant(&s, d, b, &other, &[]);
    assert_eq!(
        s.governed_sync(&other, &d.to_string(), request(Command::List, None))
            .unwrap(),
        json!([])
    );
    assert!(
        s.governed_sync(
            &other,
            &d.to_string(),
            request(Command::Get { id: policy.id }, None)
        )
        .is_err()
    );
    assert!(
        s.governed_sync(
            &manager,
            &DeploymentId::new().to_string(),
            request(Command::Get { id: policy.id }, None)
        )
        .is_err()
    );
    assert!(
        s.governed_sync(
            &manager,
            &d.to_string(),
            request(
                Command::Update {
                    id: policy.id,
                    expected_revision: 1,
                    key: "unsafe".into(),
                    config: Config {
                        mode: "continuous".into(),
                        strategy: "incremental".into(),
                        ..Default::default()
                    }
                },
                None
            )
        )
        .is_err()
    );
    assert!(s.captures().unwrap().is_empty());
    s.db.execute(
        "DELETE FROM data_grants WHERE capability='execute_sync'",
        [],
    )
    .unwrap();
    assert!(
        s.sync_source(&s.sync_policy(p, policy.id).unwrap())
            .is_err()
    );
}

#[test]
fn inspect_is_read_only_and_resync_requires_current_review_then_explicit_resume() {
    let (_dir, mut s, p, d, b) = setup();
    s.sync_command(
        p,
        d,
        Command::Inspect {
            branch: b.to_string(),
        },
        0,
    )
    .unwrap();
    assert!(s.sync_policies().unwrap().is_empty());
    assert!(s.captures().unwrap().is_empty());
    let config = Config {
        mode: "triggered".into(),
        strategy: "incremental".into(),
        ..Default::default()
    };
    let policy: Policy = serde_json::from_value(
        s.sync_command(
            p,
            d,
            Command::Create {
                branch: b.to_string(),
                key: "triggered".into(),
                config,
            },
            0,
        )
        .unwrap(),
    )
    .unwrap();
    let old = policy.capture_id.unwrap();
    let review = s.review_sync_resync(p, policy.id).unwrap();
    let mut command = Command::Resync {
        id: policy.id,
        expected_revision: 1,
        review_hash: "wrong".into(),
        key: "resync".into(),
    };
    assert!(s.sync_command(p, d, command.clone(), 1).is_err());
    if let Command::Resync { review_hash, .. } = &mut command {
        *review_hash = review["review_hash"].as_str().unwrap().into();
    }
    let response = s.sync_command(p, d, command.clone(), 1).unwrap();
    assert_eq!(s.sync_command(p, d, command, 2).unwrap(), response);
    assert_eq!(response["state"], "paused");
    let resume = Command::Resume {
        id: policy.id,
        expected_revision: 2,
        key: "resume".into(),
    };
    assert!(s.sync_command(p, d, resume.clone(), 3).is_err());
    let mut c = s.capture(p, old).unwrap();
    c.cleanup_complete = true;
    s.finish_capture_delete(&mut c).unwrap();
    let new = s.sync_command(p, d, resume, 4).unwrap();
    assert_ne!(new["capture_id"], json!(old));
    assert_eq!(new["revision"], 3);
}
