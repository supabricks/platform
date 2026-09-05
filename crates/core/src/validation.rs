//! Shared input rules; transport adapters preserve their response envelopes.
use crate::error::ValidationError;
use serde_json::Value;

/// Normalize (lowercase) and validate one identifier argument. Normalization
/// is the documented contract (review 003 P2-2): every response echoes the
/// canonical name.
pub fn valid_name(args: &Value, key: &str) -> Result<String, ValidationError> {
    let name = args[key]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(|s| s.to_lowercase())
        .ok_or_else(|| {
            ValidationError::new(
                format!("missing required argument: {key}"),
                format!("pass a {key} argument"),
            )
        })?;
    // Friendly DNS-1123 guard: without it, invalid names surface as raw
    // Kubernetes validation errors (found by the first UI user).
    let valid = name.len() <= 40
        && name
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
        && !name.starts_with('-')
        && !name.ends_with('-');
    if !valid {
        return Err(ValidationError::new(
            format!("invalid {key} {name:?}"),
            "use lowercase letters, digits, and hyphens (max 40 chars), starting and ending with a letter or digit — e.g. \"my-db\"",
        ));
    }
    Ok(name)
}

/// Bounded integer argument (review 003 P1-2): out-of-range values are
/// synchronous structured errors, mirroring the CRD schema bounds.
pub fn bounded_int(
    args: &Value,
    key: &str,
    min: i64,
    max: i64,
) -> Result<Option<i64>, ValidationError> {
    match &args[key] {
        Value::Null => Ok(None),
        v => {
            let n = v.as_i64().ok_or_else(|| {
                ValidationError::new(
                    format!("{key} must be an integer"),
                    format!("pass {key} as an integer"),
                )
            })?;
            if n < min || n > max {
                return Err(ValidationError::new(
                    format!("{key} {n} out of range"),
                    format!("use {min}..={max}"),
                ));
            }
            Ok(Some(n))
        }
    }
}

/// Strict priority parse (review 003 P1-3): unknown values are errors, never
/// silently coerced to Standard.
pub fn parse_priority(args: &Value) -> Result<Option<&'static str>, ValidationError> {
    match args["priority"].as_str() {
        None => Ok(None),
        Some(p) => match p.to_lowercase().as_str() {
            "high" => Ok(Some("High")),
            "standard" => Ok(Some("Standard")),
            "low" => Ok(Some("Low")),
            other => Err(ValidationError::new(
                format!("unknown priority {other:?}"),
                "use one of: high, standard, low",
            )),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    /// Review 003 P1-2/P1-3: boundary validation is synchronous and strict.
    #[test]
    fn invalid_inputs_are_rejected() {
        use serde_json::json;
        assert!(bounded_int(&json!({"cu_limit": -5}), "cu_limit", 1, 960).is_err());
        assert!(bounded_int(&json!({"cu_limit": 0}), "cu_limit", 1, 960).is_err());
        assert!(bounded_int(&json!({"cu_limit": 961}), "cu_limit", 1, 960).is_err());
        assert!(bounded_int(&json!({"ttl_seconds": -10}), "ttl_seconds", 1, 2592000).is_err());
        assert!(
            bounded_int(
                &json!({"suspend_after_seconds": -1}),
                "suspend_after_seconds",
                0,
                86400
            )
            .is_err()
        );
        // 0 = never suspend: explicitly allowed.
        assert_eq!(
            bounded_int(
                &json!({"suspend_after_seconds": 0}),
                "suspend_after_seconds",
                0,
                86400
            )
            .unwrap(),
            Some(0)
        );
        assert!(parse_priority(&json!({"priority": "urgent"})).is_err());
        assert_eq!(
            parse_priority(&json!({"priority": "HIGH"})).unwrap(),
            Some("High")
        );
        assert_eq!(parse_priority(&json!({})).unwrap(), None);
        // Names normalize to lowercase; invalid ones are synchronous errors.
        assert_eq!(
            valid_name(&json!({"database": "Prod"}), "database").unwrap(),
            "prod"
        );
        assert!(valid_name(&json!({"database": "no_scores"}), "database").is_err());
    }
}
