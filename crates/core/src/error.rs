use serde::{Deserialize, Serialize};

/// Validation detail; adapters choose their own HTTP/MCP/Kubernetes mapping.
#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error, Serialize, Deserialize)]
#[error("{message}")]
pub struct ValidationError {
    pub message: String,
    pub hint: String,
}
impl ValidationError {
    pub fn new(message: impl Into<String>, hint: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            hint: hint.into(),
        }
    }
}

#[derive(Debug, thiserror::Error, Serialize, Deserialize)]
#[serde(tag = "code", content = "detail", rename_all = "snake_case")]
pub enum OperationError {
    #[error(transparent)]
    InvalidInput(#[from] ValidationError),
    #[error("resource not found: {0}")]
    NotFound(String),
    #[error("operation conflict: {0}")]
    Conflict(String),
    #[error("temporarily unavailable: {0}")]
    Unavailable(String),
}
impl OperationError {
    pub fn retryable(&self) -> bool {
        matches!(self, Self::Unavailable(_))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn conflicts_are_terminal_and_preserve_wire_classification() {
        let error = OperationError::Conflict("idempotency key reused with different inputs".into());
        let wire = serde_json::to_value(&error).unwrap();
        assert_eq!(wire["code"], "conflict");
        let decoded: OperationError = serde_json::from_value(wire).unwrap();
        assert!(!decoded.retryable());
        assert!(OperationError::Unavailable("storage starting".into()).retryable());
        assert!(
            !OperationError::InvalidInput(ValidationError::new("bad name", "use a valid name"))
                .retryable()
        );
    }
}
