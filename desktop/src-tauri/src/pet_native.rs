//! The two platform facts the pet's drag needs that Tauri does not expose:
//! whether the primary mouse button is still down, and a window frame change
//! that moves and resizes in one step. Called on the main thread.

use tauri::WebviewWindow;

/// Whether the button holding a drag is still down, or `None` where the
/// shell cannot tell. A native window drag swallows the button's release, so
/// the page never sees it; this is how the shell learns the drag is over.
pub fn primary_button_down() -> Option<bool> {
    // AppKit reports physical buttons, and a Mac mouse can make its right side
    // the primary one: the drag is over once no button is down.
    #[cfg(target_os = "macos")]
    {
        Some(objc2_app_kit::NSEvent::pressedMouseButtons() != 0)
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::UI::Input::KeyboardAndMouse::{GetAsyncKeyState, VK_LBUTTON, VK_RBUTTON};
        use windows_sys::Win32::UI::WindowsAndMessaging::{GetSystemMetrics, SM_SWAPBUTTON};
        // The state of the physical button the user treats as primary.
        // SAFETY: plain Win32 queries with no pointers.
        let button = if unsafe { GetSystemMetrics(SM_SWAPBUTTON) } != 0 {
            VK_RBUTTON
        } else {
            VK_LBUTTON
        };
        Some(unsafe { GetAsyncKeyState(i32::from(button)) } as u16 & 0x8000 != 0)
    }
    #[cfg(not(any(target_os = "macos", windows)))]
    {
        None
    }
}

/// Puts the window at `origin` (its top-left) with `size`, both in the
/// window's logical points, in one step where the platform allows it, so the
/// page is never drawn in a frame that has moved but not yet resized.
pub fn set_frame(window: &WebviewWindow, origin: (f64, f64), size: (f64, f64)) {
    #[cfg(target_os = "macos")]
    if set_frame_macos(window, origin, size) {
        return;
    }
    #[cfg(windows)]
    if set_frame_windows(window, origin, size) {
        return;
    }
    let _ = window.set_size(tauri::LogicalSize::new(size.0, size.1));
    let _ = window.set_position(tauri::LogicalPosition::new(origin.0, origin.1));
}

/// Starts the native window drag from where the pointer is now. On macOS the
/// drag keeps the press's offset measured from the window's bottom edge, so a
/// press that came before a resize would drag the window by the change in
/// height; the shell gives AppKit a press measured in the current frame.
pub fn start_drag(window: &WebviewWindow) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    if start_drag_macos(window) {
        return Ok(());
    }
    window.start_dragging().map_err(|error| error.to_string())
}

#[cfg(target_os = "macos")]
fn start_drag_macos(window: &WebviewWindow) -> bool {
    use objc2::rc::Retained;
    use objc2::runtime::AnyObject;
    use objc2::{msg_send, ClassType, MainThreadMarker};
    use objc2_app_kit::{NSApplication, NSEvent, NSEventModifierFlags, NSEventType, NSWindow};
    use objc2_foundation::NSPoint;

    let (Some(mtm), Ok(pointer)) = (MainThreadMarker::new(), window.ns_window()) else {
        return false;
    };
    // SAFETY: as in `set_frame_macos`.
    let Some(native) = (unsafe { Retained::retain(pointer.cast::<NSWindow>()) }) else {
        return false;
    };
    let mouse = NSEvent::mouseLocation();
    let frame = native.frame();
    let current = NSApplication::sharedApplication(mtm).currentEvent();
    let (flags, timestamp) = current.map_or((NSEventModifierFlags::empty(), 0.0), |event| {
        (event.modifierFlags(), event.timestamp())
    });
    // SAFETY: the documented NSEvent constructor, with a nil graphics context.
    let press: Option<Retained<NSEvent>> = unsafe {
        msg_send![
            NSEvent::class(),
            mouseEventWithType: NSEventType::LeftMouseDown,
            location: NSPoint::new(mouse.x - frame.origin.x, mouse.y - frame.origin.y),
            modifierFlags: flags,
            timestamp: timestamp,
            windowNumber: native.windowNumber(),
            context: std::ptr::null_mut::<AnyObject>(),
            eventNumber: 0isize,
            clickCount: 1isize,
            pressure: 1.0f32
        ]
    };
    let Some(press) = press else {
        return false;
    };
    native.performWindowDragWithEvent(&press);
    true
}

#[cfg(target_os = "macos")]
fn set_frame_macos(window: &WebviewWindow, origin: (f64, f64), size: (f64, f64)) -> bool {
    use objc2::rc::Retained;
    use objc2::MainThreadMarker;
    use objc2_app_kit::NSWindow;
    use objc2_foundation::{NSPoint, NSRect, NSSize};

    let (Some(_mtm), Ok(pointer), Ok(position), Ok(scale)) = (
        MainThreadMarker::new(),
        window.ns_window(),
        window.outer_position(),
        window.scale_factor(),
    ) else {
        return false;
    };
    // SAFETY: Tauri hands out the live NSWindow of this window, on the main
    // thread, which `MainThreadMarker::new()` just confirmed.
    let Some(native) = (unsafe { Retained::retain(pointer.cast::<NSWindow>()) }) else {
        return false;
    };
    // AppKit measures y up from the primary display's bottom; Tauri measures
    // it down from that display's top. The current frame in both gives the
    // primary display's height without asking AppKit for its screens.
    let current = native.frame();
    let top = position.to_logical::<f64>(scale).y;
    let primary_height = current.origin.y + current.size.height + top;
    native.setFrame_display(
        NSRect::new(
            NSPoint::new(origin.0, primary_height - origin.1 - size.1),
            NSSize::new(size.0, size.1),
        ),
        true,
    );
    true
}

#[cfg(windows)]
fn set_frame_windows(window: &WebviewWindow, origin: (f64, f64), size: (f64, f64)) -> bool {
    use windows_sys::Win32::UI::WindowsAndMessaging::{SetWindowPos, SWP_NOACTIVATE, SWP_NOZORDER};

    let (Ok(hwnd), Ok(scale)) = (window.hwnd(), window.scale_factor()) else {
        return false;
    };
    let physical = |value: f64| (value * scale).round() as i32;
    // SAFETY: the handle is this live window's, and SetWindowPos takes no
    // pointers beyond it.
    unsafe {
        SetWindowPos(
            hwnd.0 as _,
            std::ptr::null_mut(),
            physical(origin.0),
            physical(origin.1),
            physical(size.0),
            physical(size.1),
            SWP_NOZORDER | SWP_NOACTIVATE,
        ) != 0
    }
}
