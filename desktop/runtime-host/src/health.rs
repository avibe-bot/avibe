//! Readiness probing against the Avibe Web UI server.

use std::time::Duration;

use async_trait::async_trait;

use crate::origin::LoopbackOrigin;

/// Answers one question: are the Avibe UI and Controller serving this origin?
///
/// The answer is deliberately a bare `bool`. Transport errors and response
/// bodies stay inside the probe so nothing from the network reaches the
/// bootstrap UI.
#[async_trait]
pub trait HealthProbe: Send + Sync {
    async fn is_healthy(&self, origin: &LoopbackOrigin) -> bool;
}

/// `GET <origin>/ready`, requiring UI, service ownership, and Controller IPC.
pub struct HttpHealthProbe {
    client: reqwest::Client,
}

impl HttpHealthProbe {
    pub fn new(timeout: Duration) -> Result<Self, reqwest::Error> {
        let client = reqwest::Client::builder()
            .timeout(timeout)
            // A proxy configured for the wider machine must never sit between the
            // shell and a loopback Runtime.
            .no_proxy()
            .build()?;
        Ok(Self { client })
    }
}

#[async_trait]
impl HealthProbe for HttpHealthProbe {
    async fn is_healthy(&self, origin: &LoopbackOrigin) -> bool {
        let Ok(response) = self.client.get(origin.readiness_url()).send().await else {
            return false;
        };
        if !response.status().is_success() {
            return false;
        }
        let Ok(body) = response.text().await else {
            return false;
        };
        is_avibe_readiness_body(&body)
    }
}

/// Whether a `/ready` body proves both the UI and Controller are ready.
///
/// The Python endpoint performs the authoritative service-lock and internal IPC
/// checks. Rust accepts only its exact affirmative payload.
pub fn is_avibe_readiness_body(body: &str) -> bool {
    let Ok(payload) = serde_json::from_str::<serde_json::Value>(body) else {
        return false;
    };
    payload.as_object().is_some_and(|object| {
        object.len() == 3
            && object.get("schema_version").and_then(serde_json::Value::as_u64) == Some(1)
            && object.get("product").and_then(serde_json::Value::as_str) == Some("avibe")
            && object.get("ready").and_then(serde_json::Value::as_bool) == Some(true)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_the_exact_runtime_readiness_payload() {
        assert!(is_avibe_readiness_body(
            r#"{"schema_version":1,"product":"avibe","ready":true}"#
        ));
    }

    #[test]
    fn rejects_ui_only_starting_stale_and_unrelated_bodies() {
        let bodies = [
            "",
            "ok",
            "<html><body>hello</body></html>",
            "{}",
            r#"{"status":"ok"}"#,
            r#"{"ready":true}"#,
            r#"{"schema_version":1,"product":"other","ready":true}"#,
            r#"{"schema_version":2,"product":"avibe","ready":true}"#,
            r#"{"ready":false,"code":"controller_unavailable"}"#,
            r#"{"schema_version":1,"product":"avibe","ready":true,"extra":1}"#,
            r#"{"ready":"true"}"#,
            "[]",
        ];
        for body in bodies {
            assert!(!is_avibe_readiness_body(body), "body {body:?} must not be adopted");
        }
    }

    #[test]
    fn probe_construction_does_not_need_a_server() {
        HttpHealthProbe::new(Duration::from_secs(2)).expect("probe builds");
    }
}
