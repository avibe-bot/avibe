//! Compiled solely by the explicit app-lock-e2e feature (forbidden in release).
//! Uses the production native owner, presentation and completion handlers. Only
//! time, foreground state and LA are deterministic. Browser tests communicate
//! over stdio; nothing is listening on a socket or exposed to a shipped page.
use super::*;
use serde_json::{json, Value};
use std::io::{BufRead, Write};

// The receiver fixture needs the shell's presentation state, but it must never
// resolve, probe or start an actual Runtime. These dependencies fail loudly if
// a future test accidentally crosses that boundary.
struct NoRuntime;
#[async_trait::async_trait]
impl avibe_runtime_host::HealthProbe for NoRuntime {
    async fn presence(&self, _: &avibe_runtime_host::LoopbackOrigin, _: Option<&str>) -> avibe_runtime_host::Presence {
        panic!("App Lock peer must not probe a Runtime")
    }
}
impl avibe_runtime_host::RuntimeLauncher for NoRuntime {
    fn resolve(
        &self,
    ) -> Result<std::sync::Arc<dyn avibe_runtime_host::ResolvedRuntimeLauncher>, avibe_runtime_host::LaunchError> {
        panic!("App Lock peer must not resolve or launch a Runtime")
    }
}

struct Peer {
    time: f64,
    active: bool,
    recovery_facts: bool,
    availability: Availability,
    outcome: Option<Outcome>,
    pending: Option<(u64, Authentication)>,
    calls: usize,
    compose_loaded: bool,
    app_pages_loaded: Vec<String>,
    executed: Vec<String>,
    centers: [Retained<NSNotificationCenter>; 3],
    native_windows: Vec<Retained<NSWindow>>,
}
thread_local! { static PEER: RefCell<Option<Peer>> = const { RefCell::new(None) }; }
pub(super) fn time() -> Option<f64> {
    PEER.with_borrow(|p| p.as_ref().map(|p| p.time))
}
pub(super) fn active() -> Option<bool> {
    PEER.with_borrow(|p| p.as_ref().map(|p| p.active))
}
pub(super) fn recovery_facts() -> Option<bool> {
    PEER.with_borrow(|p| p.as_ref().map(|p| p.recovery_facts))
}
pub(super) fn availability() -> Option<Availability> {
    PEER.with_borrow(|p| p.as_ref().map(|p| p.availability))
}
pub(super) fn authenticate(_app: &AppHandle, epoch: u64, purpose: Authentication, _reply: &Option<Reply>) -> bool {
    PEER.with_borrow_mut(|p| {
        if let Some(p) = p {
            p.calls += 1;
            p.pending = Some((epoch, purpose));
            true
        } else {
            false
        }
    })
}
pub(super) fn finish_if_ready(app: &AppHandle) {
    let finish = PEER.with_borrow_mut(|p| {
        let p = p.as_mut()?;
        p.outcome
            .and_then(|outcome| p.pending.take().map(|(epoch, purpose)| (epoch, purpose, outcome)))
    });
    if let Some((epoch, _purpose, outcome)) = finish {
        complete(app, epoch, outcome, now());
    }
}
fn output(id: Value, result: Result<Value, String>) {
    let response = match result {
        Ok(value) => json!({"id":id,"value":value}),
        Err(error) => json!({"id":id,"error":error}),
    };
    let mut stdout = std::io::stdout().lock();
    writeln!(stdout, "{response}").expect("peer stdout");
    stdout.flush().expect("peer flush");
}
fn native_window(app: &AppHandle, label: Option<&str>) {
    let mtm = MainThreadMarker::new().unwrap();
    let window = if let Some(label) = label {
        let window = tauri::WindowBuilder::new(app, label)
            .title("App Lock fixture")
            .visible(false)
            .build()
            .unwrap();
        unsafe { Retained::retain(window.ns_window().unwrap().cast::<NSWindow>()) }.unwrap()
    } else {
        let window = unsafe {
            NSWindow::initWithContentRect_styleMask_backing_defer(
                mtm.alloc(),
                NSRect::new(NSPoint::new(0.0, 0.0), NSSize::new(500.0, 400.0)),
                NSWindowStyleMask::Titled | NSWindowStyleMask::Closable,
                NSBackingStoreType::Buffered,
                false,
            )
        };
        unsafe {
            window.setReleasedWhenClosed(false);
        }
        PEER.with_borrow_mut(|p| p.as_mut().unwrap().native_windows.push(window.clone()));
        window
    };
    let content = window.contentView().unwrap();
    let private = NSTextField::labelWithString(&NSString::from_str("PRIVATE FIXTURE CONTENT"), mtm);
    private.setFrame(content.bounds());
    content.addSubview(&private);
    sync_windows(app);
}
fn snapshot(app: &AppHandle) -> Value {
    let state = status(app);
    let (locked, verifying, away_since, candidate_leave, blocked, should_prompt) = {
        let owner = machine(app);
        let state = owner.machine.lock().unwrap();
        (
            state.locked,
            state.busy(),
            state.away_since,
            state.candidate_leave,
            state.blocked,
            state.should_prompt(),
        )
    };
    let (calls, executed) = PEER.with_borrow(|p| {
        let p = p.as_ref().unwrap();
        (p.calls, p.executed.clone())
    });
    let windows = PRESENTATION.with_borrow(|p| p.covers.iter().filter_map(|cover| {
        let window = cover.window.load()?;
        let label = app.windows().into_iter().find_map(|(label, candidate)| {
            candidate.ns_window().ok().filter(|native| std::ptr::eq(native.cast::<NSWindow>(), &*window)).map(|_| label)
        });
        Some(json!({"label": label, "covered": unsafe { cover.view.superview() }.is_some(), "title": window.title().to_string(),
            "hidden":cover.hidden.iter().filter_map(Weak::load).all(|view| view.isHidden()),
            "contentCount":cover.hidden.len(), "compact":cover.view.ivars().compact.get(),
            "firstResponderIsCover":window.firstResponder().is_some_and(|r| std::ptr::eq(&*r, &*cover.view as &NSResponder)),
            "button":cover.view.ivars().button.title().to_string(),
            "pendingLabel":cover.view.ivars().pending.stringValue().to_string(),
            "buttonTop": (cover.view.ivars().button.frame().origin.y + cover.view.ivars().button.frame().size.height),
            "pendingBottom": cover.view.ivars().pending.frame().origin.y}))
    }).collect::<Vec<_>>());
    json!({"awaySince":away_since,"candidateLeave":candidate_leave,"blocked":blocked,"shouldPrompt":should_prompt,"status":state,"locked":locked,"verifying":verifying,"authCalls":calls,"executed":executed,"windows":windows,
        "persistedPolicy": std::fs::read(&machine(app).path).ok().and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok()),
        "pending":machine(app).pending.lock().unwrap().is_some(),
        "mainTitle": app.get_window(crate::MAIN_WINDOW).unwrap().title().unwrap(),
        "floatingVisible": ([crate::pet::PET_WINDOW, crate::update_popover::UPDATE_POPOVER].map(|label| app.get_window(label).unwrap().is_visible().unwrap())),
        "quitAlertVisible": PRESENTATION.with_borrow(|p| p.quit_window.as_ref().and_then(Weak::load).is_some_and(|w| w.isVisible() && !p.covers.iter().any(|c| c.window.load().is_some_and(|covered| std::ptr::eq(&*covered, &*w)))))})
}
fn handle(app: &AppHandle, request: Value) {
    let id = request["id"].clone();
    let op = request["op"].as_str().unwrap_or_default();
    let result: Result<Value, String> = (|| {
        match op {
            "status" => {
                refresh(app);
                sync_windows(app);
                Ok(serde_json::to_value(status(app)).unwrap())
            }
            "snapshot" => Ok(snapshot(app)),
            "appearance" => {
                let name = unsafe {
                    if request["dark"].as_bool().unwrap_or(false) {
                        NSAppearanceNameDarkAqua
                    } else {
                        NSAppearanceNameAqua
                    }
                };
                let appearance = NSAppearance::appearanceNamed(name);
                for window in NSApplication::sharedApplication(MainThreadMarker::new().unwrap())
                    .windows()
                    .iter()
                {
                    if !system_chrome(&window) {
                        window.setAppearance(appearance.as_deref());
                    }
                }
                sync_windows(app);
                Ok(snapshot(app))
            }
            "capture" => {
                let path = request["path"].as_str().ok_or("capture path required")?;
                let view = PRESENTATION
                    .with_borrow(|p| {
                        p.covers
                            .iter()
                            .find(|c| !c.view.ivars().compact.get())
                            .map(|c| c.view.clone())
                    })
                    .ok_or("no main cover")?;
                let bitmap = view
                    .bitmapImageRepForCachingDisplayInRect(view.bounds())
                    .ok_or("native capture unavailable")?;
                view.cacheDisplayInRect_toBitmapImageRep(view.bounds(), &bitmap);
                let png = unsafe {
                    bitmap.representationUsingType_properties(
                        NSBitmapImageFileType::PNG,
                        &objc2_foundation::NSDictionary::new(),
                    )
                }
                .ok_or("native PNG unavailable")?;
                if !png.writeToFile_atomically(&NSString::from_str(path), true) {
                    return Err("native PNG write failed".into());
                }
                Ok(json!(true))
            }
            "setPolicy" => {
                let policy: Policy = serde_json::from_value(request["policy"].clone()).map_err(|e| e.to_string())?;
                let (send, receive) = tokio::sync::oneshot::channel();
                change_policy(app, policy, send);
                let id = id.clone();
                tauri::async_runtime::spawn(async move {
                    output(id, receive.await.map_err(|e| e.to_string()));
                });
                Ok(Value::Null)
            }
            "reset" => {
                // Restore any preceding test's covers before replacing its machine.
                {
                    machine(app).machine.lock().unwrap().locked = false;
                    machine(app).machine.lock().unwrap().policy = Policy::Off;
                }
                sync_windows(app);
                let policy = serde_json::from_value(request.get("policy").cloned().unwrap_or(json!(Policy::default())))
                    .map_err(|e| e.to_string())?;
                let available = request
                    .get("availability")
                    .cloned()
                    .map(serde_json::from_value)
                    .transpose()
                    .map_err(|e| e.to_string())?
                    .unwrap_or(Availability {
                        available: true,
                        biometry: Biometry::TouchId,
                        biometry_state: BiometryState::Usable,
                    });
                PEER.with_borrow_mut(|p| {
                    let p = p.as_mut().unwrap();
                    p.time = 0.0;
                    p.active = true;
                    p.recovery_facts = true;
                    p.availability = available;
                    p.outcome = Some(Outcome::Success);
                    p.pending = None;
                    p.calls = 0;
                    p.executed.clear();
                });
                *machine(app).machine.lock().unwrap() = Machine::new(policy, available);
                *machine(app).pending.lock().unwrap() = None;
                crate::notifications::macos::e2e_notices(true);
                app.state::<crate::notifications::Notifications>()
                    .set_enabled(false)
                    .map_err(|e| e.to_string())?;
                sync_windows(app);
                Ok(snapshot(app))
            }
            "availability" => {
                let available = serde_json::from_value(request["value"].clone()).map_err(|e| e.to_string())?;
                PEER.with_borrow_mut(|p| p.as_mut().unwrap().availability = available);
                event(app, Event::Arrive);
                Ok(snapshot(app))
            }
            "outcome" => {
                let outcome = request
                    .get("value")
                    .filter(|v| !v.is_null())
                    .cloned()
                    .map(serde_json::from_value)
                    .transpose()
                    .map_err(|e| e.to_string())?;
                PEER.with_borrow_mut(|p| p.as_mut().unwrap().outcome = outcome);
                finish_if_ready(app);
                Ok(snapshot(app))
            }
            "recovery" => {
                PEER.with_borrow_mut(|p| {
                    p.as_mut().unwrap().recovery_facts = request["valid"].as_bool().unwrap_or(false)
                });
                Ok(snapshot(app))
            }
            "capability" => {
                let error = request["error"].as_i64().map(|n| n as isize);
                PEER.with_borrow_mut(|p| {
                    let p = p.as_mut().unwrap();
                    p.availability.available = password_available(error, Some(p.availability.available));
                });
                refresh(app);
                sync_windows(app);
                Ok(snapshot(app))
            }
            "application" => {
                let regular = request["regular"].as_bool().unwrap_or(false);
                let bundle = request["bundle"].as_str();
                if is_other_regular_application(2, bundle, regular, 1, Some("io.avibe.test")) {
                    event(app, Event::OtherApplication);
                }
                Ok(snapshot(app))
            }
            "leave" | "arrive" | "tick" | "sleep" | "wake" | "screenLock" | "screenUnlock" | "sessionLeave"
            | "sessionReturn" | "displaySleep" | "displayWake" => {
                let time = request["time"].as_f64().ok_or("time required")?;
                PEER.with_borrow_mut(|p| {
                    let p = p.as_mut().unwrap();
                    p.time = time;
                    if matches!(op, "leave" | "sleep" | "screenLock" | "sessionLeave" | "displaySleep") {
                        p.active = false;
                    }
                    if op == "arrive" {
                        p.active = true;
                    }
                });
                match op {
                    "leave" | "arrive" => unsafe {
                        PEER.with_borrow(|p| p.as_ref().unwrap().centers[0].clone())
                            .postNotificationName_object(
                                if op == "leave" {
                                    NSApplicationDidResignActiveNotification
                                } else {
                                    NSApplicationDidBecomeActiveNotification
                                },
                                None,
                            );
                    },
                    "sleep" | "wake" | "sessionLeave" | "sessionReturn" | "displaySleep" | "displayWake" => unsafe {
                        let name = match op {
                            "sleep" => NSWorkspaceWillSleepNotification,
                            "wake" => NSWorkspaceDidWakeNotification,
                            "sessionLeave" => NSWorkspaceSessionDidResignActiveNotification,
                            "sessionReturn" => NSWorkspaceSessionDidBecomeActiveNotification,
                            "displaySleep" => NSWorkspaceScreensDidSleepNotification,
                            _ => NSWorkspaceScreensDidWakeNotification,
                        };
                        PEER.with_borrow(|p| p.as_ref().unwrap().centers[1].clone())
                            .postNotificationName_object(name, None);
                    },
                    "screenLock" | "screenUnlock" => {
                        // Test-owned center runs the production distributed-name
                        // registration without broadcasting to the user's session.
                        unsafe {
                            PEER.with_borrow(|p| p.as_ref().unwrap().centers[2].clone())
                                .postNotificationName_object(
                                    &NSString::from_str(if op == "screenLock" {
                                        "com.apple.screenIsLocked"
                                    } else {
                                        "com.apple.screenIsUnlocked"
                                    }),
                                    None,
                                );
                        }
                    }
                    _ => {
                        machine(app).machine.lock().unwrap().tick(time);
                        finish_completion(app);
                        sync_windows(app);
                    }
                }
                Ok(snapshot(app))
            }
            "unlock" => {
                let button = PRESENTATION
                    .with_borrow(|p| p.covers.first().map(|c| c.view.ivars().button.clone()))
                    .ok_or("no native lock button")?;
                // Use the real native target/action installed on the lock view.
                unsafe {
                    button.performClick(None);
                }
                Ok(snapshot(app))
            }
            "composePage" => {
                if request["reload"].as_bool() == Some(true) {
                    PEER.with_borrow_mut(|p| p.as_mut().unwrap().compose_loaded = false);
                    app.get_webview(crate::MAIN_WINDOW)
                        .ok_or("compose fixture required")?
                        .reload()
                        .map_err(|e| e.to_string())?;
                    return Ok(snapshot(app));
                }
                let window = app.get_window(crate::MAIN_WINDOW).unwrap();
                if app.get_webview(crate::MAIN_WINDOW).is_none() {
                    let directory = machine(app).path.parent().unwrap().join("webview");
                    window
                        .add_child(
                            tauri::webview::WebviewBuilder::new(
                                crate::MAIN_WINDOW,
                                tauri::WebviewUrl::External(
                                    "http://127.0.0.1:5267/e2e/desktop-client/app-lock-compose.html"
                                        .parse()
                                        .unwrap(),
                                ),
                            )
                            .initialization_script(super::super::VOICE_MARKER)
                            .data_directory(directory)
                            .incognito(true)
                            .on_page_load(|_, payload| {
                                if matches!(payload.event(), tauri::webview::PageLoadEvent::Finished) {
                                    PEER.with_borrow_mut(|p| p.as_mut().unwrap().compose_loaded = true);
                                }
                            }),
                            tauri::LogicalPosition::new(0.0, 0.0),
                            tauri::LogicalSize::new(500.0, 400.0),
                        )
                        .map_err(|e| e.to_string())?;
                }
                Ok(snapshot(app))
            }
            "composeRead" => {
                // Wry queues early scripts without their callback until the
                // first document loads. Wait for the real page-load event.
                if !PEER.with_borrow(|p| p.as_ref().unwrap().compose_loaded) {
                    output(id.clone(), Ok(Value::Null));
                    return Ok(Value::Null);
                }
                let view = app
                    .get_webview(crate::MAIN_WINDOW)
                    .ok_or("compose fixture not loaded")?;
                let id = id.clone();
                let pause = request["paused"]
                    .as_bool()
                    .map(|paused| format!("window.__APP_LOCK_COMPOSE_PAUSED__ = {paused};"))
                    .unwrap_or_default();
                view.eval_with_callback(
                    format!("{pause} JSON.stringify(window.__APP_LOCK_COMPOSE_OBSERVED__ ?? null)"),
                    move |raw| {
                        let parsed = serde_json::from_str::<String>(&raw)
                            .ok()
                            .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
                            .unwrap_or(Value::Null);
                        output(id.clone(), Ok(parsed));
                    },
                )
                .map_err(|e| e.to_string())?;
                Ok(Value::Null)
            }
            "notificationAnswer" => {
                // Enable only this consumer, whose final UN sink is substituted.
                // Other peer cases keep the unbundled plugin fallback disabled.
                app.state::<crate::notifications::Notifications>()
                    .set_enabled(true)
                    .map_err(|e| e.to_string())?;
                let id = crate::notifications::e2e_answer(
                    app,
                    request["question"].clone(),
                    request["action"].as_str().ok_or("action required")?,
                    request["text"].as_str().map(str::to_owned),
                    request["opens"].as_str().map(str::to_owned),
                )?;
                Ok(json!({"operationId": id, "after": snapshot(app)}))
            }
            "notificationNotices" => Ok(crate::notifications::macos::e2e_notices(false)),
            "showPresentation" => {
                let window = app
                    .get_webview_window("app-show-lock-fixture")
                    .ok_or("Show fixture required")?;
                Ok(json!({"focused":window.is_focused().map_err(|e| e.to_string())?,
                    "visible":window.is_visible().map_err(|e| e.to_string())?,
                    "minimized":window.is_minimized().map_err(|e| e.to_string())?}))
            }
            "composeQueue" => Ok(crate::compose::e2e_queue_state(app)),
            "appWindow" => {
                // Exercise the physical W1 boundary (real WebviewWindow,
                // native marker, Show document-title follower and accessory)
                // without inventing a Runtime receipt or launching a Runtime.
                let label = request["label"].as_str().ok_or("label required")?;
                if !matches!(label, "app-editor-lock-fixture" | "app-show-lock-fixture") {
                    return Err("fixture window label required".into());
                }
                let builder = tauri::WebviewWindowBuilder::new(
                    app,
                    label,
                    tauri::WebviewUrl::External(
                        "http://127.0.0.1:5267/e2e/desktop-client/app-lock-window.html"
                            .parse()
                            .unwrap(),
                    ),
                )
                .title("PRIVATE APP TITLE")
                .inner_size(600.0, 400.0)
                .visible(false)
                .focused(false)
                .incognito(true)
                .data_directory(machine(app).path.parent().unwrap().join(label))
                .on_page_load(|view, payload| {
                    if matches!(payload.event(), tauri::webview::PageLoadEvent::Finished) {
                        PEER.with_borrow_mut(|p| p.as_mut().unwrap().app_pages_loaded.push(view.label().into()));
                    }
                })
                .initialization_script(crate::app_windows::NATIVE_APP_WINDOW_MARKER);
                let window = crate::show_window_title::follow(builder)
                    .build()
                    .map_err(|e| e.to_string())?;
                if label == "app-show-lock-fixture" {
                    // Real native controls and document dispatch. Only the
                    // final Chat/Browser owner effect is recorded, keeping
                    // external browsers and Runtime navigation out of the peer.
                    crate::macos_show_titlebar::install(
                        &window,
                        ["Annotate", "Open Chat", "Share", "Open in Browser"].map(str::to_owned),
                        "http://127.0.0.1:5267/e2e/desktop-client/app-lock-window.html"
                            .parse()
                            .unwrap(),
                        |action| {
                            PEER.with_borrow_mut(|p| p.as_mut().unwrap().executed.push(format!("show:{action:?}")))
                        },
                    );
                }
                window.show().map_err(|e| e.to_string())?;
                Ok(snapshot(app))
            }
            "showPress" => {
                let window = app
                    .get_webview_window("app-show-lock-fixture")
                    .ok_or("Show fixture required")?;
                let tag = request["tag"].as_i64().ok_or("tag required")? as isize;
                let app = app.clone();
                let id = id.clone();
                // performClick runs a tracking loop. Calling it inside tao's
                // user callback can reenter redraw while tao holds its mutex.
                crate::macos_terminate::on_main_queue(move || {
                    let result = (|| {
                        let native = unsafe {
                            Retained::retain(window.ns_window().map_err(|e| e.to_string())?.cast::<NSWindow>())
                        }
                        .ok_or("native window required")?;
                        let button = native
                            .titlebarAccessoryViewControllers()
                            .iter()
                            .flat_map(|controller| controller.view().subviews().to_vec())
                            .find_map(|view| view.downcast::<NSButton>().ok().filter(|button| button.tag() == tag))
                            .ok_or("Show button required")?;
                        unsafe { button.performClick(None) };
                        Ok(snapshot(&app))
                    })();
                    output(id, result);
                });
                Ok(Value::Null)
            }
            "showRoute" => {
                let window = app
                    .get_webview_window("app-show-lock-fixture")
                    .ok_or("Show fixture required")?;
                let id = id.clone();
                window
                    .as_ref()
                    .eval_with_callback("history.pushState({}, '', '/fixture-moved'); 'moved'", move |_| {
                        output(id.clone(), Ok(json!(true)))
                    })
                    .map_err(|e| e.to_string())?;
                Ok(Value::Null)
            }
            "showRead" => {
                let window = app
                    .get_webview_window("app-show-lock-fixture")
                    .ok_or("Show fixture required")?;
                let id = id.clone();
                window
                    .as_ref()
                    .eval_with_callback("JSON.stringify(window.__APP_LOCK_SHOW_EVENTS__)", move |raw| {
                        let value = serde_json::from_str::<String>(&raw)
                            .ok()
                            .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
                            .unwrap_or(Value::Null);
                        output(id.clone(), Ok(value));
                    })
                    .map_err(|e| e.to_string())?;
                Ok(Value::Null)
            }
            "appWindowTitles" => Ok(json!(["app-editor-lock-fixture", "app-show-lock-fixture"]
                .map(|label| { app.get_webview_window(label).map(|window| window.title().unwrap()) }))),
            "appWindowLoads" => Ok(PEER.with_borrow(|p| json!(p.as_ref().unwrap().app_pages_loaded))),
            "closeAppWindows" => {
                for label in ["app-editor-lock-fixture", "app-show-lock-fixture"] {
                    if let Some(window) = app.get_webview_window(label) {
                        window.destroy().map_err(|e| e.to_string())?;
                    }
                }
                PEER.with_borrow_mut(|p| p.as_mut().unwrap().app_pages_loaded.clear());
                Ok(snapshot(app))
            }
            "closeGuard" => {
                let window = app
                    .get_webview_window("app-editor-lock-fixture")
                    .ok_or("fixture window required")?;
                let handle = app.clone();
                let id = id.clone();
                tauri::async_runtime::spawn(async move {
                    let confirmed = crate::app_windows::confirm_discard(&handle, &window).await;
                    let reply_app = handle.clone();
                    let _ = handle.run_on_main_thread(move || {
                        output(id, Ok(json!({"confirmed": confirmed, "after": snapshot(&reply_app)})))
                    });
                });
                Ok(Value::Null)
            }
            "mainPresentation" => {
                let window = app.get_window(crate::MAIN_WINDOW).unwrap();
                match request["value"].as_str() {
                    Some("hidden") => window.hide().map_err(|e| e.to_string())?,
                    Some("minimized") => {
                        window.show().map_err(|e| e.to_string())?;
                        window.minimize().map_err(|e| e.to_string())?;
                    }
                    Some("shown") => {
                        window.unminimize().map_err(|e| e.to_string())?;
                        window.show().map_err(|e| e.to_string())?;
                    }
                    _ => {}
                }
                Ok(json!({"visible": window.is_visible().map_err(|e| e.to_string())?,
                    "minimized": window.is_minimized().map_err(|e| e.to_string())?}))
            }
            "sheetRead" => Ok(PRESENTATION.with_borrow(|p| {
                p.quit_alert.as_ref().map_or(Value::Null, |alert| {
                    let parent = alert.window().sheetParent();
                    let parent_label = app.windows().into_iter().find_map(|(label, window)| {
                        let native = window.ns_window().ok()?;
                        parent
                            .as_ref()
                            .filter(|parent| std::ptr::eq(parent.as_ref(), native.cast::<NSWindow>()))?;
                        Some(label)
                    });
                    json!({"parent": parent_label, "message": alert.informativeText().to_string(),
                        "title": alert.messageText().to_string(),
                        "buttons": alert.buttons().iter().map(|button| button.title().to_string()).collect::<Vec<_>>()})
                })
            })),
            "sheetAnswer" => {
                let sheet = PRESENTATION
                    .with_borrow(|p| p.quit_window.as_ref().and_then(Weak::load))
                    .ok_or("no sheet")?;
                sheet.sheetParent().ok_or("no sheet parent")?.endSheet_returnCode(
                    &sheet,
                    request["response"].as_i64().ok_or("response required")? as isize,
                );
                Ok(snapshot(app))
            }
            "ask" => {
                use avibe_runtime_host::selection::Input;
                let input = match request["kind"].as_str() {
                    Some("file") => Input::Files(vec![machine(app).path.parent().unwrap().join("ask-fixture.txt")]),
                    Some("url") => Input::Url(request["value"].as_str().ok_or("URL required")?.into()),
                    _ => Input::Text(request["value"].as_str().ok_or("text required")?.into()),
                };
                crate::compose::enqueue(app, input);
                Ok(snapshot(app))
            }
            "askFile" => {
                let path = machine(app).path.parent().unwrap().join("ask-fixture.txt");
                if let Some(text) = request["value"].as_str() {
                    std::fs::write(path, text).map_err(|e| e.to_string())?;
                } else if path.exists() {
                    std::fs::remove_file(path).map_err(|e| e.to_string())?;
                }
                Ok(snapshot(app))
            }
            "directMenu" => {
                crate::direct::shell::app_lock_menu::run(app, request["click"].as_str()).map_err(|e| e.to_string())
            }
            "shortcutCycle" => crate::pet::e2e_shortcut_cycle(
                app,
                request["kind"].as_str().ok_or("shortcut kind required")?,
                request["step"].as_str().ok_or("shortcut step required")?,
            ),
            "source" => {
                match request["value"].as_str().ok_or("source required")? {
                    "dock" => crate::focus_or_restore_main_window(app),
                    "tray" => crate::handle_menu(app, crate::OPEN_MENU_ID),
                    "settings" => crate::handle_menu(app, crate::SETTINGS_MENU_ID),
                    "signOut" => crate::handle_menu(app, "desktop-client-sign-out"),
                    "instances" => crate::handle_menu(app, "desktop-client-instances"),
                    "link" => crate::receive_native_deep_link(app, ["avibe://session/native-fixture"]),
                    "secondLaunch" => {
                        let arguments: Vec<String> =
                            serde_json::from_value(request["arguments"].clone()).map_err(|e| e.to_string())?;
                        crate::receive_native_deep_link(app, arguments);
                    }
                    "documentFinished" => crate::apply_pending_deep_link_on_main(app),
                    "shortcutPress" | "shortcutRelease" => {
                        use tauri_plugin_global_shortcut::{Shortcut, ShortcutState};
                        let shortcut: Shortcut = "Alt+Space".parse().unwrap();
                        crate::pet::handle_shortcut(
                            app,
                            &shortcut,
                            if request["value"] == "shortcutPress" {
                                ShortcutState::Pressed
                            } else {
                                ShortcutState::Released
                            },
                        );
                    }
                    _ => return Err("unknown source".into()),
                }
                Ok(snapshot(app))
            }
            "entry" => {
                let value = request["value"].as_str().ok_or("entry required")?.to_owned();
                let deferred = value.clone();
                if !queue_kind(
                    app,
                    match request["kind"].as_str() {
                        Some("navigation") => EntryKind::Navigation,
                        Some("action") => EntryKind::Action,
                        _ => EntryKind::Content,
                    },
                    move |_| PEER.with_borrow_mut(|p| p.as_mut().unwrap().executed.push(deferred)),
                ) {
                    PEER.with_borrow_mut(|p| p.as_mut().unwrap().executed.push(value));
                }
                sync_windows(app);
                Ok(snapshot(app))
            }
            "newWindow" => {
                native_window(app, None);
                Ok(snapshot(app))
            }
            "newContent" => {
                let window = app.get_window(crate::MAIN_WINDOW).unwrap();
                let window = unsafe { &*window.ns_window().unwrap().cast::<NSWindow>() };
                let label = NSTextField::labelWithString(
                    &NSString::from_str("LATE PRIVATE CONTENT"),
                    MainThreadMarker::new().unwrap(),
                );
                window.contentView().unwrap().addSubview(&label);
                Ok(snapshot(app))
            }
            "decode" => {
                // Every payload comes directly from APP_LOCK_FIXTURES in the
                // shared TypeScript contract, serialized by the browser test.
                let payload = &request["value"];
                let status: Status = serde_json::from_value(payload.get("status").unwrap_or(payload).clone())
                    .map_err(|e| e.to_string())?;
                Ok(serde_json::to_value(status).unwrap())
            }
            "floating" => {
                let window = app.get_window(crate::pet::PET_WINDOW).unwrap();
                if request["visible"].as_bool().ok_or("visibility required")? {
                    window.show().map_err(|e| e.to_string())?;
                } else {
                    window.hide().map_err(|e| e.to_string())?;
                }
                Ok(snapshot(app))
            }
            "title" => {
                app.get_window(crate::MAIN_WINDOW)
                    .unwrap()
                    .set_title(request["value"].as_str().ok_or("title required")?)
                    .map_err(|e| e.to_string())?;
                Ok(snapshot(app))
            }
            "chrome" => {
                let handle = app.clone();
                let id = id.clone();
                crate::macos_terminate::on_main_queue(move || {
                    let mtm = MainThreadMarker::new().unwrap();
                    let bar = NSStatusBar::systemStatusBar();
                    let item = bar.statusItemWithLength(NSVariableStatusItemLength);
                    let button = item.button(mtm).unwrap();
                    button.setTitle(&NSString::from_str("Lock test"));
                    sync_windows(&handle);
                    let tray = button.window().unwrap();
                    let tray_visible = !button.isHiddenOrHasHiddenAncestor() && system_chrome(&tray);
                    let menu = NSMenu::new(mtm);
                    unsafe {
                        menu.addItemWithTitle_action_keyEquivalent(
                            &NSString::from_str("Quit fixture"),
                            None,
                            &NSString::from_str(""),
                        );
                    }
                    let observed = std::rc::Rc::new(RefCell::new(Value::Null));
                    let probe = observed.clone();
                    let cancel = menu.clone();
                    let block = RcBlock::new(move |_: NonNull<objc2_foundation::NSTimer>| {
                        sync_windows(&handle);
                        let windows = NSApplication::sharedApplication(mtm).windows();
                        let chrome = windows
                            .iter()
                            .filter(|w| system_chrome(w))
                            .map(|window| {
                                let covered = PRESENTATION.with_borrow(|p| {
                                    p.covers
                                        .iter()
                                        .any(|c| c.window.load().is_some_and(|w| std::ptr::eq(&*w, &*window)))
                                });
                                json!({"class": window.class().name().to_string_lossy(), "covered": covered})
                            })
                            .collect::<Vec<_>>();
                        *probe.borrow_mut() = json!({"trayVisible": tray_visible, "chrome": chrome});
                        cancel.cancelTrackingWithoutAnimation();
                    });
                    let timer =
                        unsafe { objc2_foundation::NSTimer::timerWithTimeInterval_repeats_block(0.1, false, &block) };
                    unsafe {
                        objc2_foundation::NSRunLoop::mainRunLoop().addTimer_forMode(&timer, NSEventTrackingRunLoopMode);
                    }
                    menu.popUpMenuPositioningItem_atLocation_inView(None, NSPoint::new(0.0, 0.0), Some(&button));
                    bar.removeStatusItem(&item);
                    output(id, Ok(observed.borrow().clone()));
                });
                Ok(Value::Null)
            }
            "quitAlert" => {
                let observed = std::sync::Arc::new(Mutex::new(Value::Null));
                let probe = observed.clone();
                let handle = app.clone();
                let id = id.clone();
                let busy = request["busy"].as_bool().unwrap_or(false);
                let response = request["response"].as_i64().unwrap_or(1002) as isize;
                if busy {
                    show_lifecycle_busy(app, &crate::native_tray_catalog());
                } else {
                    show_quit_question(app, &crate::native_tray_catalog(), move |choice| {
                        PEER.with_borrow_mut(|p| p.as_mut().unwrap().executed.push(format!("quit:{choice:?}")));
                        output(
                            id,
                            Ok(json!({"during": observed.lock().unwrap().clone(), "after": snapshot(&handle)})),
                        );
                    });
                }
                let id = request["id"].clone();
                // A second lifecycle request cannot replace the exact exempt
                // window while the first asynchronous sheet is still visible.
                show_lifecycle_busy(app, &crate::native_tray_catalog());
                // Queued behind sheet creation on the same serial executor as
                // logout's replyToApplicationShouldTerminate. A synchronous
                // modal would prevent this block (and this test) from finishing.
                let handle = app.clone();
                crate::macos_terminate::on_main_queue(move || {
                    *probe.lock().unwrap() = snapshot(&handle);
                    let sheet = PRESENTATION
                        .with_borrow(|p| p.quit_window.as_ref().and_then(Weak::load))
                        .unwrap();
                    let parent = sheet.sheetParent().unwrap();
                    parent.endSheet_returnCode(&sheet, response);
                    if busy {
                        let during = probe.lock().unwrap().clone();
                        crate::macos_terminate::on_main_queue(move || {
                            output(id, Ok(json!({"during":during,"after":snapshot(&handle)})))
                        });
                    }
                });
                Ok(Value::Null)
            }

            "quit" => {
                app.exit(0);
                Ok(json!(true))
            }
            _ => Err(format!("unknown fixture operation: {op}")),
        }
    })();
    if !matches!(
        op,
        "setPolicy" | "quitAlert" | "chrome" | "composeRead" | "closeGuard" | "showRead" | "showRoute" | "showPress"
    ) || result.is_err()
    {
        output(id, result);
    }
}
pub(crate) fn run() {
    let directory = tempfile::tempdir().expect("isolated App Lock store");
    let path = directory.path().join("app-lock.json");
    let available = Availability {
        available: true,
        biometry: Biometry::TouchId,
        biometry_state: BiometryState::Usable,
    };
    PEER.with_borrow_mut(|p| {
        *p = Some(Peer {
            time: 0.0,
            active: true,
            recovery_facts: true,
            availability: available,
            outcome: Some(Outcome::Success),
            pending: None,
            calls: 0,
            compose_loaded: false,
            app_pages_loaded: Vec::new(),
            executed: Vec::new(),
            centers: std::array::from_fn(|_| NSNotificationCenter::new()),
            native_windows: Vec::new(),
        })
    });
    let app = tauri::Builder::default()
        .on_page_load(|view, payload| {
            if payload.event() == tauri::webview::PageLoadEvent::Finished {
                voice_document_ready(view);
            }
        })
        .setup(move |app| {
            app.manage(crate::compose::Queue::default());
            app.manage(crate::pet::Pet::new(path.with_file_name("pet.json")));
            crate::notifications::macos::e2e_notices(true);
            let notifications = crate::notifications::Notifications::new(path.with_file_name("notifications.json"));
            notifications
                .set_enabled(false)
                .expect("isolated notification preference");
            app.manage(notifications);
            let inert = std::sync::Arc::new(NoRuntime);
            app.manage(crate::Shell::new(
                avibe_runtime_host::RuntimeHost::new(inert.clone(), inert, Default::default()),
                avibe_runtime_host::BootstrapLog::disabled(),
            ));
            app.manage(Owner {
                machine: Mutex::new(Machine::new(load_policy(&path), available)),
                pending: Mutex::new(None),
                path: path.clone(),
                dirty: std::sync::atomic::AtomicBool::new(false),
                voice: Mutex::new((0, false)),
            });
            PRESENTATION.with_borrow_mut(|p| p.app = Some(app.handle().clone()));
            install_window_hooks();
            let centers = PEER.with_borrow(|p| p.as_ref().unwrap().centers.clone());
            install_observers(app.handle(), &centers[0], &centers[1], &centers[2]);
            for label in [
                crate::MAIN_WINDOW,
                "show-fixture",
                crate::pet::PET_WINDOW,
                crate::update_popover::UPDATE_POPOVER,
            ] {
                native_window(app.handle(), Some(label));
            }
            native_window(app.handle(), None);
            Ok(())
        })
        .build(crate::desktop_context())
        .expect("isolated native peer");
    let handle = app.handle().clone();
    std::thread::spawn(move || {
        for line in std::io::stdin().lock().lines() {
            let Ok(line) = line else { break };
            match serde_json::from_str::<Value>(&line) {
                Ok(request) => {
                    let app = handle.clone();
                    if handle.run_on_main_thread(move || self::handle(&app, request)).is_err() {
                        break;
                    }
                }
                Err(error) => output(Value::Null, Err(error.to_string())),
            }
        }
        handle.exit(0);
    });
    app.run(|_, event| {
        if matches!(event, tauri::RunEvent::Ready) {
            output(json!(0), Ok(json!({"ready":true})));
        }
    });
    drop(directory);
}
