//! A fresh, fail-closed observation of the local Runtime that the shell owns.
use avibe_runtime_host::LoopbackOrigin;
use serde::Deserialize;
use std::{sync::atomic::Ordering, time::Duration};
use tauri::{AppHandle, Manager};

const NOT_IDLE: &str = "runtime_not_idle";

#[derive(Deserialize)]
struct Idle {
    idle_seconds: u64,
}

fn local_origin(app: &AppHandle) -> Option<(LoopbackOrigin, u64)> {
    let shell = app.try_state::<crate::Shell>()?;
    if shell.activity.load(Ordering::SeqCst) != crate::ACTIVITY_MONITOR
        || crate::instance_client::presentation_busy(app)
    {
        return None;
    }
    let origin = shell.active_origin.lock().ok()?.clone()?;
    Some((origin, shell.monitor_generation.load(Ordering::SeqCst)))
}

pub(super) async fn ready(app: &AppHandle) -> Result<(), String> {
    if !super::auto_update_enabled(app) {
        return Err("auto_update_disabled".into());
    }
    let origin = local_origin(app).ok_or(NOT_IDLE)?;
    probe(&origin.0, super::AUTO_UPDATE_IDLE_WINDOW).await?;
    // Readiness loss/recovery while the controller answers invalidates its
    // conclusion. Never use the selected remote instance's activity here.
    if local_origin(app).as_ref() != Some(&origin) {
        return Err(NOT_IDLE.into());
    }
    if !super::auto_update_enabled(app) {
        return Err("auto_update_disabled".into());
    }
    Ok(())
}

async fn probe(origin: &LoopbackOrigin, required: Duration) -> Result<(), String> {
    let request = async {
        let client = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(5))
            .build()
            .map_err(|_| NOT_IDLE)?;
        let mut response = client
            .get(format!("{}/api/desktop/update/idle", origin.as_str()))
            .header(reqwest::header::CACHE_CONTROL, "no-store")
            .send()
            .await
            .map_err(|_| NOT_IDLE)?;
        if response.status() != reqwest::StatusCode::OK {
            return Err(NOT_IDLE);
        }
        let mut bytes = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(|_| NOT_IDLE)? {
            if bytes.len() + chunk.len() > 4096 {
                return Err(NOT_IDLE);
            }
            bytes.extend_from_slice(&chunk);
        }
        let idle: Idle = serde_json::from_slice(&bytes).map_err(|_| NOT_IDLE)?;
        if idle.idle_seconds < required.as_secs() {
            return Err(NOT_IDLE);
        }
        Ok(())
    };
    tokio::time::timeout(Duration::from_secs(5), request)
        .await
        .map_err(|_| NOT_IDLE.to_owned())?
        .map_err(str::to_owned)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};

    async fn reply(status: &str, body: &str) -> Result<(), String> {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let origin = LoopbackOrigin::parse(&format!("http://{}", listener.local_addr().unwrap())).unwrap();
        let response = format!(
            "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
            let mut buffer = [0; 4096];
            let count = stream.read(&mut buffer).unwrap();
            assert!(String::from_utf8_lossy(&buffer[..count]).starts_with("GET /api/desktop/update/idle "));
            let _ = stream.write_all(response.as_bytes());
        });
        let result = probe(&origin, Duration::from_secs(1800)).await;
        server.join().unwrap();
        result
    }

    #[tokio::test]
    async fn only_a_fresh_valid_idle_response_can_allow_automatic_installation() {
        assert!(reply("200 OK", r#"{"idle_seconds":1800}"#).await.is_ok());
        for body in [
            r#"{"idle_seconds":0}"#,
            r#"{"idle_seconds":1799}"#,
            r#"{"idle_seconds":-1}"#,
            r#"{"idle_seconds":true}"#,
            r#"{"idle_seconds":"1800"}"#,
            r#"{"idle_seconds":null}"#,
            r#"{"idle":true}"#,
            "not json",
        ] {
            assert_eq!(
                reply("200 OK", body).await.as_ref().map_err(String::as_str),
                Err(NOT_IDLE),
                "{body}"
            );
        }
        for status in ["404 Not Found", "503 Unavailable", "302 Found"] {
            assert_eq!(
                reply(status, r#"{"idle_seconds":1800}"#)
                    .await
                    .as_ref()
                    .map_err(String::as_str),
                Err(NOT_IDLE)
            );
        }
        assert!(reply("200 OK", &" ".repeat(4097)).await.is_err());
    }
}
