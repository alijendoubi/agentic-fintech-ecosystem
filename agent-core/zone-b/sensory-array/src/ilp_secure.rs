//! Optional TLS + ECDSA challenge/response authentication for QuestDB ILP.
//!
//! Compiled only with `--features ilp-secure`; plain unauthenticated ILP stays
//! the default for local development. Enable in production via:
//!
//! ```text
//! QUESTDB_ILP_TLS=true
//! QUESTDB_ILP_TLS_CA_FILE=/certs/questdb-ca.pem      # optional extra CA
//! QUESTDB_ILP_AUTH_KEY_ID=<kid from QuestDB auth.conf>
//! QUESTDB_ILP_AUTH_TOKEN=<base64url "d" of the P-256 private key JWK>
//! ```
//!
//! Auth protocol (QuestDB ILP over TCP): the client sends `<key id>\n`, the
//! server answers with a challenge line, the client returns the base64 of the
//! DER ECDSA/SHA-256 signature of the challenge bytes followed by `\n`.
//!
//! STATUS: the signing/handshake logic is unit-tested against an in-process
//! mock that follows the protocol above. It has NOT been verified against a
//! real QuestDB server, and the TLS path has not been exercised at all.

use std::time::Duration;

use base64::engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD};
use base64::Engine;
use p256::ecdsa::signature::Signer;
use p256::ecdsa::{Signature, SigningKey};
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, BufReader};
use tokio_native_tls::native_tls;

use crate::error::{Result, SensoryError};
use crate::questdb_writer::SecureIlp;

const MAX_CHALLENGE_BYTES: usize = 4096;
const AUTH_STEP_TIMEOUT: Duration = Duration::from_secs(5);

fn cfg_err(msg: impl Into<String>) -> SensoryError {
    SensoryError::Config { msg: msg.into() }
}

/// Wrap `stream` in TLS (if configured) and authenticate (if configured).
pub async fn secure_connect(
    stream: tokio::net::TcpStream,
    host: &str,
    sec: &SecureIlp,
) -> Result<Box<dyn AsyncWrite + Unpin + Send>> {
    if sec.tls {
        let mut tls = connect_tls(stream, host, sec).await?;
        authenticate_if_configured(&mut tls, sec).await?;
        return Ok(Box::new(tls));
    }
    let mut plain = stream;
    authenticate_if_configured(&mut plain, sec).await?;
    Ok(Box::new(plain))
}

async fn connect_tls(
    stream: tokio::net::TcpStream,
    host: &str,
    sec: &SecureIlp,
) -> Result<tokio_native_tls::TlsStream<tokio::net::TcpStream>> {
    let mut builder = native_tls::TlsConnector::builder();
    if let Some(path) = &sec.ca_file {
        let pem = tokio::fs::read(path).await?;
        let cert = native_tls::Certificate::from_pem(&pem)
            .map_err(|e| cfg_err(format!("QUESTDB_ILP_TLS_CA_FILE is not valid PEM: {e}")))?;
        builder.add_root_certificate(cert);
    }
    let connector = tokio_native_tls::TlsConnector::from(
        builder
            .build()
            .map_err(|e| cfg_err(format!("cannot build TLS connector: {e}")))?,
    );
    connector
        .connect(host, stream)
        .await
        .map_err(|e| SensoryError::session(format!("QuestDB TLS handshake failed: {e}")))
}

async fn authenticate_if_configured<S>(stream: &mut S, sec: &SecureIlp) -> Result<()>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    match (&sec.key_id, &sec.token) {
        (Some(kid), Some(token)) => authenticate(stream, kid, token).await,
        (None, None) => Ok(()),
        _ => Err(cfg_err("ILP auth key id and token must be set together")),
    }
}

/// Decode the `d` component (base64url, 32 bytes) into a signing key.
pub fn signing_key_from_token(token: &str) -> Result<SigningKey> {
    let bytes = URL_SAFE_NO_PAD
        .decode(token.trim().trim_end_matches('='))
        .map_err(|_| cfg_err("QUESTDB_ILP_AUTH_TOKEN is not base64url"))?;
    SigningKey::from_slice(&bytes)
        .map_err(|_| cfg_err("QUESTDB_ILP_AUTH_TOKEN is not a valid P-256 private key"))
}

/// Base64(DER(ECDSA-SHA256(challenge))).
pub fn sign_challenge(key: &SigningKey, challenge: &[u8]) -> String {
    let sig: Signature = key.sign(challenge);
    STANDARD.encode(sig.to_der().as_bytes())
}

async fn authenticate<S>(stream: &mut S, key_id: &str, token: &str) -> Result<()>
where
    S: AsyncRead + AsyncWrite + Unpin,
{
    let key = signing_key_from_token(token)?;
    let step = |what: &'static str| SensoryError::session(format!("ILP auth {what} timed out"));

    tokio::time::timeout(AUTH_STEP_TIMEOUT, async {
        stream.write_all(format!("{key_id}\n").as_bytes()).await?;
        stream.flush().await
    })
    .await
    .map_err(|_| step("key id send"))??;

    let mut challenge = Vec::with_capacity(128);
    {
        let mut reader = BufReader::new(&mut *stream);
        // Bound the read itself: a peer that never sends a newline must not
        // be able to make us buffer more than one byte past the limit.
        let mut bounded = (&mut reader).take(MAX_CHALLENGE_BYTES as u64 + 1);
        let n = tokio::time::timeout(AUTH_STEP_TIMEOUT, bounded.read_until(b'\n', &mut challenge))
            .await
            .map_err(|_| step("challenge"))??;
        if n == 0 || challenge.len() > MAX_CHALLENGE_BYTES || challenge.last() != Some(&b'\n') {
            return Err(SensoryError::session("ILP auth: bad or missing challenge"));
        }
    }
    challenge.pop(); // strip the newline; the signature covers the bare challenge

    let reply = format!("{}\n", sign_challenge(&key, &challenge));
    tokio::time::timeout(AUTH_STEP_TIMEOUT, async {
        stream.write_all(reply.as_bytes()).await?;
        stream.flush().await
    })
    .await
    .map_err(|_| step("response send"))??;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use p256::ecdsa::signature::Verifier;
    use p256::ecdsa::VerifyingKey;

    fn test_key() -> (SigningKey, String) {
        let bytes = [7_u8; 32];
        let key = SigningKey::from_slice(&bytes).expect("valid scalar");
        (key, URL_SAFE_NO_PAD.encode(bytes))
    }

    #[test]
    fn token_round_trips_and_bad_tokens_fail() {
        let (_, token) = test_key();
        assert!(signing_key_from_token(&token).is_ok());
        assert!(signing_key_from_token("!!!not base64!!!").is_err());
        assert!(signing_key_from_token(&URL_SAFE_NO_PAD.encode([0_u8; 32])).is_err()); // zero scalar
        assert!(signing_key_from_token(&URL_SAFE_NO_PAD.encode([1_u8; 5])).is_err());
    }

    #[test]
    fn signature_verifies_with_the_public_key() {
        let (key, _) = test_key();
        let challenge = b"random-challenge-bytes";
        let b64 = sign_challenge(&key, challenge);
        let der = STANDARD.decode(b64).expect("b64");
        let sig = Signature::from_der(&der).expect("der");
        VerifyingKey::from(&key)
            .verify(challenge, &sig)
            .expect("verifies");
    }

    #[tokio::test]
    async fn handshake_follows_the_protocol_against_a_mock_server() {
        let (key, token) = test_key();
        let (mut client, mut server) = tokio::io::duplex(4096);
        let vk = VerifyingKey::from(&key);
        let srv = tokio::spawn(async move {
            let mut reader = BufReader::new(&mut server);
            let mut kid = String::new();
            reader.read_line(&mut kid).await.expect("kid");
            assert_eq!(kid, "my-key\n");
            let challenge = b"0123456789abcdef";
            reader
                .get_mut()
                .write_all(b"0123456789abcdef\n")
                .await
                .expect("chal");
            let mut resp = String::new();
            reader.read_line(&mut resp).await.expect("resp");
            let der = STANDARD.decode(resp.trim_end()).expect("b64");
            let sig = Signature::from_der(&der).expect("der");
            vk.verify(challenge, &sig).expect("signature valid");
        });
        authenticate(&mut client, "my-key", &token)
            .await
            .expect("auth ok");
        srv.await.expect("server task");
    }

    #[tokio::test]
    async fn missing_challenge_fails_closed() {
        let (_, token) = test_key();
        let (mut client, server) = tokio::io::duplex(1024);
        drop(server); // server closes immediately
        assert!(authenticate(&mut client, "k", &token).await.is_err());
    }

    /// A hostile server streaming a never-ending challenge line must be cut
    /// off after a bounded read, not buffered until the step timeout.
    #[tokio::test]
    async fn oversized_challenge_read_is_bounded() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        use std::sync::Arc;

        let (_, token) = test_key();
        let (mut client, mut server) = tokio::io::duplex(1024);
        let sent = Arc::new(AtomicUsize::new(0));
        let sent_srv = Arc::clone(&sent);
        let srv = tokio::spawn(async move {
            let mut kid = String::new();
            BufReader::new(&mut server)
                .read_line(&mut kid)
                .await
                .expect("kid");
            let chunk = [b'A'; 1024];
            // Up to 1 MiB, never a newline; then hold the socket open.
            for _ in 0..1024 {
                if server.write_all(&chunk).await.is_err() {
                    return;
                }
                sent_srv.fetch_add(chunk.len(), Ordering::SeqCst);
            }
            tokio::time::sleep(Duration::from_secs(30)).await;
        });
        let started = std::time::Instant::now();
        assert!(authenticate(&mut client, "k", &token).await.is_err());
        drop(client);
        srv.abort();
        let _ = srv.await;
        let sent = sent.load(Ordering::SeqCst);
        assert!(
            sent < 64 * 1024,
            "server pushed {sent} bytes before the client gave up"
        );
        assert!(started.elapsed() < Duration::from_secs(4), "must fail fast");
    }

    #[tokio::test]
    async fn unpaired_credentials_are_rejected() {
        let (mut a, _b) = tokio::io::duplex(64);
        let sec = SecureIlp {
            key_id: Some("k".into()),
            ..SecureIlp::default()
        };
        assert!(authenticate_if_configured(&mut a, &sec).await.is_err());
    }
}
