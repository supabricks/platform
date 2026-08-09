//! Ed25519 compute-auth machinery (RFC 012 D7): the operator generates the
//! keypair, embeds the JWKS in every compute spec, and mints per-compute
//! admin JWTs. Derivations must match the Day-2 recipe
//! (spike/compose/mint-compute-jwt.sh): x = b64url(raw 32-byte pubkey),
//! kid = b64url(sha256(x-as-string)).

use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD as B64;
use jsonwebtoken::{Algorithm, EncodingKey, Header, encode};
use aws_lc_rs::signature::{Ed25519KeyPair, KeyPair};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::time::{SystemTime, UNIX_EPOCH};

pub struct ComputeKey {
    pkcs8: Vec<u8>,
    pub x_b64url: String,
    pub kid_b64url: String,
}

#[derive(Serialize, Deserialize, Debug)]
pub struct ComputeClaims {
    pub scope: String,
    pub aud: Vec<String>,
    pub exp: u64,
}

impl ComputeKey {
    pub fn generate() -> anyhow::Result<Self> {
        let rng = aws_lc_rs::rand::SystemRandom::new();
        let doc = Ed25519KeyPair::generate_pkcs8(&rng)
            .map_err(|e| anyhow::anyhow!("keygen: {e}"))?;
        Self::from_pkcs8(doc.as_ref())
    }

    pub fn from_pkcs8(pkcs8: &[u8]) -> anyhow::Result<Self> {
        let pair = Ed25519KeyPair::from_pkcs8(pkcs8)
            .map_err(|e| anyhow::anyhow!("bad pkcs8: {e}"))?;
        let x = B64.encode(pair.public_key().as_ref());
        let kid = B64.encode(Sha256::digest(x.as_bytes()));
        Ok(Self {
            pkcs8: pkcs8.to_vec(),
            x_b64url: x,
            kid_b64url: kid,
        })
    }

    pub fn pkcs8(&self) -> &[u8] {
        &self.pkcs8
    }

    /// Admin-scoped token for a compute_ctl external API (verified Day 2).
    /// Consumed by the P3 suspend flow (`POST /terminate`).
    #[allow(dead_code)]
    pub fn mint_admin_jwt(&self, ttl_secs: u64) -> anyhow::Result<String> {
        let mut header = Header::new(Algorithm::EdDSA);
        header.kid = Some(self.kid_b64url.clone());
        let claims = ComputeClaims {
            scope: "compute_ctl:admin".into(),
            aud: vec!["compute".into()],
            exp: SystemTime::now().duration_since(UNIX_EPOCH)?.as_secs() + ttl_secs,
        };
        Ok(encode(
            &header,
            &claims,
            &EncodingKey::from_ed_der(&self.pkcs8),
        )?)
    }

    /// The JWKS object embedded in every compute spec.
    /// Spec render consumes x/kid directly today; kept for the P2 MCP
    /// `capabilities` surface.
    #[allow(dead_code)]
    pub fn jwks(&self) -> serde_json::Value {
        serde_json::json!({
            "keys": [{
                "use": "sig", "key_ops": ["verify"], "alg": "EdDSA",
                "kid": self.kid_b64url, "kty": "OKP", "crv": "Ed25519",
                "x": self.x_b64url,
            }]
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use jsonwebtoken::{DecodingKey, Validation, decode};

    #[test]
    fn mint_verifies_against_own_jwks() {
        let key = ComputeKey::generate().unwrap();
        let token = key.mint_admin_jwt(60).unwrap();

        let raw_pub = B64.decode(&key.x_b64url).unwrap();
        assert_eq!(raw_pub.len(), 32, "x must be raw 32 bytes (Day-2 bug class)");
        let mut val = Validation::new(Algorithm::EdDSA);
        val.set_audience(&["compute"]);
        let data = decode::<ComputeClaims>(&token, &DecodingKey::from_ed_der(&raw_pub), &val)
            .expect("token must verify against the JWKS-derived key");
        assert_eq!(data.claims.scope, "compute_ctl:admin");
    }

    #[test]
    fn kid_matches_shell_recipe() {
        // Same derivation as mint-compute-jwt.sh / mk-compute.sh:
        // kid = b64url(sha256(x_b64url_string))
        let key = ComputeKey::generate().unwrap();
        let expect = B64.encode(Sha256::digest(key.x_b64url.as_bytes()));
        assert_eq!(key.kid_b64url, expect);
    }

    #[test]
    fn roundtrips_pkcs8() {
        let a = ComputeKey::generate().unwrap();
        let b = ComputeKey::from_pkcs8(a.pkcs8()).unwrap();
        assert_eq!(a.x_b64url, b.x_b64url);
    }
}
