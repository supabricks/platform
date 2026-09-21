use super::*;
use crate::catalog::{Manager, config};
use crate::store::Store;
use jsonwebtoken::{Algorithm, EncodingKey, Header};
use rsa::{pkcs1::EncodeRsaPrivateKey, pkcs8::DecodePrivateKey};
use serde_json::json;
use std::time::{Duration, Instant};

#[derive(Clone)]
pub(crate) struct Broker {
    pub provider: String,
    pub metastore: String,
    endpoint: String,
    admin: String,
    key: EncodingKey,
    kid: String,
    deadline: Instant,
}
fn segment(s: &str) -> String {
    percent_encoding::utf8_percent_encode(s, percent_encoding::NON_ALPHANUMERIC).to_string()
}
impl Broker {
    pub fn managed(manager: &Manager, store: &Store) -> Result<Self> {
        if manager.status()["mode"] != "local" {
            return Err(denied());
        }
        let adapter = manager.adapter(store)?;
        let conf = store.root().join("catalog/etc/conf");
        let properties = config::private_bytes(&conf.join("server.properties"), 16384)?;
        // No external issuer is trusted by the managed private broker profile.
        let properties = std::str::from_utf8(&properties).map_err(|_| denied())?;
        if properties
            .lines()
            .filter(|l| l.starts_with("server.allowed-issuers="))
            .collect::<Vec<_>>()
            != ["server.allowed-issuers=internal"]
            || !properties
                .lines()
                .any(|l| l == "server.authorization=enable")
        {
            return Err(denied());
        }
        Ok(Self {
            provider: adapter.provider_id,
            metastore: adapter.probe.expected_metastore.ok_or_else(denied)?,
            endpoint: adapter.probe.endpoint,
            admin: config::token(&adapter.probe.token_file)?,
            key: EncodingKey::from_rsa_der(
                rsa::RsaPrivateKey::from_pkcs8_der(&config::private_bytes(
                    &conf.join("private_key.der"),
                    16384,
                )?)
                .map_err(|_| denied())?
                .to_pkcs1_der()
                .map_err(|_| denied())?
                .as_bytes(),
            ),
            kid: String::from_utf8(config::private_bytes(&conf.join("key_id.txt"), 4096)?)
                .map_err(|_| denied())?
                .trim()
                .into(),
            deadline: Instant::now() + Duration::from_secs(20),
        })
    }
    fn request(
        &self,
        method: &str,
        control: bool,
        path: &str,
        body: Option<Value>,
        token: &str,
    ) -> Result<(u16, Value)> {
        self.request_fenced(method, control, path, body, token, None)
    }
    fn request_fenced(
        &self,
        method: &str,
        control: bool,
        path: &str,
        body: Option<Value>,
        token: &str,
        expected: Option<(&str, &str)>,
    ) -> Result<(u16, Value)> {
        if Instant::now() >= self.deadline {
            return Err(denied());
        }
        let client = reqwest::blocking::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(
                Duration::from_secs(2).min(self.deadline.saturating_duration_since(Instant::now())),
            )
            .build()
            .map_err(|_| denied())?;
        let prefix = if control {
            "/api/1.0/unity-control/"
        } else {
            "/api/2.1/unity-catalog/"
        };
        let mut request = client
            .request(
                method.parse().map_err(|_| denied())?,
                format!("{}{prefix}{path}", self.endpoint),
            )
            .bearer_auth(token);
        if let Some((object, principal)) = expected {
            request = request
                .header("X-Supabricks-Object-Id", object)
                .header("X-Supabricks-Principal-Id", principal);
        }
        if let Some(body) = body {
            request = request
                .header("Content-Type", "application/json")
                .body(body.to_string());
        }
        let mut response = request.send().map_err(|_| denied())?;
        let status = response.status().as_u16();
        use std::io::Read;
        let mut bytes = Vec::new();
        response
            .by_ref()
            .take(262145)
            .read_to_end(&mut bytes)
            .map_err(|_| denied())?;
        if bytes.len() > 262144 {
            return Err(denied());
        }
        let value = if bytes.is_empty() || (method == "DELETE" && (200..300).contains(&status)) {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).map_err(|_| denied())?
        };
        Ok((status, value))
    }
    fn admin(&self, method: &str, control: bool, path: &str, body: Option<Value>) -> Result<Value> {
        let (status, value) = self.request(method, control, path, body, &self.admin)?;
        if !(200..300).contains(&status) {
            return Err(denied());
        }
        Ok(value)
    }
    pub fn create_principal(&self, principal: &str, subject: &str) -> Result<Principal> {
        let value=self.admin("POST",true,"scim2/Users",Some(json!({"userName":subject,"displayName":subject,"externalId":principal,"active":true,"emails":[{"value":subject,"primary":true}]})))?;
        let id = value["id"].as_str().ok_or_else(denied)?.to_owned();
        let p = Principal {
            principal: principal.into(),
            subject: subject.into(),
            uc_id: id,
        };
        self.principal(&p)?;
        Ok(p)
    }
    pub(crate) fn principal(&self, p: &Principal) -> Result<()> {
        let _: uuid::Uuid = p.uc_id.parse().map_err(|_| denied())?;
        let v = self.admin(
            "GET",
            true,
            &format!("scim2/Users/{}", segment(&p.uc_id)),
            None,
        )?;
        if v["id"] != p.uc_id
            || v["userName"] != p.subject
            || v["active"] != true
            || v["externalId"] != p.principal
        {
            return Err(denied());
        }
        Ok(())
    }
    fn object(&self, object: &Object) -> Result<bool> {
        let (path, id) = match object.kind.as_str() {
            "metastore" => ("metastore_summary".into(), "metastore_id"),
            "catalog" => (format!("catalogs/{}", segment(&object.name)), "id"),
            "schema" => (format!("schemas/{}", segment(&object.name)), "schema_id"),
            "table" => (format!("tables/{}", segment(&object.name)), "table_id"),
            _ => return Err(denied()),
        };
        let (status, value) = self.request("GET", false, &path, None, &self.admin)?;
        if status == 404 && object.allow_missing {
            return Ok(false);
        }
        if status != 200 || value[id] != object.id {
            return Err(denied());
        }
        if let Some(definition) = &object.definition {
            crate::catalog::publication::worker::identity(&value, definition)
                .map_err(|_| denied())?;
        }
        Ok(true)
    }
    pub fn observe(&self, snapshot: &Snapshot) -> Result<Grants> {
        if self.provider != snapshot.provider {
            return Err(denied());
        }
        for p in &snapshot.principals {
            self.principal(p)?;
        }
        let mut grants = Grants::new();
        for object in &snapshot.objects {
            if !self.object(object)? {
                grants.insert(object.key(), BTreeMap::new());
                continue;
            }
            let value = self.admin(
                "GET",
                false,
                &format!("permissions/{}/{}", object.kind, segment(&object.name)),
                None,
            )?;
            let mut assignments = BTreeMap::new();
            for assignment in value["privilege_assignments"]
                .as_array()
                .ok_or_else(denied)?
            {
                let subject = assignment["principal"].as_str().ok_or_else(denied)?;
                if snapshot.principals.iter().any(|p| p.subject == subject) {
                    let privileges = assignment["privileges"]
                        .as_array()
                        .ok_or_else(denied)?
                        .iter()
                        .map(|v| v.as_str().map(str::to_owned).ok_or_else(denied))
                        .collect::<Result<BTreeSet<_>>>()?;
                    if !privileges.is_empty() {
                        assignments.insert(subject.into(), privileges);
                    }
                }
            }
            grants.insert(object.key(), assignments);
        }
        Ok(grants)
    }
    pub fn apply(&self, plan: &Plan) -> Result<()> {
        // Reviewed remote state must still match before the first effect.
        if self.observe(&plan.snapshot)? != plan.observed {
            return Err(denied());
        }
        // Revoke first. Every effect was preceded by a durable local deny fence.
        for remove in [true, false] {
            for object in &plan.snapshot.objects {
                for p in &plan.snapshot.principals {
                    let empty = BTreeSet::new();
                    let old = plan
                        .observed
                        .get(&object.key())
                        .and_then(|m| m.get(&p.subject))
                        .unwrap_or(&empty);
                    let new = plan
                        .snapshot
                        .desired
                        .get(&object.key())
                        .and_then(|m| m.get(&p.subject))
                        .unwrap_or(&empty);
                    let delta: Vec<_> = if remove {
                        old.difference(new).cloned().collect()
                    } else {
                        new.difference(old).cloned().collect()
                    };
                    if delta.is_empty() {
                        continue;
                    }
                    if !self.object(object)? {
                        return Err(denied());
                    }
                    self.principal(p)?;
                    let (add, revoke) = if remove {
                        (vec![], delta)
                    } else {
                        (delta, vec![])
                    };
                    let (status, _) = self.request_fenced(
                        "PATCH",
                        false,
                        &format!("permissions/{}/{}", object.kind, segment(&object.name)),
                        Some(
                            json!({"changes":[{"principal":p.subject,"add":add,"remove":revoke}]}),
                        ),
                        &self.admin,
                        Some((&object.id, &p.uc_id)),
                    )?;
                    if !(200..300).contains(&status) {
                        return Err(denied());
                    }
                }
            }
        }
        if self.observe(&plan.snapshot)? != plan.snapshot.desired {
            return Err(denied());
        }
        Ok(())
    }
    fn user_token(&self, p: &Principal, expires_ms: i64) -> Result<String> {
        let now = crate::identity::now() / 1000;
        let expires = (expires_ms / 1000).min(now + 60);
        if expires <= now {
            return Err(denied());
        }
        let mut header = Header::new(Algorithm::RS512);
        header.kid = Some(self.kid.clone());
        jsonwebtoken::encode(
            &header,
            &json!({"iss":"internal","sub":p.subject,"iat":now,"exp":expires,"type":"ACCESS"}),
            &self.key,
        )
        .map_err(|_| denied())
    }
    pub(crate) fn fresh(&self) -> Self {
        let mut b = self.clone();
        b.deadline = Instant::now() + Duration::from_secs(20);
        b
    }
    pub fn read(
        &self,
        snapshot: &Snapshot,
        principal: &str,
        expires_ms: i64,
        command: &ReadCommand,
    ) -> Result<Value> {
        // Administrative preflight only detects drift. It never supplies user metadata.
        if self.observe(snapshot)? != snapshot.desired {
            return Err(denied());
        }
        let p = snapshot
            .principals
            .iter()
            .find(|p| p.principal == principal)
            .ok_or_else(denied)?;
        let token = self.user_token(p, expires_ms)?;
        // Even an empty publication registry authenticates at UC as the user.
        let (status, catalogs) =
            self.request("GET", false, "catalogs?max_results=1", None, &token)?;
        if status != 200 || !catalogs["catalogs"].is_array() {
            return Err(denied());
        }
        let mut items = Vec::new();
        for table in &snapshot.tables {
            if let ReadCommand::Describe {
                publication,
                table: id,
                publication_revision,
            } = command
            {
                if publication != &table.publication
                    || id != &table.object.id
                    || publication_revision != &table.revision
                {
                    continue;
                }
            }
            let (status, value) = self.request(
                "GET",
                false,
                &format!("tables/{}", segment(&table.object.name)),
                None,
                &token,
            )?;
            if matches!(status, 403 | 404) {
                continue;
            }
            if status != 200 || value["table_id"] != table.object.id {
                return Err(denied());
            }
            crate::catalog::publication::worker::identity(
                &value,
                table.object.definition.as_ref().ok_or_else(denied)?,
            )
            .map_err(|_| denied())?;
            if let ReadCommand::List { search } = command {
                if !table
                    .object
                    .name
                    .to_lowercase()
                    .contains(&search.to_lowercase())
                {
                    continue;
                }
            }
            items.push(public_table(table, &value));
        }
        // A concurrent privilege/identity change during reads cannot release cached metadata.
        if self.observe(snapshot)? != snapshot.desired {
            return Err(denied());
        }
        match command {
            ReadCommand::List { .. } => Ok(json!({"items":items})),
            ReadCommand::Describe { .. } => Ok(json!({"item":items.into_iter().next()})),
        }
    }
}

#[cfg(test)]
impl Broker {
    pub(crate) fn test_endpoint(&self) -> &str {
        &self.endpoint
    }
    pub(crate) fn with_endpoint(&self, endpoint: String) -> Self {
        let mut b = self.fresh();
        b.endpoint = endpoint;
        b
    }
    pub(crate) fn snapshot_fixture(provider: String, metastore: String) -> Self {
        Self {
            provider,
            metastore,
            endpoint: "http://127.0.0.1:1".into(),
            admin: "unavailable".into(),
            key: EncodingKey::from_secret(b"unavailable"),
            kid: "none".into(),
            deadline: Instant::now(),
        }
    }
    pub(crate) fn fixture(
        provider: String,
        metastore: String,
        endpoint: String,
        conf: &std::path::Path,
    ) -> Self {
        let key = rsa::RsaPrivateKey::from_pkcs8_der(
            &std::fs::read(conf.join("private_key.der")).unwrap(),
        )
        .unwrap();
        Self {
            provider,
            metastore,
            endpoint,
            admin: std::fs::read_to_string(conf.join("token.txt"))
                .unwrap()
                .trim()
                .into(),
            key: EncodingKey::from_rsa_der(key.to_pkcs1_der().unwrap().as_bytes()),
            kid: std::fs::read_to_string(conf.join("key_id.txt"))
                .unwrap()
                .trim()
                .into(),
            deadline: Instant::now() + Duration::from_secs(20),
        }
    }
    pub(crate) fn test_request(
        &self,
        method: &str,
        control: bool,
        path: &str,
        body: Option<Value>,
    ) -> Result<Value> {
        self.admin(method, control, path, body)
    }
    pub(crate) fn test_user_request(
        &self,
        p: &Principal,
        method: &str,
        path: &str,
        body: Option<Value>,
    ) -> Result<u16> {
        Ok(self
            .request(
                method,
                false,
                path,
                body,
                &self.user_token(p, crate::identity::now() + 60000)?,
            )?
            .0)
    }
}
