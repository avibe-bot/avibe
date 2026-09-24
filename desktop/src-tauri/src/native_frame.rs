use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use avibe_runtime_host::window_frame::{clamp_window_frame, MonitorArea, WindowFrame};
use tauri::plugin::{Builder, TauriPlugin};
use tauri::{Manager, PhysicalPosition, PhysicalSize, Runtime, Window, WindowEvent};
use tauri_plugin_window_state::{AppHandleExt, StateFlags};

use crate::MAIN_WINDOW;

pub fn state_flags() -> StateFlags {
    StateFlags::POSITION | StateFlags::SIZE | StateFlags::MAXIMIZED
}

pub fn clamp<R: Runtime>(window: &Window<R>) -> tauri::Result<()> {
    if window.is_maximized()? || window.is_fullscreen()? || window.is_minimized()? {
        return Ok(());
    }
    let position = window.outer_position()?;
    let size = window.inner_size()?;
    let frame = WindowFrame {
        x: position.x,
        y: position.y,
        width: size.width,
        height: size.height,
    };
    let monitors: Vec<_> = window
        .available_monitors()?
        .into_iter()
        .map(|monitor| {
            let area = monitor.work_area();
            MonitorArea {
                frame: WindowFrame {
                    x: area.position.x,
                    y: area.position.y,
                    width: area.size.width,
                    height: area.size.height,
                },
                scale_factor: monitor.scale_factor(),
            }
        })
        .collect();
    let Some(config) = window
        .config()
        .app
        .windows
        .iter()
        .find(|config| config.label == MAIN_WINDOW)
    else {
        return Ok(());
    };
    let restored = clamp_window_frame(
        frame,
        &monitors,
        (config.min_width.unwrap_or(0.0), config.min_height.unwrap_or(0.0)),
    );
    if restored.width != frame.width || restored.height != frame.height {
        window.set_size(PhysicalSize::new(restored.width, restored.height))?;
    }
    if restored.x != frame.x || restored.y != frame.y {
        window.set_position(PhysicalPosition::new(restored.x, restored.y))?;
    }
    Ok(())
}

pub fn init<R: Runtime>() -> TauriPlugin<R> {
    Builder::new("native-frame")
        .on_window_ready(|window| {
            if window.label() != MAIN_WINDOW {
                return;
            }
            let _ = clamp(&window);
            let _ = window.show();
            let generation = Arc::new(AtomicU64::new(0));
            let app = window.app_handle().clone();
            window.on_window_event(move |event| {
                if !matches!(event, WindowEvent::Moved(_) | WindowEvent::Resized(_)) {
                    return;
                }
                let observed = generation.fetch_add(1, Ordering::SeqCst) + 1;
                let generation = generation.clone();
                let app = app.clone();
                tauri::async_runtime::spawn(async move {
                    tokio::time::sleep(Duration::from_millis(400)).await;
                    if generation.load(Ordering::SeqCst) != observed {
                        return;
                    }
                    let save_app = app.clone();
                    let _ = app.run_on_main_thread(move || {
                        if generation.load(Ordering::SeqCst) == observed {
                            let _ = save_app.save_window_state(state_flags());
                        }
                    });
                });
            });
        })
        .build()
}
