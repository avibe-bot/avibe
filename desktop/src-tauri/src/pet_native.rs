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

/// How the shell drags the window once it has shrunk to the drag strip.
pub enum Drag {
    /// The platform's own window drag runs until the button is released.
    Native,
    /// The shell moves the window itself, keeping this point of the window
    /// under the pointer (`follow_pointer`, on every poll of the button).
    Follow(PointerOffset),
}

/// A point of the window, in its points from its top-left corner, that the
/// drag keeps under the pointer.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct PointerOffset {
    pub x: f64,
    pub y: f64,
}

/// Starts dragging the window from where the pointer is now.
///
/// On macOS the shell moves the window itself rather than handing the drag to
/// AppKit (`performWindowDragWithEvent`): a system window drag takes part in
/// the window manager's edge tiling, so a pointer that reaches a display's
/// side, the Dock or the menu bar has the system move the pet away on release
/// (to a whole-window place, or to the middle of the display) before the shell
/// can place it. Elsewhere the platform drags the window.
///
/// `hold` is the point of the window to keep under the pointer: where the
/// press was, so the window does not jump by however far the pointer moved
/// before the drag reached the shell. Without it, the pointer's place now.
pub fn start_drag(window: &WebviewWindow, hold: Option<PointerOffset>) -> Result<Drag, String> {
    #[cfg(target_os = "macos")]
    if let Some(offset) = hold.or_else(|| pointer_offset_macos(window)) {
        return Ok(Drag::Follow(offset));
    }
    #[cfg(not(target_os = "macos"))]
    let _ = hold;
    window.start_dragging().map_err(|error| error.to_string())?;
    Ok(Drag::Native)
}

/// Moves the window so the pointer keeps `offset` in it. The window's own
/// frame setter, not a system drag: no tiling, and no constraint below the
/// menu bar while dragging (the drop is placed by the shell).
pub fn follow_pointer(window: &WebviewWindow, offset: PointerOffset) {
    #[cfg(target_os = "macos")]
    follow_pointer_macos(window, offset);
    #[cfg(not(target_os = "macos"))]
    let _ = (window, offset);
}

#[cfg(target_os = "macos")]
fn native_window(window: &WebviewWindow) -> Option<objc2::rc::Retained<objc2_app_kit::NSWindow>> {
    let (Some(_mtm), Ok(pointer)) = (objc2::MainThreadMarker::new(), window.ns_window()) else {
        return None;
    };
    // SAFETY: as in `set_frame_macos`.
    unsafe { objc2::rc::Retained::retain(pointer.cast::<objc2_app_kit::NSWindow>()) }
}

#[cfg(target_os = "macos")]
fn pointer_offset_macos(window: &WebviewWindow) -> Option<PointerOffset> {
    let native = native_window(window)?;
    let mouse = objc2_app_kit::NSEvent::mouseLocation();
    let frame = native.frame();
    // AppKit measures y up from the bottom; the offset is from the top.
    Some(PointerOffset {
        x: mouse.x - frame.origin.x,
        y: frame.origin.y + frame.size.height - mouse.y,
    })
}

#[cfg(target_os = "macos")]
fn follow_pointer_macos(window: &WebviewWindow, offset: PointerOffset) {
    let Some(native) = native_window(window) else {
        return;
    };
    let mouse = objc2_app_kit::NSEvent::mouseLocation();
    let height = native.frame().size.height;
    let origin = objc2_foundation::NSPoint::new(mouse.x - offset.x, mouse.y + offset.y - height);
    if native.frame().origin != origin {
        native.setFrameOrigin(origin);
    }
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
