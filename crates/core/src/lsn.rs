//! PostgreSQL WAL positions. Malformed input must never compare as zero.
use crate::error::ValidationError;
use serde::{Deserialize, Serialize};
use std::{fmt, str::FromStr};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Hash, Serialize, Deserialize)]
#[serde(try_from = "String", into = "String")]
pub struct Lsn(u64);

impl FromStr for Lsn {
    type Err = ValidationError;
    fn from_str(value: &str) -> Result<Self, Self::Err> {
        let parse = || {
            let (hi, lo) = value.split_once('/')?;
            if [hi, lo].iter().any(|part| {
                part.is_empty() || part.len() > 8 || !part.bytes().all(|b| b.is_ascii_hexdigit())
            }) {
                return None;
            }
            Some(Self(
                (u64::from(u32::from_str_radix(hi, 16).ok()?) << 32)
                    | u64::from(u32::from_str_radix(lo, 16).ok()?),
            ))
        };
        parse().ok_or_else(|| {
            ValidationError::new(
                "invalid LSN",
                "use two 1–8 digit hexadecimal words separated by /",
            )
        })
    }
}
impl fmt::Display for Lsn {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:X}/{:X}", self.0 >> 32, self.0 & 0xffff_ffff)
    }
}
impl TryFrom<String> for Lsn {
    type Error = ValidationError;
    fn try_from(value: String) -> Result<Self, Self::Error> {
        value.parse()
    }
}
impl From<Lsn> for String {
    fn from(value: Lsn) -> Self {
        value.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn strict_parse_and_order() {
        for invalid in [
            "",
            "0",
            "0/",
            "/0",
            "0/100000000",
            "100000000/0",
            "+1/0",
            "0/-1",
            "0/1/2",
            " 0/0",
            "nope",
        ] {
            assert!(invalid.parse::<Lsn>().is_err(), "{invalid}");
        }
        assert!("1/0".parse::<Lsn>().unwrap() > "0/FFFFFFFF".parse().unwrap());
        assert_eq!("00/ab".parse::<Lsn>().unwrap().to_string(), "0/AB");
        assert!(serde_json::from_str::<Lsn>("\"bogus\"").is_err());
        let max = "FFFFFFFF/FFFFFFFF".parse::<Lsn>().unwrap();
        assert_eq!(
            serde_json::from_str::<Lsn>(&serde_json::to_string(&max).unwrap()).unwrap(),
            max
        );
    }
}
