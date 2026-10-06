use super::*;

impl Daemon {
    /// Preserve each entry point's authorization before journaling any deletion.
    fn defer_environment_gc(&mut self, request: &Request, stream: &UnixStream) -> Result<bool> {
        use crate::environments::Command::Collect;
        let binding = match request {
            Request::Api {
                api_version,
                binding,
                action: crate::api::Action::Environment { command: Collect },
            } => {
                if *api_version != crate::api::VERSION {
                    return Err(invalid("unsupported application API version"));
                }
                binding
            }
            Request::ConsoleAction {
                binding,
                generation,
                owner,
                action: crate::console::workspace::Command::Environment { command: Collect },
            } => {
                self.validate_console_scope(binding, *generation, owner)?;
                if binding.worktree == self.store.root().join("console-home") {
                    return Err(conflict(
                        "create or open a project before working with databases, notebooks or analytics",
                    ));
                }
                self.consoles
                    .owns(binding, owner.split_once(':').unwrap().0)?;
                binding
            }
            _ => return Ok(false),
        };
        binding.validate(&mut self.store)?;
        if self.environment_gc.is_some() {
            return Err(conflict("environment collection is already running"));
        }
        let reply = stream.try_clone()?;
        let job = self.environments.collection_job(&mut self.store, binding)?;
        let worker = std::thread::Builder::new()
            .name("environment-gc".into())
            .spawn(job)?;
        self.environment_gc = Some((reply, worker));
        Ok(true)
    }

    fn finish_environment_gc(&mut self) {
        if !self
            .environment_gc
            .as_ref()
            .is_some_and(|(_, worker)| worker.is_finished())
        {
            return;
        }
        self.join_environment_gc();
    }

    pub(super) fn join_environment_gc(&mut self) {
        let Some((mut reply, worker)) = self.environment_gc.take() else {
            return;
        };
        // Finalize authorized deletion even during shutdown, which waits for
        // this worker before releasing daemon ownership. No Store crosses threads.
        let result = worker
            .join()
            .map_err(|_| invalid("environment collection worker unavailable"))
            .and_then(|r| r)
            .and_then(|commit| commit(&mut self.store));
        let _ = writeln!(reply, "{}", response(result));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    };

    #[test]
    fn pending_collection_leaves_control_available_and_drop_joins_before_unlock() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("data");
        let mut daemon = Daemon::bind(&root).unwrap();
        let (reply, mut client) = UnixStream::pair().unwrap();
        let (release, waiting) = mpsc::channel();
        let committed = Arc::new(AtomicBool::new(false));
        let flag = committed.clone();
        let check_root = root.clone();
        let job: crate::store::IdentityJob = Box::new(move || {
            waiting.recv().unwrap();
            Ok(Box::new(move |_store| {
                // A replacement owner must not be admitted before completion.
                assert!(Store::open(&check_root).is_err());
                flag.store(true, Ordering::SeqCst);
                Ok(json!({"collected":[]}))
            }))
        });
        daemon.environment_gc = Some((reply, std::thread::spawn(job)));
        daemon.finish_environment_gc();
        assert!(daemon.environment_gc.is_some());
        assert!(daemon.handle(Request::Status).is_ok());
        assert!(!committed.load(Ordering::SeqCst));
        release.send(()).unwrap();
        drop(daemon);
        assert!(committed.load(Ordering::SeqCst));
        let mut response = String::new();
        BufReader::new(&mut client)
            .read_line(&mut response)
            .unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&response).unwrap()["result"]["collected"],
            json!([])
        );
        assert!(Store::open(&root).is_ok());
    }
}
