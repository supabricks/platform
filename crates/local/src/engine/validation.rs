//! The deletion queue needs only generation validation, not a storage controller.
//! The daemon retains ownership and publishes a read-only snapshot to this worker.
use super::*;
use std::{
    io::Read,
    sync::{
        Arc, RwLock,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ValidationRequest {
    tenants: Vec<TenantGeneration>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TenantGeneration {
    id: String,
    r#gen: u32,
}

pub(crate) struct Validator {
    tenants: Arc<RwLock<HashSet<String>>>,
    stop: Arc<AtomicBool>,
}
impl Validator {
    pub(crate) fn bind(store: &Store) -> Result<Self> {
        let config = RuntimeConfig::load(store)?;
        let listener = TcpListener::bind(("127.0.0.1", config.ports["validator"]))?;
        let server = tiny_http::Server::from_listener(listener, None)
            .map_err(|_| conflict("cannot start local generation validator"))?;
        let tenants = Arc::new(RwLock::new(store.validation_tenants()?));
        let stop = Arc::new(AtomicBool::new(false));
        let generation = u32::try_from(store.generation())
            .map_err(|_| conflict("storage generation exhausted"))?;
        let worker_tenants = tenants.clone();
        let worker_stop = stop.clone();
        let authorization = format!("Bearer {}", config.validation_token);
        std::thread::Builder::new()
            .name("generation-validator".into())
            .spawn(move || {
                while !worker_stop.load(Ordering::Acquire) {
                    let mut request = match server.recv_timeout(Duration::from_millis(200)) {
                        Ok(Some(request)) => request,
                        Ok(None) => continue,
                        Err(_) => break,
                    };
                    let authenticated = request.headers().iter().any(|h| {
                        h.field.equiv("Authorization") && h.value.as_str() == authorization
                    });
                    let (status, body) = if !authenticated {
                        (401, json!({"error":"unauthorized"}))
                    } else if request.method() != &tiny_http::Method::Post
                        || request.url() != "/validate"
                    {
                        (404, json!({"error":"not found"}))
                    } else if request.body_length().is_none_or(|n| n > 65536) {
                        (413, json!({"error":"bounded Content-Length required"}))
                    } else {
                        let mut body = Vec::new();
                        let result = request.as_reader().take(65537).read_to_end(&mut body);
                        match result
                            .ok()
                            .and_then(|_| serde_json::from_slice::<ValidationRequest>(&body).ok())
                        {
                            Some(input)
                                if input.tenants.len() <= 128
                                    && !worker_stop.load(Ordering::Acquire) =>
                            {
                                match worker_tenants.read() {
                                    Ok(known) => (200, validate(input, &known, generation)),
                                    Err(_) => (503, json!({"error":"validator unavailable"})),
                                }
                            }
                            _ => (400, json!({"error":"invalid validation request"})),
                        }
                    };
                    let response = tiny_http::Response::from_string(body.to_string())
                        .with_status_code(status)
                        .with_header(
                            tiny_http::Header::from_bytes("Content-Type", "application/json")
                                .unwrap(),
                        );
                    let _ = request.respond(response);
                }
            })?;
        Ok(Self { tenants, stop })
    }
    pub(crate) fn refresh(&self, store: &Store) -> Result<()> {
        *self
            .tenants
            .write()
            .map_err(|_| conflict("validator unavailable"))? = store.validation_tenants()?;
        Ok(())
    }
}
impl Drop for Validator {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
    }
}
fn validate(input: ValidationRequest, known: &HashSet<String>, generation: u32) -> Value {
    json!({"tenants":input.tenants.into_iter().map(|t| {
        json!({"valid":t.r#gen == generation && known.contains(&t.id),"id":t.id})
    }).collect::<Vec<_>>()})
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn validates_only_owned_tenants_in_current_generation() {
        let input = serde_json::from_value(json!({"tenants":[
            {"id":"known","gen":2},{"id":"known","gen":1},{"id":"other","gen":2}
        ]}))
        .unwrap();
        let output = validate(input, &HashSet::from(["known".into()]), 2);
        assert_eq!(
            output["tenants"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v["valid"].as_bool().unwrap())
                .collect::<Vec<_>>(),
            [true, false, false]
        );
    }
}
