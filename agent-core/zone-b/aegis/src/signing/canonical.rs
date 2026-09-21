//! Canonical attestation text `afe-attest-v1` (spec section 7).
//!
//! One `key=value` per line, fixed order, `\n`-terminated, integers in
//! decimal, no whitespace padding:
//!
//! ```text
//! afe-attest-v1
//! signal_id=<uuid>
//! symbol=<SYM>
//! side=<BUY|SELL|SELL_SHORT>
//! order_type=<LIMIT|...>
//! qty_nanos=<int>
//! limit_price_nanos=<int>
//! stop_price_nanos=<int>
//! decided_at_ns=<int>
//! expires_at_ns=<int>
//! aegis_state_seq=<int>
//! limits_config_sha256=<hex>
//! key_id=<string>
//! ```
//!
//! This is exactly the text of spec section 7 and the text the execution-motor
//! (`execution_motor/proto_adapter.py`) rebuilds. The client order id is bound
//! indirectly: Aegis sets `order_id == signal_id` and verifiers enforce it
//! (see `attest` and `verify`).

use sha2::{Digest, Sha256};

use super::SignError;
use crate::domain::Side;

pub const CANONICAL_VERSION: &str = "afe-attest-v1";

/// Everything the signature binds.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AttestationFields {
    pub signal_id: String,
    pub symbol: String,
    pub side: Side,
    /// `OrderType` proto name without prefix, e.g. `LIMIT`.
    pub order_type: String,
    pub qty_nanos: i64,
    pub limit_price_nanos: i64,
    pub stop_price_nanos: i64,
    pub decided_at_ns: i64,
    pub expires_at_ns: i64,
    pub aegis_state_seq: u64,
    pub limits_config_sha256: String,
    pub key_id: String,
}

pub fn side_text(side: Side) -> &'static str {
    match side {
        Side::Buy => "BUY",
        Side::Sell => "SELL",
        Side::SellShort => "SELL_SHORT",
    }
}

/// A free-text field may not smuggle a line break or `=` into the payload.
fn clean<'a>(name: &str, v: &'a str) -> Result<&'a str, SignError> {
    if v.is_empty() || v.chars().any(|c| c.is_control() || c == '=') {
        return Err(SignError::Canonical(format!(
            "field {name} is empty or has forbidden characters"
        )));
    }
    Ok(v)
}

impl AttestationFields {
    pub fn canonical_text(&self) -> Result<String, SignError> {
        Ok(format!(
            "{CANONICAL_VERSION}\n\
             signal_id={}\n\
             symbol={}\n\
             side={}\n\
             order_type={}\n\
             qty_nanos={}\n\
             limit_price_nanos={}\n\
             stop_price_nanos={}\n\
             decided_at_ns={}\n\
             expires_at_ns={}\n\
             aegis_state_seq={}\n\
             limits_config_sha256={}\n\
             key_id={}\n",
            clean("signal_id", &self.signal_id)?,
            clean("symbol", &self.symbol)?,
            side_text(self.side),
            clean("order_type", &self.order_type)?,
            self.qty_nanos,
            self.limit_price_nanos,
            self.stop_price_nanos,
            self.decided_at_ns,
            self.expires_at_ns,
            self.aegis_state_seq,
            clean("limits_config_sha256", &self.limits_config_sha256)?,
            clean("key_id", &self.key_id)?,
        ))
    }

    /// SHA-256 of the canonical text: the value that is signed.
    pub fn digest(&self) -> Result<[u8; 32], SignError> {
        let text = self.canonical_text()?;
        Ok(Sha256::digest(text.as_bytes()).into())
    }
}

#[cfg(test)]
pub(crate) fn sample_fields() -> AttestationFields {
    AttestationFields {
        signal_id: "0b4e7c9e-6a61-4b0e-9a54-0e1e5d3f9a11".into(),
        symbol: "AAPL".into(),
        side: Side::Buy,
        order_type: "LIMIT".into(),
        qty_nanos: 10_000_000_000,
        limit_price_nanos: 150_000_000_000,
        stop_price_nanos: 0,
        decided_at_ns: 1_790_000_000_000_000_000,
        expires_at_ns: 1_790_000_005_000_000_000,
        aegis_state_seq: 7,
        limits_config_sha256: "ab".repeat(32),
        key_id: "dev-ed25519-1234abcd".into(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hex;

    /// Golden vector: any other implementation (the broker gateway, Python)
    /// must reproduce these exact bytes and digest.
    #[test]
    fn golden_vector_text_and_digest() {
        let f = sample_fields();
        let expected = "afe-attest-v1\n\
signal_id=0b4e7c9e-6a61-4b0e-9a54-0e1e5d3f9a11\n\
symbol=AAPL\n\
side=BUY\n\
order_type=LIMIT\n\
qty_nanos=10000000000\n\
limit_price_nanos=150000000000\n\
stop_price_nanos=0\n\
decided_at_ns=1790000000000000000\n\
expires_at_ns=1790000005000000000\n\
aegis_state_seq=7\n\
limits_config_sha256=abababababababababababababababababababababababababababababababab\n\
key_id=dev-ed25519-1234abcd\n";
        assert_eq!(f.canonical_text().unwrap(), expected);
        let digest = f.digest().unwrap();
        assert_eq!(
            hex::encode(&digest),
            hex::encode(&Sha256::digest(expected.as_bytes()))
        );
    }

    #[test]
    fn every_bound_field_changes_the_digest() {
        let base = sample_fields().digest().unwrap();
        type Mutation = Box<dyn Fn(&mut AttestationFields)>;
        let mutations: Vec<Mutation> = vec![
            Box::new(|f| f.signal_id.push('0')),
            Box::new(|f| f.symbol = "MSFT".into()),
            Box::new(|f| f.side = Side::Sell),
            Box::new(|f| f.side = Side::SellShort),
            Box::new(|f| f.order_type = "MARKET".into()),
            Box::new(|f| f.qty_nanos += 1),
            Box::new(|f| f.limit_price_nanos += 1),
            Box::new(|f| f.stop_price_nanos += 1),
            Box::new(|f| f.decided_at_ns += 1),
            Box::new(|f| f.expires_at_ns += 1),
            Box::new(|f| f.aegis_state_seq += 1),
            Box::new(|f| f.limits_config_sha256 = "cd".repeat(32)),
            Box::new(|f| f.key_id = "other".into()),
        ];
        for (i, m) in mutations.iter().enumerate() {
            let mut f = sample_fields();
            m(&mut f);
            assert_ne!(
                f.digest().unwrap(),
                base,
                "mutation {i} must change the digest"
            );
        }
    }

    #[test]
    fn injection_of_newlines_or_equals_is_refused() {
        for bad in ["A\nqty_nanos=1", "A=B", "", "A\r"] {
            let mut f = sample_fields();
            f.symbol = bad.into();
            assert!(f.canonical_text().is_err(), "{bad:?}");
        }
    }
}
