import Foundation
import WebKit

// The native rendezvous request (decision D3 of
// docs/plans/remote-direct-phase1.md; rendezvous contract, section 5).
// Nothing in the App calls it until a direct transport is registered.

enum DirectRendezvousFetchError: Error, Equatable {
    /// The WebView does not show a document of the instance's origin yet,
    /// or that document has no CSRF cookie yet. Worth asking again soon.
    case pageNotReady
    /// The instance answered, and not with `200`: the session is missing or
    /// revoked, or the request was refused.
    case refused(status: Int)
    /// A service worker controls the document, or took control while the
    /// request ran. Whatever came back is not known to be the instance's.
    case controlled
    /// The network, the deadline, a redirect, or an answer that is too
    /// large or not text.
    case failed
}

/// Asks the instance how to dial it, as a same-origin request from the
/// runtime's own WebView, so the request carries the account data store's
/// remote session and the instance's usual Origin and CSRF checks apply.
///
/// The request runs as a fixed script in `WKContentWorld.defaultClient`,
/// which page script cannot reach: it shares the document and its cookies,
/// not the page's JavaScript objects. The answer comes back through
/// `callAsyncJavaScript` and never through the bridge, so no page, Show
/// Pages included, can hand native code an answer of its own.
///
/// A content world does not keep a service worker out: a worker that
/// controls the document answers its requests whichever world made them,
/// and `cache: "no-store"` does not pass it. So the script refuses for the
/// whole request: it listens for a change of controller before it looks,
/// and looks again before the request, after each await, and before it
/// answers. A fetch is intercepted only by the document's controller, and
/// a document that has one keeps one until it unloads, so an interception
/// leaves a controller to find. This App's WebViews have no service worker
/// container today (`DirectRendezvousFetcherTests` holds that), so the
/// refusal has never met a real worker: if a container appears, it must be
/// qualified against one before the direct path is used.
@MainActor
enum DirectRendezvousFetcher {
    static let defaultDeadlineMilliseconds = 5_000

    /// The answer's bytes, exactly as the instance sent them with `200`.
    static func fetch(
        in webView: WKWebView,
        origin: WebOrigin,
        clientEndpointID: DirectEndpointID,
        deadlineMilliseconds: Int = defaultDeadlineMilliseconds
    ) async throws -> Data {
        guard isShowing(origin, in: webView) else {
            throw DirectRendezvousFetchError.pageNotReady
        }
        let result: Any?
        do {
            result = try await webView.callAsyncJavaScript(
                script,
                arguments: [
                    // Absolute, and built here: a page can add a `<base>`
                    // element, which would move a relative path.
                    "url": origin.serialized + DirectRendezvous.path,
                    "origin": origin.serialized,
                    "body": DirectRendezvous.requestBody(clientEndpointID: clientEndpointID),
                    "deadlineMilliseconds": deadlineMilliseconds,
                    "maxBytes": DirectRendezvous.maxAnswerBytes,
                ],
                in: nil,
                contentWorld: .defaultClient
            )
        } catch {
            throw DirectRendezvousFetchError.failed
        }
        guard
            // A navigation may have committed while the request ran.
            isShowing(origin, in: webView),
            let text = result as? String,
            let data = text.data(using: .utf8),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            throw DirectRendezvousFetchError.failed
        }
        guard object["ok"] as? Bool == true else {
            switch object["failure"] as? String {
            case "origin", "csrf":
                throw DirectRendezvousFetchError.pageNotReady
            case "controlled":
                throw DirectRendezvousFetchError.controlled
            default:
                throw DirectRendezvousFetchError.failed
            }
        }
        guard
            let status = object["status"] as? Int,
            let body = object["body"] as? String
        else {
            throw DirectRendezvousFetchError.failed
        }
        guard status == 200 else {
            throw DirectRendezvousFetchError.refused(status: status)
        }
        return Data(body.utf8)
    }

    private static func isShowing(_ origin: WebOrigin, in webView: WKWebView) -> Bool {
        guard
            let url = webView.url,
            let current = WebOrigin(url: url, requiresHTTPS: false)
        else {
            return false
        }
        return current == origin
    }

    /// The body of an async function. Its parameters are the arguments
    /// above. It answers a JSON string: `{ok: true, status, body}` or
    /// `{ok: false, failure}`.
    private static let script = """
    const fail = (failure) => JSON.stringify({ok: false, failure});
    if (window.location.origin !== origin) return fail("origin");
    // A service worker that controls this document, now or at any moment
    // until the answer is read, could answer in the instance's place.
    const workers = navigator.serviceWorker;
    let taken = false;
    const onControllerChange = () => {
      taken = true;
    };
    if (workers) workers.addEventListener("controllerchange", onControllerChange);
    const controlled = () => taken || Boolean(workers && workers.controller);
    const controller = new AbortController();
    let timedOut = false;
    let timer = null;
    try {
      if (controlled()) return fail("controlled");
      const cookiePrefix = "vibe_csrf_token=";
      let token = null;
      for (const part of document.cookie.split(";")) {
        const trimmed = part.trim();
        if (trimmed.startsWith(cookiePrefix)) {
          try {
            token = decodeURIComponent(trimmed.slice(cookiePrefix.length));
          } catch {
            token = null;
          }
          break;
        }
      }
      if (!token) return fail("csrf");
      timer = window.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, Math.max(1, Number(deadlineMilliseconds) || 1));
      const response = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        redirect: "error",
        headers: {
          "Content-Type": "application/json",
          "X-Vibe-CSRF-Token": token
        },
        body,
        signal: controller.signal
      });
      if (controlled()) return fail("controlled");
      if (response.redirected || response.url !== url || response.type !== "basic") {
        return fail("redirect");
      }
      let text = "";
      if (response.status === 200 && response.body) {
        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8", {fatal: true});
        let total = 0;
        for (;;) {
          const {done, value} = await reader.read();
          if (controlled()) return fail("controlled");
          if (done) break;
          total += value.byteLength;
          if (total > maxBytes) return fail("oversize");
          text += decoder.decode(value, {stream: true});
        }
        text += decoder.decode();
      }
      if (controlled()) return fail("controlled");
      return JSON.stringify({ok: true, status: response.status, body: text});
    } catch (error) {
      return fail(controlled() ? "controlled" : timedOut ? "timeout" : "network");
    } finally {
      if (timer !== null) window.clearTimeout(timer);
      if (workers) workers.removeEventListener("controllerchange", onControllerChange);
      // Ends a request whose answer was refused before it was read.
      controller.abort();
    }
    """
}
