use supabricks_core::error::{OperationError, ValidationError};

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error(transparent)]
    Operation(#[from] OperationError),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Sql(#[from] rusqlite::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error("invalid project file: {0}")]
    Toml(#[from] toml::de::Error),
    #[error("cannot serialize project file: {0}")]
    TomlWrite(#[from] toml::ser::Error),
}
pub type Result<T> = std::result::Result<T, Error>;
pub(crate) fn invalid(message: impl Into<String>) -> Error {
    OperationError::InvalidInput(ValidationError::new(
        message,
        "check the local state request",
    ))
    .into()
}
pub(crate) fn conflict(message: impl Into<String>) -> Error {
    OperationError::Conflict(message.into()).into()
}
pub(crate) fn missing(message: impl Into<String>) -> Error {
    OperationError::NotFound(message.into()).into()
}
