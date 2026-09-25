//! PKCS#11 signing backend (cargo feature `pkcs11`), ADR-001 / spec section 7.
//!
//! Targets SoftHSM2 in development and a PKCS#11 HSM in production. Mechanism:
//! `CKM_ECDSA` over the 32-byte SHA-256 digest with an EC P-256 private key
//! looked up by label; the result is the raw `r || s` (64 bytes) signature.
//! TODO(owner): confirm the production HSM supports CKM_ECDSA on P-256 and that
//! ADR-001's "2-of-3 MPC threshold ECDSA" is (or is not) required.
//!
//! The PIN is read from a file at connect time, wrapped in a secret type and
//! never logged. A failed sign drops the session and reconnects once; any
//! remaining failure is `SignError::Unavailable` (REJECT `REASON_HSM_UNAVAILABLE`).

use std::sync::Mutex;

use cryptoki::context::{CInitializeArgs, CInitializeFlags, Pkcs11};
use cryptoki::mechanism::Mechanism;
use cryptoki::object::{Attribute, AttributeType, ObjectClass, ObjectHandle};
use cryptoki::session::{Session, UserType};
use cryptoki::slot::Slot;
use cryptoki::types::AuthPin;

use super::hsm::{HsmBackend, HsmSigner};
use super::{Algorithm, SignError};
use crate::config::Pkcs11Config;

fn unavailable(what: &str, e: impl std::fmt::Display) -> SignError {
    // Only the operation and the library's error text: never PIN or key bytes.
    SignError::Unavailable(format!("{what}: {e}"))
}

struct Live {
    session: Session,
    key: ObjectHandle,
}

pub struct CryptokiBackend {
    ctx: Pkcs11,
    slot: Slot,
    cfg: Pkcs11Config,
    live: Mutex<Option<Live>>,
}

impl std::fmt::Debug for CryptokiBackend {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CryptokiBackend")
            .field("cfg", &self.cfg)
            .finish_non_exhaustive()
    }
}

fn find_slot(ctx: &Pkcs11, token_label: &str) -> Result<Slot, SignError> {
    let slots = ctx
        .get_slots_with_token()
        .map_err(|e| unavailable("list slots", e))?;
    for slot in slots {
        let info = ctx
            .get_token_info(slot)
            .map_err(|e| unavailable("token info", e))?;
        if info.label().trim_end() == token_label {
            return Ok(slot);
        }
    }
    Err(SignError::Unavailable(format!(
        "no token with label {token_label:?}"
    )))
}

fn read_pin(cfg: &Pkcs11Config) -> Result<AuthPin, SignError> {
    let raw =
        std::fs::read_to_string(&cfg.pin_file).map_err(|e| unavailable("read pin file", e))?;
    Ok(AuthPin::new(
        raw.trim_end_matches(['\r', '\n'])
            .to_owned()
            .into_boxed_str(),
    ))
}

fn open(ctx: &Pkcs11, slot: Slot, cfg: &Pkcs11Config) -> Result<Live, SignError> {
    let session = ctx
        .open_rw_session(slot)
        .map_err(|e| unavailable("open session", e))?;
    let pin = read_pin(cfg)?;
    session
        .login(UserType::User, Some(&pin))
        .map_err(|e| unavailable("login", e))?;
    let template = [
        Attribute::Class(ObjectClass::PRIVATE_KEY),
        Attribute::Label(cfg.key_label.clone().into_bytes()),
        Attribute::Sign(true),
    ];
    let key = session
        .find_objects(&template)
        .map_err(|e| unavailable("find key", e))?
        .into_iter()
        .next()
        .ok_or_else(|| {
            SignError::Unavailable(format!("no signing key labelled {:?}", cfg.key_label))
        })?;
    Ok(Live { session, key })
}

impl CryptokiBackend {
    pub fn connect(cfg: &Pkcs11Config) -> Result<CryptokiBackend, SignError> {
        let ctx = Pkcs11::new(&cfg.module).map_err(|e| unavailable("load module", e))?;
        // cryptoki 0.12: OS-provided locking (was CInitializeArgs::OsThreads in 0.7).
        ctx.initialize(CInitializeArgs::new(CInitializeFlags::OS_LOCKING_OK))
            .map_err(|e| unavailable("initialize", e))?;
        let slot = find_slot(&ctx, &cfg.token_label)?;
        let live = open(&ctx, slot, cfg)?;
        Ok(CryptokiBackend {
            ctx,
            slot,
            cfg: cfg.clone(),
            live: Mutex::new(Some(live)),
        })
    }

    /// Uncompressed SEC1 public key (`0x04 || X || Y`) of the signing key, for
    /// registering with verifiers (gateway) and for tests.
    pub fn public_key_sec1(&self) -> Result<Vec<u8>, SignError> {
        let guard = self
            .live
            .lock()
            .map_err(|_| SignError::Unavailable("hsm lock poisoned".into()))?;
        let live = guard
            .as_ref()
            .ok_or_else(|| SignError::Unavailable("no hsm session".into()))?;
        let template = [
            Attribute::Class(ObjectClass::PUBLIC_KEY),
            Attribute::Label(self.cfg.key_label.clone().into_bytes()),
        ];
        let handle = live
            .session
            .find_objects(&template)
            .map_err(|e| unavailable("find public key", e))?
            .into_iter()
            .next()
            .ok_or_else(|| SignError::Unavailable("public key not found".into()))?;
        let attrs = live
            .session
            .get_attributes(handle, &[AttributeType::EcPoint])
            .map_err(|e| unavailable("read EC point", e))?;
        match attrs.into_iter().next() {
            Some(Attribute::EcPoint(der)) => Ok(strip_der_octet_string(&der)),
            _ => Err(SignError::Unavailable("EC point attribute missing".into())),
        }
    }

    fn sign_once(live: &Live, digest: &[u8; 32]) -> Result<Vec<u8>, SignError> {
        live.session
            .sign(&Mechanism::Ecdsa, live.key, digest)
            .map_err(|e| unavailable("sign", e))
    }
}

impl HsmBackend for CryptokiBackend {
    fn algorithm(&self) -> Algorithm {
        Algorithm::EcdsaP256Sha256
    }

    fn sign_digest(&self, digest: &[u8; 32]) -> Result<Vec<u8>, SignError> {
        let mut guard = self
            .live
            .lock()
            .map_err(|_| SignError::Unavailable("hsm lock poisoned".into()))?;
        if let Some(live) = guard.as_ref() {
            if let Ok(sig) = CryptokiBackend::sign_once(live, digest) {
                return Ok(sig);
            }
        }
        // Session lost or key gone: drop it and reconnect exactly once.
        *guard = None;
        let live = open(&self.ctx, self.slot, &self.cfg)?;
        let sig = CryptokiBackend::sign_once(&live, digest)?;
        *guard = Some(live);
        Ok(sig)
    }

    fn is_alive(&self) -> bool {
        self.ctx.get_token_info(self.slot).is_ok()
            && self.live.lock().map(|g| g.is_some()).unwrap_or(false)
    }
}

/// `CKA_EC_POINT` is a DER OCTET STRING wrapping the point; tolerate tokens
/// that return the bare point.
fn strip_der_octet_string(der: &[u8]) -> Vec<u8> {
    match der {
        [0x04, len, rest @ ..]
            if usize::from(*len) == rest.len() && rest.first() == Some(&0x04) =>
        {
            rest.to_vec()
        }
        other => other.to_vec(),
    }
}

/// Build the production signer from configuration.
pub fn build_signer(cfg: &Pkcs11Config) -> Result<HsmSigner, SignError> {
    let backend = CryptokiBackend::connect(cfg)?;
    HsmSigner::new(&cfg.key_label, Box::new(backend))
}
