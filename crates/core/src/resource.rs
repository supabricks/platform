//! Portable identities are independent of display names and adapter resources.
use crate::{error::ValidationError, lsn::Lsn};
use serde::{Deserialize, Serialize};
use std::{fmt, str::FromStr};

macro_rules! uuid_id {
    ($($name:ident),+) => { $(
        #[derive(Clone, Copy, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
        #[serde(transparent)]
        pub struct $name(uuid::Uuid);
        impl $name {
            pub fn new() -> Self { Self(uuid::Uuid::new_v4()) }
        }
        impl Default for $name { fn default() -> Self { Self::new() } }
        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { self.0.fmt(f) }
        }
        impl FromStr for $name {
            type Err = uuid::Error;
            fn from_str(s: &str) -> Result<Self, Self::Err> { Ok(Self(s.parse()?)) }
        }
    )+ };
}
uuid_id!(ProjectId, BranchId, EndpointId, OperationId);

macro_rules! engine_id {
    ($($name:ident),+) => { $(
        #[derive(Clone, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
        #[serde(try_from = "String", into = "String")]
        pub struct $name(String);
        impl FromStr for $name {
            type Err = ValidationError;
            fn from_str(s: &str) -> Result<Self, Self::Err> {
                if s.len() != 32 || !s.bytes().all(|b| b.is_ascii_hexdigit()) {
                    return Err(ValidationError::new(concat!("invalid ", stringify!($name)), "use exactly 32 hexadecimal digits"));
                }
                Ok(Self(s.to_ascii_lowercase()))
            }
        }
        impl TryFrom<String> for $name {
            type Error = ValidationError;
            fn try_from(s: String) -> Result<Self, Self::Error> { s.parse() }
        }
        impl From<$name> for String { fn from(id: $name) -> Self { id.0 } }
        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { self.0.fmt(f) }
        }
    )+ };
}
engine_id!(TenantId, TimelineId);

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(try_from = "u16", into = "u16")]
pub enum PgMajor {
    V16,
    V17,
}
impl TryFrom<u16> for PgMajor {
    type Error = ValidationError;
    fn try_from(n: u16) -> Result<Self, Self::Error> {
        match n {
            16 => Ok(Self::V16),
            17 => Ok(Self::V17),
            _ => Err(ValidationError::new(
                "unsupported PostgreSQL major",
                "select an explicitly supported engine: 16 or 17",
            )),
        }
    }
}
impl From<PgMajor> for u16 {
    fn from(major: PgMajor) -> Self {
        match major {
            PgMajor::V16 => 16,
            PgMajor::V17 => 17,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Project {
    pub id: ProjectId,
    pub name: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Branch {
    pub id: BranchId,
    pub project_id: ProjectId,
    pub name: String,
    pub tenant_id: TenantId,
    pub timeline_id: TimelineId,
    pub parent_id: Option<BranchId>,
    pub ancestor_lsn: Option<Lsn>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DesiredState {
    Running,
    Suspended,
    Deleted,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Endpoint {
    pub id: EndpointId,
    pub branch_id: BranchId,
    pub pg_major: PgMajor,
    pub desired_state: DesiredState,
}

/// Adapters report implemented behavior; a renderable spec alone grants none.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Capabilities {
    pub pg_majors: Vec<PgMajor>,
    pub head_branching: bool,
    pub branch_at_time: bool,
    pub suspend_resume: bool,
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn identities_roundtrip_and_reject_invalid_wire_values() {
        let id = ProjectId::new();
        assert_eq!(id.to_string().parse::<ProjectId>().unwrap(), id);
        assert!(serde_json::from_str::<ProjectId>("\"prod\"").is_err());
        assert!(serde_json::from_str::<TenantId>("\"../tenant\"").is_err());
        assert!(serde_json::from_str::<PgMajor>("18").is_err());
        let timeline: TimelineId = "ABCDEF0123456789ABCDEF0123456789".parse().unwrap();
        assert_eq!(timeline.to_string(), "abcdef0123456789abcdef0123456789");
        assert_eq!(
            serde_json::from_str::<TimelineId>(&serde_json::to_string(&timeline).unwrap()).unwrap(),
            timeline
        );
    }
}
