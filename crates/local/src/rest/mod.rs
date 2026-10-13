//! Platform API v1 (`api/openapi.yaml`). Transport-independent: an HTTP server
//! authenticates the caller, then forwards a `Call`; the daemon routes it to
//! the same typed actions the command line uses.
mod branches;
mod compute;
mod databases;
mod extensions;
mod imports;
mod operations;
mod problem;
mod projects;
mod roles;
mod routes;
mod shared;
mod sql;
mod tables;

pub use problem::Problem;
pub use routes::{Operation, ROUTES, Role};

use crate::store::{SCHEMA_VERSION, Store};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;

pub const PREFIX: &str = "/api/v1";
/// Largest request body a transport may forward.
pub const BODY_LIMIT: usize = 60000;

/// Who is calling. Never read from the wire: the daemon sets it from how the
/// request arrived, so nothing a client sends can claim an identity.
#[derive(Clone, Debug, Default)]
pub enum Caller {
    /// Not established. Every call is refused.
    #[default]
    Nobody,
    /// The owner of a local installation, through a launched console session.
    LocalOwner,
    /// A signed-in person or service on a governed server.
    Principal(crate::identity::Context),
}
impl Caller {
    fn profile(&self) -> &'static str {
        match self {
            Self::Principal(_) => "governed",
            Self::LocalOwner | Self::Nobody => "local",
        }
    }
    /// Separates one caller's idempotency keys from another's.
    pub(crate) fn scope(&self) -> String {
        match self {
            Self::Principal(context) => format!("principal:{}", context.actor_id),
            Self::LocalOwner | Self::Nobody => "local-owner".into(),
        }
    }
}

/// What only the owner's own console brings: the project it was launched in,
/// and the engines and connection gateway of this machine when they run.
pub(crate) struct Local<'a> {
    pub binding: &'a crate::api::Binding,
    pub cell: Option<&'a crate::engine::Cell>,
    pub gateway: Option<&'a mut crate::connections::Gateway>,
    /// The statements this server is running, and has lately run, for the API.
    pub statements: Option<&'a mut crate::console::workspace::Queries>,
    /// SQL workers busy with work that is not one of those statements.
    pub busy: usize,
    /// Files being received for import, and the workers that read and load them.
    pub uploads: Option<&'a mut crate::console::ingestion::Uploads>,
    pub imports: Option<&'a mut crate::ingest::service::Service>,
}

/// A piece of a body that a transport sends on as it arrives, because the
/// whole would not fit in one call.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Part {
    /// Where the piece starts.
    pub offset: u64,
    pub hex: String,
    /// The body has ended at `offset`; `hex` is empty.
    pub last: bool,
}

/// One request, after the transport has checked the session.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Call {
    #[serde(skip)]
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
    /// Set by a transport when it repeats a call whose reply said `wait`.
    /// Never read from the wire.
    #[serde(default)]
    pub resume: Option<String>,
    /// Set by a transport that streams a body. Never read from the wire.
    #[serde(default)]
    pub part: Option<Part>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Reply {
    pub status: u16,
    #[serde(default)]
    pub headers: BTreeMap<String, String>,
    #[serde(default)]
    pub body: Option<Value>,
    /// The answer is not ready: the transport repeats the call with this as
    /// `resume` until a reply comes without it. A read that runs SQL uses it,
    /// so the daemon never waits on PostgreSQL. Never sent to a client.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub wait: Option<String>,
}
impl Reply {
    pub fn ok(body: Value) -> Self {
        Self {
            status: 200,
            headers: BTreeMap::new(),
            body: Some(body),
            wait: None,
        }
    }
    /// Not ready yet; see `wait`.
    pub(crate) fn waiting(token: String) -> Self {
        Self {
            status: 503,
            headers: BTreeMap::new(),
            body: None,
            wait: Some(token),
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

/// Whether a signed-in caller may run the operation. A project the caller
/// holds no role in is reported as missing, so its existence is not revealed.
fn authorize(
    store: &Store,
    context: &crate::identity::Context,
    operation: Operation,
    project: &str,
) -> Result<(), Problem> {
    if !operation.governed() {
        return Err(Problem::new(
            403,
            "unavailable_in_profile",
            "A governed server does not offer this operation yet.",
        ));
    }
    let needed = match operation.role() {
        Role::Any => return Ok(()),
        Role::InstallationAdmin => {
            return if store.installation_admin(context) {
                Ok(())
            } else {
                Err(Problem::forbidden(
                    "Only an installation administrator may do this.",
                ))
            };
        }
        Role::Viewer => 1,
        Role::Editor => 2,
        Role::Administrator => 3,
    };
    let held = shared::internal_id("prj", project)
        .map(|id| store.project_role(&context.actor_id, &id))
        .unwrap_or(0);
    match held {
        0 => Err(Problem::not_found("There is no such project.")),
        held if held < needed => Err(Problem::forbidden(
            "Your role in this project does not allow this.",
        )),
        _ => Ok(()),
    }
}

/// Route one call and run it. Never fails: every failure is a problem reply.
/// `host` is the console the local owner launched; a governed call has none.
pub(crate) fn dispatch(store: &mut Store, mut host: Option<Local>, call: &Call) -> Reply {
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
    match &call.caller {
        Caller::Nobody => {
            return Problem::unauthorized("The session is missing or has expired.").into();
        }
        Caller::LocalOwner => {}
        // Checked before the request is read, so a refusal says nothing about
        // whether the request was otherwise valid.
        Caller::Principal(context) => {
            if let Err(problem) = authorize(store, context, found.operation, parameter("project")) {
                let deployment = shared::internal_id("prj", parameter("project"));
                let _ = store.audit_denial(context, deployment.as_deref());
                return problem.into();
            }
        }
    }
    let branch = || {
        (
            parameter("project"),
            parameter("database"),
            parameter("branch"),
        )
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
        Operation::CreateProject => {
            projects::create(store, host.as_ref().map(|host| host.binding), call)
        }
        Operation::UpdateProject => projects::update(store, call, parameter("project")),
        Operation::DeleteProject => projects::delete(store, call, parameter("project")),
        Operation::GetProjectDeletionImpact => {
            projects::deletion_impact(store, parameter("project"))
        }
        Operation::ListDatabases => databases::list(store, call, parameter("project")),
        Operation::GetDatabase => {
            databases::get(store, parameter("project"), parameter("database"))
        }
        Operation::CreateDatabase => {
            databases::create(store, &mut host, call, parameter("project"))
        }
        Operation::ListComputeSizes => Ok(Reply::ok(crate::compute::catalog())),
        Operation::UpdateDatabase => databases::update(
            store,
            &mut host,
            call,
            parameter("project"),
            parameter("database"),
        ),
        Operation::DeleteDatabase => databases::delete(
            store,
            &mut host,
            call,
            parameter("project"),
            parameter("database"),
        ),
        Operation::SuspendDatabase => databases::suspend(
            store,
            &mut host,
            call,
            parameter("project"),
            parameter("database"),
        ),
        Operation::ResumeDatabase => databases::resume(
            store,
            &mut host,
            call,
            parameter("project"),
            parameter("database"),
        ),
        Operation::GetDatabaseConnection => databases::connection(
            store,
            &mut host,
            call,
            parameter("project"),
            parameter("database"),
        ),
        Operation::RevealDatabaseCredentials => databases::credentials(
            store,
            &mut host,
            call,
            parameter("project"),
            parameter("database"),
        ),
        Operation::ResetDatabaseCredentials => databases::reset_credentials(
            store,
            &mut host,
            call,
            parameter("project"),
            parameter("database"),
        ),
        Operation::ListBranches => {
            branches::list(store, call, parameter("project"), parameter("database"))
        }
        Operation::GetBranch => branches::get(
            store,
            parameter("project"),
            parameter("database"),
            parameter("branch"),
        ),
        Operation::CreateBranch => branches::create(
            store,
            &mut host,
            call,
            parameter("project"),
            parameter("database"),
        ),
        Operation::UpdateBranch => branches::update(
            store,
            &mut host,
            call,
            parameter("project"),
            parameter("database"),
            parameter("branch"),
        ),
        Operation::DeleteBranch => branches::delete(
            store,
            &mut host,
            call,
            parameter("project"),
            parameter("database"),
            parameter("branch"),
        ),
        Operation::SuspendBranch => branches::suspend(
            store,
            &mut host,
            call,
            (
                parameter("project"),
                parameter("database"),
                parameter("branch"),
            ),
        ),
        Operation::ResumeBranch => branches::resume(
            store,
            &mut host,
            call,
            (
                parameter("project"),
                parameter("database"),
                parameter("branch"),
            ),
        ),
        Operation::SetDefaultBranch => branches::make_default(
            store,
            &mut host,
            parameter("project"),
            parameter("database"),
            parameter("branch"),
        ),
        Operation::RunStatement => sql::run(store, &mut host, call, parameter("project")),
        Operation::GetStatement => sql::get(
            store,
            &mut host,
            parameter("project"),
            parameter("statement"),
        ),
        Operation::CancelStatement => sql::cancel(
            store,
            &mut host,
            parameter("project"),
            parameter("statement"),
        ),
        Operation::ListSavedQueries => sql::list(store, call, parameter("project")),
        Operation::GetSavedQuery => sql::get_saved(store, parameter("project"), parameter("query")),
        Operation::CreateSavedQuery => sql::create_saved(store, call, parameter("project")),
        Operation::UpdateSavedQuery => {
            sql::update_saved(store, call, parameter("project"), parameter("query"))
        }
        Operation::DeleteSavedQuery => {
            sql::delete_saved(store, call, parameter("project"), parameter("query"))
        }
        Operation::ListTables => tables::list(
            store,
            &mut host,
            call,
            (
                parameter("project"),
                parameter("database"),
                parameter("branch"),
            ),
        ),
        Operation::GetTable => tables::get(
            store,
            &mut host,
            call,
            (
                parameter("project"),
                parameter("database"),
                parameter("branch"),
            ),
            (parameter("schema"), parameter("table")),
        ),
        Operation::ListTableRows => tables::rows(
            store,
            &mut host,
            call,
            (
                parameter("project"),
                parameter("database"),
                parameter("branch"),
            ),
            (parameter("schema"), parameter("table")),
        ),
        Operation::ListImportSources => {
            imports::list_sources(store, &mut host, call, parameter("project"))
        }
        Operation::CreateImportSource => {
            imports::create_source(store, &mut host, call, parameter("project"))
        }
        Operation::GetImportSource => {
            imports::get_source(store, &mut host, parameter("project"), parameter("source"))
        }
        Operation::DeleteImportSource => {
            imports::delete_source(store, &mut host, parameter("project"), parameter("source"))
        }
        Operation::UploadImportSource => imports::upload(
            store,
            &mut host,
            call,
            parameter("project"),
            parameter("source"),
        ),
        Operation::InspectImportSource => imports::inspect(
            store,
            &mut host,
            call,
            parameter("project"),
            parameter("source"),
        ),
        Operation::ListImports => imports::list(store, call, parameter("project")),
        Operation::CreateImport => imports::create(store, &mut host, call, parameter("project")),
        Operation::GetImport => imports::get(store, parameter("project"), parameter("import")),
        Operation::CancelImport => {
            imports::cancel(store, parameter("project"), parameter("import"))
        }
        Operation::RetryImport => {
            imports::retry(store, call, parameter("project"), parameter("import"))
        }
        Operation::ListExtensions => extensions::list(store, &mut host, call, branch()),
        Operation::EnableExtension => {
            extensions::enable(store, &mut host, call, branch(), parameter("extension"))
        }
        Operation::DisableExtension => {
            extensions::disable(store, &mut host, call, branch(), parameter("extension"))
        }
        Operation::ListRoles => roles::list(store, &mut host, call, branch()),
        Operation::CreateRole => roles::create(store, &mut host, call, branch()),
        Operation::GetRole => roles::get(store, &mut host, call, branch(), parameter("role")),
        Operation::UpdateRole => roles::update(store, &mut host, call, branch(), parameter("role")),
        Operation::DeleteRole => roles::delete(store, &mut host, call, branch(), parameter("role")),
        Operation::ResetRolePassword => {
            roles::reset_password(store, &mut host, call, branch(), parameter("role"))
        }
        Operation::SetRolePrivileges => roles::set_privileges(
            store,
            &mut host,
            call,
            branch(),
            parameter("role"),
            parameter("schema"),
        ),
        Operation::ListOperations => operations::list(store, call, parameter("project")),
        Operation::GetOperation => {
            operations::get(store, parameter("project"), parameter("operation"))
        }
        Operation::CancelOperation => {
            operations::cancel(store, call, parameter("project"), parameter("operation"))
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
