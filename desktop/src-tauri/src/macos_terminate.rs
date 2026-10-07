//! Routes every quit AppKit carries out through the shell's Runtime lifecycle.
//!
//! The Dock's Quit, AppleScript's `quit app "Avibe"`, an installer, a direct
//! `-[NSApplication terminate:]`, and logout, restart and shutdown all end in
//! `terminate:`. Before it ends the process, AppKit asks the app delegate's
//! `applicationShouldTerminate:`. tao's delegate has no such method (through
//! tao 0.37; tauri-apps/tauri#9198), so AppKit ended the shell without
//! `ExitRequested`, and this app's Runtime kept running without being asked
//! about. The shell adds the method to tao's delegate class at setup.
//!
//! AppKit attaches no Apple Event to the menus' Quit, which runs the lifecycle
//! itself. A quit request from anywhere else is answered by its reason:
//!
//! * A user's quit is cancelled and handed to the lifecycle, which asks the
//!   same question as every Quit the menus offer, and exits only on its answer.
//! * Logout, restart and shutdown cannot wait on a question. Cancelling one
//!   would abort the logout, so the quit is held (`NSTerminateLater`) while the
//!   shell stops its own Runtime, and is always let through afterwards; see
//!   [`crate::stop_for_session_end`].
//!
//! Answers are sent from the main dispatch queue, never from inside a tao
//! callback: a "yes" runs `applicationWillTerminate:`, and tao's handler for it
//! takes the lock tao holds while one of its callbacks runs.

use std::ffi::c_void;
use std::sync::OnceLock;

use objc2::rc::Retained;
use objc2::runtime::{AnyClass, AnyObject, Imp, Sel};
use objc2::{msg_send, sel, MainThreadMarker};
use objc2_app_kit::{NSApplication, NSApplicationTerminateReply};
use objc2_foundation::{NSAppleEventDescriptor, NSAppleEventManager};
use tauri::{AppHandle, Manager};

use crate::Shell;

const CORE_EVENT_CLASS: u32 = u32::from_be_bytes(*b"aevt");
const QUIT_APPLICATION: u32 = u32::from_be_bytes(*b"quit");
/// `kAEQuitReason`. AERegistry.h documents it as a parameter, and loginwindow
/// has sent it as an attribute, so both are read.
const QUIT_REASON: u32 = u32::from_be_bytes(*b"why?");
const TYPE_TYPE: u32 = u32::from_be_bytes(*b"type");
/// `kAELogOut`, `kAEReallyLogOut`, `kAEShowRestartDialog`, `kAERestart`,
/// `kAEShowShutdownDialog` and `kAEShutDown`: the reasons that end the session.
const SESSION_END_REASONS: [[u8; 4]; 6] = [*b"logo", *b"rlgo", *b"rrst", *b"rest", *b"rsdn", *b"shut"];

/// `NSApplicationTerminateReply (*)(id, SEL, NSApplication *)`.
const SHOULD_TERMINATE_TYPES: &std::ffi::CStr = c"Q@:@";

type ShouldTerminate = unsafe extern "C-unwind" fn(&AnyObject, Sel, &AnyObject) -> NSApplicationTerminateReply;

static APP: OnceLock<AppHandle> = OnceLock::new();

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum QuitRequest {
    User,
    SessionEnd,
}

/// What a quit request is, from the Apple Event AppKit is handling while it
/// asks. A quit carrying no session-ending reason, or no event at all, is a
/// user's.
fn quit_request(event: Option<&NSAppleEventDescriptor>) -> QuitRequest {
    let Some(event) = event else {
        return QuitRequest::User;
    };
    // SAFETY: the selectors are NSAppleEventDescriptor's, with these types.
    let (class, id): (u32, u32) = unsafe { (msg_send![event, eventClass], msg_send![event, eventID]) };
    if class != CORE_EVENT_CLASS || id != QUIT_APPLICATION {
        return QuitRequest::User;
    }
    let attribute: Option<Retained<NSAppleEventDescriptor>> =
        unsafe { msg_send![event, attributeDescriptorForKeyword: QUIT_REASON] };
    let reason = attribute
        .or_else(|| unsafe { msg_send![event, paramDescriptorForKeyword: QUIT_REASON] })
        .map(|reason: Retained<NSAppleEventDescriptor>| {
            let kind: u32 = unsafe { msg_send![&*reason, descriptorType] };
            if kind == TYPE_TYPE {
                reason.typeCodeValue()
            } else {
                reason.enumCodeValue()
            }
        });
    match reason {
        Some(reason) if SESSION_END_REASONS.contains(&reason.to_be_bytes()) => QuitRequest::SessionEnd,
        _ => QuitRequest::User,
    }
}

/// Adds `applicationShouldTerminate:` to the class of the delegate tao set on
/// the application. Called once, from setup on the main thread, after the
/// shell's state is managed.
pub fn install(app: &AppHandle) {
    let Some(mtm) = MainThreadMarker::new() else {
        return;
    };
    let Some(delegate) = NSApplication::sharedApplication(mtm).delegate() else {
        eprintln!("the application has no delegate; system quit requests bypass the Runtime lifecycle");
        return;
    };
    let delegate: &AnyObject = delegate.as_ref();
    let class: *const AnyClass = delegate.class();
    if APP.set(app.clone()).is_err() {
        return;
    }
    // SAFETY: `should_terminate` has the signature the selector and the type
    // string declare. `class_addMethod` refuses a class that already answers
    // the selector, leaving that implementation in place.
    let added = unsafe {
        objc2::ffi::class_addMethod(
            class.cast_mut(),
            sel!(applicationShouldTerminate:),
            std::mem::transmute::<ShouldTerminate, Imp>(should_terminate),
            SHOULD_TERMINATE_TYPES.as_ptr(),
        )
    };
    if !added.as_bool() {
        eprintln!("tao's delegate already answers applicationShouldTerminate:; system quit requests keep its answer");
    }
}

unsafe extern "C-unwind" fn should_terminate(
    _delegate: &AnyObject,
    _selector: Sel,
    _sender: &AnyObject,
) -> NSApplicationTerminateReply {
    let Some(app) = APP.get() else {
        return NSApplicationTerminateReply::TerminateNow;
    };
    let Some(shell) = app.try_state::<Shell>() else {
        return NSApplicationTerminateReply::TerminateNow;
    };
    if shell.exit_authorized.load(std::sync::atomic::Ordering::SeqCst) {
        return NSApplicationTerminateReply::TerminateNow;
    }
    let event: Option<Retained<NSAppleEventDescriptor>> =
        NSAppleEventManager::sharedAppleEventManager().currentAppleEvent();
    match quit_request(event.as_deref()) {
        QuitRequest::User => {
            let app = app.clone();
            on_main_queue(move || crate::request_runtime_lifecycle(app, true));
            NSApplicationTerminateReply::TerminateCancel
        }
        QuitRequest::SessionEnd => match crate::stop_for_session_end(app) {
            Some(stop) => {
                tauri::async_runtime::spawn(async move {
                    stop.await;
                    on_main_queue(|| {
                        if let Some(mtm) = MainThreadMarker::new() {
                            NSApplication::sharedApplication(mtm).replyToApplicationShouldTerminate(true);
                        }
                    });
                });
                NSApplicationTerminateReply::TerminateLater
            }
            None => NSApplicationTerminateReply::TerminateNow,
        },
    }
}

#[repr(C)]
struct DispatchQueue {
    _opaque: [u8; 0],
}

extern "C" {
    /// The queue `dispatch_get_main_queue()` returns.
    static _dispatch_main_q: DispatchQueue;
    fn dispatch_async_f(queue: *const DispatchQueue, context: *mut c_void, work: extern "C" fn(*mut c_void));
}

/// Runs `work` on the main thread from the main dispatch queue, which AppKit
/// also drains while it waits on a held quit, and outside every tao callback.
fn on_main_queue(work: impl FnOnce() + Send + 'static) {
    extern "C" fn run(context: *mut c_void) {
        // SAFETY: `context` is the box leaked below, handed over exactly once.
        let work = unsafe { Box::from_raw(context.cast::<Box<dyn FnOnce() + Send>>()) };
        work();
    }
    let work: Box<Box<dyn FnOnce() + Send>> = Box::new(Box::new(work));
    // SAFETY: the main queue lives for the process, and `run` takes ownership
    // of the context.
    unsafe { dispatch_async_f(&raw const _dispatch_main_q, Box::into_raw(work).cast(), run) }
}

#[cfg(test)]
mod tests {
    use objc2::ClassType;

    use super::*;

    fn code(raw: &[u8; 4]) -> u32 {
        u32::from_be_bytes(*raw)
    }

    fn apple_event(class: &[u8; 4], id: &[u8; 4]) -> Retained<NSAppleEventDescriptor> {
        unsafe {
            msg_send![
                NSAppleEventDescriptor::class(),
                appleEventWithEventClass: code(class),
                eventID: code(id),
                targetDescriptor: None::<&NSAppleEventDescriptor>,
                returnID: -1_i16,
                transactionID: 0_i32,
            ]
        }
    }

    enum Carried {
        Attribute,
        Parameter,
    }

    fn set_reason(event: &NSAppleEventDescriptor, reason: &NSAppleEventDescriptor, carried: Carried) {
        unsafe {
            match carried {
                Carried::Attribute => {
                    let _: () = msg_send![event, setAttributeDescriptor: reason, forKeyword: QUIT_REASON];
                }
                Carried::Parameter => {
                    let _: () = msg_send![event, setParamDescriptor: reason, forKeyword: QUIT_REASON];
                }
            }
        }
    }

    fn quit_with_reason(
        reason: Retained<NSAppleEventDescriptor>,
        carried: Carried,
    ) -> Retained<NSAppleEventDescriptor> {
        let event = apple_event(b"aevt", b"quit");
        set_reason(&event, &reason, carried);
        event
    }

    /// Logout, restart and shutdown must never be cancelled, which would abort
    /// the session end; every other quit must reach the question instead of
    /// stopping the Runtime unasked. The reason arrives as an enumeration or a
    /// type code, as an attribute or a parameter.
    #[test]
    fn only_a_session_ending_quit_reason_skips_the_question() {
        for reason in [b"logo", b"rlgo", b"rrst", b"rest", b"rsdn", b"shut"] {
            for descriptor in [
                NSAppleEventDescriptor::descriptorWithEnumCode(code(reason)),
                NSAppleEventDescriptor::descriptorWithTypeCode(code(reason)),
            ] {
                for carried in [Carried::Attribute, Carried::Parameter] {
                    let event = quit_with_reason(descriptor.clone(), carried);
                    assert_eq!(quit_request(Some(&event)), QuitRequest::SessionEnd, "{reason:?}");
                }
            }
        }

        // The Dock's Quit and AppleScript's `quit app` carry no reason; a
        // direct `terminate:` carries no event.
        assert_eq!(quit_request(Some(&apple_event(b"aevt", b"quit"))), QuitRequest::User);
        assert_eq!(quit_request(None), QuitRequest::User);
        // `kAEQuitAll` does not end the session.
        let quit_all = quit_with_reason(
            NSAppleEventDescriptor::descriptorWithEnumCode(code(b"quia")),
            Carried::Attribute,
        );
        assert_eq!(quit_request(Some(&quit_all)), QuitRequest::User);
        // A session-ending reason on another event is not a quit request.
        let other = apple_event(b"GURL", b"GURL");
        set_reason(
            &other,
            &NSAppleEventDescriptor::descriptorWithEnumCode(code(b"shut")),
            Carried::Attribute,
        );
        assert_eq!(quit_request(Some(&other)), QuitRequest::User);
    }
}
