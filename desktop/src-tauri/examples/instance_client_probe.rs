//! Actual bundled U1 -> Rust command -> coordinator -> native store fixture.
//! All transports and credential storage are explicit test-owned dependencies.
use async_trait::async_trait;
use avibe_desktop_lib::instance_client::{self, NativeClientConfig};
use avibe_runtime_host::{
    instance_client::{
        account::Passkeys,
        protocol::*,
        storage::{ConnectionStore, Credentials},
        transport::{Epoch, Request, Response, Transport},
    },
    LoopbackOrigin,
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io::BufRead,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
};
use tauri::{Listener, Manager, WebviewUrl, WebviewWindowBuilder};
use tokio::sync::oneshot;
use url::Url;

#[derive(Deserialize)]
struct Config {
    root: PathBuf,
    backend: Url,
    provider: Url,
    local: Url,
    proxy: Url,
    mappings: PathBuf,
}
struct Wire {
    client: reqwest::Client,
    config: Arc<Config>,
    controls: Arc<Controls>,
}
#[derive(Default)]
struct Controls {
    hold: Mutex<Option<(String, oneshot::Receiver<()>)>>,
    release: Mutex<Option<oneshot::Sender<()>>>,
    bad_csrf: AtomicBool,
}
#[async_trait]
impl Transport for Wire {
    async fn send(&self, request: Request) -> Result<Response, ClientError> {
        let target = match request.url.host_str() {
            Some("avibe.bot") => self.config.backend.clone(),
            Some("provider.fixture.test") => self.config.provider.clone(),
            _ if request.url.origin() == self.config.local.origin() => self.config.local.clone(),
            Some(host) => {
                let mappings: HashMap<String, Url> = serde_json::from_slice(
                    &std::fs::read(&self.config.mappings).map_err(|_| ClientError::new("fixture_mapping_missing"))?,
                )
                .map_err(|_| ClientError::new("fixture_mapping_invalid"))?;
                mappings
                    .get(host)
                    .cloned()
                    .ok_or_else(|| ClientError::new("fixture_egress_denied"))?
            }
            _ => return Err(ClientError::new("fixture_egress_denied")),
        };
        assert_eq!(target.scheme(), "http");
        assert_eq!(target.host_str(), Some("127.0.0.1"));
        let mut url = target;
        url.set_path(request.url.path());
        url.set_query(request.url.query());
        let mut headers = request.headers;
        headers.insert(
            "Host",
            request.url[url::Position::BeforeHost..url::Position::AfterPort]
                .parse()
                .unwrap(),
        );
        headers.insert("X-Fixture-Scheme", request.url.scheme().parse().unwrap());
        if request.url.path() == "/api/desktop/local-instance/prepare"
            && self.controls.bad_csrf.swap(false, Ordering::SeqCst)
        {
            headers.insert("X-Vibe-CSRF-Token", "fixture-wrong-token".parse().unwrap());
        }
        let mut call = self.client.request(request.method, url).headers(headers);
        if let Some(body) = request.body {
            call = call.json(&body);
        }
        let response = call.send().await?;
        let result = Response {
            status: response.status().as_u16(),
            headers: response.headers().clone(),
            body: response.bytes().await?.to_vec(),
        };
        let held = {
            let mut hold = self.controls.hold.lock().unwrap();
            if hold.as_ref().is_some_and(|(path, _)| path == request.url.path()) {
                hold.take().map(|(_, receiver)| receiver)
            } else {
                None
            }
        };
        if let Some(receiver) = held {
            emit(json!({"event":"http_held","path":request.url.path(),"status":result.status}));
            tokio::time::timeout(std::time::Duration::from_secs(30), receiver)
                .await
                .map_err(|_| ClientError::new("fixture_hold_timeout"))?
                .map_err(|_| ClientError::new("fixture_hold_cancelled"))?;
        }
        Ok(result)
    }
}
#[derive(Default)]
struct Private(Mutex<HashMap<String, Vec<u8>>>);
impl Credentials for Private {
    fn read(&self, id: &str) -> Result<Option<Vec<u8>>, ClientError> {
        Ok(self.0.lock().unwrap().get(id).cloned())
    }
    fn write(&self, id: &str, bytes: &[u8]) -> Result<(), ClientError> {
        self.0.lock().unwrap().insert(id.into(), bytes.into());
        Ok(())
    }
    fn erase(&self, id: &str) -> Result<(), ClientError> {
        self.0.lock().unwrap().remove(id);
        Ok(())
    }
}
struct NoSystemPasskey;
#[async_trait]
impl Passkeys for NoSystemPasskey {
    async fn assert(&self, _: Value, _: Epoch) -> Result<Value, ClientError> {
        Err(ClientError::new("fixture_passkey_unavailable"))
    }
}
fn emit(value: Value) {
    println!("{value}");
}
#[tauri::command]
async fn desktop_client_request(
    window: tauri::WebviewWindow,
    app: tauri::AppHandle,
    request: ClientRequest,
) -> Result<ClientReply, String> {
    let reply = instance_client::desktop_client_request(window, app, request).await;
    emit(json!({"event":"reply","error":match &reply {
        Ok(reply) => reply.error.as_ref().map(|error|error.code.as_str()),
        Err(error) => Some(error.as_str()),
    }}));
    reply
}

fn main() {
    let config: Config =
        serde_json::from_slice(&std::fs::read(std::env::args().nth(1).expect("fixture config")).unwrap()).unwrap();
    let home = PathBuf::from(std::env::var_os("HOME").unwrap());
    assert!(home.starts_with(&config.root));
    assert_eq!(config.proxy.host_str(), Some("127.0.0.1"));
    let config = Arc::new(config);
    let mut context = tauri::generate_context!();
    context.config_mut().build.dev_url = None;
    context.config_mut().app.windows[0].proxy_url = Some(config.proxy.clone());
    let setup = config.clone();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .invoke_handler(tauri::generate_handler![desktop_client_request])
        .plugin(tauri::plugin::Builder::<_,()>::new("instance-fixture")
            .on_webview_ready(|view| {
                #[cfg(target_os="macos")]
                support::fixture_tls::install(&view);
            })
            .on_page_load(|view, payload| {
                emit(json!({"event":"page_load","label":view.label(),"finished":payload.event()==tauri::webview::PageLoadEvent::Finished,"origin":payload.url().origin().ascii_serialization(),"path":payload.url().path()}));
                if view.label()=="main" && payload.event()==tauri::webview::PageLoadEvent::Finished {
                    instance_client::page_loaded(view.app_handle());
                    emit(json!({"event":"loaded","url":payload.url().as_str()}));
                }
            })
            .on_navigation(|view, url| {
                if url.scheme()=="fixture-result" {
                    let data = url.query_pairs().find(|(key,_)|key=="value").map(|(_,value)|value.into_owned()).unwrap_or_default();
                    emit(json!({"event":"javascript","value":serde_json::from_str::<Value>(&data).unwrap_or(Value::String(data))}));
                    return false;
                }
                if instance_client::presentation_request(view.app_handle(),view.label(),||view.url().ok(),url) {
                    return false;
                }
                let allowed=instance_client::navigation(view.app_handle(),view.label(),url).unwrap_or_else(|| {
                    url.scheme()=="tauri" || url.host_str()==Some("127.0.0.1")
                });
                emit(json!({"event":"navigation","origin":url.origin().ascii_serialization(),"path":url.path(),"allowed":allowed}));
                allowed
            }).build())
        .setup(move |app| {
            assert!(app.path().app_local_data_dir()?.starts_with(&setup.root));
            assert!(app.path().app_data_dir()?.starts_with(&setup.root));
            avibe_desktop_lib::updater::init(app.handle())?;
            app.listen_any(avibe_desktop_lib::updater::STATE_EVENT, |event| {
                emit(json!({"event":"update","state":serde_json::from_str::<Value>(event.payload()).unwrap()}));
            });
            let bundle:Url="tauri://localhost/".parse()?;
            let asset=app.asset_resolver().get("account/index.html".into()).expect("U1 bundle missing");
            assert!(std::str::from_utf8(asset.bytes()).unwrap().contains("avibe-desktop-client-version"));
            let store=ConnectionStore::new(setup.root.join("connection.json"));
            let record=store.load("小明的 Mac").map_err(|error|std::io::Error::other(error.code))?;
            let private=Arc::new(Private::default());
            let controls=Arc::new(Controls::default());
            let local=LoopbackOrigin::parse(setup.local.as_str())?;
            let coordinator=instance_client::attach(app.handle(),NativeClientConfig {
                bundle:bundle.clone(), state:ClientState::new("小明的 Mac".into(),"0.1.0".into(),json!({"phase":"idle","channel":"stable","prereleases":false,"interactive":false})),
                record,store,local_origin:Arc::new(Mutex::new(Some(local.clone()))),
            },Arc::new(Wire { client:reqwest::Client::builder().no_proxy().redirect(reqwest::redirect::Policy::none()).build()?,config:setup.clone(),controls:controls.clone() }),private.clone(),Arc::new(NoSystemPasskey));
            coordinator.local_origin(Some(local));
            let mut window_config=app.config().app.windows[0].clone();
            window_config.url=WebviewUrl::External(bundle);
            WebviewWindowBuilder::from_config(app,&window_config)?
                .initialization_script(instance_client::initial_script(app.handle()).unwrap())
                .visible(false).build()?;
            instance_client::present(app.handle(),"account");
            let handle=app.handle().clone();
            std::thread::spawn(move || {
                for line in std::io::stdin().lock().lines() {
                    let command:Value=serde_json::from_str(&line.unwrap()).unwrap();
                    match command["action"].as_str().unwrap() {
                        "state"=>emit(json!({"event":"state","state":coordinator.state(),"credential_count":private.0.lock().unwrap().len(),
                            "instance_stores": std::fs::read_dir(handle.path().app_local_data_dir().unwrap().join("instance-stores")).map(|entries|entries.count()).unwrap_or(0),
                        })),
                        "wrong_csrf"=> { controls.bad_csrf.store(true, Ordering::SeqCst);emit(json!({"event":"control_ready"})); },
                        "hold"=> {
                            let (sender,receiver)=oneshot::channel();
                            *controls.hold.lock().unwrap()=Some((command["path"].as_str().unwrap().into(),receiver));
                            *controls.release.lock().unwrap()=Some(sender);
                            emit(json!({"event":"control_ready"}));
                        },
                        "release"=> { controls.release.lock().unwrap().take().unwrap().send(()).unwrap(); },
                        #[cfg(target_os="macos")]
                        "dialog"=> {
                            let choice=command["choice"].as_u64().map(|value|value as usize);
                            handle.run_on_main_thread(move || emit(support::fixture_dialog::inspect(choice))).unwrap();
                        },
                        "escape"=> {
                            let coordinator=coordinator.clone();
                            tauri::async_runtime::spawn(async move {
                                let reply=coordinator.request(ClientRequest{version:1,request_id:"fixture-local-escape".into(),
                                    expected_generation:coordinator.state().generation,action:ClientAction::ReturnLocal}).await;
                                emit(json!({"event":"escaped","ok":reply.ok}));
                            });
                        },
                        "eval"=> { let window=handle.get_webview_window("main").expect("main"); window.eval(command["script"].as_str().unwrap()).unwrap(); },
                        "present"=>instance_client::present(&handle, match command["view"].as_str().unwrap() {
                            "instances"=>"instances","sign-out"=>"sign-out","account"=>"account",_=>panic!("unsupported view"),
                        }),
                        "exit"=> {handle.exit(0);break;},
                        _=>panic!("unsupported fixture command"),
                    }
                }
            });
            emit(json!({"event":"native_ready"}));
            Ok(())
        }).build(context).unwrap();
    app.run(|app, event| {
        if let tauri::RunEvent::ExitRequested { api, code, .. } = event {
            let replacement = instance_client::replacing_window_exit(app, code);
            emit(json!({"event":"exit_requested","code":code,"replacement":replacement}));
            if replacement {
                api.prevent_exit();
            }
        }
    });
}
mod support {
    #[cfg(target_os = "macos")]
    pub mod fixture_dialog;
    #[cfg(target_os = "macos")]
    pub mod fixture_tls;
}
