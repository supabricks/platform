use super::*;
use std::os::unix::fs::{PermissionsExt, symlink};

fn external(root: &Path, endpoint: &str) -> Provider {
    let token_file = root.join("token");
    supervisor::write_private(&token_file, b"private-test-service-token").unwrap();
    Provider::External {
        endpoint: endpoint.into(),
        token_file,
        ca_file: None,
        metastore_id: "00000000-0000-4000-8000-000000000001".into(),
    }
}

#[test]
fn external_configuration_rejects_credential_urls_plaintext_remote_and_unsafe_secrets() {
    let dir = tempfile::tempdir().unwrap();
    for url in [
        "http://uc.example",
        "https://user:secret@uc.example",
        "https://uc.example/path",
        "https://uc.example/?token=secret",
        "https://uc.example/#fragment",
    ] {
        assert!(
            config::validate(&external(dir.path(), url)).is_err(),
            "{url}"
        );
    }
    assert!(config::validate(&external(dir.path(), "https://uc.example:443")).is_ok());
    assert!(config::validate(&external(dir.path(), "http://127.0.0.1:1234")).is_ok());
    let provider = external(dir.path(), "https://uc.example");
    fs::set_permissions(dir.path().join("token"), fs::Permissions::from_mode(0o644)).unwrap();
    assert!(config::validate(&provider).is_err());
    fs::remove_file(dir.path().join("token")).unwrap();
    supervisor::write_private(&dir.path().join("target"), b"private-test-service-token").unwrap();
    symlink(dir.path().join("target"), dir.path().join("token")).unwrap();
    assert!(config::validate(&provider).is_err());
}

#[test]
fn stale_catalog_pid_blocks_only_catalog_and_is_never_signalled() {
    let dir = tempfile::tempdir().unwrap();
    let mut store = Store::open(&dir.path().join("data")).unwrap();
    store
        .record_native_process(&supervisor::OwnedProcess {
            root: store.root().into(),
            generation: store.generation(),
            role: ROLE.into(),
            pid: std::process::id(),
            start_identity: "not-this-process".into(),
            token: "not-this-process".into(),
            branch: None,
        })
        .unwrap();
    let mut manager = Manager::recover(&mut store);
    assert_eq!(manager.status()["error"], "ownership_recovery_failed");
    assert_eq!(store.native_processes().unwrap().len(), 1);
    assert!(manager.tick(&mut store, false));
    assert!(!manager.tick(&mut store, true));
    assert!(
        supervisor::os::identity(std::process::id())
            .unwrap()
            .is_some()
    );
}

#[test]
fn local_provider_identity_survives_reconfiguration() {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(&dir.path().join("data")).unwrap();
    let one = config::new(&store, Provider::Local { runtime: None }).unwrap();
    let two = config::new(&store, Provider::Local { runtime: None }).unwrap();
    assert_eq!(one.provider_id, two.provider_id);
}

#[test]
fn local_identity_and_metastore_loss_fail_closed() {
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(&dir.path().join("data")).unwrap();
    let configured = config::new(&store, Provider::Local { runtime: None }).unwrap();
    supervisor::write_json(&store.root().join("catalog-provider.json"), &configured).unwrap();
    let root = runtime::data_directory(&store).unwrap();
    let mut identity = config::local_identity(&store).unwrap();
    identity.metastore_id = Some(supabricks_core::resource::ProjectId::new().to_string());
    supervisor::write_json(&store.root().join("catalog-local.json"), &identity).unwrap();
    fs::remove_dir_all(root).unwrap();
    assert!(runtime::data_directory(&store).is_err());
    assert_eq!(
        config::load(&store).unwrap().unwrap().provider_id,
        configured.provider_id
    );
    fs::remove_file(store.root().join("catalog-local.json")).unwrap();
    assert!(config::load(&store).is_err());
    assert!(config::new(&store, Provider::Local { runtime: None }).is_err());
}

#[test]
fn catalog_paths_are_checked_before_launch_mutations() {
    use std::os::unix::fs::symlink;
    let dir = tempfile::tempdir().unwrap();
    let store = Store::open(&dir.path().join("data")).unwrap();
    config::new(&store, Provider::Local { runtime: None }).unwrap();
    let root = runtime::data_directory(&store).unwrap();
    let target = dir.path().join("unrelated");
    fs::write(&target, b"untouched").unwrap();
    fs::remove_file(root.join("process.log")).unwrap();
    symlink(&target, root.join("process.log")).unwrap();
    assert!(runtime::data_directory(&store).is_err());
    assert_eq!(fs::read(&target).unwrap(), b"untouched");
    fs::remove_file(root.join("process.log")).unwrap();
    fs::remove_dir(root.join("etc/conf")).unwrap();
    symlink(dir.path(), root.join("etc/conf")).unwrap();
    assert!(runtime::data_directory(&store).is_err());
}

fn probe_fixture(
    anonymous_status: u16,
    identity: &str,
) -> std::result::Result<http::Health, &'static str> {
    let dir = tempfile::tempdir().unwrap();
    supervisor::write_private(&dir.path().join("token"), b"private-test-service-token").unwrap();
    let server = tiny_http::Server::http("127.0.0.1:0").unwrap();
    let endpoint = format!("http://{}", server.server_addr());
    let identity = identity.to_owned();
    let thread = std::thread::spawn(move || {
        let requests = if anonymous_status == 200 { 1 } else { 3 };
        for index in 0..requests {
            let req = server
                .recv_timeout(Duration::from_secs(3))
                .unwrap()
                .unwrap();
            let auth = req
                .headers()
                .iter()
                .find(|h| h.field.equiv("Authorization"));
            if index == 0 {
                assert!(auth.is_none());
            } else {
                assert_eq!(
                    auth.unwrap().value.as_str(),
                    "Bearer private-test-service-token"
                );
            }
            let body = if index == 2 {
                json!({"metastore_id":identity})
            } else {
                json!({"catalogs":[]})
            };
            req.respond(
                tiny_http::Response::from_string(body.to_string())
                    .with_status_code(if index == 0 { anonymous_status } else { 200 }),
            )
            .unwrap();
        }
    });
    let result = http::Probe {
        endpoint,
        token_file: dir.path().join("token"),
        ca_file: None,
        expected_metastore: Some("00000000-0000-4000-8000-000000000001".into()),
    }
    .run();
    thread.join().unwrap();
    result
}

#[test]
fn readiness_requires_authentication_and_expected_metastore() {
    assert_eq!(
        probe_fixture(200, "").err(),
        Some("authentication_not_enforced")
    );
    assert_eq!(
        probe_fixture(401, "00000000-0000-4000-8000-000000000002").err(),
        Some("metastore_identity_changed")
    );
    assert!(probe_fixture(401, "00000000-0000-4000-8000-000000000001").is_ok());
}
