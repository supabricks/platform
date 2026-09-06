//! Native single-owner cell. Process Compose is an executor, never desired state.
mod http;
mod pageserver;
mod s3;
use crate::{
    operations::Step,
    store::{
        BranchRecord, Result, Store,
        error::{conflict, invalid},
    },
    supervisor::{self, Launch, write_json, write_private},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashSet},
    fs,
    net::TcpListener,
    path::{Path, PathBuf},
    process::Child,
};
use supabricks_core::{
    keys::{ComputeKey, StorageScope, pg_md5},
    resource::{DesiredState, OperationId},
    spec::SpecParams,
};

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeConfig {
    pub version: u32,
    pub bundle: PathBuf,
    pub process_compose: PathBuf,
    pub weed: PathBuf,
    pub ports: BTreeMap<String, u16>,
    pub s3_access: String,
    pub s3_secret: String,
    pub supervisor_token: String,
}
impl RuntimeConfig {
    pub fn initialize(root: &Path, bundle: &Path, helpers: &Path) -> Result<()> {
        let path = root.join("runtime.json");
        if path.exists() {
            return Ok(());
        }
        let mut ports = BTreeMap::new();
        let mut listeners = Vec::new();
        for name in [
            "supervisor",
            "broker",
            "sk_pg",
            "sk_http",
            "ps_pg",
            "ps_http",
            "weed_master",
            "weed_master_grpc",
            "weed_volume",
            "weed_volume_grpc",
            "weed_filer",
            "weed_filer_grpc",
            "weed_s3",
            "weed_s3_grpc",
        ] {
            let socket = TcpListener::bind("127.0.0.1:0")?;
            ports.insert(name.into(), socket.local_addr()?.port());
            listeners.push(socket);
        }
        let config = Self {
            version: 1,
            bundle: bundle.canonicalize()?,
            process_compose: helpers.join("process-compose").canonicalize()?,
            weed: helpers.join("weed").canonicalize()?,
            ports,
            s3_access: secret(),
            s3_secret: secret(),
            supervisor_token: secret(),
        };
        for executable in [
            config.bundle.join("bin/pageserver"),
            config.bundle.join("bin/safekeeper"),
            config.bundle.join("bin/storage_broker"),
            config.bundle.join("bin/compute_ctl"),
            config.bundle.join("pg_install/v17/bin/postgres"),
        ] {
            if !executable.is_file() {
                return Err(invalid("incomplete native engine bundle"));
            }
        }
        write_json(&path, &config)
    }
}
fn secret() -> String {
    format!("{}{}", OperationId::new(), OperationId::new())
}
fn path(p: &Path) -> Result<String> {
    p.to_str()
        .map(str::to_owned)
        .ok_or_else(|| invalid("native runtime paths must be UTF-8"))
}
fn dir(p: &Path) -> Result<()> {
    fs::create_dir_all(p)?;
    Ok(())
}

pub struct Cell {
    config: RuntimeConfig,
    root: PathBuf,
    generation: i64,
    key: ComputeKey,
    launches: BTreeMap<String, Launch>,
    processes: BTreeMap<String, Value>,
    supervisor: Option<Child>,
    bucket_ready: bool,
    storage_ready: bool,
    attached: HashSet<supabricks_core::resource::BranchId>,
    pub last_error: Option<String>,
}
impl Cell {
    pub fn open(store: &mut Store) -> Result<Self> {
        // Stop the old executor before inspecting its children: it must not be
        // able to race recovery by launching another generation of writers.
        Self::recover(store)?;
        let config: RuntimeConfig =
            serde_json::from_slice(&fs::read(store.root().join("runtime.json"))?)?;
        if config.version != 1 || config.ports.values().any(|p| *p == 0) {
            return Err(invalid("unsupported runtime configuration"));
        }
        let root = store.root().to_owned();
        for name in [
            "logs",
            "launches",
            "pageserver",
            "safekeeper",
            "objects",
            "computes",
            "tmp",
        ] {
            dir(&root.join(name))?;
        }
        let key_path = root.join("storage.pk8");
        if !key_path.exists() {
            write_private(
                &key_path,
                ComputeKey::generate()
                    .map_err(|_| conflict("key generation failed"))?
                    .pkcs8(),
            )?;
        }
        let key = ComputeKey::from_pkcs8(&fs::read(key_path)?)
            .map_err(|_| conflict("invalid storage key"))?;
        write_private(&root.join("storage.pub"), key.public_pem().as_bytes())?;
        let mut cell = Self {
            config,
            root,
            generation: store.generation(),
            key,
            launches: BTreeMap::new(),
            processes: BTreeMap::new(),
            supervisor: None,
            bucket_ready: false,
            storage_ready: false,
            attached: HashSet::new(),
            last_error: None,
        };
        cell.prepare_storage()?;
        cell.write_project()?;
        let argv = vec![
            path(&cell.config.process_compose)?,
            "--address".into(),
            "127.0.0.1".into(),
            "--port".into(),
            cell.port("supervisor").to_string(),
            "--token-file".into(),
            path(&cell.root.join("supervisor.token"))?,
            "--log-file".into(),
            path(&cell.root.join("logs/process-compose.log"))?,
            "up".into(),
            "-t=false".into(),
            "--disable-dotenv".into(),
            "--no-watch".into(),
            "--keep-project".into(),
            "-f".into(),
            path(&cell.root.join("process-compose.json"))?,
        ];
        let launch = cell.launch("supervisor", argv, None, BTreeMap::new(), cell.root.clone());
        cell.supervisor = Some(supervisor::start_supervisor(
            store,
            &launch,
            &cell.root.join("launches/supervisor.json"),
        )?);
        Ok(cell)
    }
    pub fn recover(store: &mut Store) -> Result<()> {
        let mut records = store.native_processes()?;
        records.sort_by_key(|p| {
            if p.role == "supervisor" {
                0
            } else if p.branch.is_some() {
                1
            } else {
                2
            }
        });
        for record in records {
            if record.root != store.root() {
                return Err(conflict("process belongs to a different data root"));
            }
            supervisor::stop(&record)?;
            store.forget_native_process(&record)?;
        }
        if !store.processes()?.is_empty() {
            return Err(conflict(
                "unverified legacy process records prevent engine startup",
            ));
        }
        Ok(())
    }
    fn port(&self, name: &str) -> u16 {
        self.config.ports[name]
    }
    fn addr(&self, name: &str) -> String {
        format!("127.0.0.1:{}", self.port(name))
    }
    fn launch(
        &self,
        role: &str,
        argv: Vec<String>,
        branch: Option<(supabricks_core::resource::BranchId, i64)>,
        mut env: BTreeMap<String, String>,
        cwd: PathBuf,
    ) -> Launch {
        env.insert(
            "PATH".into(),
            format!(
                "{}:/usr/bin:/bin",
                self.config.bundle.join("pg_install/v17/bin").display()
            ),
        );
        env.insert("HOME".into(), self.root.to_string_lossy().into_owned());
        env.insert(
            "TMPDIR".into(),
            self.root.join("tmp").to_string_lossy().into_owned(),
        );
        env.insert("OTEL_SDK_DISABLED".into(), "true".into());
        Launch {
            root: self.root.clone(),
            generation: self.generation,
            role: role.into(),
            token: secret(),
            branch,
            argv,
            env,
            cwd,
        }
    }
    fn add(&mut self, launch: Launch) -> Result<()> {
        let file = self.root.join(format!("launches/{}.json", launch.role));
        write_json(&file, &launch)?;
        // entrypoint is argv, bypassing shell and template interpolation entirely.
        self.processes.insert(
            launch.role.clone(),
            json!({
                "entrypoint":[std::env::current_exe()?,"child","--launch",file],
                "environment":[format!("SUPABRICKS_PROCESS_TOKEN={}",launch.token)],
                "working_dir":self.root,"availability":{"restart":"no"},
                "is_dotenv_disabled":true,"is_template_disabled":true,
                "log_location":self.root.join(format!("logs/{}.log",launch.role)),
                "shutdown":{"signal":9,"timeout_seconds":2}
            }),
        );
        let probe = match launch.role.as_str() {
            "pageserver" => Some((
                self.port("ps_http"),
                "/v1/status",
                Some(StorageScope::Pageserver),
            )),
            "safekeeper" => Some((
                self.port("sk_http"),
                "/v1/status",
                Some(StorageScope::Safekeeper),
            )),
            "objects" => Some((self.port("weed_filer"), "/", None)),
            _ => None,
        };
        if let Some((port, path, scope)) = probe {
            let headers = if let Some(scope) = scope {
                json!({"Authorization":format!("Bearer {}",self.key.mint_storage_jwt(scope).map_err(|_| conflict("probe token failed"))?)})
            } else {
                json!({})
            };
            self.processes.get_mut(&launch.role).unwrap()["readiness_probe"] = json!({"http_get":{"host":"127.0.0.1","port":port.to_string(),"path":path,"headers":headers},"period_seconds":5,"timeout_seconds":1});
        }
        self.launches.insert(launch.role.clone(), launch);
        Ok(())
    }
    fn write_project(&self) -> Result<()> {
        write_json(
            &self.root.join("process-compose.json"),
            &json!({"version":"0.5","name":"supabricks-local","disable_env_expansion":true,"processes":self.processes}),
        )
    }
    fn pc(&self, method: &str, path: &str) -> Result<bool> {
        let (code, _) = http::Http::default().json(
            self.port("supervisor"),
            method,
            path,
            &[("X-PC-Token-Key", &self.config.supervisor_token)],
            None,
        )?;
        Ok(code == 200)
    }
    fn update(&self) -> Result<()> {
        self.write_project()?;
        if !self.pc("POST", "/project/configuration")? {
            return Err(conflict("supervisor rejected dynamic configuration"));
        }
        Ok(())
    }
    fn prepare_storage(&mut self) -> Result<()> {
        write_private(
            &self.root.join("supervisor.token"),
            self.config.supervisor_token.as_bytes(),
        )?;
        write_json(
            &self.root.join("objects/s3.json"),
            &json!({"identities":[{"name":"supabricks","credentials":[{"accessKey":self.config.s3_access,"secretKey":self.config.s3_secret}],"actions":["Admin","Read","Write","List","Tagging"]}]}),
        )?;
        // SQLite is intentionally required. The upstream default LevelDB store
        // acknowledges metadata writes with WriteOptions::Sync disabled.
        write_private(
            &self.root.join("objects/filer.toml"),
            format!(
                "[leveldb2]\nenabled = false\n[sqlite]\nenabled = true\ndbFile = {}\n",
                json!(format!(
                    "{}?_pragma=journal_mode(WAL)&_pragma=synchronous(FULL)",
                    self.root.join("objects/filer.db").display()
                ))
            )
            .as_bytes(),
        )?;
        write_json(
            &self.root.join("objects/filer.conf"),
            &json!({"locations":[{"locationPrefix":"/buckets/","fsync":true}]}),
        )?;
        let mut weed = vec![
            path(&self.config.weed)?,
            "server".into(),
            "-ip=127.0.0.1".into(),
            "-ip.bind=127.0.0.1".into(),
            format!("-dir={}", self.root.join("objects").display()),
            "-s3".into(),
            "-master.telemetry=false".into(),
            "-master.raftHashicorp=true".into(),
            "-master.volumeSizeLimitMB=64".into(),
            "-volume.max=16".into(),
            "-volume.minFreeSpace=1".into(),
            "-s3.port.iceberg=0".into(),
            "-s3.port.lance=0".into(),
            "-s3.iam=false".into(),
            "-s3.autoCreateBucket=false".into(),
            format!("-s3.config={}", self.root.join("objects/s3.json").display()),
            format!(
                "-s3.localSocket={}",
                self.root.join("objects/s3.sock").display()
            ),
            format!(
                "-filer.localSocket={}",
                self.root.join("objects/filer.sock").display()
            ),
        ];
        for (flag, port) in [
            ("master.port", "weed_master"),
            ("master.port.grpc", "weed_master_grpc"),
            ("volume.port", "weed_volume"),
            ("volume.port.grpc", "weed_volume_grpc"),
            ("filer.port", "weed_filer"),
            ("filer.port.grpc", "weed_filer_grpc"),
            ("s3.port", "weed_s3"),
            ("s3.port.grpc", "weed_s3_grpc"),
        ] {
            weed.push(format!("-{flag}={}", self.port(port)));
        }
        self.add(self.launch(
            "objects",
            weed,
            None,
            BTreeMap::new(),
            self.root.join("objects"),
        ))?;
        let bin = self.config.bundle.join("bin");
        self.add(self.launch(
            "broker",
            vec![
                path(&bin.join("storage_broker"))?,
                "--listen-addr".into(),
                self.addr("broker"),
            ],
            None,
            BTreeMap::new(),
            self.root.clone(),
        ))?;
        self.add(self.launch(
            "safekeeper",
            vec![
                path(&bin.join("safekeeper"))?,
                "-D".into(),
                path(&self.root.join("safekeeper"))?,
                "--id=1".into(),
                format!("--listen-pg={}", self.addr("sk_pg")),
                format!("--listen-http={}", self.addr("sk_http")),
                format!("--broker-endpoint=http://{}", self.addr("broker")),
                format!(
                    "--pg-auth-public-key-path={}",
                    self.root.join("storage.pub").display()
                ),
                format!(
                    "--http-auth-public-key-path={}",
                    self.root.join("storage.pub").display()
                ),
            ],
            None,
            BTreeMap::new(),
            self.root.clone(),
        ))?;
        write_private(&self.root.join("pageserver/identity.toml"), b"id = 1\n")?;
        let ps = format!(
            "listen_pg_addr = {}\nlisten_http_addr = {}\npg_distrib_dir = {}\nbroker_endpoint = {}\npg_auth_type = \"NeonJWT\"\nhttp_auth_type = \"NeonJWT\"\nauth_validation_public_key_path = {}\ncontrol_plane_emergency_mode = true\ncontrol_plane_api = \"http://127.0.0.1:1\"\nvirtual_file_io_mode = \"buffered\"\nremote_storage = {{bucket_name = \"supabricks\", bucket_region = \"us-east-1\", endpoint = {}, prefix_in_bucket = \"pageserver\"}}\n",
            json!(self.addr("ps_pg")),
            json!(self.addr("ps_http")),
            json!(self.config.bundle.join("pg_install")),
            json!(format!("http://{}", self.addr("broker"))),
            json!(self.root.join("storage.pub")),
            json!(format!("http://{}", self.addr("weed_s3")))
        );
        write_private(&self.root.join("pageserver/pageserver.toml"), ps.as_bytes())?;
        let env = BTreeMap::from([
            ("AWS_ACCESS_KEY_ID".into(), self.config.s3_access.clone()),
            (
                "AWS_SECRET_ACCESS_KEY".into(),
                self.config.s3_secret.clone(),
            ),
            ("AWS_EC2_METADATA_DISABLED".into(), "true".into()),
            (
                "NEON_AUTH_TOKEN".into(),
                self.key
                    .mint_storage_jwt(StorageScope::Safekeeper)
                    .map_err(|_| conflict("storage token failed"))?,
            ),
        ]);
        self.add(self.launch(
            "pageserver",
            vec![
                path(&bin.join("pageserver"))?,
                "-D".into(),
                path(&self.root.join("pageserver"))?,
            ],
            None,
            env,
            self.root.clone(),
        ))
    }
    fn pageserver(&self) -> Result<pageserver::Pageserver> {
        Ok(pageserver::Pageserver {
            port: self.port("ps_http"),
            token: self
                .key
                .mint_storage_jwt(StorageScope::Pageserver)
                .map_err(|_| conflict("storage token failed"))?,
            generation: self.generation,
        })
    }
    fn ensure_timeline(&mut self, branch: &BranchRecord) -> Result<bool> {
        if self.attached.contains(&branch.branch.id) {
            return Ok(true);
        }
        if self.pageserver()?.ensure(branch)? {
            self.attached.insert(branch.branch.id);
            return Ok(true);
        }
        Ok(false)
    }
    pub fn authorize(
        &self,
        store: &mut Store,
        role: &str,
        generation: i64,
        token: &str,
        pid: u32,
    ) -> Result<()> {
        let launch = self
            .launches
            .get(role)
            .ok_or_else(|| conflict("unknown launch"))?;
        if generation != self.generation || token != launch.token {
            return Err(conflict("stale launch authorization"));
        }
        store.record_native_process(&supervisor::evidence(launch, pid)?)
    }
    fn compute_role(branch: &BranchRecord) -> String {
        format!("compute-{}", branch.endpoint.id)
    }
    fn ensure_compute(&mut self, store: &mut Store, branch: &BranchRecord) -> Result<bool> {
        let role = Self::compute_role(branch);
        let ports = branch
            .ports
            .ok_or_else(|| conflict("missing compute ports"))?;
        if let Some(record) = store
            .native_processes()?
            .into_iter()
            .find(|p| p.role == role)
        {
            if record.branch != Some((branch.branch.id, branch.revision)) {
                self.stop_compute(store, branch)?;
                return Ok(false);
            }
            if supervisor::os::identity(record.pid)?.is_none_or(|id| id.zombie) {
                supervisor::stop(&record)?;
                store.forget_native_process(&record)?;
                self.processes.remove(&role);
                self.launches.remove(&role);
                self.update()?;
                return Ok(false);
            }
            let token = self
                .key
                .mint_admin_jwt(60)
                .map_err(|_| conflict("compute token failed"))?;
            let (code, value) = http::Http::default().json(
                ports.external_http,
                "GET",
                "/status",
                &[("Authorization", &format!("Bearer {token}"))],
                None,
            )?;
            return Ok(code == 200 && value["status"] == "running");
        }
        if self.launches.contains_key(&role) {
            return Ok(false);
        }
        let root = self
            .root
            .join("computes")
            .join(branch.endpoint.id.to_string());
        dir(&root)?;
        let password = store.endpoint_password(branch.endpoint.id)?;
        let tenant = branch.branch.tenant_id.to_string();
        let timeline = branch.branch.timeline_id.to_string();
        let params = SpecParams {
            tenant_id: &tenant,
            timeline_id: &timeline,
            encrypted_password: &pg_md5(&password, "cloud_admin"),
            jwks_x_b64url: &self.key.x_b64url,
            jwks_kid_b64url: &self.key.kid_b64url,
            safekeepers: &self.addr("sk_pg"),
            pageserver_connstring: &format!("host=127.0.0.1 port={}", self.port("ps_pg")),
        };
        let spec_file = root.join("spec.json");
        // compute_ctl must bootstrap the owner role before it has a password.
        // Keep that trust connection on a private Unix socket; TCP requires a
        // password from the first instant Postgres starts listening.
        let sockets = self.root.join("tmp").join(branch.endpoint.id.to_string());
        if sockets.as_os_str().len() + 20 > 104 {
            return Err(invalid(
                "data root is too long for private PostgreSQL sockets",
            ));
        }
        dir(&sockets)?;
        let hba = root.join("pg_hba.conf");
        write_private(&hba,b"local all cloud_admin trust\nlocal all all reject\nhost all all 127.0.0.1/32 md5\nhost replication all 127.0.0.1/32 md5\n")?;
        let mut plan = crate::plan_compute(
            &crate::ComputeInput {
                pg_major: branch.endpoint.pg_major,
                bundle: &self.config.bundle,
                data: &root.join("pgdata"),
                config: &spec_file,
                compute_id: &branch.endpoint.id.to_string(),
                sql: ([127, 0, 0, 1], ports.sql).into(),
                external_http: ([127, 0, 0, 1], ports.external_http).into(),
                internal_http: ([127, 0, 0, 1], ports.internal_http).into(),
            },
            &params,
        )?;
        plan.config["spec"]["storage_auth_token"] = json!(
            self.key
                .mint_storage_jwt(StorageScope::Tenant(branch.branch.tenant_id.clone()))
                .map_err(|_| conflict("storage token failed"))?
        );
        let settings = plan.config["spec"]["cluster"]["settings"]
            .as_array_mut()
            .expect("rendered settings");
        for setting in settings.iter_mut() {
            if setting["name"] == "unix_socket_directories" {
                setting["value"] = json!(sockets);
            }
        }
        settings.push(json!({"name":"hba_file","value":hba,"vartype":"string"}));
        let socket_url = percent_encoding::utf8_percent_encode(
            &path(&sockets)?,
            percent_encoding::NON_ALPHANUMERIC,
        )
        .to_string();
        for arg in &mut plan.command {
            if arg.starts_with("--connstr=") {
                *arg = format!(
                    "--connstr=postgresql://cloud_admin@localhost/postgres?host={socket_url}&port={}",
                    ports.sql
                );
            }
        }
        write_json(&spec_file, &plan.config)?;
        self.add(self.launch(
            &role,
            plan.command,
            Some((branch.branch.id, branch.revision)),
            BTreeMap::new(),
            root,
        ))?;
        self.update()?;
        Ok(false)
    }
    fn stop_compute(&mut self, store: &mut Store, branch: &BranchRecord) -> Result<bool> {
        let role = Self::compute_role(branch);
        // Revoke launch authorization first. A delayed helper cannot start after suspension.
        self.launches.remove(&role);
        if let Some(record) = store
            .native_processes()?
            .into_iter()
            .find(|p| p.role == role)
        {
            // Let Postgres finish its shutdown checkpoint before stopping compute_ctl.
            let pid_file = self.root.join(format!(
                "computes/{}/pgdata/postmaster.pid",
                branch.endpoint.id
            ));
            if let Ok(content) = fs::read_to_string(pid_file) {
                let pid = content
                    .lines()
                    .next()
                    .and_then(|p| p.parse::<u32>().ok())
                    .ok_or_else(|| conflict("invalid postmaster identity"))?;
                if let Some(id) = supervisor::os::identity(pid)?
                    && !id.zombie
                {
                    if id.group != record.pid || !supervisor::os::has_token(&id, &record.token)? {
                        return Err(conflict("postmaster ownership is ambiguous"));
                    }
                    if unsafe { libc::kill(pid as i32, libc::SIGINT) } != 0 {
                        return Err(std::io::Error::last_os_error().into());
                    }
                    return Ok(false);
                }
            }
            supervisor::stop(&record)?;
            store.forget_native_process(&record)?;
        }
        if self.processes.remove(&role).is_some() {
            self.update()?;
        }
        Ok(true)
    }
    pub fn tick(&mut self, store: &mut Store) -> Result<()> {
        if let Some(child) = &mut self.supervisor {
            let _ = child.try_wait()?;
        }
        if store
            .native_processes()?
            .iter()
            .find(|p| p.role == "supervisor")
            .is_none_or(|p| supervisor::members(p).is_ok_and(|m| m.is_empty()))
        {
            *self = Self::open(store)?;
            return Ok(());
        }
        if !self.pc("GET", "/live")? {
            return Ok(());
        }
        for record in store.native_processes()? {
            if record.branch.is_none()
                && record.role != "supervisor"
                && supervisor::os::identity(record.pid)?.is_none_or(|id| id.zombie)
            {
                *self = Self::open(store)?;
                return Ok(());
            }
        }
        if !self.bucket_ready {
            // Filer policies live INSIDE the filer, not beside filer.toml.
            // Store this small policy in synchronously committed SQLite metadata.
            let policy = fs::read(self.root.join("objects/filer.conf"))?;
            let (code, _) = http::Http::default().request(
                self.port("weed_filer"),
                "PUT",
                "/etc/seaweedfs/filer.conf?saveInside=true",
                &[("Content-Type", "application/json")],
                &policy,
            )?;
            if !(200..300).contains(&code) {
                return Ok(());
            }
            self.bucket_ready = s3::ensure_bucket(
                self.port("weed_s3"),
                &self.config.s3_access,
                &self.config.s3_secret,
            )?;
            if !self.bucket_ready {
                return Ok(());
            }
        }
        let (ps, _) = self.pageserver()?.request("GET", "status", None)?;
        let sk_token = self
            .key
            .mint_storage_jwt(StorageScope::Safekeeper)
            .map_err(|_| conflict("storage token failed"))?;
        let (sk, _) = http::Http::default().json(
            self.port("sk_http"),
            "GET",
            "/v1/status",
            &[("Authorization", &format!("Bearer {sk_token}"))],
            None,
        )?;
        self.storage_ready = ps == 200 && sk == 200;
        if !self.storage_ready {
            return Ok(());
        }
        for operation in store.pending()? {
            let Some(ticket) = store.ticket(operation.id)? else {
                continue;
            };
            let branch = store.branch(ticket.branch_id)?;
            let done = match ticket.step {
                Step::EnsureTimeline => self.ensure_timeline(&branch)?,
                Step::StartCompute => {
                    self.ensure_timeline(&branch)? && self.ensure_compute(store, &branch)?
                }
                Step::StopCompute => self.stop_compute(store, &branch)?,
                Step::DeleteTimeline => self.pageserver()?.delete(&branch)?,
            };
            if done {
                store.checkpoint(&ticket, json!({"effect_key":ticket.idempotency_key()}))?;
            }
        }
        // Completed desired state still needs reconciling after a cell restart.
        for branch in store.branches()? {
            if branch.revision == branch.observed_revision
                && branch.endpoint.desired_state == DesiredState::Running
                && self.ensure_timeline(&branch)?
            {
                self.ensure_compute(store, &branch)?;
            }
        }
        Ok(())
    }
    pub fn stop(&mut self, store: &mut Store) -> Result<bool> {
        if !self.pc("GET", "/live").unwrap_or(false) {
            Self::recover(store)?;
            if let Some(mut child) = self.supervisor.take() {
                let _ = child.wait();
            }
            return Ok(true);
        }
        let mut done = true;
        for branch in store.branches()? {
            if !self.stop_compute(store, &branch)? {
                done = false;
            }
        }
        if !done {
            return Ok(false);
        }
        Self::recover(store)?;
        if let Some(mut child) = self.supervisor.take() {
            let _ = child.wait();
        }
        Ok(true)
    }
    pub fn status(&self, store: &Store) -> Result<Value> {
        let records = store.native_processes()?;
        Ok(
            json!({"supervisor":"process-compose","object_store":"seaweedfs-sqlite","ready":self.storage_ready,"last_error":self.last_error,
            "processes":records.iter().map(|p|json!({"role":p.role,"pid":p.pid,"generation":p.generation})).collect::<Vec<_>>() }),
        )
    }
}
