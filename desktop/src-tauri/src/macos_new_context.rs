//! Lets WKWebView ask the shell for a new browsing context.
//!
//! WKWebView decides a `target="_blank"` link in two steps. It first asks the
//! navigation delegate whether the navigation may happen, marking a request for
//! a new context with a nil `targetFrame`, and only when that answer is Allow
//! does it ask the UI delegate for a webview — the step Tauri exposes as
//! `on_new_window`. wry hands the shell's navigation handler the URL alone, so
//! that handler cannot tell a new context from a navigation of this webview and
//! cancels every destination outside the Workbench origin. The link then does
//! nothing, although `on_new_window` would have sent it to the system browser.
//! `window.open` skips the first step, which is why it already worked.
//!
//! The shell therefore answers the first step itself for exactly one case: a
//! request for a new context whose destination `on_new_window` would hand to
//! the system browser. It allows that request, so WebKit carries it to
//! `on_new_window`, which opens the browser and never returns a webview.
//! Every other navigation, downloads included, still goes to wry unchanged,
//! so the in-window navigation rules stay where they are.
//!
//! The decision is made on WebKit's own navigation action, after the page has
//! finished handling the click: page scripts, frames, and modifier keys cannot
//! hide a link from it, and nothing is injected into the page.

use std::sync::OnceLock;

use objc2::rc::Retained;
use objc2::runtime::{AnyObject, Imp, Sel};
use objc2::sel;
use objc2_foundation::NSObjectProtocol;
use objc2_web_kit::{WKNavigationAction, WKNavigationActionPolicy, WKWebView};
use tauri::WebviewWindow;
use url::Url;

use crate::{new_window_decision, NewWindowDecision};

type DecisionHandler = block2::DynBlock<dyn Fn(WKNavigationActionPolicy)>;
type PolicyImp = unsafe extern "C-unwind" fn(&AnyObject, Sel, &WKWebView, &WKNavigationAction, &DecisionHandler);

/// wry's own answer to the first step, which the shell defers to for
/// everything it does not answer itself.
static WRY_POLICY: OnceLock<PolicyImp> = OnceLock::new();

/// Whether the shell allows a navigation action instead of asking wry.
fn allows_new_context(opens_new_context: bool, should_download: bool, url: Option<&Url>) -> bool {
    opens_new_context
        && !should_download
        && url.is_some_and(|url| new_window_decision(url) == NewWindowDecision::OpenInSystemBrowser)
}

/// Routes the webview's new-context requests to `on_new_window`. wry installs
/// one navigation delegate class for every webview it builds; the shell builds
/// only the main window, and the first call adopts that class for the process.
pub fn install(window: &WebviewWindow) {
    let _ = window.with_webview(|webview| {
        if WRY_POLICY.get().is_some() {
            return;
        }
        // SAFETY: Tauri hands out the live WKWebView, on the main thread, for
        // the duration of this closure.
        let Some(webview) = (unsafe { Retained::retain(webview.inner().cast::<WKWebView>()) }) else {
            return;
        };
        let Some(delegate) = (unsafe { webview.navigationDelegate() }) else {
            return;
        };
        let delegate: &AnyObject = delegate.as_ref();
        let selector = sel!(webView:decidePolicyForNavigationAction:decisionHandler:);
        // WebKit calls the preferences variant instead when a delegate has one,
        // and would then never reach this answer.
        let preferences = sel!(webView:decidePolicyForNavigationAction:preferences:decisionHandler:);
        if delegate.class().instance_method(preferences).is_some() {
            eprintln!("wry's navigation delegate changed shape; new-context links stay in wry's hands");
            return;
        }
        let Some(method) = delegate.class().instance_method(selector) else {
            return;
        };
        // SAFETY: The selector fixes the signature of both implementations.
        let wry_policy: PolicyImp = unsafe { std::mem::transmute::<Imp, PolicyImp>(method.implementation()) };
        if WRY_POLICY.set(wry_policy).is_err() {
            return;
        }
        // SAFETY: `decide_policy` has the signature the selector declares.
        unsafe {
            method.set_implementation(std::mem::transmute::<PolicyImp, Imp>(decide_policy));
        }
    });
}

unsafe extern "C-unwind" fn decide_policy(
    delegate: &AnyObject,
    selector: Sel,
    webview: &WKWebView,
    action: &WKNavigationAction,
    handler: &DecisionHandler,
) {
    // SAFETY: WebKit calls this on the main thread with a live action.
    let (opens_new_context, should_download, url) = unsafe {
        let should_download = action.respondsToSelector(sel!(shouldPerformDownload)) && action.shouldPerformDownload();
        let url = action
            .request()
            .URL()
            .and_then(|url| url.absoluteString())
            .and_then(|url| Url::parse(&url.to_string()).ok());
        (action.targetFrame().is_none(), should_download, url)
    };
    if allows_new_context(opens_new_context, should_download, url.as_ref()) {
        handler.call((WKNavigationActionPolicy::Allow,));
        return;
    }
    let wry_policy = WRY_POLICY.get().expect("installed before it can be called");
    // SAFETY: The arguments are the ones WebKit passed for this selector.
    unsafe { wry_policy(delegate, selector, webview, action, handler) }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn url(raw: &str) -> Url {
        Url::parse(raw).expect("test URL")
    }

    #[test]
    fn only_new_web_contexts_skip_the_in_window_rules() {
        let web = url("https://github.com/avibe-bot/avibe/releases/tag/gh-v3.1.1rc23");
        assert!(allows_new_context(true, false, Some(&web)));
        assert!(allows_new_context(
            true,
            false,
            Some(&url("http://127.0.0.1:5123/show/page"))
        ));

        // A navigation of this webview keeps the shell's in-window rules.
        assert!(!allows_new_context(false, false, Some(&web)));
        // A download keeps wry's download handling.
        assert!(!allows_new_context(true, true, Some(&web)));
        // No destination the system browser can take.
        assert!(!allows_new_context(true, false, None));
        for raw in [
            "about:blank",
            "javascript:alert(1)",
            "blob:http://127.0.0.1:5123/7f1c",
            "file:///etc/hosts",
        ] {
            assert!(!allows_new_context(true, false, Some(&url(raw))), "{raw}");
        }
    }
}
