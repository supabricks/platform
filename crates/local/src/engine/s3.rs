//! Only bucket provisioning belongs here; the engine uses its existing S3 client.
use super::http::Http;
use crate::store::Result;
use hmac::{Hmac, KeyInit, Mac};
use sha2::{Digest, Sha256};
fn sign(key: &[u8], value: &str) -> Vec<u8> {
    let mut h =
        <Hmac<Sha256> as KeyInit>::new_from_slice(key).expect("HMAC accepts any key length");
    h.update(value.as_bytes());
    h.finalize().into_bytes().to_vec()
}
pub fn ensure_bucket(port: u16, access: &str, secret: &str) -> Result<bool> {
    let date = chrono::Utc::now().format("%Y%m%dT%H%M%SZ").to_string();
    let day = &date[..8];
    let host = format!("127.0.0.1:{port}");
    let hash = hex::encode(Sha256::digest([]));
    let canonical = format!(
        "PUT\n/supabricks\n\nhost:{host}\nx-amz-content-sha256:{hash}\nx-amz-date:{date}\n\nhost;x-amz-content-sha256;x-amz-date\n{hash}"
    );
    let scope = format!("{day}/us-east-1/s3/aws4_request");
    let key = sign(
        &sign(
            &sign(&sign(format!("AWS4{secret}").as_bytes(), day), "us-east-1"),
            "s3",
        ),
        "aws4_request",
    );
    let sig = hex::encode(sign(
        &key,
        &format!(
            "AWS4-HMAC-SHA256\n{date}\n{scope}\n{}",
            hex::encode(Sha256::digest(canonical.as_bytes()))
        ),
    ));
    let auth = format!(
        "AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature={sig}"
    );
    let (code, body) = Http::default().request(
        port,
        "PUT",
        "/supabricks",
        &[
            ("Authorization", &auth),
            ("x-amz-date", &date),
            ("x-amz-content-sha256", &hash),
        ],
        &[],
    )?;
    Ok((200..300).contains(&code)
        || (code == 409
            && String::from_utf8_lossy(&body).contains("<Code>BucketAlreadyOwnedByYou</Code>")))
}
