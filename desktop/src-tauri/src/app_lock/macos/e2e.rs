//! Compiled solely by the explicit app-lock-e2e feature (forbidden in release).
//! Uses the production native owner, presentation and completion handlers. Only
//! time, foreground state and LA are deterministic. Browser tests communicate
//! over stdio; nothing is listening on a socket or exposed to a shipped page.
use super::*;
use serde_json::{json, Value};
use std::io::{BufRead, Write};

struct Peer {
    time: f64,
    active: bool,
    availability: Availability,
    outcome: Option<Outcome>,
    pending: Option<(u64, Authentication)>,
    calls: usize,
    executed: Vec<String>,
    native_windows: Vec<Retained<NSWindow>>,
}
thread_local! { static PEER: RefCell<Option<Peer>> = const { RefCell::new(None) }; }
pub(super) fn time() -> Option<f64> {
    PEER.with_borrow(|p| p.as_ref().map(|p| p.time))
}
pub(super) fn active() -> Option<bool> {
    PEER.with_borrow(|p| p.as_ref().map(|p| p.active))
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
    if let Some((epoch, purpose, outcome)) = finish {
        complete(app, epoch, purpose, outcome);
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
    let (locked, verifying) = {
        let owner = machine(app);
        let state = owner.machine.lock().unwrap();
        (state.locked, state.authenticating.is_some())
    };
    let (calls, executed) = PEER.with_borrow(|p| {
        let p = p.as_ref().unwrap();
        (p.calls, p.executed.clone())
    });
    let windows = PRESENTATION.with_borrow(|p| p.covers.iter().filter_map(|cover| {
        let window = cover.window.load()?;
        Some(json!({"covered": unsafe { cover.view.superview() }.is_some(), "title": window.title().to_string(),
            "hidden":cover.hidden.iter().filter_map(Weak::load).all(|view| view.isHidden()),
            "contentCount":cover.hidden.len(), "compact":cover.view.ivars().compact.get(),
            "firstResponderIsCover":window.firstResponder().is_some_and(|r| std::ptr::eq(&*r, &*cover.view as &NSResponder)),
            "button":cover.view.ivars().button.title().to_string(),
            "pendingLabel":cover.view.ivars().pending.stringValue().to_string()}))
    }).collect::<Vec<_>>());
    json!({"status":state,"locked":locked,"verifying":verifying,"authCalls":calls,"executed":executed,"windows":windows,
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
                let policy = serde_json::from_value(request.get("policy").cloned().unwrap_or(json!("off")))
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
                    p.availability = available;
                    p.outcome = Some(Outcome::Success);
                    p.pending = None;
                    p.calls = 0;
                    p.executed.clear();
                });
                *machine(app).machine.lock().unwrap() = Machine::new(policy, available);
                *machine(app).pending.lock().unwrap() = None;
                PRESENTATION.with_borrow_mut(|p| p.completion = None);
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
            "leave" | "arrive" | "tick" | "sleep" | "wake" => {
                let time = request["time"].as_f64().ok_or("time required")?;
                PEER.with_borrow_mut(|p| {
                    let p = p.as_mut().unwrap();
                    p.time = time;
                    if op == "leave" {
                        p.active = false;
                    }
                    if op == "arrive" {
                        p.active = true;
                    }
                });
                match op {
                    "leave" => event(app, Event::Leave),
                    "arrive" => event(app, Event::Arrive),
                    "sleep" => event(app, Event::SystemLeave(1)),
                    "wake" => event(app, Event::SystemReturn(1)),
                    _ => {
                        machine(app).machine.lock().unwrap().tick(time);
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
                // Tauri's user-event dispatcher intentionally does not reenter
                // during NSAlert.runModal. Probe from the real modal run loop.
                let observed = std::sync::Arc::new(Mutex::new(Value::Null));
                let probe = observed.clone();
                let handle = app.clone();
                let block = RcBlock::new(move |_: NonNull<objc2_foundation::NSTimer>| {
                    *probe.lock().unwrap() = snapshot(&handle);
                    NSApplication::sharedApplication(MainThreadMarker::new().unwrap()).stopModalWithCode(1002);
                    if let Some(window) = PRESENTATION.with_borrow(|p| p.quit_window.as_ref().and_then(Weak::load)) {
                        window.orderOut(None);
                    }
                });
                let timer =
                    unsafe { objc2_foundation::NSTimer::timerWithTimeInterval_repeats_block(0.05, false, &block) };
                unsafe {
                    objc2_foundation::NSRunLoop::mainRunLoop().addTimer_forMode(&timer, NSModalPanelRunLoopMode);
                }
                let handle = app.clone();
                let id = id.clone();
                show_quit_question(app, &crate::native_tray_catalog(), move |choice| {
                    PEER.with_borrow_mut(|p| p.as_mut().unwrap().executed.push(format!("quit:{choice:?}")));
                    output(
                        id,
                        Ok(json!({"during": observed.lock().unwrap().clone(), "after": snapshot(&handle)})),
                    );
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
    if !matches!(op, "setPolicy" | "quitAlert" | "chrome") || result.is_err() {
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
            availability: available,
            outcome: Some(Outcome::Success),
            pending: None,
            calls: 0,
            executed: Vec::new(),
            native_windows: Vec::new(),
        })
    });
    let app = tauri::Builder::default()
        .setup(move |app| {
            app.manage(Owner {
                machine: Mutex::new(Machine::new(Policy::Off, available)),
                pending: Mutex::new(None),
                path: path.clone(),
                dirty: std::sync::atomic::AtomicBool::new(false),
            });
            PRESENTATION.with_borrow_mut(|p| p.app = Some(app.handle().clone()));
            install_window_hooks();
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
