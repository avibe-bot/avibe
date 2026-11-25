//! Native menu consumer with unsaved choices and no Direct worker or network.
use super::*;

pub(crate) fn run(app: &AppHandle, click: Option<&str>) -> tauri::Result<serde_json::Value> {
    if direct(app).is_none() {
        let menu = ConnectionMenu::build(app)?.expect("product menu copy");
        menu.show(Some((Mode::Automatic, false)));
        app.manage(Arc::new(Direct {
            wiring: Mutex::new(Wiring::new(Routing::Unsupported, false, Choices::unsaved())),
            stops: Mutex::default(),
            asks: Mutex::default(),
            returns: Mutex::default(),
            shown: Mutex::new(Some(Published {
                target: Target {
                    account: "fixture-account".into(),
                    origin: "https://fixture.invalid".into(),
                    instance: "fixture-instance".into(),
                },
                runtime: "fixture-runtime".into(),
                document: true,
            })),
            wake: Notify::new(),
            menu: Mutex::new(Some(menu)),
            pages: Mutex::default(),
            available: true,
            dead_end: OnceLock::new(),
        }));
    }
    let state = direct(app).expect("test owner");
    if let Some(id) = click {
        {
            let menu = lock(&state.menu);
            let menu = menu.as_ref().unwrap();
            let item = match id {
                MENU_AUTOMATIC => &menu.automatic,
                MENU_TUNNEL => &menu.tunnel,
                _ => panic!("unknown fixture menu item"),
            };
            // AppKit/muda toggles this real CheckMenuItem before dispatch.
            item.set_checked(!item.is_checked()?)?;
        }
        crate::handle_menu(app, id);
    }
    let saved = chosen(lock(&state.shown).as_ref(), &lock(&state.wiring)).map(|(mode, _)| mode);
    let menu = lock(&state.menu);
    let menu = menu.as_ref().unwrap();
    Ok(serde_json::json!({
        "saved": saved,
        "automatic": menu.automatic.is_checked()?,
        "tunnel": menu.tunnel.is_checked()?,
    }))
}
