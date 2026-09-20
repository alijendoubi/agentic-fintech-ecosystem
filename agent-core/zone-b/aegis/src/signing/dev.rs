//! DEV-ONLY software Ed25519 signer.
//!
//! This key lives in process memory (or a seed file): it provides NO
//! non-extractability and NO separation of duties. It exists so the pipeline
//! can be exercised without an HSM. Every constructor refuses to run when
//! `AEGIS_ENV` is production (or unset, which is treated as production), and
//! `config` refuses `AEGIS_SIGNER=dev` in production as a second gate.

use std::path::Path;

use ed25519_dalek::{Signer as _, SigningKey, VerifyingKey};
use sha2::{Digest, Sha256};

use super::{validate_key_id, Algorithm, SignError, Signer};
use crate::config::Environment;
use crate::hex;

pub struct DevEd25519Signer {
    key: SigningKey,
    key_id: String,
}

impl std::fmt::Debug for DevEd25519Signer {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DevEd25519Signer")
            .field("key_id", &self.key_id)
            .field("dev_only", &true)
            .finish_non_exhaustive()
    }
}

fn refuse_in_production(env: Environment) -> Result<(), SignError> {
    if env.is_production() {
        Err(SignError::DevSignerForbidden)
    } else {
        Ok(())
    }
}

impl DevEd25519Signer {
    pub fn from_seed(env: Environment, seed: [u8; 32]) -> Result<DevEd25519Signer, SignError> {
        refuse_in_production(env)?;
        let key = SigningKey::from_bytes(&seed);
        let fingerprint = hex::encode(&Sha256::digest(key.verifying_key().as_bytes()));
        let key_id = format!("dev-ed25519-{}", &fingerprint[..16]);
        validate_key_id(&key_id)?;
        Ok(DevEd25519Signer { key, key_id })
    }

    /// Ephemeral key: attestations stop verifying after a restart.
    pub fn generate(env: Environment) -> Result<DevEd25519Signer, SignError> {
        refuse_in_production(env)?;
        DevEd25519Signer::from_seed(env, SigningKey::generate(&mut rand_core::OsRng).to_bytes())
    }

    /// 32-byte seed as 64 hex characters in a file.
    pub fn from_seed_file(env: Environment, path: &Path) -> Result<DevEd25519Signer, SignError> {
        refuse_in_production(env)?;
        let text = std::fs::read_to_string(path)
            .map_err(|e| SignError::Unavailable(format!("seed file unreadable: {e}")))?;
        let bytes = hex::decode(text.trim())
            .and_then(|b| <[u8; 32]>::try_from(b).ok())
            .ok_or_else(|| {
                SignError::Unavailable("seed file must hold 64 hex characters".into())
            })?;
        DevEd25519Signer::from_seed(env, bytes)
    }

    pub fn verifying_key(&self) -> VerifyingKey {
        self.key.verifying_key()
    }
}

impl Signer for DevEd25519Signer {
    fn key_id(&self) -> &str {
        &self.key_id
    }

    fn algorithm(&self) -> Algorithm {
        Algorithm::Ed25519
    }

    fn sign(&self, digest: &[u8; 32]) -> Result<Vec<u8>, SignError> {
        Ok(self.key.sign(digest).to_bytes().to_vec())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::Verifier as _;

    #[test]
    fn refused_in_production_by_every_constructor() {
        assert_eq!(
            DevEd25519Signer::generate(Environment::Production).unwrap_err(),
            SignError::DevSignerForbidden
        );
        assert_eq!(
            DevEd25519Signer::from_seed(Environment::Production, [1; 32]).unwrap_err(),
            SignError::DevSignerForbidden
        );
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("seed");
        std::fs::write(&p, "00".repeat(32)).unwrap();
        assert_eq!(
            DevEd25519Signer::from_seed_file(Environment::Production, &p).unwrap_err(),
            SignError::DevSignerForbidden
        );
        assert!(DevEd25519Signer::from_seed_file(Environment::Development, &p).is_ok());
    }

    #[test]
    fn signs_verifiably_with_a_stable_key_id_and_does_not_leak_the_key() {
        let s = DevEd25519Signer::from_seed(Environment::Test, [7; 32]).unwrap();
        let again = DevEd25519Signer::from_seed(Environment::Test, [7; 32]).unwrap();
        assert_eq!(s.key_id(), again.key_id());
        assert!(s.key_id().starts_with("dev-ed25519-"));
        let digest = [9u8; 32];
        let sig = s.sign(&digest).unwrap();
        let sig = ed25519_dalek::Signature::from_slice(&sig).unwrap();
        assert!(s.verifying_key().verify(&digest, &sig).is_ok());
        let dbg = format!("{s:?}");
        assert!(dbg.contains("dev_only") && !dbg.contains("07070707"));
    }

    #[test]
    fn bad_seed_files_are_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("seed");
        for bad in ["", "zz", "00", &"00".repeat(33)] {
            std::fs::write(&p, bad).unwrap();
            assert!(
                DevEd25519Signer::from_seed_file(Environment::Test, &p).is_err(),
                "{bad:?}"
            );
        }
        assert!(
            DevEd25519Signer::from_seed_file(Environment::Test, &dir.path().join("missing"))
                .is_err()
        );
    }
}
