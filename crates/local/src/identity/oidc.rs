//! Maintained OIDC verification, exact provider/redirect binding and fail-closed
//! introspection. Provider tokens never cross the browser/CLI boundary.
use super::*;
use openidconnect::{
    AccessToken, AccessTokenHash, AuthorizationCode, ClientId, ClientSecret, CsrfToken, IssuerUrl,
    Nonce, OAuth2TokenResponse, PkceCodeChallenge, PkceCodeVerifier, RedirectUrl, Scope,
    SubjectIdentifier, TokenResponse,
    core::{CoreAuthenticationFlow, CoreClient, CoreProviderMetadata, CoreUserInfoClaims},
};
use reqwest::{Url, blocking::Client};
use std::time::Duration;

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub issuer: String,
    pub client_id: String,
    pub client_secret: String,
    pub introspection_url: String,
    pub redirects: Vec<String>,
    #[serde(default)]
    pub ca_pem: Option<String>,
}
impl Config {
    pub fn validate(&self) -> Result<()> {
        endpoint(&self.issuer)?;
        endpoint(&self.introspection_url)?;
        if Url::parse(&self.issuer).map_err(|_| denied())?.origin()
            != Url::parse(&self.introspection_url)
                .map_err(|_| denied())?
                .origin()
            || self.client_id.is_empty()
            || self.client_id.len() > 256
            || self.client_secret.is_empty()
            || self.client_secret.len() > 4096
            || self.redirects.is_empty()
            || self.redirects.len() > 16
        {
            return Err(denied());
        }
        for redirect in &self.redirects {
            let url = Url::parse(redirect).map_err(|_| denied())?;
            if !url.username().is_empty()
                || url.password().is_some()
                || url.query().is_some()
                || url.fragment().is_some()
                || url.path() != "/auth/v1/callback"
                || !matches!(url.scheme(), "https" | "http")
                || (url.scheme() == "http"
                    && !matches!(url.host_str(), Some("127.0.0.1" | "[::1]")))
            {
                return Err(denied());
            }
        }
        Ok(())
    }
    pub(crate) fn redirect(&self, redirect: &str) -> Result<()> {
        self.validate()?;
        if !self.redirects.iter().any(|x| x == redirect) {
            return Err(denied());
        }
        Ok(())
    }
}
fn endpoint(value: &str) -> Result<()> {
    let u = Url::parse(value).map_err(|_| denied())?;
    if u.scheme() != "https"
        || u.host_str().is_none()
        || !u.username().is_empty()
        || u.password().is_some()
        || u.fragment().is_some()
        || u.query().is_some()
    {
        return Err(denied());
    }
    Ok(())
}
struct Http {
    client: Client,
    origin: String,
}
impl openidconnect::SyncHttpClient for Http {
    type Error = std::io::Error;
    fn call(
        &self,
        request: openidconnect::HttpRequest,
    ) -> std::io::Result<openidconnect::HttpResponse> {
        use std::io::{Error, Read};
        let fail = || Error::other("OIDC transport unavailable");
        let url = Url::parse(&request.uri().to_string()).map_err(|_| fail())?;
        if url.origin().ascii_serialization() != self.origin
            || !url.username().is_empty()
            || url.password().is_some()
        {
            return Err(fail());
        }
        let response = self
            .client
            .execute(request.try_into().map_err(|_| fail())?)
            .map_err(|_| fail())?;
        let mut builder = hyper::Response::builder().status(response.status());
        for (name, value) in response.headers() {
            builder = builder.header(name, value);
        }
        let mut bytes = Vec::new();
        response
            .take(262145)
            .read_to_end(&mut bytes)
            .map_err(|_| fail())?;
        if bytes.len() > 262144 {
            return Err(fail());
        }
        builder.body(bytes).map_err(|_| fail())
    }
}
fn http(config: &Config) -> Result<Http> {
    let mut builder = Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(5))
        .connect_timeout(Duration::from_secs(2));
    if let Some(pem) = &config.ca_pem {
        if pem.len() > 65536 {
            return Err(denied());
        }
        builder = builder.add_root_certificate(
            reqwest::Certificate::from_pem(pem.as_bytes()).map_err(|_| denied())?,
        );
    }
    Ok(Http {
        client: builder.build().map_err(|_| denied())?,
        origin: Url::parse(&config.issuer)
            .map_err(|_| denied())?
            .origin()
            .ascii_serialization(),
    })
}
fn metadata(config: &Config, http: &Http) -> Result<CoreProviderMetadata> {
    let metadata = CoreProviderMetadata::discover(
        &IssuerUrl::new(config.issuer.clone()).map_err(|_| denied())?,
        http,
    )
    .map_err(|_| denied())?;
    // Refuse discovery that would send tokens, codes or credentials to another origin.
    let origin = Url::parse(&config.issuer).map_err(|_| denied())?.origin();
    let mut endpoints = vec![
        metadata.authorization_endpoint().as_str(),
        metadata.jwks_uri().as_str(),
    ];
    if let Some(u) = metadata.token_endpoint() {
        endpoints.push(u.as_str());
    }
    if let Some(u) = metadata.userinfo_endpoint() {
        endpoints.push(u.as_str());
    }
    for value in endpoints {
        endpoint(value)?;
        if Url::parse(value).map_err(|_| denied())?.origin() != origin {
            return Err(denied());
        }
    }
    Ok(metadata)
}
pub(crate) struct Login {
    pub url: String,
    pub state: String,
    pub nonce: String,
    pub verifier: String,
}
pub(crate) fn begin(config: &Config, redirect: &str) -> Result<Login> {
    config.redirect(redirect)?;
    let http = http(config)?;
    let client = CoreClient::from_provider_metadata(
        metadata(config, &http)?,
        ClientId::new(config.client_id.clone()),
        Some(ClientSecret::new(config.client_secret.clone())),
    )
    .set_redirect_uri(RedirectUrl::new(redirect.into()).map_err(|_| denied())?);
    let (challenge, verifier) = PkceCodeChallenge::new_random_sha256();
    let (url, state, nonce) = client
        .authorize_url(
            CoreAuthenticationFlow::AuthorizationCode,
            CsrfToken::new_random,
            Nonce::new_random,
        )
        .add_scope(Scope::new("email".into()))
        .set_pkce_challenge(challenge)
        .url();
    Ok(Login {
        url: url.to_string(),
        state: state.secret().clone(),
        nonce: nonce.secret().clone(),
        verifier: verifier.secret().clone(),
    })
}
pub(crate) struct Verified {
    pub subject: String,
    pub label: String,
    pub access_token: String,
    pub expires_ms: i64,
}
pub(crate) fn complete(
    config: &Config,
    redirect: &str,
    code: String,
    nonce: String,
    verifier: String,
) -> Result<Verified> {
    config.redirect(redirect)?;
    let http = http(config)?;
    // Fresh discovery/JWKS for each exchange supports provider signing-key rotation.
    let client = CoreClient::from_provider_metadata(
        metadata(config, &http)?,
        ClientId::new(config.client_id.clone()),
        Some(ClientSecret::new(config.client_secret.clone())),
    )
    .set_redirect_uri(RedirectUrl::new(redirect.into()).map_err(|_| denied())?);
    let token = client
        .exchange_code(AuthorizationCode::new(code))
        .map_err(|_| denied())?
        .set_pkce_verifier(PkceCodeVerifier::new(verifier))
        .request(&http)
        .map_err(|_| denied())?;
    let id_token = token.id_token().ok_or_else(denied)?;
    let verifier = client.id_token_verifier();
    let claims = id_token
        .claims(&verifier, &Nonce::new(nonce))
        .map_err(|_| denied())?;
    if let Some(expected) = claims.access_token_hash() {
        let actual = AccessTokenHash::from_token(
            token.access_token(),
            id_token.signing_alg().map_err(|_| denied())?,
            id_token.signing_key(&verifier).map_err(|_| denied())?,
        )
        .map_err(|_| denied())?;
        if *expected != actual {
            return Err(denied());
        }
    }
    let subject = claims.subject().as_str().to_owned();
    if subject.is_empty() || subject.len() > 1024 {
        return Err(denied());
    }
    let label = claims
        .email()
        .map(|x| x.as_str())
        .unwrap_or("OIDC user")
        .to_owned();
    super::label(&label)?;
    let access_token = token.access_token().secret().clone();
    let expires_ms = claims
        .expiration()
        .timestamp_millis()
        .min(
            now()
                + token
                    .expires_in()
                    .map(|x| x.as_millis().min(3_600_000) as i64)
                    .unwrap_or(300_000),
        )
        .min(now() + 3_600_000);
    active(config, &access_token, &subject)?;
    // Also validate user-info subject binding; do not trust email to link identities.
    let _: CoreUserInfoClaims = client
        .user_info(
            AccessToken::new(access_token.clone()),
            Some(SubjectIdentifier::new(subject.clone())),
        )
        .map_err(|_| denied())?
        .request(&http)
        .map_err(|_| denied())?;
    Ok(Verified {
        subject,
        label,
        access_token,
        expires_ms,
    })
}
pub(crate) fn active(config: &Config, token: &str, subject: &str) -> Result<()> {
    config.validate()?;
    let response = http(config)?
        .client
        .post(&config.introspection_url)
        .basic_auth(&config.client_id, Some(&config.client_secret))
        .form(&[("token", token), ("token_type_hint", "access_token")])
        .send()
        .map_err(|_| denied())?;
    if !response.status().is_success() {
        return Err(denied());
    }
    use std::io::Read;
    let mut bytes = Vec::new();
    response
        .take(65_537)
        .read_to_end(&mut bytes)
        .map_err(|_| denied())?;
    if bytes.len() > 65_536 {
        return Err(denied());
    }
    let value: serde_json::Value = serde_json::from_slice(&bytes).map_err(|_| denied())?;
    let audience = &value["aud"];
    let audience_ok = audience.as_str() == Some(&config.client_id)
        || audience
            .as_array()
            .is_some_and(|a| a.iter().any(|v| v.as_str() == Some(&config.client_id)));
    if value["active"] != true
        || value["sub"].as_str() != Some(subject)
        || value["iss"].as_str() != Some(&config.issuer)
        || !audience_ok
        || value["exp"].as_i64().is_none_or(|x| x <= now() / 1000)
    {
        return Err(denied());
    }
    Ok(())
}
