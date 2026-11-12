//! Fixture-only certificate handling. No Keychain changes or public trust bypass.
//! The injected proxy accepts only fixture destinations and the exact generated
//! certificate fingerprint is checked before accepting its server trust.
use objc2::{
    msg_send,
    runtime::{AnyClass, AnyObject, Imp, Sel},
    sel,
};
use objc2_foundation::NSString;
use sha2::{Digest, Sha256};
use std::{
    ffi::{c_char, c_void},
    sync::OnceLock,
};
use tauri::Webview;

type Completion = block2::DynBlock<dyn Fn(isize, *mut AnyObject)>;
#[link(name = "Security", kind = "framework")]
unsafe extern "C" {
    fn SecTrustGetCertificateAtIndex(trust: *const c_void, index: isize) -> *const c_void;
    fn SecCertificateCopyData(cert: *const c_void) -> *const c_void;
}
#[link(name = "CoreFoundation", kind = "framework")]
unsafe extern "C" {
    fn CFDataGetBytePtr(data: *const c_void) -> *const u8;
    fn CFDataGetLength(data: *const c_void) -> isize;
    fn CFRelease(object: *const c_void);
}
#[link(name = "objc")]
unsafe extern "C" {
    fn class_addMethod(class: *const AnyClass, selector: Sel, implementation: Imp, types: *const c_char) -> bool;
}
static FINGERPRINT: OnceLock<String> = OnceLock::new();
static INSTALLED: OnceLock<()> = OnceLock::new();
pub fn install(view: &Webview) {
    FINGERPRINT.get_or_init(|| std::env::var("AVIBE_FIXTURE_TLS_SHA256").expect("fixture certificate fingerprint"));
    view.with_webview(|platform| {
        INSTALLED.get_or_init(|| unsafe {
            let view = &*platform.inner().cast::<AnyObject>();
            let delegate: *mut AnyObject = msg_send![view, navigationDelegate];
            assert!(!delegate.is_null());
            let class = (&*delegate).class();
            let selector = sel!(webView:didReceiveAuthenticationChallenge:completionHandler:);
            assert!(
                class.instance_method(selector).is_none(),
                "fixture must not replace existing auth logic"
            );
            let implementation: unsafe extern "C-unwind" fn(&AnyObject, Sel, &AnyObject, &AnyObject, &Completion) =
                challenge;
            assert!(class_addMethod(
                class,
                selector,
                std::mem::transmute::<
                    unsafe extern "C-unwind" fn(&AnyObject, Sel, &AnyObject, &AnyObject, &Completion),
                    Imp,
                >(implementation),
                c"v@:@@@?".as_ptr()
            ));
        });
    })
    .unwrap();
}
unsafe extern "C-unwind" fn challenge(_: &AnyObject, _: Sel, _: &AnyObject, challenge: &AnyObject, done: &Completion) {
    unsafe {
        let space: *mut AnyObject = msg_send![challenge, protectionSpace];
        let method: *mut NSString = msg_send![space, authenticationMethod];
        if method.is_null() || (*method).to_string() != "NSURLAuthenticationMethodServerTrust" {
            done.call((1, std::ptr::null_mut()));
            return;
        }
        let trust: *const c_void = msg_send![space, serverTrust];
        let cert = SecTrustGetCertificateAtIndex(trust, 0);
        let data = SecCertificateCopyData(cert);
        let bytes = std::slice::from_raw_parts(CFDataGetBytePtr(data), CFDataGetLength(data) as usize);
        let fingerprint = Sha256::digest(bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        CFRelease(data);
        if FINGERPRINT.get() != Some(&fingerprint) {
            done.call((2, std::ptr::null_mut()));
            return;
        }
        let credential: *mut AnyObject = msg_send![objc2::class!(NSURLCredential),credentialForTrust:trust];
        done.call((0, credential));
    }
}
