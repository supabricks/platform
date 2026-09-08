//! Bounded application SQL on dedicated workers, never the single writer.
use crate::store::{Result, error::invalid};
use futures_util::TryStreamExt;
use serde_json::{Value, json};
use std::{
    io,
    pin::Pin,
    task::{Context, Poll},
    time::Duration,
};
use supabricks_core::error::OperationError;
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, ReadBuf},
    net::TcpStream,
};
use tokio_postgres::{Config, NoTls, SimpleQueryMessage};
pub const WORKERS: usize = 4;
const BYTES: usize = 262144;
#[derive(Clone, Debug, serde::Serialize, serde::Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Query {
    pub sql: String,
    pub read_only: bool,
    pub max_rows: usize,
    pub timeout_ms: u64,
}
impl Query {
    pub fn validate(&self) -> Result<()> {
        if self.sql.trim().is_empty()
            || self.sql.len() > 32768
            || !(1..=1000).contains(&self.max_rows)
            || !(100..=30000).contains(&self.timeout_ms)
        {
            return Err(invalid(
                "SQL requires 1–32768 bytes, 1–1000 rows and a 100–30000 ms deadline",
            ));
        }
        Ok(())
    }
    pub fn catalog() -> Self {
        Self {sql:"SELECT table_schema, table_name, column_name, data_type, is_nullable, ordinal_position::text FROM information_schema.columns WHERE table_schema NOT IN ('pg_catalog','information_schema','_supabricks') ORDER BY table_schema,table_name,ordinal_position".into(),read_only:true,max_rows:1000,timeout_ms:10000}
    }
    pub fn run(self, target: Value) -> Result<Value> {
        self.validate()?;
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?
            .block_on(self.execute(target))
    }
    pub fn run_cancellable(
        self,
        target: Value,
        cancel: tokio::sync::watch::Receiver<bool>,
    ) -> Result<Value> {
        self.validate()?;
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?
            .block_on(self.execute_cancellable(target, Some(cancel)))
    }
    async fn execute(self, target: Value) -> Result<Value> {
        self.execute_cancellable(target, None).await
    }
    async fn execute_cancellable(
        self,
        target: Value,
        mut cancellation: Option<tokio::sync::watch::Receiver<bool>>,
    ) -> Result<Value> {
        let started = std::time::Instant::now();
        let connect = async {
            let port = target["port"]
                .as_u64()
                .ok_or_else(|| invalid("missing gateway port"))? as u16;
            let socket = TcpStream::connect(("127.0.0.1", port)).await?;
            let (client,connection)=Config::new().user("supabricks_owner")
                    .password(target["password"].as_str().ok_or_else(||invalid("missing app credential"))?)
                    .dbname("postgres").application_name("supabricks-local-api")
                    .options(format!("-c statement_timeout={} -c lock_timeout=1000 -c idle_in_transaction_session_timeout=5000",self.timeout_ms))
                    .connect_raw(FramedSocket::new(socket),NoTls).await.map_err(db_error)?;
            Ok::<_, crate::store::Error>((client, ConnectionTask(tokio::spawn(connection))))
        };
        let (client, _connection) = tokio::select! {
            biased;
            _ = cancelled(&mut cancellation) => return Err(cancel_error()),
            result = tokio::time::timeout(Duration::from_secs(32), connect) => result.map_err(|_| OperationError::Unavailable("SQL connection deadline exceeded".into()))??,
        };
        let cancel = client.cancel_token();
        let run = async {
            client
                .batch_execute(if self.read_only {
                    "BEGIN READ ONLY"
                } else {
                    "BEGIN READ WRITE"
                })
                .await
                .map_err(db_error)?;
            // PostgreSQL Parse rejects multiple statements. Execute the identical
            // single statement through simple protocol for lossless text values.
            let statement = client.prepare(&self.sql).await.map_err(db_error)?;
            let columns: Vec<_> = statement
                .columns()
                .iter()
                .map(|c| json!({"name":c.name(),"type":c.type_().name(),"oid":c.type_().oid()}))
                .collect();
            if serde_json::to_vec(&columns)?.len() > BYTES - 4096 {
                return Err(limit());
            }
            let stream = client.simple_query_raw(&self.sql).await.map_err(db_error)?;
            futures_util::pin_mut!(stream);
            let mut rows = Vec::new();
            let mut affected = 0;
            let mut bytes = serde_json::to_vec(&columns)?.len();
            while let Some(message) = stream.try_next().await.map_err(db_error)? {
                match message {
                    SimpleQueryMessage::Row(row) => {
                        let row: Vec<_> = (0..row.len())
                            .map(|i| row.get(i).map(str::to_owned))
                            .collect();
                        bytes += serde_json::to_vec(&row)?.len() + 1;
                        if rows.len() >= self.max_rows || bytes > BYTES - 4096 {
                            return Err(limit());
                        }
                        rows.push(row);
                    }
                    SimpleQueryMessage::CommandComplete(n) => affected = n,
                    _ => {}
                }
            }
            client.batch_execute("COMMIT").await.map_err(db_error)?;
            Ok(
                json!({"branch_id":target["branch_id"],"columns":columns,"rows":rows,"affected_rows":affected,"read_only":self.read_only}),
            )
        };
        let budget = Duration::from_millis(self.timeout_ms)
            .min(Duration::from_secs(44).saturating_sub(started.elapsed()));
        let result = tokio::select! {
            biased;
            result = tokio::time::timeout(budget, run) => result,
            _ = cancelled(&mut cancellation) => Ok(Err(cancel_error())),
        };
        if !matches!(&result, Ok(Ok(_))) {
            // Independent cancellation plus connection-task drop closes the
            // session even if SQL resets statement_timeout. No write replay.
            let _ = tokio::time::timeout(Duration::from_secs(1), async {
                let socket =
                    TcpStream::connect(("127.0.0.1", target["port"].as_u64().unwrap() as u16))
                        .await
                        .map_err(|_| ())?;
                let mut socket = CancelSocket(socket);
                cancel
                    .cancel_query_raw(&mut socket, NoTls)
                    .await
                    .map_err(|_| ())?;
                // Keep the cancel socket open until PostgreSQL closes it. An
                // early EOF can reach the gateway before its route is ready,
                // causing the startup buffer (and cancel packet) to be discarded.
                socket.read(&mut [0; 1]).await.map(|_| ()).map_err(|_| ())
            })
            .await;
        }
        result.map_err(|_|OperationError::Query {sqlstate:"57014".into(),message:"SQL deadline exceeded; transaction aborted unless commit was already acknowledged by PostgreSQL".into()})?
    }
}
async fn cancelled(receiver: &mut Option<tokio::sync::watch::Receiver<bool>>) {
    match receiver {
        Some(rx) => {
            let _ = rx.wait_for(|v| *v).await;
        }
        None => std::future::pending::<()>().await,
    }
}
fn cancel_error() -> crate::store::Error {
    OperationError::Query { sqlstate: "57014".into(), message: "Cancellation requested; a write interrupted near COMMIT may have committed. Inspect the database before retrying; no automatic replay.".into() }.into()
}
fn limit() -> crate::store::Error {
    OperationError::Query {
        sqlstate: "54000".into(),
        message:
            "SQL result limit exceeded; use LIMIT, smaller values or a regular PostgreSQL client"
                .into(),
    }
    .into()
}
fn db_error(e: tokio_postgres::Error) -> crate::store::Error {
    if let Some(db) = e.as_db_error() {
        // Do not echo detail/context, query text or server notices (can include data).
        OperationError::Query {
            sqlstate: db.code().code().into(),
            message: db.message().chars().take(512).collect(),
        }
        .into()
    } else {
        OperationError::Unavailable("SQL connection failed or PostgreSQL response exceeded the 1 MiB frame limit; inspect doctor before retrying writes".into()).into()
    }
}
/// tokio-postgres half-closes the cancel socket after sending its packet.
/// The gateway may still be routing that startup packet. Keep both directions
/// open until the server closes or the bounded cancellation deadline expires.
struct CancelSocket(TcpStream);
impl AsyncRead for CancelSocket {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().0).poll_read(cx, buf)
    }
}
impl AsyncWrite for CancelSocket {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.get_mut().0).poll_write(cx, buf)
    }
    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().0).poll_flush(cx)
    }
    fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}
struct ConnectionTask(tokio::task::JoinHandle<std::result::Result<(), tokio_postgres::Error>>);
impl Drop for ConnectionTask {
    fn drop(&mut self) {
        self.0.abort();
    }
}
/// Reject an oversized backend message before tokio-postgres allocates its body.
/// No TLS negotiation here: this is the private local app worker over loopback.
struct FramedSocket {
    socket: TcpStream,
    header: [u8; 5],
    read: usize,
    body: usize,
}
impl FramedSocket {
    fn new(socket: TcpStream) -> Self {
        Self {
            socket,
            header: [0; 5],
            read: 0,
            body: 0,
        }
    }
}
impl AsyncRead for FramedSocket {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        if buf.remaining() == 0 {
            return Poll::Ready(Ok(()));
        }
        if this.body == 0 {
            while this.read < 5 {
                let mut header = ReadBuf::new(&mut this.header[this.read..]);
                match Pin::new(&mut this.socket).poll_read(cx, &mut header) {
                    Poll::Ready(Ok(())) => {
                        let n = header.filled().len();
                        if n == 0 {
                            return Poll::Ready(Err(io::ErrorKind::UnexpectedEof.into()));
                        }
                        this.read += n;
                    }
                    other => return other,
                }
            }
            let size = u32::from_be_bytes(this.header[1..].try_into().unwrap()) as usize;
            if !(4..=1048576).contains(&size) {
                return Poll::Ready(Err(io::Error::other(
                    "PostgreSQL frame exceeds SQL worker limit",
                )));
            }
            // The caller's normal read buffer has ample room; support tiny reads too.
            this.body = size + 1;
        }
        if this.read > 0 {
            let offset = 5 - this.read;
            let n = this.read.min(buf.remaining());
            buf.put_slice(&this.header[offset..offset + n]);
            this.read -= n;
            this.body -= n;
            return Poll::Ready(Ok(()));
        }
        let n = this.body.min(buf.remaining());
        let mut part = ReadBuf::new(buf.initialize_unfilled_to(n));
        match Pin::new(&mut this.socket).poll_read(cx, &mut part) {
            Poll::Ready(Ok(())) => {
                let n = part.filled().len();
                this.body -= n;
                buf.advance(n);
                Poll::Ready(Ok(()))
            }
            other => other,
        }
    }
}
impl AsyncWrite for FramedSocket {
    fn poll_write(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.get_mut().socket).poll_write(cx, buf)
    }
    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().socket).poll_flush(cx)
    }
    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().socket).poll_shutdown(cx)
    }
}
