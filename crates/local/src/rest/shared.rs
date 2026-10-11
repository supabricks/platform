//! Behaviour every domain shares: public IDs, paging, timestamps and ETags.
use super::{Call, Problem, Reply};
use serde_json::Value;

/// A public ID is a kind prefix and the platform's UUID without hyphens.
pub fn public_id(prefix: &str, uuid: &str) -> String {
    format!("{prefix}_{}", uuid.replace('-', ""))
}
/// The platform UUID behind a public ID, or `None` if it is not one of `prefix`.
pub fn internal_id(prefix: &str, public: &str) -> Option<String> {
    let hex = public.strip_prefix(prefix)?.strip_prefix('_')?;
    if hex.len() != 32
        || !hex
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return None;
    }
    Some(format!(
        "{}-{}-{}-{}-{}",
        &hex[..8],
        &hex[8..12],
        &hex[12..16],
        &hex[16..20],
        &hex[20..]
    ))
}
pub fn timestamp(ms: i64) -> String {
    chrono::DateTime::from_timestamp_millis(ms)
        .unwrap_or_default()
        .to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}
pub fn etag(revision: i64) -> String {
    format!("\"{revision}\"")
}

impl Call {
    /// The single value of a query parameter. Repeating one is an error.
    pub fn parameter(&self, name: &str) -> Result<Option<&str>, Problem> {
        let mut values = self.query.iter().filter(|(n, _)| n == name);
        let first = values.next().map(|(_, v)| v.as_str());
        if values.next().is_some() {
            return Err(Problem::invalid(format!(
                "{name} was given more than once."
            )));
        }
        Ok(first)
    }
    /// `limit`, between 1 and 200. The default is 50.
    pub fn limit(&self) -> Result<usize, Problem> {
        match self.parameter("limit")? {
            None => Ok(50),
            Some(text) => text
                .parse()
                .ok()
                .filter(|n| (1..=200).contains(n))
                .ok_or_else(|| Problem::invalid("limit must be a whole number from 1 to 200.")),
        }
    }
    /// The position a `cursor` names, as the values the listing is ordered by.
    pub fn cursor(&self) -> Result<Option<Vec<String>>, Problem> {
        // An empty cursor means the first page, as if it were absent.
        let Some(text) = self.parameter("cursor")?.filter(|text| !text.is_empty()) else {
            return Ok(None);
        };
        hex::decode(text)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Vec<String>>(&bytes).ok())
            .map(Some)
            .ok_or_else(|| Problem::invalid("cursor is not one this server issued."))
    }
}
/// An opaque cursor for the position after an item with these ordering values.
pub fn cursor(position: &[&str]) -> String {
    hex::encode(serde_json::to_vec(position).unwrap())
}
/// One page: `items` and the cursor for the next, or null on the last page.
pub fn page(items: Vec<Value>, next: Option<String>) -> Reply {
    Reply::ok(serde_json::json!({"items": items, "next_cursor": next}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn public_ids_round_trip_and_reject_other_shapes() {
        let uuid = "5f0c2a9e-7b1d-4c3f-a8e6-d2b4c1a09f73";
        let public = public_id("prj", uuid);
        assert_eq!(public, "prj_5f0c2a9e7b1d4c3fa8e6d2b4c1a09f73");
        assert_eq!(internal_id("prj", &public).as_deref(), Some(uuid));
        for bad in [
            uuid,
            "db_5f0c2a9e7b1d4c3fa8e6d2b4c1a09f73",
            "prj_5F0C2A9E7B1D4C3FA8E6D2B4C1A09F73",
            "prj_5f0c",
            "prj_5f0c2a9e7b1d4c3fa8e6d2b4c1a09f7é",
            "prj_",
        ] {
            assert_eq!(internal_id("prj", bad), None, "{bad}");
        }
    }
    #[test]
    fn cursors_round_trip() {
        let text = cursor(&["name", "id"]);
        let call = Call {
            caller: crate::rest::Caller::LocalOwner,
            method: "GET".into(),
            path: "/".into(),
            query: vec![("cursor".into(), text)],
            idempotency_key: None,
            if_match: None,
            body: None,
        };
        assert_eq!(
            call.cursor().unwrap(),
            Some(vec!["name".into(), "id".into()])
        );
    }
}
