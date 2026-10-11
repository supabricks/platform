//! HTTP transport for the platform API v1: session, CSRF and framing only.
//! Routing and behaviour live in `crate::rest`, on the daemon.
use super::{CONTROL_TIMEOUT, Reply, State, response, single};
use crate::{
    client,
    daemon::Request,
    rest::{self, BODY_LIMIT, Call, Caller, Problem},
};
use http_body_util::{BodyExt, Limited};
use hyper::{Method, Request as HttpRequest, body::Incoming, header::HeaderValue};
use percent_encoding::percent_decode_str;
use serde_json::Value;

/// Longest a transport keeps asking for an answer that is not ready.
const WAIT: std::time::Duration = std::time::Duration::from_secs(45);

pub(super) fn reply(reply: rest::Reply) -> Reply {
    let body = reply
        .body
        .as_ref()
        .map(|body| serde_json::to_vec(body).unwrap())
        .unwrap_or_default();
    let mut out = response(reply.status, reply.content_type(), body);
    for (name, value) in &reply.headers {
        if let (Ok(name), Ok(value)) = (
            hyper::header::HeaderName::from_bytes(name.as_bytes()),
            HeaderValue::from_str(value),
        ) {
            out.headers_mut().insert(name, value);
        }
    }
    out
}

fn query(raw: Option<&str>) -> Option<Vec<(String, String)>> {
    let decode = |part: &str| {
        percent_decode_str(&part.replace('+', " "))
            .decode_utf8()
            .ok()
            .map(|s| s.into_owned())
    };
    raw.unwrap_or_default()
        .split('&')
        .filter(|pair| !pair.is_empty())
        .map(|pair| {
            let (name, value) = pair.split_once('=').unwrap_or((pair, ""));
            Some((decode(name)?, decode(value)?))
        })
        .collect()
}

impl State {
    pub(super) async fn rest(&self, path: String, request: HttpRequest<Incoming>) -> Reply {
        let headers = request.headers();
        let Some((_, csrf)) = self.authenticated(headers) else {
            return reply(
                Problem::unauthorized("The session is missing or has expired. Sign in again.")
                    .into(),
            );
        };
        let method = request.method().clone();
        if !matches!(method, Method::GET | Method::HEAD)
            && (single(headers, "origin") != Some(self.origin.as_str())
                || single(headers, "x-csrf-token") != Some(csrf.as_str()))
        {
            return reply(
                Problem::forbidden(
                    "A change from a browser session needs its origin and X-CSRF-Token.",
                )
                .into(),
            );
        }
        let Some(query) = query(request.uri().query()) else {
            return reply(Problem::invalid("The query string is not valid UTF-8.").into());
        };
        let text = |name: &str| single(headers, name).map(str::to_owned);
        let (idempotency_key, if_match) = (text("idempotency-key"), text("if-match"));
        let json = single(headers, "content-type").is_some_and(|kind| {
            kind.split(';').next().unwrap_or_default().trim() == "application/json"
        });
        let bytes = match Limited::new(request.into_body(), BODY_LIMIT)
            .collect()
            .await
        {
            Ok(body) => body.to_bytes(),
            Err(_) => {
                return reply(
                    Problem::new(413, "request_too_large", "The request body is too large.").into(),
                );
            }
        };
        let body = if bytes.is_empty() {
            None
        } else if !json {
            return reply(
                Problem::new(
                    415,
                    "unsupported_media_type",
                    "Send the request body as application/json.",
                )
                .into(),
            );
        } else {
            match serde_json::from_slice::<Value>(&bytes) {
                Ok(body) => Some(body),
                Err(_) => {
                    return reply(Problem::invalid("The request body is not valid JSON.").into());
                }
            }
        };
        let call = Call {
            // The daemon sets the caller; this transport only serves the local owner.
            caller: Caller::default(),
            method: method.as_str().to_owned(),
            path,
            query,
            idempotency_key,
            if_match,
            body,
            resume: None,
        };
        // A read that runs SQL answers `wait` until PostgreSQL has; the daemon
        // bounds the SQL itself, so this only guards against a lost answer.
        let until = tokio::time::Instant::now() + WAIT;
        let mut call = call;
        loop {
            let answer = self.forward(call.clone()).await;
            match answer.wait.clone() {
                None => return reply(answer),
                Some(_) if tokio::time::Instant::now() >= until => {
                    return reply(
                        Problem::unavailable("The database took too long to answer.").into(),
                    );
                }
                Some(token) => {
                    call.resume = Some(token);
                    tokio::time::sleep(std::time::Duration::from_millis(15)).await;
                }
            }
        }
    }
    async fn forward(&self, call: Call) -> rest::Reply {
        let config = self.config.clone();
        let result = tokio::task::spawn_blocking(move || {
            client::request_timeout(
                &config.root,
                Request::Rest {
                    binding: config.binding,
                    generation: config.generation,
                    call,
                },
                CONTROL_TIMEOUT,
            )
        })
        .await;
        match result {
            Ok(Ok(value)) => serde_json::from_value(value).unwrap_or_else(|_| {
                Problem::new(
                    500,
                    "internal_error",
                    "The server returned an unreadable reply.",
                )
                .into()
            }),
            Ok(Err(error)) => Problem::from(&error).into(),
            Err(_) => Problem::unavailable(
                "The server is busy. Check the operation before retrying a change.",
            )
            .into(),
        }
    }
}
