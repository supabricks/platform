use super::*;
struct Fixture {
    _root: tempfile::TempDir,
    store: Store,
    alice: Context,
    bob: Context,
    service: Context,
    one: String,
    two: String,
}
impl Fixture {
    fn new() -> Self {
        let root = tempfile::tempdir().unwrap();
        let mut store = Store::open(&root.path().join("state")).unwrap();
        let mut project = |name: &str| {
            let config = ProjectConfig {
                id: ProjectId::new(),
                name: name.into(),
                format_version: 1,
            };
            store.register_project(&config).unwrap();
            store
                .db
                .query_row(
                    "SELECT id FROM deployments WHERE runtime_project_id=?1",
                    [config.id.to_string()],
                    |r| r.get::<_, String>(0),
                )
                .unwrap()
        };
        let one = project("one");
        let two = project("two");
        let base = owner(&store.db).unwrap();
        let principal = |kind: &str| {
            let id = identity::id();
            store
                .db
                .execute(
                    "INSERT INTO identity_principals(id,kind,label) VALUES (?1,?2,?3)",
                    params![id, kind, kind],
                )
                .unwrap();
            Context {
                actor_id: id.clone(),
                effective_principal_id: id,
                ..base.clone()
            }
        };
        let alice = principal("user");
        let bob = principal("user");
        let service = principal("service");
        Self {
            _root: root,
            store,
            alice,
            bob,
            service,
            one,
            two,
        }
    }
    fn revision(&self) -> i64 {
        policy(&self.store.db, &self.one).unwrap()
    }
    fn role(&mut self, who: &Context, role: Role) {
        self.store
            .authorization_admin(AdminCommand::SetRole {
                deployment: self.one.clone(),
                subject: Subject::Principal(who.actor_id.clone()),
                role: Some(role),
                expected_policy: self.revision(),
                key: identity::id(),
            })
            .unwrap();
    }
    fn grant(
        &mut self,
        who: &Context,
        grant: Grant,
        effective: Option<&Context>,
        source: Option<&str>,
    ) {
        self.store
            .authorization_admin(AdminCommand::SetGrant {
                deployment: self.one.clone(),
                subject: Subject::Principal(who.actor_id.clone()),
                grant,
                effective_principal: effective.map(|p| p.actor_id.clone()),
                source_revision: source.map(str::to_owned),
                present: true,
                expected_policy: self.revision(),
                key: identity::id(),
            })
            .unwrap();
    }
    fn save(&mut self, contents: &str, head: Option<String>) -> String {
        self.store
            .authorized_command(
                &self.alice,
                Command::SaveSource {
                    deployment: self.one.clone(),
                    asset: "query".into(),
                    kind: "sql".into(),
                    contents: contents.into(),
                    expected_head: head,
                    expected_policy: self.revision(),
                    key: identity::id(),
                },
            )
            .unwrap()["revision"]
            .as_str()
            .unwrap()
            .into()
    }
    fn admit(&mut self, who: &Context, source: &str, effective: Option<&Context>) -> Result<Value> {
        self.store.authorized_command(
            who,
            Command::AdmitExecution {
                deployment: self.one.clone(),
                source_revision: source.into(),
                effective_principal: effective.map(|p| p.actor_id.clone()),
                expected_policy: self.revision(),
                key: identity::id(),
            },
        )
    }
}
#[test]
fn authorization_defaults_deny_and_project_listing_never_leaks_other_projects() {
    let mut f = Fixture::new();
    assert_eq!(
        f.store
            .authorized_command(&f.alice, Command::Projects {})
            .unwrap()["projects"],
        json!([])
    );
    assert!(
        f.store
            .authorized_command(
                &f.alice,
                Command::Project {
                    deployment: f.one.clone()
                }
            )
            .is_err()
    );
    f.role(&f.alice.clone(), Role::Viewer);
    let projects = f
        .store
        .authorized_command(&f.alice, Command::Projects {})
        .unwrap();
    assert_eq!(projects["projects"].as_array().unwrap().len(), 1);
    assert!(!projects.to_string().contains(&f.two));
    assert!(!projects.to_string().contains("worktree"));
    assert!(
        f.store
            .authorized_command(
                &f.alice,
                Command::Project {
                    deployment: f.two.clone()
                }
            )
            .is_err()
    );
    assert!(
        f.store
            .authorized_command(
                &f.bob,
                Command::Project {
                    deployment: f.one.clone()
                }
            )
            .is_err()
    );
    assert!(
        f.store
            .authorized_command(
                &f.alice,
                Command::SaveSource {
                    deployment: f.one.clone(),
                    asset: "a".into(),
                    kind: "sql".into(),
                    contents: "select 1".into(),
                    expected_head: None,
                    expected_policy: f.revision(),
                    key: "edit".into()
                }
            )
            .is_err()
    );
    let mut forged = f.alice.clone();
    forged.effective_principal_id = f.service.actor_id.clone();
    assert!(
        f.store
            .authorized_command(&forged, Command::Projects {})
            .is_err()
    );
    forged = f.alice.clone();
    forged.realm_id = identity::id();
    assert!(
        f.store
            .authorized_command(&forged, Command::Projects {})
            .is_err()
    );
}
#[test]
fn authorization_membership_never_grants_execution_data_or_operator_capabilities() {
    use crate::authorization::inventory::GatedOperation::*;
    let mut f = Fixture::new();
    f.role(&f.alice.clone(), Role::Administrator);
    let source = f.save("select 1", None);
    assert!(f.admit(&f.alice.clone(), &source, None).is_err());
    for operation in [
        CatalogRead,
        CatalogGrant,
        PostgresRead,
        PostgresWrite,
        PostgresDdl,
        Ingestion,
        Migration,
        FixtureLoad,
        Publication,
        PackageImport,
        PackageExport,
        BranchClone,
        EnvironmentHook,
        NotebookOutput,
        SavedQueryResult,
        Connect,
        Deploy,
        Import,
        Export,
        Backup,
        Restore,
        Delete,
        WorkloadLaunch,
        Logs,
        ArtifactDownload,
        WebSocket,
    ] {
        assert!(
            f.store
                .authorized_command(
                    &f.alice,
                    Command::Unavailable {
                        deployment: f.one.clone(),
                        operation
                    }
                )
                .is_err()
        );
    }
    f.grant(&f.alice.clone(), Grant::Execute, None, None);
    let admission = f.admit(&f.alice.clone(), &source, None).unwrap();
    assert_eq!(admission["actor_id"], f.alice.actor_id);
    assert_eq!(admission["effective_principal_id"], f.alice.actor_id);
    assert_eq!(admission["runtime_started"], false);
}
#[test]
fn authorization_service_use_requires_two_execution_grants_and_exact_approved_source() {
    let mut f = Fixture::new();
    f.role(&f.alice.clone(), Role::Editor);
    f.role(&f.service.clone(), Role::Viewer);
    let source = f.save("select 1", None);
    f.grant(&f.alice.clone(), Grant::Execute, None, None);
    assert!(
        f.admit(&f.alice.clone(), &source, Some(&f.service.clone()))
            .is_err()
    );
    f.grant(
        &f.alice.clone(),
        Grant::ActAs,
        Some(&f.service.clone()),
        Some(&source),
    );
    assert!(
        f.admit(&f.alice.clone(), &source, Some(&f.service.clone()))
            .is_err()
    );
    f.grant(&f.service.clone(), Grant::Execute, None, None);
    let admitted = f
        .admit(&f.alice.clone(), &source, Some(&f.service.clone()))
        .unwrap();
    assert_eq!(admitted["actor_id"], f.alice.actor_id);
    assert_eq!(admitted["effective_principal_id"], f.service.actor_id);
    let edited = f.save("select secret", Some(source.clone()));
    assert!(
        f.admit(&f.alice.clone(), &edited, Some(&f.service.clone()))
            .is_err()
    );
    // Existing approval retains the old immutable code; no implicit newest revision.
    assert_eq!(
        f.admit(&f.alice.clone(), &source, Some(&f.service.clone()))
            .unwrap()["source_revision"],
        source
    );
    let old = f
        .store
        .authorized_command(
            &f.alice,
            Command::Source {
                deployment: f.one.clone(),
                revision: source,
            },
        )
        .unwrap();
    assert_eq!(old["contents"], "select 1");
    assert!(
        f.admit(&f.alice.clone(), &edited, Some(&f.bob.clone()))
            .is_err()
    );
    f.store
        .identity_admin(crate::identity::AdminCommand::Disable {
            principal: f.service.actor_id.clone(),
            disabled: true,
        })
        .unwrap();
    assert!(
        f.admit(&f.alice.clone(), &edited, Some(&f.service.clone()))
            .is_err()
    );
}
#[test]
fn authorization_stop_other_actor_requires_explicit_grant_and_cross_project_ids_are_denied() {
    let mut f = Fixture::new();
    f.role(&f.alice.clone(), Role::Editor);
    f.role(&f.bob.clone(), Role::Administrator);
    let source = f.save("select 1", None);
    f.grant(&f.alice.clone(), Grant::Execute, None, None);
    let admission = f.admit(&f.alice.clone(), &source, None).unwrap();
    let id = admission["id"].as_str().unwrap().to_owned();
    assert!(
        f.store
            .authorized_command(
                &f.bob,
                Command::Execution {
                    deployment: f.one.clone(),
                    id: id.clone()
                }
            )
            .is_err()
    );
    assert_eq!(
        f.store
            .authorized_command(
                &f.bob,
                Command::Executions {
                    deployment: f.one.clone()
                }
            )
            .unwrap()["executions"],
        json!([])
    );
    assert!(
        f.store
            .authorized_command(
                &f.bob,
                Command::StopExecution {
                    deployment: f.one.clone(),
                    id: id.clone(),
                    expected_policy: f.revision(),
                    key: "stop".into()
                }
            )
            .is_err()
    );
    f.grant(&f.bob.clone(), Grant::StopAny, None, None);
    let stopped = f
        .store
        .authorized_command(
            &f.bob,
            Command::StopExecution {
                deployment: f.one.clone(),
                id: id.clone(),
                expected_policy: f.revision(),
                key: "stop".into(),
            },
        )
        .unwrap();
    assert_eq!(stopped["state"], "cancelled");
    f.store
        .authorization_admin(AdminCommand::SetRole {
            deployment: f.two.clone(),
            subject: Subject::Principal(f.bob.actor_id.clone()),
            role: Some(Role::Administrator),
            expected_policy: 1,
            key: "role".into(),
        })
        .unwrap();
    assert!(
        f.store
            .authorized_command(
                &f.bob,
                Command::Execution {
                    deployment: f.two.clone(),
                    id
                }
            )
            .is_err()
    );
}
#[test]
fn authorization_optimistic_policy_source_heads_and_idempotency_are_actor_scoped() {
    let mut f = Fixture::new();
    f.role(&f.alice.clone(), Role::Editor);
    let revision = f.revision();
    let command = Command::SaveSource {
        deployment: f.one.clone(),
        asset: "query".into(),
        kind: "sql".into(),
        contents: "select 1".into(),
        expected_head: None,
        expected_policy: revision,
        key: "save".into(),
    };
    let first = f
        .store
        .authorized_command(&f.alice, command.clone())
        .unwrap();
    assert_eq!(
        first,
        f.store
            .authorized_command(&f.alice, command.clone())
            .unwrap()
    );
    let mut altered = command.clone();
    if let Command::SaveSource { contents, .. } = &mut altered {
        *contents = "select 2".into();
    }
    assert!(f.store.authorized_command(&f.alice, altered).is_err());
    let mut stale = command.clone();
    if let Command::SaveSource { key, .. } = &mut stale {
        *key = "new-key".into();
    }
    assert!(f.store.authorized_command(&f.alice, stale).is_err());
    f.role(&f.alice.clone(), Role::Viewer);
    assert!(f.store.authorized_command(&f.alice, command).is_err());
    f.role(&f.alice.clone(), Role::Editor);
    assert!(
        f.store
            .authorized_command(
                &f.alice,
                Command::SaveSource {
                    deployment: f.one.clone(),
                    asset: "different".into(),
                    kind: "sql".into(),
                    contents: "select 1".into(),
                    expected_head: None,
                    expected_policy: revision,
                    key: "stale-policy".into()
                }
            )
            .is_err()
    );
    assert_eq!(
        f.store
            .db
            .query_row(
                "SELECT count(*) FROM authorization_mutations WHERE actor=?1",
                [&f.alice.actor_id],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
        1
    );
}
#[test]
fn authorization_audit_or_policy_storage_failure_denies_every_mutation_atomically() {
    let mut f = Fixture::new();
    f.role(&f.alice.clone(), Role::Administrator);
    let source = f.save("select 1", None);
    f.grant(&f.alice.clone(), Grant::Execute, None, None);
    let before = f.revision();
    f.store.db.execute_batch("CREATE TRIGGER reject_authorization_audit BEFORE INSERT ON authorization_audit BEGIN SELECT RAISE(ABORT,'audit unavailable'); END;").unwrap();
    assert!(f.admit(&f.alice.clone(), &source, None).is_err());
    assert!(
        f.store
            .authorized_command(
                &f.alice,
                Command::SaveSource {
                    deployment: f.one.clone(),
                    asset: "denied".into(),
                    kind: "sql".into(),
                    contents: "select 1".into(),
                    expected_head: None,
                    expected_policy: before,
                    key: "failure".into()
                }
            )
            .is_err()
    );
    assert!(
        f.store
            .authorized_command(
                &f.alice,
                Command::SetRole {
                    deployment: f.one.clone(),
                    subject: Subject::Principal(f.bob.actor_id.clone()),
                    role: Some(Role::Administrator),
                    expected_policy: before,
                    key: "role-failure".into()
                }
            )
            .is_err()
    );
    assert_eq!(f.revision(), before);
    assert_eq!(role(&f.store.db, &f.bob.actor_id, &f.one).unwrap(), 0);
    assert_eq!(
        f.store
            .db
            .query_row("SELECT count(*) FROM authorization_executions", [], |r| r
                .get::<_, i64>(
                0
            ))
            .unwrap(),
        0
    );
    assert_eq!(
        f.store
            .db
            .query_row("SELECT count(*) FROM authorization_sources", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        1
    );
    f.store.db.execute_batch("DROP TRIGGER reject_authorization_audit; CREATE TRIGGER reject_policy BEFORE UPDATE ON authorization_policy BEGIN SELECT RAISE(ABORT,'policy unavailable'); END;").unwrap();
    assert!(
        f.store
            .authorization_admin(AdminCommand::SetRole {
                deployment: f.one.clone(),
                subject: Subject::Principal(f.bob.actor_id.clone()),
                role: Some(Role::Viewer),
                expected_policy: before,
                key: "policy-failure".into()
            })
            .is_err()
    );
    assert_eq!(role(&f.store.db, &f.bob.actor_id, &f.one).unwrap(), 0);
}
#[test]
fn authorization_group_changes_invalidate_plans_and_revocation_survives_restart() {
    let mut f = Fixture::new();
    let group = f
        .store
        .identity_admin(crate::identity::AdminCommand::Group {
            label: "authors".into(),
        })
        .unwrap()["group_id"]
        .as_str()
        .unwrap()
        .to_owned();
    f.store
        .authorization_admin(AdminCommand::SetRole {
            deployment: f.one.clone(),
            subject: Subject::Group(group.clone()),
            role: Some(Role::Editor),
            expected_policy: f.revision(),
            key: "group-role".into(),
        })
        .unwrap();
    assert!(
        f.store
            .authorized_command(
                &f.alice,
                Command::Project {
                    deployment: f.one.clone()
                }
            )
            .is_err()
    );
    let revision = f.revision();
    f.store
        .identity_admin(crate::identity::AdminCommand::Membership {
            group: group.clone(),
            principal: f.alice.actor_id.clone(),
            present: true,
        })
        .unwrap();
    assert!(f.revision() > revision);
    f.save("select 1", None);
    f.store
        .identity_admin(crate::identity::AdminCommand::Membership {
            group,
            principal: f.alice.actor_id.clone(),
            present: false,
        })
        .unwrap();
    let path = f.store.root().to_owned();
    drop(f.store);
    let mut reopened = Store::open(&path).unwrap();
    assert!(
        reopened
            .authorized_command(&f.alice, Command::Sources { deployment: f.one })
            .is_err()
    );
}
#[test]
fn authorization_wire_contract_denies_unknown_actions_and_client_selected_context() {
    assert!(serde_json::from_value::<Command>(json!({"action":"new_unguarded_action"})).is_err());
    assert!(
        serde_json::from_value::<Command>(json!({"action":"projects","actor_id":"owner"})).is_err()
    );
    assert!(serde_json::from_value::<auth::Envelope>(json!({"api_version":1,"token":"x","channel":"cli","csrf":null,"command":{"action":"projects"},"effective_principal_id":"owner"})).is_err());
    let mut f = Fixture::new();
    let principal = f.service.actor_id.clone();
    let credential = f
        .store
        .identity_admin(crate::identity::AdminCommand::IssueService {
            principal,
            scopes: vec![identity::SELF_SCOPE.into()],
            ttl_seconds: 60,
        })
        .unwrap();
    let job = f
        .store
        .authorization_job(auth::Envelope {
            api_version: 1,
            token: credential["token"].as_str().unwrap().into(),
            channel: identity::Channel::Service,
            csrf: None,
            command: Command::Projects {},
        })
        .unwrap();
    assert!(job().unwrap()(&mut f.store).is_err());
}

#[test]
fn authorization_disabled_service_approval_can_be_revoked_and_receipts_do_not_restore_it() {
    let mut f = Fixture::new();
    f.role(&f.alice.clone(), Role::Editor);
    f.role(&f.service.clone(), Role::Viewer);
    let source = f.save("select 1", None);
    f.grant(&f.alice.clone(), Grant::Execute, None, None);
    f.grant(&f.service.clone(), Grant::Execute, None, None);
    f.grant(
        &f.alice.clone(),
        Grant::ActAs,
        Some(&f.service.clone()),
        Some(&source),
    );
    let command = Command::AdmitExecution {
        deployment: f.one.clone(),
        source_revision: source.clone(),
        effective_principal: Some(f.service.actor_id.clone()),
        expected_policy: f.revision(),
        key: "admit-once".into(),
    };
    f.store
        .authorized_command(&f.alice, command.clone())
        .unwrap();
    f.store
        .identity_admin(crate::identity::AdminCommand::Disable {
            principal: f.service.actor_id.clone(),
            disabled: true,
        })
        .unwrap();
    assert!(
        f.store
            .authorized_command(&f.alice, command.clone())
            .is_err()
    );
    f.store
        .authorization_admin(AdminCommand::SetGrant {
            deployment: f.one.clone(),
            subject: Subject::Principal(f.alice.actor_id.clone()),
            grant: Grant::ActAs,
            effective_principal: Some(f.service.actor_id.clone()),
            source_revision: Some(source),
            present: false,
            expected_policy: f.revision(),
            key: "revoke-disabled-service".into(),
        })
        .unwrap();
    f.store
        .identity_admin(crate::identity::AdminCommand::Disable {
            principal: f.service.actor_id.clone(),
            disabled: false,
        })
        .unwrap();
    assert!(f.store.authorized_command(&f.alice, command).is_err());
    assert_eq!(
        f.store
            .db
            .query_row("SELECT count(*) FROM authorization_executions", [], |r| r
                .get::<_, i64>(
                0
            ))
            .unwrap(),
        1
    );
}
