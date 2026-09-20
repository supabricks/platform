//! Independent local-owner OSS UC service. No project tables are registered here.
mod adapter;
mod config;
mod http;
pub mod metadata;
mod runtime;
#[cfg(test)]
mod tests;
use crate::{
    store::{
        Result, Store,
        error::{conflict, invalid},
    },
    supervisor::{self, Launch},
};
pub use config::Provider;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
    process::Child,
    thread::JoinHandle,
    time::{Duration, Instant},
};

const ROLE: &str = "unity-catalog";
#[derive(Debug, Serialize, Deserialize)]
#[serde(tag = "action", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Status,
    Configure { provider: Provider },
    Restart,
    RotateKey,
}

pub fn stop_owned(store: &mut Store) -> Result<()> {
    for record in store
        .native_processes()?
        .into_iter()
        .filter(|p| p.role == ROLE)
    {
        if record.root != store.root() {
            return Err(conflict("catalog process belongs to a different data root"));
        }
        supervisor::stop(&record)?;
        store.forget_native_process(&record)?;
    }
    Ok(())
}

pub struct Manager {
    config: Option<config::Config>,
    runtime: Option<runtime::Runtime>,
    child: Option<Child>,
    probe: Option<JoinHandle<std::result::Result<http::Health, &'static str>>>,
    endpoint: Option<String>,
    metastore: Option<String>,
    state: &'static str,
    error: Option<&'static str>,
    attempts: u32,
    failures: u32,
    started: Instant,
    next: Instant,
    ready_seconds: Option<f64>,
}
impl Manager {
    pub fn recover(store: &mut Store) -> Self {
        let mut manager = Self {
            config: None,
            runtime: None,
            child: None,
            probe: None,
            endpoint: None,
            metastore: None,
            state: "disabled",
            error: None,
            attempts: 0,
            failures: 0,
            started: Instant::now(),
            next: Instant::now(),
            ready_seconds: None,
        };
        if stop_owned(store).is_err() {
            manager.state = "failed";
            manager.error = Some("ownership_recovery_failed");
            return manager;
        }
        match config::load(store) {
            Ok(config) => {
                manager.config = config;
                if manager.config.is_some() {
                    manager.state = "starting";
                }
            }
            Err(_) => {
                manager.state = "failed";
                manager.error = Some("configuration_invalid");
            }
        }
        manager
    }

    pub(crate) fn adapter(&self, store: &Store) -> Result<adapter::Adapter> {
        if self.state != "ready" {
            return Err(supabricks_core::error::OperationError::Unavailable(
                "catalog provider is not ready; inspect catalog health".into(),
            )
            .into());
        }
        let config = self
            .config
            .as_ref()
            .ok_or_else(|| invalid("catalog is not configured"))?;
        let probe = match &config.provider {
            Provider::Local { .. } => http::Probe {
                endpoint: self.endpoint.clone().unwrap(),
                token_file: store.root().join("catalog/etc/conf/token.txt"),
                ca_file: None,
                expected_metastore: self.metastore.clone(),
            },
            Provider::External {
                endpoint,
                token_file,
                ca_file,
                metastore_id,
            } => http::Probe {
                endpoint: endpoint.clone(),
                token_file: token_file.clone(),
                ca_file: ca_file.clone(),
                expected_metastore: Some(metastore_id.clone()),
            },
        };
        Ok(adapter::Adapter {
            probe,
            provider_id: config.provider_id.clone(),
        })
    }

    pub fn status(&self) -> Value {
        let mode = self.config.as_ref().map(|c| match c.provider {
            Provider::Local { .. } => "local",
            Provider::External { .. } => "external",
        });
        json!({"protocol_version":1,"provider":"oss_unity_catalog","mode":mode,
            "provider_id":self.config.as_ref().map(|c| &c.provider_id),"metastore_id":self.metastore,
            "state":self.state,"ready":self.state=="ready","error":self.error,"endpoint":self.endpoint,
            "start_attempts":self.attempts,"readiness_seconds":self.ready_seconds,
            "capabilities":{"metadata_api":"2.1","authenticated":self.state=="ready", "local_owner_files":mode==Some("local"),"cross_host_storage":false,"credential_vending":false,"governed_multiuser":false},
            "hint":if self.error.is_some() { Some("inspect private catalog logs or secret references; use catalog service restart after correction") } else { None }})
    }

    pub fn command(&mut self, store: &mut Store, command: Command) -> Result<Value> {
        if matches!(command, Command::Status) {
            return Ok(self.status());
        }
        let replacement = if let Command::Configure { provider } = &command {
            if matches!(provider, Provider::Local { .. }) {
                runtime::resolve(provider)?;
            }
            Some(config::new(store, provider.clone())?)
        } else {
            None
        };
        if matches!(command, Command::RotateKey)
            && !self
                .config
                .as_ref()
                .is_some_and(|c| matches!(c.provider, Provider::Local { .. }))
        {
            return Err(invalid(
                "signing key rotation applies only to a managed local catalog",
            ));
        }
        stop_owned(store)?;
        if let Some(mut child) = self.child.take() {
            let _ = child.wait();
        }
        // Detached probes only read; their response cannot mutate a new configuration.
        self.probe.take();
        if let Some(config) = replacement {
            supervisor::write_json(&store.root().join("catalog-provider.json"), &config)?;
            self.config = Some(config);
        } else if matches!(command, Command::RotateKey) {
            let root = runtime::data_directory(store)?;
            // Durable intent survives interruption between individual key removals.
            supervisor::write_json(&root.join("rotate-key.json"), &json!({"version":1}))?;
        } else {
            self.config = config::load(store)?;
        }
        self.runtime = None;
        self.endpoint = None;
        self.metastore = None;
        self.attempts = 0;
        self.failures = 0;
        self.error = None;
        self.ready_seconds = None;
        self.state = if self.config.is_some() {
            "starting"
        } else {
            "disabled"
        };
        self.next = Instant::now();
        Ok(self.status())
    }

    fn start(&mut self, store: &mut Store) -> Result<()> {
        stop_owned(store)?;
        let provider = &self.config.as_ref().unwrap().provider;
        self.runtime = Some(runtime::resolve(provider)?);
        let runtime = self.runtime.as_ref().unwrap();
        let root = runtime::data_directory(store)?;
        if root.join("rotate-key.json").try_exists()?
            || !root.join("bootstrapped.json").try_exists()?
        {
            for name in [
                "bootstrapped.json",
                "etc/conf/public_key.der",
                "etc/conf/private_key.der",
                "etc/conf/key_id.txt",
                "etc/conf/token.txt",
                "etc/conf/certs.json",
            ] {
                match fs::remove_file(root.join(name)) {
                    Ok(()) => (),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
                    Err(e) => return Err(e.into()),
                }
            }
            if root.join("etc/conf").try_exists()? {
                fs::File::open(root.join("etc/conf"))?.sync_all()?;
            }
            fs::File::open(&root)?.sync_all()?;
        }
        let root = runtime::prepare(store, runtime)?;
        let (front, back) = runtime::ports(store)?;
        let port = front.local_addr()?.port();
        let launch = Launch {
            root: store.root().into(),
            generation: store.generation(),
            role: ROLE.into(),
            token: crate::console::secret()?,
            branch: None,
            argv: vec![
                runtime
                    .root
                    .join("java/bin/java")
                    .to_string_lossy()
                    .into_owned(),
                "-Xms64m".into(),
                "-Xmx256m".into(),
                "-XX:ActiveProcessorCount=2".into(),
                "-Djava.net.preferIPv4Stack=true".into(),
                format!("-Djava.io.tmpdir={}", root.join("tmp").display()),
                "-cp".into(),
                runtime.classpath.clone(),
                "io.unitycatalog.server.UnityCatalogServer".into(),
                "--port".into(),
                port.to_string(),
            ],
            env: BTreeMap::from([
                ("PATH".into(), "/usr/bin:/bin".into()),
                ("OTEL_SDK_DISABLED".into(), "true".into()),
            ]),
            cwd: root.clone(),
        };
        // UC's API cannot inherit prebound sockets. A racing collision becomes a
        // failed owned launch and gets a fresh pair on bounded retry, never adoption.
        drop((front, back));
        self.child = Some(supervisor::start_owned(
            store,
            &launch,
            &root.join("launch.json"),
            &root.join("process.log"),
        )?);
        self.endpoint = Some(format!("http://127.0.0.1:{port}"));
        self.started = Instant::now();
        self.state = "starting";
        self.ready_seconds = None;
        Ok(())
    }

    fn retry(&mut self, store: &mut Store, error: &'static str) -> Result<()> {
        stop_owned(store)?;
        if let Some(mut child) = self.child.take() {
            let _ = child.wait();
        }
        self.probe.take();
        self.error = Some(error);
        self.endpoint = None;
        self.state = if self.attempts >= 3 {
            "failed"
        } else {
            "backoff"
        };
        self.next = Instant::now() + Duration::from_secs(1 << self.attempts.min(3));
        Ok(())
    }

    pub fn tick(&mut self, store: &mut Store, stopping: bool) -> bool {
        if stopping {
            if stop_owned(store).is_err() {
                self.error = Some("ownership_cleanup_failed");
                self.state = "failed";
                return false;
            }
            if let Some(mut child) = self.child.take() {
                let _ = child.wait();
            }
            self.probe.take();
            self.state = "stopped";
            return true;
        }
        if self.step(store).is_err() {
            // Never forward JVM/HTTP/credential contents to status or public logs.
            self.error = Some("lifecycle_failed");
            self.state = "failed";
        }
        true
    }

    fn step(&mut self, store: &mut Store) -> Result<()> {
        let Some(config) = self.config.clone() else {
            return Ok(());
        };
        if self.state == "failed" {
            return Ok(());
        }
        let local = matches!(config.provider, Provider::Local { .. });
        if local {
            if self
                .child
                .as_mut()
                .map(|c| c.try_wait())
                .transpose()?
                .flatten()
                .is_some()
            {
                self.retry(store, "process_exited")?;
                return Ok(());
            }
            if self.child.is_none() {
                if Instant::now() < self.next {
                    return Ok(());
                }
                self.attempts += 1;
                if self.start(store).is_err() {
                    self.retry(store, "start_failed")?;
                    return Ok(());
                }
            }
            runtime::rotate_log(&store.root().join("catalog/process.log"))?;
            if self.ready_seconds.is_none() && self.started.elapsed() > Duration::from_secs(20) {
                self.retry(store, "readiness_timeout")?;
                return Ok(());
            }
        }
        if self.probe.as_ref().is_some_and(|p| p.is_finished()) {
            let result = self
                .probe
                .take()
                .unwrap()
                .join()
                .unwrap_or(Err("health_worker_failed"));
            match result {
                Ok(health) => {
                    if local && self.ready_seconds.is_none() {
                        let root = store.root().join("catalog");
                        let mut identity = config::local_identity(store)?;
                        identity.metastore_id = Some(health.metastore_id.clone());
                        supervisor::write_json(
                            &store.root().join("catalog-local.json"),
                            &identity,
                        )?;
                        supervisor::write_json(
                            &root.join("bootstrapped.json"),
                            &json!({"version":1}),
                        )?;
                        if root.join("rotate-key.json").try_exists()? {
                            fs::remove_file(root.join("rotate-key.json"))?;
                            fs::File::open(&root)?.sync_all()?;
                        }
                    }
                    self.metastore = Some(health.metastore_id);
                    self.error = None;
                    self.state = "ready";
                    self.failures = 0;
                    if self.ready_seconds.is_none() {
                        self.ready_seconds = Some(self.started.elapsed().as_secs_f64());
                    }
                }
                Err(error) => {
                    self.error = Some(error);
                    self.failures += 1;
                    if self.ready_seconds.is_some() {
                        self.state = "degraded";
                    }
                    if local && self.ready_seconds.is_some() && self.failures >= 3 {
                        self.retry(store, error)?;
                        return Ok(());
                    }
                    if !local {
                        self.state = "unavailable";
                    }
                }
            }
            self.next = Instant::now() + Duration::from_secs(2);
        }
        if self.probe.is_none() && Instant::now() >= self.next {
            let probe = match &config.provider {
                Provider::Local { .. } => {
                    let root = store.root().join("catalog");
                    let expected = config::local_identity(store)?.metastore_id;
                    http::Probe {
                        endpoint: self.endpoint.clone().unwrap(),
                        token_file: root.join("etc/conf/token.txt"),
                        ca_file: None,
                        expected_metastore: expected,
                    }
                }
                Provider::External {
                    endpoint,
                    token_file,
                    ca_file,
                    metastore_id,
                } => {
                    self.endpoint = Some(endpoint.clone());
                    http::Probe {
                        endpoint: endpoint.clone(),
                        token_file: token_file.clone(),
                        ca_file: ca_file.clone(),
                        expected_metastore: Some(metastore_id.clone()),
                    }
                }
            };
            self.probe = Some(std::thread::spawn(move || probe.run()));
        }
        Ok(())
    }
}
