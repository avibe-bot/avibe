//! Windows: the rendezvous request made by the instance's own WebView2, the
//! argument that sends only that instance's host to the direct listener, and
//! the answer to the listener's own proxy challenge.
//!
//! Library functions only: which WebView, when, and for which instance is the
//! shell's decision. Everything here runs on the WebView2 apartment. Nothing
//! is logged: a caller that logs keeps to families and counts.

use std::{cell::RefCell, net::SocketAddrV4, rc::Rc};

use base64::{engine::general_purpose::STANDARD, Engine};
use serde_json::{json, Value};
use webview2_com::{
    take_pwstr, BasicAuthenticationRequestedEventHandler, CallDevToolsProtocolMethodCompletedHandler,
    Microsoft::Web::WebView2::Win32::{ICoreWebView2, ICoreWebView2_10},
};
use windows::core::{Interface, HSTRING, PWSTR};

use super::rendezvous::{origin_of, Reply, Request, WORLD};

/// The realm of the listener's `407`. Its challenge is answered only when
/// it is exactly this.
const CHALLENGE: &str = "Basic realm=\"Avibe Direct\"";

/// Asks the instance how to dial it, as a same-origin request of the
/// document `view` shows, and gives `done` the outcome. `done` runs once, on
/// the WebView2 apartment.
///
/// The request runs through the WebView's own DevTools protocol, in an
/// isolated world made on the top frame for this one call. That world shares
/// the document and its cookies with the page, not the page's JavaScript
/// objects: page script can neither replace what the script calls nor see
/// what it reads, and the answer comes back through the protocol's own
/// completion, never through a web message a page could post. The request
/// therefore carries this WebView's signed-in session and meets the
/// instance's usual Origin and CSRF checks.
///
/// `current` is the shell's own word that the selection this request was
/// started for is still the one on show: the same session and the same
/// instance view. It is asked, with the top frame's exact origin, before the
/// request, and again when the answer arrives. A selection that is no longer
/// current answers `SelectionChanged`, whatever the instance said. A
/// document that is not the instance's (the view is loading or reloading in
/// the same selection) answers `NotReady` before the request.
///
/// A ready answer is accepted only if the top frame is still the same
/// document (the same frame and the same loader), which takes one more
/// question to WebView2; otherwise it is dropped. Every other outcome, a
/// refusal by the instance and a selection that changed above all, needs no
/// such look and is delivered at once, so nothing can make it arrive too
/// late.
pub fn fetch_rendezvous(
    view: &ICoreWebView2,
    request: &Request,
    current: impl Fn() -> bool + 'static,
    done: impl FnOnce(Reply) + 'static,
) {
    let origin = request.origin().to_owned();
    // Whatever is delivered, and from whichever path (a failed frame lookup
    // or a world that could not be made included), is delivered for the
    // selection as it is at that moment: `done` asks `current` itself, every
    // time, and a selection that is no longer current gets `SelectionChanged`.
    let current = Rc::new(current);
    let done = {
        let current = current.clone();
        move |reply: Reply| done(reply.for_selection(current()))
    };
    if !current() || !shows(view, &origin) {
        done(Reply::NotReady);
        return;
    }
    let expression = request.expression();
    let first = view.clone();
    top_document(view, origin.clone(), move |document| {
        let Some(document) = document else {
            done(Reply::NotReady);
            return;
        };
        let world = json!({ "frameId": document.frame, "worldName": WORLD, "grantUniveralAccess": false });
        let second = first.clone();
        call(&first, "Page.createIsolatedWorld", &world, move |made| {
            let Some(context) = made.and_then(|made| made.get("executionContextId").and_then(Value::as_i64)) else {
                done(Reply::Failed);
                return;
            };
            let evaluation = json!({
                "expression": expression,
                "contextId": context,
                "awaitPromise": true,
                "returnByValue": true,
            });
            let third = second.clone();
            call(&second, "Runtime.evaluate", &evaluation, move |evaluated| {
                let text = evaluated
                    .as_ref()
                    .filter(|evaluated| evaluated.get("exceptionDetails").is_none())
                    .and_then(|evaluated| evaluated.pointer("/result/value"))
                    .and_then(Value::as_str)
                    .map(str::to_owned);
                let reply = Reply::read(text.as_deref());
                // Only a ready answer depends on which document gave it,
                // and only while the selection is still current: everything
                // else is delivered at once. A selection that changed is
                // never made to wait for another question to WebView2.
                if !matches!(reply, Reply::Ready(_)) || !current() {
                    done(reply);
                    return;
                }
                // A navigation may have committed while the request ran:
                // its answer is then not this document's to give.
                let fourth = third.clone();
                top_document(&third, origin.clone(), move |after| {
                    let same = after.is_some_and(|after| after == document) && shows(&fourth, &origin);
                    done(if same { reply } else { Reply::Failed });
                });
            });
        });
    });
}

/// The top frame's document: which frame, and which load of it.
#[derive(PartialEq, Eq)]
struct Document {
    frame: String,
    loader: String,
}

/// The top frame's document if its URL has exactly `origin`.
fn top_document(view: &ICoreWebView2, origin: String, then: impl FnOnce(Option<Document>) + 'static) {
    call(view, "Page.getFrameTree", &json!({}), move |tree| {
        let frame = tree.as_ref().and_then(|tree| tree.pointer("/frameTree/frame"));
        let text = |name: &str| frame.and_then(|frame| frame.get(name)).and_then(Value::as_str);
        let document = match (text("id"), text("loaderId"), text("url")) {
            (Some(frame), Some(loader), Some(url)) if origin_of(url).as_deref() == Some(origin.as_str()) => {
                Some(Document {
                    frame: frame.to_owned(),
                    loader: loader.to_owned(),
                })
            }
            _ => None,
        };
        then(document);
    });
}

/// One DevTools protocol call on `view`. `then` runs once, with the result
/// or with `None` when the call could not be made or failed.
fn call(view: &ICoreWebView2, method: &str, parameters: &Value, then: impl FnOnce(Option<Value>) + 'static) {
    let then = Rc::new(RefCell::new(Some(then)));
    let completed = then.clone();
    let handler = CallDevToolsProtocolMethodCompletedHandler::create(Box::new(move |status, result: String| {
        if let Some(then) = completed.borrow_mut().take() {
            then(status.ok().and_then(|()| serde_json::from_str(&result).ok()));
        }
        Ok(())
    }));
    // SAFETY: The WebView2 apartment; the strings outlive the call.
    let started = unsafe {
        view.CallDevToolsProtocolMethod(&HSTRING::from(method), &HSTRING::from(parameters.to_string()), &handler)
    };
    if started.is_err() {
        if let Some(then) = then.borrow_mut().take() {
            then(None);
        }
    }
}

/// Whether the top document `view` shows has exactly `origin`.
fn shows(view: &ICoreWebView2, origin: &str) -> bool {
    let mut source = PWSTR::null();
    // SAFETY: The WebView2 apartment; WebView2 allocates the string, and
    // `take_pwstr` frees it.
    let read = unsafe { view.Source(&mut source) };
    read.is_ok() && origin_of(&take_pwstr(source)).is_some_and(|shown| shown == origin)
}

/// The WebView2 argument that sends the `https` and `wss` connections to
/// `host`, and only those, to the listener at `proxy`. `None` for a host
/// that is not a plain lowercase DNS name.
///
/// It is a PAC script in a `data:` URL: one exact host, so a child host is
/// not routed at all, and `DIRECT` after the listener, so an unreachable
/// listener leaves the instance on the tunnel. It holds no credential: the
/// listener's challenge is answered by `answer_listener_challenge`.
///
/// A WebView2 environment's arguments are fixed when the first WebView of
/// its user-data folder is made, and every environment on that folder must
/// use the same ones. So the shell gives this argument to every window of a
/// direct-eligible instance from the start, with the listener's port known
/// before the first window; it is never applied to a window that is open.
///
/// Two limits the shell's wiring (D2) has to respect:
/// - With this argument the environment uses the PAC alone. The system's
///   proxy settings and WPAD are no longer consulted for any host, and the
///   PAC's `DIRECT` is a direct connection, not "as before".
/// - `; DIRECT` applies only when the listener cannot be reached. A refusal
///   the listener answers (`502` with no backend, `403`) is final: WebView2
///   does not go on to `DIRECT` after a failed tunnel.
///
/// D2 must not wire Windows until the public-fallback/system-proxy rule is
/// settled (see D1c).
pub fn route_argument(host: &str, proxy: SocketAddrV4) -> Option<String> {
    let plain = !host.is_empty()
        && host.len() <= 253
        && host
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'.' || byte == b'-');
    if !plain {
        return None;
    }
    let script = format!(
        "function FindProxyForURL(url, host) {{ \
         if (host === \"{host}\" && (url.lastIndexOf(\"https:\", 0) === 0 || url.lastIndexOf(\"wss:\", 0) === 0)) \
         return \"PROXY {proxy}; DIRECT\"; return \"DIRECT\"; }}"
    );
    Some(format!(
        "--proxy-pac-url=data:application/x-ns-proxy-autoconfig;base64,{}",
        STANDARD.encode(script)
    ))
}

/// Whether a challenge's URI is the listener's own address, `ip:port`: as
/// WebView2 names a proxy, with or without the `http` scheme and a final
/// slash. Anything with a path, another scheme or another port is a site.
fn names_listener(uri: &str, listener: &str) -> bool {
    let authority = uri.strip_prefix("http://").unwrap_or(uri);
    authority.strip_suffix('/').unwrap_or(authority) == listener
}

/// Answers the direct listener's proxy challenge on `view` with its
/// credential, and no other challenge: only one that names the listener at
/// `proxy` and carries the listener's own realm. Every other challenge (a
/// site's, another proxy's) is left to WebView2 exactly as before. Returns
/// the registration, for `ICoreWebView2_10::remove_BasicAuthenticationRequested`.
///
/// The credential lives in this handler's memory. It is never in the PAC,
/// a URL, an argument, storage, or anything page script can read. The
/// listener answers a request that is not a `CONNECT` with `405`, never
/// with a challenge, so no page can make its address ask for it.
///
/// The PAC applies to every WebView of the environment, and this answer to
/// one WebView. A WebView of that environment without it gets the listener's
/// `407` on its first request to the instance, and WebView2 then shows its
/// own sign-in prompt for the proxy. So the shell registers this on every
/// WebView of that environment before its first navigation, and removes it
/// before the direct client is dropped.
pub fn answer_listener_challenge(
    view: &ICoreWebView2,
    proxy: SocketAddrV4,
    username: &str,
    password: &str,
) -> windows::core::Result<i64> {
    let listener = proxy.to_string();
    let (username, password) = (HSTRING::from(username), HSTRING::from(password));
    let handler = BasicAuthenticationRequestedEventHandler::create(Box::new(move |_, arguments| {
        let Some(arguments) = arguments else { return Ok(()) };
        let (mut uri, mut challenge) = (PWSTR::null(), PWSTR::null());
        // SAFETY: The WebView2 apartment; WebView2 allocates both strings,
        // and `take_pwstr` frees them.
        unsafe {
            arguments.Uri(&mut uri)?;
            arguments.Challenge(&mut challenge)?;
        }
        let (uri, challenge) = (take_pwstr(uri), take_pwstr(challenge));
        if !names_listener(&uri, &listener) || challenge.trim() != CHALLENGE {
            return Ok(());
        }
        // SAFETY: The WebView2 apartment; the strings outlive the calls.
        unsafe {
            let response = arguments.Response()?;
            response.SetUserName(&username)?;
            response.SetPassword(&password)?;
        }
        Ok(())
    }));
    let mut token = 0i64;
    // SAFETY: The WebView2 apartment; the handler is owned by WebView2 from here.
    unsafe {
        view.cast::<ICoreWebView2_10>()?
            .add_BasicAuthenticationRequested(&handler, &mut token)?;
    }
    Ok(token)
}
