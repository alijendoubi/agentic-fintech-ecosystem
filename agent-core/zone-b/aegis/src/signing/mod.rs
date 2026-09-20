//! Attestation signing (spec section 7, ADR-004 PROPOSED Option C).
//!
//! Aegis never signs "protobuf bytes". It signs the SHA-256 of a canonical
//! UTF-8 text (see [`canonical`]) that binds every order field the broker
//! gateway will act on. The signature primitive is behind the [`Signer`]
//! trait:
//!
//! * [`dev::DevEd25519Signer`] - software key, DEV ONLY, refused in production.
//! * [`hsm::HsmSigner`] over a PKCS#11 backend (cargo feature `pkcs11`,
//!   SoftHSM2 in dev, an HSM in production), ECDSA P-256 with SHA-256.
//!
//! The order-level side mapping is defined in [`verify`] and in the README
//! section "Attestation contract".

pub mod attest;
pub mod canonical;
pub mod dev;
pub mod hsm;
#[cfg(feature = "pkcs11")]
pub mod pkcs11;
pub mod verify;

use thiserror::Error;

/// Signature algorithm of a signer key. Attestations do not carry an algorithm
/// field (frozen proto); verifiers select it by `key_id` from their key
/// registry.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Algorithm {
    Ed25519,
    /// ECDSA over P-256 of the 32-byte digest, raw `r || s` (64 bytes).
    EcdsaP256Sha256,
}

impl Algorithm {
    pub fn as_str(self) -> &'static str {
        match self {
            Algorithm::Ed25519 => "ED25519",
            Algorithm::EcdsaP256Sha256 => "ECDSA_P256_SHA256",
        }
    }
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum SignError {
    /// HSM/session down, PIN locked, key missing: no attestation is possible
    /// and the decision becomes REJECT `REASON_HSM_UNAVAILABLE`.
    #[error("signer unavailable: {0}")]
    Unavailable(String),
    #[error("the dev software signer is forbidden when AEGIS_ENV is production")]
    DevSignerForbidden,
    #[error("invalid key id")]
    InvalidKeyId,
    #[error("cannot build canonical attestation text: {0}")]
    Canonical(String),
}

/// Signs a 32-byte SHA-256 digest.
pub trait Signer: Send + Sync {
    fn key_id(&self) -> &str;
    fn algorithm(&self) -> Algorithm;
    fn sign(&self, digest: &[u8; 32]) -> Result<Vec<u8>, SignError>;
    /// Cheap health probe for `GetAegisState.hsm_ok`.
    fn is_healthy(&self) -> bool {
        true
    }
}

/// Key ids go into the canonical text: restrict them to a safe alphabet.
pub fn validate_key_id(id: &str) -> Result<(), SignError> {
    let ok = !id.is_empty()
        && id.len() <= 64
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | ':' | '-'));
    if ok {
        Ok(())
    } else {
        Err(SignError::InvalidKeyId)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn key_id_alphabet() {
        assert!(validate_key_id("dev-ed25519-ab12.cd:3_4").is_ok());
        for bad in ["", "has space", "new\nline", "eq=ual", &"x".repeat(65)] {
            assert_eq!(
                validate_key_id(bad),
                Err(SignError::InvalidKeyId),
                "{bad:?}"
            );
        }
    }
}
