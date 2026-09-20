//! Polygon.io (now Massive) stocks WebSocket wire format.
//!
//! Verified against the vendor docs (massive.com/docs/websocket/stocks/quotes,
//! .../trades, checked 2026-09-20):
//! * Quote `Q`: ticker in `sym`, `bp/bs/ap/as`, `t` = SIP timestamp in Unix
//!   **milliseconds**. There is NO `y` field on WebSocket quotes.
//! * Trade `T`: ticker in `sym`, `p` price, `s` size, `t` SIP ms timestamp.
//! * The previous code read the ticker from `T` and would have dropped every
//!   real message as "unrecognised"; `T` is still accepted as an alias.
//!
//! Handshake statuses (`ev:"status"`): `connected`, `auth_success`,
//! `auth_failed`. `auth_failed` and `{"status":"error","message":"authentication
//! failed"}` are documented by client libraries/forum reports; `auth_success`
//! is the documented success value but was not re-verified against a live
//! socket in this environment. To stay safe against shape drift we also accept
//! the legacy `ev:"auth"` event and treat any `error` status mentioning
//! "auth" as a credential failure. `max_connections` is transient.

use serde::Deserialize;

use crate::error::{Result, SensoryError};

#[derive(Debug, Deserialize, PartialEq)]
#[serde(tag = "ev")]
pub enum PolyMsg {
    #[serde(rename = "status", alias = "auth")]
    Status {
        #[serde(default)]
        status: String,
        #[serde(default)]
        message: String,
    },

    #[serde(rename = "Q")]
    Quote {
        #[serde(rename = "sym", alias = "T")]
        ticker: String,
        #[serde(rename = "bp")]
        bid_price: f64,
        #[serde(rename = "bs")]
        bid_size: f64,
        #[serde(rename = "ap")]
        ask_price: f64,
        #[serde(rename = "as")]
        ask_size: f64,
        /// SIP timestamp, Unix milliseconds.
        #[serde(rename = "t")]
        timestamp_ms: i64,
    },

    #[serde(rename = "T")]
    Trade {
        #[serde(rename = "sym", alias = "T")]
        ticker: String,
        #[serde(rename = "p")]
        price: f64,
        #[serde(rename = "s")]
        size: f64,
        /// SIP timestamp, Unix milliseconds.
        #[serde(rename = "t")]
        timestamp_ms: i64,
    },

    /// Any other event type (aggregates, LULD, ...): ignored.
    #[serde(other)]
    Unknown,
}

/// Outcome of inspecting one status message during the handshake.
#[derive(Debug, PartialEq, Eq)]
pub enum Handshake {
    /// Authenticated: the session may subscribe.
    Authenticated,
    /// Credentials rejected: fatal.
    Rejected(String),
    /// Server-side condition that reconnecting may resolve.
    Transient(String),
    /// Not relevant to the handshake (`connected`, informational, data).
    Ignore,
}

pub fn classify_handshake(msg: &PolyMsg) -> Handshake {
    let PolyMsg::Status { status, message } = msg else {
        return Handshake::Ignore;
    };
    match status.as_str() {
        "auth_success" => Handshake::Authenticated,
        "auth_failed" => Handshake::Rejected(message.clone()),
        "max_connections" => Handshake::Transient(message.clone()),
        "error" if message.to_ascii_lowercase().contains("auth") => {
            Handshake::Rejected(message.clone())
        }
        "error" => Handshake::Transient(message.clone()),
        _ => Handshake::Ignore,
    }
}

/// Parse a text frame into individual raw items.
///
/// Polygon sends JSON arrays. A frame that is not an array is an error the
/// caller logs and skips (it must not tear the connection down).
pub fn parse_frame(text: &str) -> Result<Vec<serde_json::Value>> {
    serde_json::from_str::<Vec<serde_json::Value>>(text).map_err(SensoryError::from)
}

/// Decode one array item; `None` if it is not a well-formed known message.
pub fn parse_item(raw: serde_json::Value) -> Option<PolyMsg> {
    serde_json::from_value(raw).ok()
}

/// Build the `params` string of the subscribe action.
pub fn subscription_params(symbols: &[String]) -> String {
    if symbols.iter().any(|s| s == "*") {
        return "Q.*,T.*".to_string();
    }
    symbols
        .iter()
        .flat_map(|s| [format!("Q.{s}"), format!("T.{s}")])
        .collect::<Vec<_>>()
        .join(",")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn item(json: &str) -> Option<PolyMsg> {
        parse_item(serde_json::from_str(json).expect("json"))
    }

    #[test]
    fn parses_documented_quote_shape() {
        let m = item(
            r#"{"ev":"Q","sym":"MSFT","bx":4,"bp":114.125,"bs":100,"ax":7,"ap":114.128,"as":160,"c":0,"i":[604],"t":1536036818784,"z":3,"q":50385480}"#,
        );
        assert_eq!(
            m,
            Some(PolyMsg::Quote {
                ticker: "MSFT".into(),
                bid_price: 114.125,
                bid_size: 100.0,
                ask_price: 114.128,
                ask_size: 160.0,
                timestamp_ms: 1_536_036_818_784,
            })
        );
    }

    #[test]
    fn parses_documented_trade_shape() {
        let m = item(
            r#"{"ev":"T","sym":"MSFT","x":4,"i":"12345","z":3,"p":114.125,"s":100,"c":[0,12],"t":1536036818784,"q":3681328}"#,
        );
        assert_eq!(
            m,
            Some(PolyMsg::Trade {
                ticker: "MSFT".into(),
                price: 114.125,
                size: 100.0,
                timestamp_ms: 1_536_036_818_784,
            })
        );
    }

    #[test]
    fn legacy_ticker_key_is_accepted() {
        assert!(item(r#"{"ev":"Q","T":"AAPL","bp":1.0,"bs":1,"ap":1.1,"as":1,"t":1}"#).is_some());
    }

    #[test]
    fn malformed_and_unknown_items_are_none_or_unknown() {
        assert!(item(r#"{"ev":"Q","sym":"AAPL"}"#).is_none());
        assert!(item(r#"{"nope":1}"#).is_none());
        assert_eq!(item(r#"{"ev":"AM","sym":"AAPL"}"#), Some(PolyMsg::Unknown));
        assert!(item(r#"{"ev":"Q","sym":"A","bp":"x","bs":1,"ap":1,"as":1,"t":1}"#).is_none());
    }

    #[test]
    fn handshake_classification() {
        let st = |status: &str, message: &str| PolyMsg::Status {
            status: status.into(),
            message: message.into(),
        };
        assert_eq!(classify_handshake(&st("auth_success", "authenticated")), Handshake::Authenticated);
        assert!(matches!(classify_handshake(&st("auth_failed", "authentication failed")), Handshake::Rejected(_)));
        assert!(matches!(classify_handshake(&st("error", "authentication failed")), Handshake::Rejected(_)));
        assert!(matches!(classify_handshake(&st("max_connections", "x")), Handshake::Transient(_)));
        assert!(matches!(classify_handshake(&st("error", "boom")), Handshake::Transient(_)));
        assert_eq!(classify_handshake(&st("connected", "Connected Successfully")), Handshake::Ignore);
        assert_eq!(classify_handshake(&PolyMsg::Unknown), Handshake::Ignore);
    }

    #[test]
    fn both_documented_and_legacy_auth_shapes_authenticate() {
        let a = item(r#"{"ev":"status","status":"auth_success","message":"authenticated"}"#);
        let b = item(r#"{"ev":"auth","status":"auth_success"}"#);
        for m in [a, b] {
            assert_eq!(classify_handshake(&m.expect("parsed")), Handshake::Authenticated);
        }
    }

    #[test]
    fn frame_parsing_requires_an_array() {
        assert_eq!(parse_frame(r#"[{"ev":"status"}]"#).expect("array").len(), 1);
        assert!(parse_frame(r#"{"ev":"status"}"#).is_err());
        assert!(parse_frame("not json").is_err());
    }

    #[test]
    fn subscription_params_shapes() {
        assert_eq!(subscription_params(&["*".into()]), "Q.*,T.*");
        assert_eq!(
            subscription_params(&["AAPL".into(), "MSFT".into()]),
            "Q.AAPL,T.AAPL,Q.MSFT,T.MSFT"
        );
    }
}
