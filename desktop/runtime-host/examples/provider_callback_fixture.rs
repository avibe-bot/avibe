//! AUTH-SETUP-183: real loopback sockets for the isolated production-UI browser probe.
//! No browser opening, account, HTTP upstream, keychain, or persisted user state.
use avibe_runtime_host::instance_client::{
    loopback::{Callback, CallbackCopy, Loopback, ResultPage},
    transport::Epoch,
};
use std::{
    io::{self, BufRead, Write},
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
    time::Duration,
};

#[tokio::main(flavor = "current_thread")]
async fn main() {
    let mode = std::env::args().nth(1).unwrap_or_else(|| "normal".into());
    let locale = std::env::args().nth(2).unwrap_or_else(|| "zh".into());
    let source = if locale == "en" {
        include_str!("../../../ui/src/i18n/en.json")
    } else {
        include_str!("../../../ui/src/i18n/zh.json")
    };
    let catalog: serde_json::Value = serde_json::from_str(source).unwrap();
    let copy: CallbackCopy = serde_json::from_value(catalog["desktopBootstrap"]["providerCallback"].clone()).unwrap();
    let generation = Arc::new(AtomicU64::new(1));
    let epoch = Epoch::new(generation.clone(), 1);
    let mut listener = Loopback::bind(&copy).await.unwrap();
    let redirect = listener.redirect_uri("synthetic-browser-fixture-state");
    println!("{}", serde_json::json!({"event":"ready", "redirect":redirect.as_str()}));
    io::stdout().flush().unwrap();
    std::thread::spawn(move || {
        for line in io::stdin().lock().lines() {
            if line.unwrap_or_default() == "cancel" {
                generation.store(2, Ordering::SeqCst);
                break;
            }
        }
    });
    let code = if mode == "abandon" {
        "stale_generation".to_owned()
    } else {
        let deadline = if mode == "timeout" {
            Duration::from_millis(700)
        } else {
            Duration::from_secs(30)
        };
        match listener.wait("synthetic-browser-fixture-state", &epoch, deadline).await {
            Ok(Callback::Code(_)) => {
                listener.complete(ResultPage::Complete);
                "success".into()
            }
            Ok(Callback::Failed(failure)) => failure.code.to_owned(),
            Err(error) => error.code,
        }
    };
    drop(listener);
    println!("{}", serde_json::json!({"event":"result", "code":code}));
    io::stdout().flush().unwrap();
    // The probe terminates this child; otherwise its responder expires normally.
    tokio::time::sleep(Duration::from_secs(601)).await;
}
