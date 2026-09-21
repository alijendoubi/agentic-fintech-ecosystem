//! Generic HSM-backed signer: wraps any [`HsmBackend`] (PKCS#11 in
//! production/SoftHSM2, a mock in unit tests) and enforces the contract that
//! matters to Aegis: a backend failure or a malformed signature is
//! `SignError::Unavailable`, which becomes REJECT `REASON_HSM_UNAVAILABLE`.

use super::{validate_key_id, Algorithm, SignError, Signer};

/// Length of a raw `r || s` P-256 signature and of an Ed25519 signature.
const SIGNATURE_LEN: usize = 64;

pub trait HsmBackend: Send + Sync {
    fn algorithm(&self) -> Algorithm;
    fn sign_digest(&self, digest: &[u8; 32]) -> Result<Vec<u8>, SignError>;
    fn is_alive(&self) -> bool;
}

pub struct HsmSigner {
    backend: Box<dyn HsmBackend>,
    key_id: String,
}

impl std::fmt::Debug for HsmSigner {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("HsmSigner")
            .field("key_id", &self.key_id)
            .finish_non_exhaustive()
    }
}

impl HsmSigner {
    pub fn new(key_id: &str, backend: Box<dyn HsmBackend>) -> Result<HsmSigner, SignError> {
        validate_key_id(key_id)?;
        Ok(HsmSigner {
            backend,
            key_id: key_id.to_owned(),
        })
    }
}

impl Signer for HsmSigner {
    fn key_id(&self) -> &str {
        &self.key_id
    }

    fn algorithm(&self) -> Algorithm {
        self.backend.algorithm()
    }

    fn sign(&self, digest: &[u8; 32]) -> Result<Vec<u8>, SignError> {
        let sig = self.backend.sign_digest(digest)?;
        if sig.len() != SIGNATURE_LEN {
            return Err(SignError::Unavailable(format!(
                "unexpected signature length {}",
                sig.len()
            )));
        }
        Ok(sig)
    }

    fn is_healthy(&self) -> bool {
        self.backend.is_alive()
    }
}

#[cfg(test)]
pub(crate) mod mock {
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    use super::*;

    /// Deterministic fake HSM: "signature" is 64 bytes derived from the digest.
    #[derive(Clone, Default)]
    pub struct MockHsm {
        pub down: Arc<AtomicBool>,
        pub bad_len: Arc<AtomicBool>,
    }

    impl HsmBackend for MockHsm {
        fn algorithm(&self) -> Algorithm {
            Algorithm::EcdsaP256Sha256
        }

        fn sign_digest(&self, digest: &[u8; 32]) -> Result<Vec<u8>, SignError> {
            if self.down.load(Ordering::SeqCst) {
                return Err(SignError::Unavailable("session lost".into()));
            }
            let mut out = digest.to_vec();
            out.extend_from_slice(digest);
            if self.bad_len.load(Ordering::SeqCst) {
                out.truncate(10);
            }
            Ok(out)
        }

        fn is_alive(&self) -> bool {
            !self.down.load(Ordering::SeqCst)
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::Ordering;

    use super::mock::MockHsm;
    use super::*;

    #[test]
    fn backend_failure_and_malformed_signatures_are_unavailable() {
        let hsm = MockHsm::default();
        let s = HsmSigner::new("hsm-key-1", Box::new(hsm.clone())).unwrap();
        assert_eq!(s.sign(&[3; 32]).unwrap().len(), 64);
        assert!(s.is_healthy());
        hsm.bad_len.store(true, Ordering::SeqCst);
        assert!(matches!(s.sign(&[3; 32]), Err(SignError::Unavailable(_))));
        hsm.bad_len.store(false, Ordering::SeqCst);
        hsm.down.store(true, Ordering::SeqCst);
        assert!(matches!(s.sign(&[3; 32]), Err(SignError::Unavailable(_))));
        assert!(!s.is_healthy());
    }

    #[test]
    fn invalid_key_ids_are_rejected_at_construction() {
        assert!(HsmSigner::new("bad key", Box::new(MockHsm::default())).is_err());
    }
}
