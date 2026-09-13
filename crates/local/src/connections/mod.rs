//! Stable branch listeners. The daemon serializes accept/lease/lifecycle decisions;
//! Tokio only buffers startup bytes and relays the PostgreSQL byte stream.
use crate::{
    engine::Cell,
    store::{Result, Store, error::conflict},
};
use std::{
    collections::HashMap,
    net::{TcpListener, TcpStream},
    time::Duration,
};
use supabricks_core::resource::{BranchId, DesiredState, LeaseId};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    sync::oneshot,
    task::JoinHandle,
};

const MAX_CONNECTIONS: usize = 256;
const MAX_BRANCH_CONNECTIONS: usize = 64;
const MAX_STARTUP_BYTES: usize = 64 * 1024;
fn bind_listener(port: u16) -> std::io::Result<TcpListener> {
    let socket = socket2::Socket::new(
        socket2::Domain::IPV4,
        socket2::Type::STREAM,
        Some(socket2::Protocol::TCP),
    )?;
    // Permit restart after TIME_WAIT, but never allow two listeners on one port.
    socket.set_reuse_address(true)?;
    socket.bind(&std::net::SocketAddr::from(([127, 0, 0, 1], port)).into())?;
    socket.listen(128)?;
    socket.set_nonblocking(true)?;
    Ok(socket.into())
}
struct Session {
    branch: BranchId,
    task: JoinHandle<()>,
    route: Option<oneshot::Sender<u16>>,
    backend: Option<(i64, u32, String)>,
}
pub(crate) struct Gateway {
    runtime: Option<tokio::runtime::Runtime>,
    listeners: HashMap<BranchId, TcpListener>,
    sessions: HashMap<LeaseId, Session>,
    timeout: Duration,
    pub errors: HashMap<BranchId, String>,
}
impl Gateway {
    pub fn new(store: &mut Store, timeout: Duration) -> Result<Self> {
        // Cell::open has fenced the previous owner and its compute processes.
        store.clear_connection_leases()?;
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()?;
        let mut gateway = Self {
            runtime: Some(runtime),
            listeners: HashMap::new(),
            sessions: HashMap::new(),
            timeout,
            errors: HashMap::new(),
        };
        for (branch, port) in store.connection_ports()? {
            let b = store.branch(branch)?;
            if b.ports.is_none() || b.endpoint.desired_state == DesiredState::Deleted {
                continue;
            }
            let listener = bind_listener(port).map_err(|_| {
                conflict(format!(
                    "branch {branch} listener port {port} is unavailable"
                ))
            })?;
            listener.set_nonblocking(true)?;
            gateway.listeners.insert(branch, listener);
        }
        Ok(gateway)
    }
    pub fn ensure_listener(
        &mut self,
        store: &mut Store,
        cell: &Cell,
        branch: BranchId,
    ) -> Result<()> {
        if self.listeners.contains_key(&branch) {
            return Ok(());
        }
        let listener = if let Some(port) = store.connection_port(branch)? {
            bind_listener(port).map_err(|_| {
                conflict(format!(
                    "branch {branch} listener port {port} is unavailable"
                ))
            })?
        } else {
            // Keep the socket bound while committing its reservation.
            let reserved: Vec<_> = store
                .branches()?
                .iter()
                .filter_map(|b| b.ports)
                .flat_map(|p| [p.sql, p.external_http, p.internal_http])
                .collect();
            loop {
                let listener = bind_listener(0)?;
                let port = listener.local_addr()?.port();
                if !reserved.contains(&port) && !cell.reserved_port(port) {
                    store.reserve_connection_port(branch, port)?;
                    break listener;
                }
            }
        };
        listener.set_nonblocking(true)?;
        self.listeners.insert(branch, listener);
        Ok(())
    }
    pub fn tick(&mut self, store: &mut Store, cell: &Cell) -> Result<()> {
        for id in self
            .sessions
            .iter()
            .filter(|(_, s)| s.task.is_finished())
            .map(|(id, _)| *id)
            .collect::<Vec<_>>()
        {
            self.sessions.remove(&id);
            store.release_connection(id)?;
        }
        let computes: HashMap<_, _> = store
            .native_processes()?
            .into_iter()
            // Other owned workers (including ingestion) also carry a branch.
            // Only the PostgreSQL compute identity can invalidate its clients.
            .filter(|p| p.role.starts_with("compute-"))
            .filter_map(|p| {
                p.branch
                    .map(|(branch, revision)| (branch, (revision, p.pid, p.start_identity)))
            })
            .collect();
        for session in self.sessions.values().filter(|s| s.backend.is_some()) {
            if computes.get(&session.branch) != session.backend.as_ref() {
                session.task.abort();
            }
        }
        self.errors.clear();
        let branches = store.branches()?;
        for b in &branches {
            if store.is_export(b.branch.id)? {
                continue;
            }
            let id = b.branch.id;
            if b.endpoint.desired_state == DesiredState::Deleted {
                self.listeners.remove(&id);
                for session in self.sessions.values().filter(|s| s.branch == id) {
                    session.task.abort();
                }
            } else if !b.expired
                && let Err(e) = self.ensure_listener(store, cell, id)
            {
                self.errors.insert(id, e.to_string());
            }
        }
        // Accept is serialized with control requests. An accept before suspend
        // protects compute; an accept after suspend waits for retirement, then wakes.
        for (branch, listener) in &self.listeners {
            for _ in 0..16 {
                let stream = match listener.accept() {
                    Ok((stream, _)) => stream,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                    Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(e) => return Err(e.into()),
                };
                if self.sessions.len() >= MAX_CONNECTIONS
                    || self
                        .sessions
                        .values()
                        .filter(|s| s.branch == *branch)
                        .count()
                        >= MAX_BRANCH_CONNECTIONS
                {
                    continue;
                }
                let lease = match store.accept_connection(*branch) {
                    Ok(id) => id,
                    Err(_) => continue,
                };
                stream.set_nonblocking(true)?;
                let (route, receiver) = oneshot::channel();
                let task =
                    self.runtime
                        .as_ref()
                        .unwrap()
                        .spawn(relay(stream, receiver, self.timeout));
                self.sessions.insert(
                    lease,
                    Session {
                        branch: *branch,
                        task,
                        route: Some(route),
                        backend: None,
                    },
                );
            }
        }
        for session in self
            .sessions
            .values_mut()
            .filter(|s| s.route.is_some() && !s.task.is_finished())
        {
            let b = store.branch(session.branch)?;
            if b.expired || b.endpoint.desired_state == DesiredState::Deleted {
                session.task.abort();
                continue;
            }
            if let Err(e) = store.wake_for_connection(session.branch) {
                self.errors.insert(session.branch, e.to_string());
                continue;
            }
            let b = store.branch(session.branch)?;
            if b.observed_revision == b.revision
                && b.endpoint.desired_state == DesiredState::Running
                && cell.connection_ready(store, &b)?
            {
                session.backend = computes.get(&session.branch).cloned();
                if session.backend.is_some() {
                    let _ = session.route.take().unwrap().send(b.ports.unwrap().sql);
                }
            }
        }
        Ok(())
    }
    pub fn stop(&mut self, store: &mut Store) -> Result<bool> {
        self.listeners.clear();
        for s in self.sessions.values() {
            s.task.abort();
        }
        for id in self
            .sessions
            .iter()
            .filter(|(_, s)| s.task.is_finished())
            .map(|(id, _)| *id)
            .collect::<Vec<_>>()
        {
            self.sessions.remove(&id);
            store.release_connection(id)?;
        }
        Ok(self.sessions.is_empty())
    }
    pub fn status(&self) -> serde_json::Value {
        serde_json::json!({"listeners":self.listeners.len(),"connections":self.sessions.len(),"errors":self.errors,
            "startup_timeout_ms":self.timeout.as_millis(),"max_connections":MAX_CONNECTIONS,"max_connections_per_branch":MAX_BRANCH_CONNECTIONS})
    }
}
impl Drop for Gateway {
    fn drop(&mut self) {
        self.listeners.clear();
        for s in self.sessions.values() {
            s.task.abort();
        }
        if let Some(runtime) = self.runtime.take() {
            runtime.shutdown_timeout(Duration::from_secs(1));
        }
    }
}
async fn relay(stream: TcpStream, mut route: oneshot::Receiver<u16>, timeout: Duration) {
    let Ok(mut client) = tokio::net::TcpStream::from_std(stream) else {
        return;
    };
    let _ = client.set_nodelay(true);
    let startup = async {
        let mut buffered = Vec::new();
        let mut chunk = [0; 4096];
        let port = loop {
            tokio::select! {
                biased;
                n=client.read(&mut chunk)=>{
                    let n=n.ok()?;
                    if n==0 || buffered.len()+n>MAX_STARTUP_BYTES { return None; }
                    buffered.extend_from_slice(&chunk[..n]);
                }
                port=&mut route=>break port.ok()?,
            }
        };
        let mut backend = tokio::net::TcpStream::connect(("127.0.0.1", port))
            .await
            .ok()?;
        let _ = backend.set_nodelay(true);
        backend.write_all(&buffered).await.ok()?;
        Some(backend)
    };
    let Ok(Some(mut backend)) = tokio::time::timeout(timeout, startup).await else {
        return;
    };
    // Preserve a client half-close while Postgres finishes its response. Once
    // Postgres closes its side, release the session even if an idle pool has not
    // yet noticed EOF (including failed auth or pg_terminate_backend).
    let (mut client_read, mut client_write) = client.split();
    let (mut backend_read, mut backend_write) = backend.split();
    let upload = async {
        tokio::io::copy(&mut client_read, &mut backend_write).await?;
        backend_write.shutdown().await
    };
    let download = async {
        tokio::io::copy(&mut backend_read, &mut client_write).await?;
        client_write.shutdown().await
    };
    tokio::pin!(upload, download);
    tokio::select! {
        _=&mut download=>{},
        result=&mut upload=>{ if result.is_ok() { let _=download.await; } },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    async fn pair() -> (tokio::net::TcpStream, TcpStream) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let client = tokio::net::TcpStream::connect(listener.local_addr().unwrap())
            .await
            .unwrap();
        let (server, _) = listener.accept().await.unwrap();
        (client, server.into_std().unwrap())
    }
    #[tokio::test]
    async fn disconnect_and_deadline_close_a_waiting_startup() {
        let (mut client, server) = pair().await;
        let (_send, receive) = oneshot::channel();
        let task = tokio::spawn(relay(server, receive, Duration::from_secs(5)));
        client.write_all(b"startup").await.unwrap();
        drop(client);
        tokio::time::timeout(Duration::from_secs(1), task)
            .await
            .unwrap()
            .unwrap();
        let (mut client, server) = pair().await;
        let (_send, receive) = oneshot::channel();
        let task = tokio::spawn(relay(server, receive, Duration::from_millis(100)));
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(1), client.read(&mut [0; 1]))
                .await
                .unwrap()
                .unwrap(),
            0
        );
        task.await.unwrap();
    }
    #[tokio::test]
    async fn relays_buffered_bytes_and_preserves_half_close() {
        let backend = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let (mut client, server) = pair().await;
        let (send, receive) = oneshot::channel();
        let relay = tokio::spawn(relay(server, receive, Duration::from_secs(2)));
        client.write_all(b"startup").await.unwrap();
        send.send(backend.local_addr().unwrap().port()).unwrap();
        let (mut backend, _) = backend.accept().await.unwrap();
        let mut received = [0; 7];
        backend.read_exact(&mut received).await.unwrap();
        assert_eq!(&received, b"startup");
        client.shutdown().await.unwrap();
        assert_eq!(backend.read(&mut [0; 1]).await.unwrap(), 0);
        backend.write_all(b"response").await.unwrap();
        backend.shutdown().await.unwrap();
        let mut response = Vec::new();
        client.read_to_end(&mut response).await.unwrap();
        assert_eq!(response, b"response");
        relay.await.unwrap();
    }
    #[tokio::test]
    async fn backend_eof_releases_an_idle_frontend() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let (mut client, server) = pair().await;
        let (send, receive) = oneshot::channel();
        let task = tokio::spawn(relay(server, receive, Duration::from_secs(2)));
        send.send(listener.local_addr().unwrap().port()).unwrap();
        let (mut backend, _) = listener.accept().await.unwrap();
        backend.write_all(b"final response").await.unwrap();
        backend.shutdown().await.unwrap();
        tokio::time::timeout(Duration::from_secs(1), task)
            .await
            .unwrap()
            .unwrap();
        let mut response = Vec::new();
        client.read_to_end(&mut response).await.unwrap();
        assert_eq!(response, b"final response");
    }
}
