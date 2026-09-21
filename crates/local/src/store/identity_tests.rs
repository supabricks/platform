use super::*;
use crate::identity::{AdminCommand as Admin, AuthCommand as Auth, Channel};
use std::{
    io::{BufRead, BufReader},
    process::{Child, Command, Stdio},
};

struct Fixture {
    dir: tempfile::TempDir,
    store: Store,
}
impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let store = Store::open(&dir.path().join("state")).unwrap();
        Self { dir, store }
    }
    fn reopen(&mut self) {
        let path = self.store.root().to_owned();
        let replacement = Store::open(&self.dir.path().join("unused")).unwrap();
        drop(std::mem::replace(&mut self.store, replacement));
        self.store = Store::open(&path).unwrap();
    }
    fn service(&mut self) -> String {
        self.store
            .identity_admin(Admin::Service {
                label: "worker".into(),
            })
            .unwrap()["principal_id"]
            .as_str()
            .unwrap()
            .into()
    }
    fn issue(&mut self, id: &str) -> String {
        self.store
            .identity_admin(Admin::IssueService {
                principal: id.into(),
                scopes: vec![iam::SELF_SCOPE.into()],
                ttl_seconds: 300,
            })
            .unwrap()["token"]
            .as_str()
            .unwrap()
            .into()
    }
}
struct Provider {
    child: Child,
    config: oidc::Config,
    http: reqwest::blocking::Client,
}
impl Provider {
    fn new() -> Self {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/identity");
        let mut child = Command::new("python3")
            .arg(path.join("provider.py"))
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let mut issuer = String::new();
        BufReader::new(child.stdout.take().unwrap())
            .read_line(&mut issuer)
            .unwrap();
        let issuer = issuer.trim().to_owned();
        assert!(issuer.starts_with("https://127.0.0.1:"));
        let pem = std::fs::read_to_string(path.join("cert.pem")).unwrap();
        let http = reqwest::blocking::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(std::time::Duration::from_secs(5))
            .add_root_certificate(reqwest::Certificate::from_pem(pem.as_bytes()).unwrap())
            .build()
            .unwrap();
        let config = oidc::Config {
            issuer: issuer.clone(),
            client_id: "platform".into(),
            client_secret: "fixture-secret".into(),
            introspection_url: format!("{issuer}/introspect"),
            redirects: vec!["http://127.0.0.1:39001/auth/v1/callback".into()],
            ca_pem: Some(pem),
        };
        Self {
            child,
            config,
            http,
        }
    }
    fn install(&self, store: &mut Store) {
        store
            .identity_admin(Admin::Configure {
                provider: "fixture".into(),
                config: self.config.clone(),
            })
            .unwrap();
    }
    fn control(&self, query: &str) {
        self.http
            .get(format!("{}/control?{query}", self.config.issuer))
            .send()
            .unwrap()
            .error_for_status()
            .unwrap();
    }
    fn pending(
        &self,
        store: &mut Store,
        case: &str,
        subject: &str,
        email: &str,
    ) -> (String, String, String) {
        let binding = iam::secret().unwrap();
        let redirect = self.config.redirects[0].clone();
        let start = store
            .identity_auth(Auth::Begin {
                provider: "fixture".into(),
                redirect,
                binding: binding.clone(),
                channel: Channel::Cli,
            })
            .unwrap();
        let mut url = reqwest::Url::parse(start["authorization_url"].as_str().unwrap()).unwrap();
        url.query_pairs_mut()
            .append_pair("fixture_case", case)
            .append_pair("fixture_subject", subject)
            .append_pair("fixture_email", email);
        let response = self.http.get(url).send().unwrap();
        assert_eq!(response.status(), 302);
        let location =
            reqwest::Url::parse(response.headers()["location"].to_str().unwrap()).unwrap();
        let fields = location
            .query_pairs()
            .collect::<std::collections::BTreeMap<_, _>>();
        (
            fields["state"].to_string(),
            fields["code"].to_string(),
            binding,
        )
    }
    fn finish(&self, store: &mut Store, pending: (String, String, String)) -> Result<Value> {
        store.identity_auth(Auth::Complete {
            state: pending.0,
            code: pending.1,
            binding: pending.2,
            redirect: self.config.redirects[0].clone(),
            channel: Channel::Cli,
        })
    }
}
impl Drop for Provider {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[test]
fn identity_migration_reuses_local_realm_and_owner_without_remote_grants() {
    let f = Fixture::new();
    let (realm, owner): (String, String) = f
        .store
        .db
        .query_row(
            "SELECT r.id,p.id FROM realms r JOIN principals p ON p.realm_id=r.id",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    let (actual, local): (String, String) = f
        .store
        .db
        .query_row("SELECT id,local_owner FROM identity_realm", [], |r| {
            Ok((r.get(0)?, r.get(1)?))
        })
        .unwrap();
    assert_eq!((realm, owner), (actual, local));
    assert_eq!(
        f.store
            .db
            .query_row("SELECT count(*) FROM identity_subjects", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        0
    );
    assert!(
        f.store
            .db
            .query_row("SELECT bootstrap_principal FROM identity_realm", [], |r| {
                r.get::<_, Option<String>>(0)
            })
            .unwrap()
            .is_none()
    );
}
#[test]
fn identity_service_scope_expiry_disable_rotation_and_restart() {
    let mut f = Fixture::new();
    let id = f.service();
    let token = f.issue(&id);
    let context = f
        .store
        .identity_context(&token, Channel::Service, None)
        .unwrap();
    assert_eq!(context.actor_id, id);
    assert_eq!(context.effective_principal_id, id);
    assert!(
        f.store
            .identity_context(&token, Channel::Cli, None)
            .is_err()
    );
    assert!(
        f.store
            .identity_admin(Admin::IssueService {
                principal: id.clone(),
                scopes: vec!["admin".into()],
                ttl_seconds: 30
            })
            .is_err()
    );
    f.reopen();
    assert_eq!(
        f.store
            .identity_context(&token, Channel::Service, None)
            .unwrap()
            .realm_id,
        context.realm_id
    );
    f.store
        .identity_admin(Admin::Disable {
            principal: id.clone(),
            disabled: true,
        })
        .unwrap();
    assert!(
        f.store
            .identity_context(&token, Channel::Service, None)
            .is_err()
    );
    f.store
        .identity_admin(Admin::Disable {
            principal: id.clone(),
            disabled: false,
        })
        .unwrap();
    assert!(
        f.store
            .identity_context(&token, Channel::Service, None)
            .is_err()
    );
    let token = f.issue(&id);
    f.store.identity_admin(Admin::RotateSessions).unwrap();
    assert!(
        f.store
            .identity_context(&token, Channel::Service, None)
            .is_err()
    );
    let token = f.issue(&id);
    f.store
        .db
        .execute("UPDATE identity_sessions SET expires_ms=0", [])
        .unwrap();
    assert!(
        f.store
            .identity_context(&token, Channel::Service, None)
            .is_err()
    );
}
#[test]
fn identity_audit_failure_rolls_back_issuance_and_disable() {
    let mut f = Fixture::new();
    let id = f.service();
    let token = f.issue(&id);
    f.store.db.execute_batch("CREATE TRIGGER deny_identity_audit BEFORE INSERT ON identity_audit BEGIN SELECT RAISE(ABORT,'audit unavailable'); END;").unwrap();
    assert!(
        f.store
            .identity_admin(Admin::Disable {
                principal: id.clone(),
                disabled: true
            })
            .is_err()
    );
    assert!(
        f.store
            .identity_context(&token, Channel::Service, None)
            .is_ok()
    );
    assert!(
        f.store
            .identity_admin(Admin::IssueService {
                principal: id,
                scopes: vec![iam::SELF_SCOPE.into()],
                ttl_seconds: 300
            })
            .is_err()
    );
    assert_eq!(
        f.store
            .db
            .query_row("SELECT count(*) FROM identity_sessions", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        1
    );
}
#[test]
fn identity_browser_csrf_logout_and_secret_storage() {
    let mut f = Fixture::new();
    let id = f.service();
    let session = issue(
        &f.store.db,
        Grant {
            principal: &id,
            channel: Channel::Browser,
            scopes: vec![iam::SELF_SCOPE.into()],
            expires: now() + 300_000,
            provider: None,
            access_token: None,
            subject: None,
        },
    )
    .unwrap();
    let token = session["token"].as_str().unwrap();
    let csrf = session["csrf"].as_str().unwrap();
    assert!(
        f.store
            .identity_context_local(token, Channel::Browser, None)
            .is_err()
    );
    assert!(
        f.store
            .identity_context_local(token, Channel::Browser, Some("forged"))
            .is_err()
    );
    assert!(
        f.store
            .identity_context_local(token, Channel::Browser, Some(csrf))
            .is_ok()
    );
    f.store
        .identity_auth(Auth::Logout {
            token: token.into(),
            channel: Channel::Browser,
            csrf: Some(csrf.into()),
        })
        .unwrap();
    assert!(
        f.store
            .identity_context_local(token, Channel::Browser, Some(csrf))
            .is_err()
    );
    let events = f
        .store
        .identity_admin(Admin::Audit { after: 0 })
        .unwrap()
        .to_string();
    assert!(!events.contains(token));
    assert!(!events.contains(csrf));
    assert!(
        !format!(
            "{:?}",
            Auth::Authenticate {
                token: token.into(),
                channel: Channel::Browser,
                csrf: Some(csrf.into())
            }
        )
        .contains(token)
    );
}
#[test]
fn identity_real_oidc_pkce_stability_collision_replay_and_key_rotation() {
    let p = Provider::new();
    let mut f = Fixture::new();
    p.install(&mut f.store);
    let pending = p.pending(&mut f.store, "", "alice", "shared@example.test");
    let replay = pending.clone();
    let alice = p.finish(&mut f.store, pending).unwrap();
    assert!(p.finish(&mut f.store, replay).is_err());
    let pending = p.pending(&mut f.store, "", "bob", "shared@example.test");
    let bob = p.finish(&mut f.store, pending).unwrap();
    assert_ne!(alice["principal_id"], bob["principal_id"]);
    f.reopen();
    p.control("kid=rotated");
    let pending = p.pending(&mut f.store, "", "alice", "admin");
    let renamed = p.finish(&mut f.store, pending).unwrap();
    assert_eq!(alice["principal_id"], renamed["principal_id"]);
    let pending = p.pending(&mut f.store, "", "alice-recreated", "shared@example.test");
    let recreated = p.finish(&mut f.store, pending).unwrap();
    assert_ne!(alice["principal_id"], recreated["principal_id"]);
    assert!(
        f.store
            .db
            .query_row("SELECT bootstrap_principal FROM identity_realm", [], |r| {
                r.get::<_, Option<String>>(0)
            })
            .unwrap()
            .is_none()
    );
    let token = alice["token"].as_str().unwrap();
    assert!(f.store.identity_context(token, Channel::Cli, None).is_ok());
    p.control("active=false");
    assert!(f.store.identity_context(token, Channel::Cli, None).is_err());
    p.control("outage=true");
    assert!(f.store.identity_context(token, Channel::Cli, None).is_err());
    f.store
        .identity_auth(Auth::Logout {
            token: token.into(),
            channel: Channel::Cli,
            csrf: None,
        })
        .unwrap();
    p.control("active=true");
    assert!(f.store.identity_context(token, Channel::Cli, None).is_err());
}
#[test]
fn identity_oidc_rejects_bad_claims_binding_expiry_and_redirects() {
    let p = Provider::new();
    let mut f = Fixture::new();
    p.install(&mut f.store);
    for case in [
        "issuer",
        "audience",
        "nonce",
        "expired",
        "algorithm",
        "signature",
    ] {
        let pending = p.pending(&mut f.store, case, "alice", "alice@example.test");
        assert!(p.finish(&mut f.store, pending).is_err(), "accepted {case}");
    }
    let pending = p.pending(&mut f.store, "", "alice", "alice@example.test");
    let mut forged = pending.clone();
    forged.2 = iam::secret().unwrap();
    assert!(p.finish(&mut f.store, forged).is_err());
    assert!(
        f.store
            .identity_auth(Auth::Complete {
                state: pending.0.clone(),
                code: pending.1.clone(),
                binding: pending.2.clone(),
                redirect: "http://127.0.0.1:39002/auth/v1/callback".into(),
                channel: Channel::Cli
            })
            .is_err()
    );
    f.store
        .db
        .execute("UPDATE identity_logins SET expires_ms=0", [])
        .unwrap();
    assert!(p.finish(&mut f.store, pending).is_err());
    assert!(
        f.store
            .identity_auth(Auth::Begin {
                provider: "fixture".into(),
                redirect: "https://attacker.example/auth/v1/callback".into(),
                binding: iam::secret().unwrap(),
                channel: Channel::Cli
            })
            .is_err()
    );
    assert_eq!(
        f.store
            .db
            .query_row("SELECT count(*) FROM identity_sessions", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        0
    );
}
#[test]
fn identity_bootstrap_is_operator_selected_and_provider_change_revokes() {
    let p = Provider::new();
    let mut f = Fixture::new();
    p.install(&mut f.store);
    let boot = f
        .store
        .identity_admin(Admin::Bootstrap {
            issuer: p.config.issuer.clone(),
            subject: "selected".into(),
            label: "admin".into(),
        })
        .unwrap();
    assert!(
        f.store
            .identity_admin(Admin::Bootstrap {
                issuer: p.config.issuer.clone(),
                subject: "other".into(),
                label: "other".into()
            })
            .is_err()
    );
    let pending = p.pending(&mut f.store, "", "selected", "new@example.test");
    let session = p.finish(&mut f.store, pending).unwrap();
    assert_eq!(boot["principal_id"], session["principal_id"]);
    p.install(&mut f.store);
    assert!(
        f.store
            .identity_context(session["token"].as_str().unwrap(), Channel::Cli, None)
            .is_err()
    );
    let group = f
        .store
        .identity_admin(Admin::Group {
            label: "analysts".into(),
        })
        .unwrap();
    f.store
        .identity_admin(Admin::Membership {
            group: group["group_id"].as_str().unwrap().into(),
            principal: boot["principal_id"].as_str().unwrap().into(),
            present: true,
        })
        .unwrap();
    assert_eq!(
        f.store
            .db
            .query_row("SELECT count(*) FROM identity_memberships", [], |r| r
                .get::<_, i64>(0))
            .unwrap(),
        1
    );
}

#[test]
fn identity_inflight_provider_rotation_and_revocation_cannot_resurrect_sessions() {
    let p = Provider::new();
    let mut f = Fixture::new();
    p.install(&mut f.store);
    let work = f
        .store
        .identity_job(Auth::Begin {
            provider: "fixture".into(),
            redirect: p.config.redirects[0].clone(),
            binding: iam::secret().unwrap(),
            channel: Channel::Cli,
        })
        .unwrap();
    let commit = work().unwrap();
    p.install(&mut f.store);
    assert!(commit(&mut f.store).is_err());
    let pending = p.pending(&mut f.store, "", "alice", "alice@example.test");
    let job = f
        .store
        .identity_job(Auth::Complete {
            state: pending.0,
            code: pending.1,
            binding: pending.2,
            redirect: p.config.redirects[0].clone(),
            channel: Channel::Cli,
        })
        .unwrap();
    let commit = job().unwrap();
    f.store.identity_admin(Admin::RotateSessions).unwrap();
    assert!(commit(&mut f.store).is_err());
    let pending = p.pending(&mut f.store, "", "alice", "alice@example.test");
    let session = p.finish(&mut f.store, pending).unwrap();
    let job = f
        .store
        .identity_job(Auth::Authenticate {
            token: session["token"].as_str().unwrap().into(),
            channel: Channel::Cli,
            csrf: None,
        })
        .unwrap();
    let commit = job().unwrap();
    f.store
        .identity_admin(Admin::Revoke {
            principal: session["principal_id"].as_str().unwrap().into(),
        })
        .unwrap();
    assert!(commit(&mut f.store).is_err());
}
