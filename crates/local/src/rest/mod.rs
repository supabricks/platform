//! Platform API v1 (`api/openapi.yaml`). Transport-independent: an HTTP server
//! authenticates the caller, then forwards a `Call`; the daemon routes it to
//! the same typed actions the command line uses.
mod operations;
mod problem;
mod projects;
mod routes;
mod shared;

pub use problem::Problem;
pub use routes::{Operation, ROUTES};

use crate::store::{SCHEMA_VERSION, Store};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;

pub const PREFIX: &str = "/api/v1";
/// Largest request body a transport may forward.
pub const BODY_LIMIT: usize = 60000;

/// Who is calling, as established by the transport.
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Caller {
    /// The owner of a local installation, through a launched console session.
    LocalOwner,
}
impl Caller {
    fn profile(self) -> &'static str {
        match self {
            Self::LocalOwner => "local",
        }
    }
}

/// One request, after the transport has checked the session.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Call {
    pub caller: Caller,
    pub method: String,
    /// Path below `/api/v1`, starting with `/`.
    pub path: String,
    #[serde(default)]
    pub query: Vec<(String, String)>,
    #[serde(default)]
    pub idempotency_key: Option<String>,
    #[serde(default)]
    pub if_match: Option<String>,
    #[serde(default)]
    pub body: Option<Value>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Reply {
    pub status: u16,
    #[serde(default)]
    pub headers: BTreeMap<String, String>,
    #[serde(default)]
    pub body: Option<Value>,
}
impl Reply {
    pub fn ok(body: Value) -> Self {
        Self {
            status: 200,
            headers: BTreeMap::new(),
            body: Some(body),
        }
    }
    /// The media type of the body. Every error is a problem document.
    pub fn content_type(&self) -> &'static str {
        if self.status >= 400 {
            "application/problem+json"
        } else {
            "application/json"
        }
    }
}

/// A matched route and its path parameters, in template order.
#[derive(Debug, PartialEq)]
pub struct Match<'a> {
    pub operation: Operation,
    pub parameters: Vec<(&'static str, &'a str)>,
}

fn template<'a>(template: &'static str, path: &'a str) -> Option<Vec<(&'static str, &'a str)>> {
    let (mut want, mut have) = (template.split('/'), path.split('/'));
    let mut parameters = Vec::new();
    loop {
        match (want.next(), have.next()) {
            (None, None) => return Some(parameters),
            (Some(w), Some(h)) => match w.strip_prefix('{').and_then(|w| w.strip_suffix('}')) {
                Some(name) if !h.is_empty() => parameters.push((name, h)),
                Some(_) => return None,
                None if w == h => {}
                None => return None,
            },
            _ => return None,
        }
    }
}

/// Find the operation for a method and path. An unknown path is `404`; a known
/// path with another method is `405` with the methods it does accept.
pub fn resolve<'a>(method: &str, path: &'a str) -> Result<Match<'a>, Reply> {
    let mut allowed = Vec::new();
    for (verb, pattern, operation) in ROUTES {
        if let Some(parameters) = template(pattern, path) {
            if *verb == method {
                return Ok(Match {
                    operation: *operation,
                    parameters,
                });
            }
            allowed.push(*verb);
        }
    }
    if allowed.is_empty() {
        return Err(Problem::not_found("There is nothing at this path.").into());
    }
    let mut reply: Reply = Problem::new(
        405,
        "method_not_allowed",
        "This path does not accept that method.",
    )
    .into();
    reply.headers.insert("Allow".into(), allowed.join(", "));
    Err(reply)
}

/// Route one call and run it. Never fails: every failure is a problem reply.
pub fn dispatch(store: &mut Store, host: &crate::api::Binding, call: &Call) -> Reply {
    let found = match resolve(&call.method, &call.path) {
        Ok(found) => found,
        Err(reply) => return reply,
    };
    let parameter = |name: &str| {
        found
            .parameters
            .iter()
            .find(|(n, _)| *n == name)
            .map(|(_, value)| *value)
            .unwrap_or_default()
    };
    let result = match found.operation {
        Operation::GetServerVersion => Ok(Reply::ok(json!({
            "version": env!("CARGO_PKG_VERSION"),
            "api_version": "v1",
            "profile": call.caller.profile(),
            "schema_version": SCHEMA_VERSION,
        }))),
        Operation::ListProjects => projects::list(store, call),
        Operation::GetProject => projects::get(store, call, parameter("project")),
        Operation::CreateProject => projects::create(store, host, call),
        Operation::ListOperations => operations::list(store, call, parameter("project")),
        Operation::GetOperation => {
            operations::get(store, parameter("project"), parameter("operation"))
        }
        Operation::CancelOperation => {
            operations::cancel(store, parameter("project"), parameter("operation"))
        }
    };
    result.unwrap_or_else(Reply::from)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn templates_match_whole_paths_and_capture_parameters() {
        assert_eq!(template("/a/{x}/b", "/a/1/b"), Some(vec![("x", "1")]));
        assert_eq!(template("/a", "/a"), Some(vec![]));
        for path in ["/a/1", "/a/1/b/c", "/a//b", "/a/1/c", "a/1/b"] {
            assert_eq!(template("/a/{x}/b", path), None, "{path}");
        }
        assert_eq!(template("/a", "/a/"), None);
    }

    #[test]
    fn unknown_paths_and_methods_are_problems() {
        let missing = resolve("GET", "/nothing").unwrap_err();
        assert_eq!(missing.status, 404);
        assert_eq!(missing.body.unwrap()["code"], "not_found");
        let wrong = resolve("DELETE", "/server/version").unwrap_err();
        assert_eq!(wrong.status, 405);
        assert_eq!(wrong.headers["Allow"], "GET");
        assert_eq!(wrong.body.unwrap()["code"], "method_not_allowed");
        assert_eq!(
            resolve("GET", "/server/version").unwrap().operation,
            Operation::GetServerVersion
        );
    }

    #[test]
    fn route_table_is_unambiguous() {
        for (i, (verb, pattern, _)) in ROUTES.iter().enumerate() {
            assert!(pattern.starts_with('/') && !pattern.ends_with('/'));
            for (other_verb, other, _) in &ROUTES[i + 1..] {
                let same_shape = pattern.split('/').count() == other.split('/').count()
                    && pattern
                        .split('/')
                        .zip(other.split('/'))
                        .all(|(a, b)| a == b || (a.starts_with('{') && b.starts_with('{')));
                assert!(
                    !(verb == other_verb && same_shape),
                    "{verb} {pattern} and {other}"
                );
            }
        }
    }
}
