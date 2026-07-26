//! Readiness probing against the Avibe Web UI server.

use std::time::Duration;

use async_trait::async_trait;

use crate::origin::LoopbackOrigin;

/// Answers one question: is an Avibe Runtime serving this origin right now?
///
/// The answer is deliberately a bare `bool`. Transport errors and response
/// bodies stay inside the probe so nothing from the network reaches the
/// bootstrap UI.
#[async_trait]
pub trait HealthProbe: Send + Sync {
    async fn is_healthy(&self, origin: &LoopbackOrigin) -> bool;
}

/// `GET <origin>/health`, expecting Avibe's `{"status": "ok"}`.
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
        let Ok(response) = self.client.get(origin.health_url()).send().await else {
            return false;
        };
        if !response.status().is_success() {
            return false;
        }
        let Ok(body) = response.text().await else {
            return false;
        };
        is_avibe_health_body(&body)
    }
}

/// Whether a `/health` body identifies an Avibe Runtime.
///
/// Checking the body, not just the status code, keeps the shell from adopting
/// some unrelated service that happens to hold the configured loopback port.
pub fn is_avibe_health_body(body: &str) -> bool {
    serde_json::from_str::<serde_json::Value>(body)
        .ok()
        .and_then(|payload| payload.get("status")?.as_str().map(str::to_owned))
        .is_some_and(|status| status == "ok")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_the_avibe_health_payload() {
        assert!(is_avibe_health_body(r#"{"status": "ok"}"#));
        assert!(is_avibe_health_body(r#"{"status":"ok","extra":1}"#));
    }

    #[test]
    fn rejects_bodies_that_do_not_identify_an_avibe_runtime() {
        let bodies = [
            "",
            "ok",
            "<html><body>hello</body></html>",
            "{}",
            r#"{"status": "starting"}"#,
            r#"{"status": "OK"}"#,
            r#"{"status": true}"#,
            r#"{"health": "ok"}"#,
            "[]",
        ];
        for body in bodies {
            assert!(!is_avibe_health_body(body), "body {body:?} must not be adopted");
        }
    }

    #[test]
    fn probe_construction_does_not_need_a_server() {
        HttpHealthProbe::new(Duration::from_secs(2)).expect("probe builds");
    }
}
