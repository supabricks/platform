use super::*;
use crate::project::ProjectConfig;
use std::os::unix::fs::symlink;

struct Fixture {
    _tmp: tempfile::TempDir,
    store: Store,
    manager: Manager,
    binding: Binding,
    package: Package,
}
impl Fixture {
    fn new() -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let project = tmp.path().join("project");
        fs::create_dir(&project).unwrap();
        // macOS temporary paths can traverse /var -> /private/var. Production
        // bindings are canonical; the fixture must supply that same contract.
        let project = project.canonicalize().unwrap();
        let config = ProjectConfig::initialize(&project, "example").unwrap();
        let mut store = Store::open(&tmp.path().join("data")).unwrap();
        store.register_project(&config).unwrap();
        let root = tmp.path().join("package");
        fs::create_dir_all(root.join("python/analytics")).unwrap();
        fs::create_dir_all(root.join("python/notebooks")).unwrap();
        fs::write(root.join("python/analytics/export.py"), "").unwrap();
        supervisor::write_json(
            &store.root().join("analytics.json"),
            &json!({"python":"/bin/sh","worker":root.join("python/analytics/export.py")}),
        )
        .unwrap();
        let mut files = BTreeMap::new();
        fs::create_dir_all(root.join("python/runtime/bin")).unwrap();
        fs::write(
            root.join("python/runtime/bin/python3.12"),
            b"qualified interpreter",
        )
        .unwrap();
        files.insert(
            "python/runtime/bin/python3.12".into(),
            hash(b"qualified interpreter"),
        );
        for (name, bytes) in [
            ("pyproject.toml", b"manifest".as_slice()),
            ("uv.lock", b"lock"),
            ("requirements.lock", b"requirements"),
        ] {
            fs::write(root.join("python/notebooks").join(name), bytes).unwrap();
            files.insert(format!("python/notebooks/{name}"), hash(bytes));
        }
        supervisor::write_json(&root.join("python/notebooks/kernel-contract.json"),&json!({"version":1,"target":if cfg!(target_os="linux"){"linux-x86_64"}else{"macos-arm64"},"python_version":"3.12.13","files":files,
            "templates":{"base":{"manifest":"python/notebooks/pyproject.toml","lock":"python/notebooks/uv.lock","requirements":"python/notebooks/requirements.lock","inputs":{"manifest":hash(b"manifest"),"lock":hash(b"lock")},"packages":{}}}})).unwrap();
        let package = Package::load(&store).unwrap();
        Self {
            _tmp: tmp,
            store,
            manager: Manager::default(),
            binding: Binding {
                project_id: config.id,
                worktree: project,
            },
            package,
        }
    }
    fn initialize(&mut self) -> Operation {
        let v = self
            .manager
            .handle(
                &mut self.store,
                &self.binding,
                Command::Initialize {
                    key: "init".into(),
                    template: "base".into(),
                },
            )
            .unwrap();
        self.manager.tick(&mut self.store, false).unwrap();
        self.store
            .environment_operations()
            .unwrap()
            .into_iter()
            .find(|o| json!(o.id) == v["id"])
            .unwrap()
    }
    fn ready(&mut self, key: &str) -> Generation {
        let inputs = self.package.template("base").unwrap().inputs.clone();
        let path = self.store.root().join("notebook-environments");
        files::directory(&path).unwrap();
        let wk = hash(
            json!([self.binding.project_id, self.binding.worktree])
                .to_string()
                .as_bytes(),
        );
        let path = path.join(&wk);
        files::directory(&path).unwrap();
        let id = OperationId::new();
        let path = path.join(id.to_string());
        let directory = files::directory(&path).unwrap();
        fs::write(path.join("package.py"), b"original").unwrap();
        let mut g = Generation {
            id,
            project: self.binding.project_id,
            worktree: self.binding.worktree.clone(),
            worktree_key: wk,
            path: path.clone(),
            directory,
            inputs: inputs.clone(),
            contract: self.package.identity.clone(),
            interpreter: self.package.root.join("python/runtime/bin/python3.12"),
            installation: "source".into(),
            state: "building".into(),
            inventory: Some(files::inventory(&path).unwrap()),
        };
        let mut o = Operation {
            id: OperationId::new(),
            project: g.project,
            worktree: g.worktree.clone(),
            key: key.into(),
            state: "preparing".into(),
            kind: "prepare".into(),
            inputs,
            template: "base".into(),
            contract: g.contract.clone(),
            generation: Some(id),
            created: now(),
            started: Some(now()),
            worker: None,
            error: None,
            cancel: false,
            workflow: None,
            result: None,
            publication: None,
        };
        self.store.save_environment_generation(&g).unwrap();
        self.store.save_environment_operation(&o).unwrap();
        g.state = "ready".into();
        o.state = "ready".into();
        self.store.publish_environment(&o, &g).unwrap();
        g
    }
}

#[test]
fn worktree_alias_does_not_create_a_second_operation_namespace() {
    let mut f = Fixture::new();
    f.initialize();
    let alias = f._tmp.path().join("alias");
    symlink(&f.binding.worktree, &alias).unwrap();
    let binding = Binding {
        worktree: alias,
        ..f.binding.clone()
    };
    assert!(
        f.manager
            .handle(&mut f.store, &binding, Command::Inspect)
            .is_err()
    );
    assert_eq!(f.store.environment_operations().unwrap().len(), 1);
}

#[test]
fn initialize_atomically_preserves_existing_pairs_and_recovers_published_pairs() {
    let mut f = Fixture::new();
    let operation = f.initialize();
    assert_eq!(operation.state, "ready");
    let expected = files::inputs(&f.binding.worktree).unwrap();
    assert_eq!(expected, operation.inputs);
    let repeated = f
        .manager
        .handle(
            &mut f.store,
            &f.binding,
            Command::Initialize {
                key: "init".into(),
                template: "base".into(),
            },
        )
        .unwrap();
    assert_eq!(repeated["id"], json!(operation.id));
    let mut interrupted = operation.clone();
    interrupted.state = "initializing".into();
    f.store.save_environment_operation(&interrupted).unwrap();
    Manager::recover(&mut f.store).unwrap();
    assert_eq!(f.store.environment_operations().unwrap()[0].state, "ready");
    fs::write(
        f.binding.worktree.join("notebooks/environment/uv.lock"),
        b"user edit",
    )
    .unwrap();
    f.manager
        .handle(
            &mut f.store,
            &f.binding,
            Command::Initialize {
                key: "conflict".into(),
                template: "base".into(),
            },
        )
        .unwrap();
    f.manager.tick(&mut f.store, false).unwrap();
    assert_eq!(
        fs::read(f.binding.worktree.join("notebooks/environment/uv.lock")).unwrap(),
        b"user edit"
    );
    assert_eq!(
        f.store
            .environment_operations()
            .unwrap()
            .last()
            .unwrap()
            .state,
        "failed"
    );
}

#[test]
fn interrupted_pair_staging_never_overwrites_a_project_file() {
    let mut f = Fixture::new();
    f.manager
        .handle(
            &mut f.store,
            &f.binding,
            Command::Initialize {
                key: "pending".into(),
                template: "base".into(),
            },
        )
        .unwrap();
    let o = f.store.environment_operations().unwrap().pop().unwrap();
    let stage = f
        .binding
        .worktree
        .join("notebooks")
        .join(format!(".environment-{}", o.id));
    fs::create_dir_all(&stage).unwrap();
    fs::write(stage.join("pyproject.toml"), b"manifest").unwrap();
    files::initialize(&o, f.package.template("base").unwrap(), &f.package).unwrap();
    assert_eq!(files::inputs(&o.worktree).unwrap(), o.inputs);
    assert!(!stage.exists());
}

#[test]
fn leases_and_active_pointer_exclude_collection_and_substituted_roots_are_quarantined() {
    let mut f = Fixture::new();
    f.initialize();
    let old = f.ready("old");
    let lease = f
        .manager
        .acquire(&mut f.store, &f.binding, old.id, "kernel-a")
        .unwrap();
    assert_eq!(
        lease,
        f.manager
            .acquire(&mut f.store, &f.binding, old.id, "kernel-a")
            .unwrap()
    );
    let active = f.ready("new");
    assert!(f.manager.collect(&mut f.store, None).unwrap().is_empty());
    f.manager.release(&f.store, lease).unwrap();
    assert_eq!(f.manager.collect(&mut f.store, None).unwrap(), vec![old.id]);
    assert!(!old.path.exists());
    assert!(active.path.exists());
    assert!(
        f.manager
            .acquire(&mut f.store, &f.binding, old.id, "kernel-b")
            .is_err()
    );
    let moved = active.path.with_extension("moved");
    fs::rename(&active.path, &moved).unwrap();
    let external = f._tmp.path().join("external");
    fs::create_dir(&external).unwrap();
    fs::write(external.join("keep"), b"external").unwrap();
    symlink(&external, &active.path).unwrap();
    assert!(
        f.manager
            .acquire(&mut f.store, &f.binding, active.id, "kernel-b")
            .is_err()
    );
    assert!(f.manager.collect(&mut f.store, None).unwrap().is_empty());
    assert_eq!(fs::read(external.join("keep")).unwrap(), b"external");
    assert!(moved.join("package.py").exists());
}

#[test]
fn stale_inputs_cancellation_and_disk_admission_keep_the_previous_generation() {
    let mut f = Fixture::new();
    f.initialize();
    let previous = f.ready("previous");
    let inputs = previous.inputs.clone();
    f.manager.limits.admission_free = 0;
    let request = Command::Prepare {
        key: "prepare".into(),
        expected: inputs.clone(),
    };
    let result = f
        .manager
        .handle(&mut f.store, &f.binding, request.clone())
        .unwrap();
    assert_eq!(
        f.manager.handle(&mut f.store, &f.binding, request).unwrap()["id"],
        result["id"]
    );
    assert!(
        f.manager
            .handle(
                &mut f.store,
                &f.binding,
                Command::Prepare {
                    key: "racing".into(),
                    expected: inputs.clone()
                }
            )
            .is_err()
    );
    fs::write(
        f.binding.worktree.join("notebooks/environment/uv.lock"),
        b"changed",
    )
    .unwrap();
    f.manager.tick(&mut f.store, false).unwrap();
    assert_eq!(
        f.store
            .active_environment(f.binding.project_id, &f.binding.worktree)
            .unwrap(),
        Some(previous.id)
    );
    fs::write(
        f.binding.worktree.join("notebooks/environment/uv.lock"),
        b"lock",
    )
    .unwrap();
    let v = f
        .manager
        .handle(
            &mut f.store,
            &f.binding,
            Command::Prepare {
                key: "cancel".into(),
                expected: inputs.clone(),
            },
        )
        .unwrap();
    let id = serde_json::from_value(v["id"].clone()).unwrap();
    f.manager
        .handle(&mut f.store, &f.binding, Command::Cancel { id })
        .unwrap();
    f.manager.tick(&mut f.store, false).unwrap();
    assert_eq!(
        f.manager
            .handle(&mut f.store, &f.binding, Command::Status { id })
            .unwrap()["state"],
        "cancelled"
    );
    f.manager.limits.admission_free = u64::MAX;
    assert!(
        f.manager
            .handle(
                &mut f.store,
                &f.binding,
                Command::Prepare {
                    key: "full".into(),
                    expected: inputs
                }
            )
            .is_err()
    );
    assert_eq!(
        f.store
            .active_environment(f.binding.project_id, &f.binding.worktree)
            .unwrap(),
        Some(previous.id)
    );
}

#[test]
fn ready_report_without_committed_activation_remains_invalid_after_crash() {
    let mut f = Fixture::new();
    f.initialize();
    let active = f.ready("active");
    let mut partial = active.clone();
    partial.id = OperationId::new();
    partial.state = "building".into();
    partial.path = active.path.parent().unwrap().join(partial.id.to_string());
    partial.directory = files::directory(&partial.path).unwrap();
    fs::write(partial.path.join("ready.json"), b"verified").unwrap();
    f.store.save_environment_generation(&partial).unwrap();
    Manager::recover(&mut f.store).unwrap();
    assert_eq!(
        f.store
            .active_environment(f.binding.project_id, &f.binding.worktree)
            .unwrap(),
        Some(active.id)
    );
    assert_eq!(
        f.store
            .environment_generations()
            .unwrap()
            .into_iter()
            .find(|g| g.id == partial.id)
            .unwrap()
            .state,
        "invalid"
    );
}

#[test]
fn collection_resumes_after_tree_removal_before_catalog_commit() {
    let mut f = Fixture::new();
    f.initialize();
    let mut old = f.ready("old");
    let active = f.ready("active");
    old.state = "deleting".into();
    assert!(f.store.collect_environment(&old).unwrap());
    fs::remove_dir_all(&old.path).unwrap();
    Manager::recover(&mut f.store).unwrap();
    assert_eq!(f.manager.collect(&mut f.store, None).unwrap(), vec![old.id]);
    assert!(active.path.exists());
    assert!(f.manager.collect(&mut f.store, None).unwrap().is_empty());
}

#[test]
fn project_declarations_reject_symlinks_and_incomplete_pairs() {
    let tmp = tempfile::tempdir().unwrap();
    let dir = tmp.path().join("notebooks/environment");
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join("pyproject.toml"), "manifest").unwrap();
    assert!(files::inputs(tmp.path()).is_err());
    fs::write(dir.join("uv.lock"), "lock").unwrap();
    let original = files::inputs(tmp.path()).unwrap();
    fs::rename(dir.join("uv.lock"), tmp.path().join("external")).unwrap();
    symlink(tmp.path().join("external"), dir.join("uv.lock")).unwrap();
    assert!(files::inputs(tmp.path()).is_err());
    assert_eq!(fs::read(tmp.path().join("external")).unwrap(), b"lock");
    assert_eq!(original.lock, hash(b"lock"));
}

#[test]
fn recovery_fences_every_partial_stage_and_keeps_idempotency_records() {
    for stage in ["queued", "initializing", "preparing", "verifying"] {
        let tmp = tempfile::tempdir().unwrap();
        let project = tmp.path().join("project");
        fs::create_dir(&project).unwrap();
        let config = ProjectConfig::initialize(&project, "example").unwrap();
        let root = tmp.path().join("data");
        let mut store = Store::open(&root).unwrap();
        store.register_project(&config).unwrap();
        let operation = Operation {
            id: OperationId::new(),
            project: config.id,
            worktree: project.clone(),
            key: "retry".into(),
            state: stage.into(),
            kind: "prepare".into(),
            inputs: Inputs {
                manifest: hash(b"m"),
                lock: hash(b"l"),
            },
            template: "base".into(),
            contract: hash(b"c"),
            generation: None,
            created: now(),
            started: None,
            worker: None,
            error: None,
            cancel: false,
            workflow: None,
            result: None,
            publication: None,
        };
        store.save_environment_operation(&operation).unwrap();
        drop(store);
        let mut store = Store::open(&root).unwrap();
        Manager::recover(&mut store).unwrap();
        let recovered = store.environment_operations().unwrap().pop().unwrap();
        assert_eq!(recovered.id, operation.id);
        assert_eq!(recovered.state, "failed");
        assert_eq!(store.active_environment(config.id, &project).unwrap(), None);
    }
}

#[test]
fn kernel_selection_fences_drift_and_keeps_old_generations_after_declaration_changes() {
    let mut f = Fixture::new();
    f.initialize();
    let old = f.ready("old");
    let selected = Manager::select(&mut f.store, &f.binding, old.id).unwrap();
    assert_eq!(selected.python, old.path.join("bin/python"));
    fs::write(
        f.binding.worktree.join("notebooks/environment/uv.lock"),
        b"new declaration",
    )
    .unwrap();
    assert!(Manager::declarations_changed(
        &f.store,
        &f.binding,
        &selected.identity
    ));
    assert_eq!(
        Manager::select(&mut f.store, &f.binding, old.id)
            .unwrap()
            .identity,
        selected.identity
    );
    fs::write(old.path.join("package.py"), b"drift").unwrap();
    assert!(Manager::select(&mut f.store, &f.binding, old.id).is_err());
    fs::write(old.path.join("package.py"), b"original").unwrap();
    let mut wrong = f.binding.clone();
    wrong.project_id = ProjectId::new();
    assert!(Manager::select(&mut f.store, &wrong, old.id).is_err());
    fs::remove_dir_all(&old.path).unwrap();
    assert!(Manager::select(&mut f.store, &f.binding, old.id).is_err());
}

#[test]
fn inventory_matches_canonical_worker_format_and_rejects_hardlinks() {
    let tmp = tempfile::tempdir().unwrap();
    fs::write(tmp.path().join("a"), b"abc").unwrap();
    symlink("a", tmp.path().join("link")).unwrap();
    assert_eq!(
        files::inventory(tmp.path()).unwrap(),
        hash(format!("{{\"a\":\"{}\",\"link\":{{\"link\":\"a\"}}}}", hash(b"abc")).as_bytes())
    );
    fs::hard_link(tmp.path().join("a"), tmp.path().join("b")).unwrap();
    assert!(files::inventory(tmp.path()).is_err());
}

#[test]
fn managed_requests_fence_revisions_and_replay_original_parameters() {
    let mut f = Fixture::new();
    let initialized = f.initialize();
    let request = Command::Manage {
        key: "add".into(),
        expected: initialized.inputs.clone(),
        change: Change::Add {
            requirement: "humanize==4.13.0".into(),
        },
        offline: true,
    };
    let first = f
        .manager
        .handle(&mut f.store, &f.binding, request.clone())
        .unwrap();
    fs::write(
        f.binding.worktree.join("notebooks/environment/uv.lock"),
        b"external edit",
    )
    .unwrap();
    assert_eq!(
        f.manager.handle(&mut f.store, &f.binding, request).unwrap()["id"],
        first["id"]
    );
    assert!(
        f.manager
            .handle(
                &mut f.store,
                &f.binding,
                Command::Manage {
                    key: "add".into(),
                    expected: initialized.inputs,
                    change: Change::Sync,
                    offline: true
                }
            )
            .is_err()
    );
    f.manager.tick(&mut f.store, false).unwrap();
    assert_eq!(
        f.manager
            .handle(
                &mut f.store,
                &f.binding,
                Command::Status {
                    id: serde_json::from_value(first["id"].clone()).unwrap()
                }
            )
            .unwrap()["state"],
        "failed"
    );
    assert_eq!(
        fs::read(f.binding.worktree.join("notebooks/environment/uv.lock")).unwrap(),
        b"external edit"
    );
}

#[test]
fn publication_swaps_the_complete_pair_and_preserves_previous_revision() {
    let mut f = Fixture::new();
    let mut o = f.initialize();
    let root = Manager::operation_dir(&f.store, o.id).unwrap();
    let docs = root.join("documents");
    fs::create_dir(&docs).unwrap();
    fs::write(docs.join("pyproject.toml"), b"new manifest").unwrap();
    fs::write(docs.join("uv.lock"), b"new lock").unwrap();
    o.publication = Some(transaction::result_inputs(&root).unwrap());
    f.store.save_environment_operation(&o).unwrap();
    transaction::publish(&o, &root).unwrap();
    assert_eq!(
        files::inputs(&o.worktree).unwrap(),
        o.publication.clone().unwrap()
    );
    let old = PathBuf::from(format!("notebooks/.environment-{}", o.id));
    let (manifest, lock) = transaction::declaration(&o.worktree, &old).unwrap();
    assert_eq!(
        Inputs {
            manifest: hash(&manifest),
            lock: hash(&lock)
        },
        o.inputs
    );
    // Simulate crash after the pair exchange, before activation. Recovery never
    // treats the report or pair alone as permission to activate a generation.
    o.state = "verifying".into();
    o.kind = "manage".into();
    o.workflow = Some(Workflow {
        change: Change::Sync,
        offline: true,
    });
    fs::create_dir(docs.join("wheels")).unwrap();
    fs::write(docs.join("wheels/transfer.whl"), b"temporary").unwrap();
    f.store.save_environment_operation(&o).unwrap();
    Manager::recover(&mut f.store).unwrap();
    assert!(!docs.join("wheels").exists());
    assert!(docs.join("uv.lock").exists());
    assert!(
        f.store
            .active_environment(o.project, &o.worktree)
            .unwrap()
            .is_none()
    );
    assert_eq!(files::inputs(&o.worktree).unwrap(), o.publication.unwrap());
    assert_eq!(
        f.store
            .environment_operations()
            .unwrap()
            .last()
            .unwrap()
            .state,
        "failed"
    );
}

#[test]
fn publication_rejects_edits_and_unrelated_files_and_adoption_cannot_escape() {
    let mut f = Fixture::new();
    let mut o = f.initialize();
    let root = Manager::operation_dir(&f.store, o.id).unwrap();
    let workflow = Workflow {
        change: Change::Sync,
        offline: true,
    };
    transaction::snapshot(&o, &workflow, &root).unwrap();
    fs::write(root.join("documents/uv.lock"), b"new").unwrap();
    o.publication = Some(transaction::result_inputs(&root).unwrap());
    fs::write(o.worktree.join("notebooks/environment/keep"), b"mine").unwrap();
    assert!(transaction::publish(&o, &root).is_err());
    fs::remove_file(o.worktree.join("notebooks/environment/keep")).unwrap();
    fs::write(o.worktree.join("notebooks/environment/uv.lock"), b"edited").unwrap();
    assert!(transaction::publish(&o, &root).is_err());
    assert_eq!(
        fs::read(o.worktree.join("notebooks/environment/uv.lock")).unwrap(),
        b"edited"
    );
    assert!(
        transaction::declaration(&o.worktree, Path::new("../project/notebooks/environment"))
            .is_err()
    );
    symlink(
        o.worktree.join("notebooks/environment"),
        o.worktree.join("alias"),
    )
    .unwrap();
    assert!(transaction::declaration(&o.worktree, Path::new("alias")).is_err());
}

#[test]
fn explicit_adoption_can_read_project_root_without_following_a_virtualenv() {
    let f = Fixture::new();
    fs::write(f.binding.worktree.join("pyproject.toml"), b"root manifest").unwrap();
    fs::write(f.binding.worktree.join("uv.lock"), b"root lock").unwrap();
    symlink("/unrelated", f.binding.worktree.join(".venv")).unwrap();
    assert_eq!(
        transaction::declaration(&f.binding.worktree, Path::new(".")).unwrap(),
        (b"root manifest".to_vec(), b"root lock".to_vec())
    );
}

#[test]
fn console_inspection_reports_only_bound_generations_and_recorded_packages() {
    let mut f = Fixture::new();
    f.initialize();
    let g = f.ready("display");
    let operations = f.store.environment_operations().unwrap();
    let mut operation = operations
        .into_iter()
        .find(|o| o.generation == Some(g.id))
        .unwrap();
    operation.result = Some(json!({"packages":{"example":"1.2.3"}}));
    f.store.save_environment_operation(&operation).unwrap();
    let inspect = f
        .manager
        .handle(&mut f.store, &f.binding, Command::Inspect)
        .unwrap();
    assert_eq!(inspect["environments"][0]["packages"]["example"], "1.2.3");
    assert_eq!(inspect["python_version"], "3.12.13");
    let mut other = f.binding.clone();
    other.project_id = ProjectId::new();
    assert!(
        status::generations(
            &f.store,
            &other,
            &f.package,
            &f.store.environment_operations().unwrap()
        )
        .unwrap()
        .is_empty()
    );
    assert!(
        f.manager
            .handle(&mut f.store, &other, Command::Status { id: operation.id })
            .is_err()
    );
    // Package status reads recorded inventory; it never imports arbitrary files.
    fs::write(
        g.path.join("package.py"),
        "raise RuntimeError('do not execute')",
    )
    .unwrap();
    assert_eq!(
        f.manager
            .handle(&mut f.store, &f.binding, Command::Inspect)
            .unwrap()["environments"][0]["packages"]["example"],
        "1.2.3"
    );
    assert!(Manager::select(&mut f.store, &f.binding, g.id).is_err());
}
