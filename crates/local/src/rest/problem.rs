//! Error bodies (RFC 9457). `code` is the stable part of the contract.
use super::Reply;
use crate::store::Error;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use supabricks_core::error::OperationError;

#[derive(Clone, Debug, PartialEq)]
pub struct Problem {
    pub status: u16,
    pub code: &'static str,
    pub detail: String,
}
impl Problem {
    pub fn new(status: u16, code: &'static str, detail: impl Into<String>) -> Self {
        Self {
            status,
            code,
            detail: detail.into(),
        }
    }
    pub fn not_found(detail: impl Into<String>) -> Self {
        Self::new(404, "not_found", detail)
    }
    pub fn invalid(detail: impl Into<String>) -> Self {
        Self::new(400, "invalid_request", detail)
    }
    pub fn unauthorized(detail: impl Into<String>) -> Self {
        Self::new(401, "unauthorized", detail)
    }
    pub fn forbidden(detail: impl Into<String>) -> Self {
        Self::new(403, "not_permitted", detail)
    }
    pub fn unavailable(detail: impl Into<String>) -> Self {
        Self::new(503, "unavailable", detail)
    }
    fn title(&self) -> &'static str {
        match self.status {
            400 => "Invalid request",
            401 => "Not signed in",
            403 => "Not permitted",
            404 => "Not found",
            405 => "Method not allowed",
            408 => "Request timed out",
            409 => "Conflict",
            412 => "Precondition failed",
            413 => "Request too large",
            415 => "Unsupported media type",
            428 => "Precondition required",
            429 => "Too many requests",
            503 => "Unavailable",
            _ => "Server error",
        }
    }
    pub fn body(&self) -> Value {
        json!({
            "type": format!("https://supabricks.dev/problems/{}", self.code.replace('_', "-")),
            "title": self.title(),
            "status": self.status,
            "code": self.code,
            "detail": self.detail,
        })
    }
}
impl From<Problem> for Reply {
    fn from(problem: Problem) -> Self {
        Self {
            status: problem.status,
            headers: BTreeMap::new(),
            body: Some(problem.body()),
        }
    }
}
/// The one place a platform error becomes an HTTP status and code.
impl From<&Error> for Problem {
    fn from(error: &Error) -> Self {
        match error {
            Error::Operation(OperationError::InvalidInput(e)) => Self::invalid(e.message.clone()),
            Error::Operation(OperationError::NotFound(s)) => Self::not_found(s.clone()),
            // A conflict may name a more specific stable code as a prefix.
            Error::Operation(OperationError::Conflict(s)) => match s.split_once(": ") {
                Some(("name_taken", detail)) => Self::new(409, "name_taken", detail),
                _ => Self::new(409, "conflict", s.clone()),
            },
            Error::Operation(OperationError::Unavailable(s)) => Self::unavailable(s.clone()),
            // Internal detail (paths, SQL) stays in the server log.
            _ => Self::new(
                500,
                "internal_error",
                "The server could not complete the request.",
            ),
        }
    }
}
