import AVFAudio
import Foundation
import UIKit
import WebKit

struct NativeBridgePolicy {
    let instanceOrigin: WebOrigin

    func allows(isMainFrame: Bool, origin: WebOrigin?) -> Bool {
        isMainFrame && origin == instanceOrigin
    }
}

struct NativeShellMetadata: Equatable {
    static let contract = 1
    static let platform = "ios"
    /// Every shell's capabilities. `direct` is added only when the App has a
    /// direct transport registered (`withDirect(_:)`).
    static let capabilities = [
        "accountPresentation": 2,
        "app": 1,
        "badge": 1,
        "haptics": 1,
        "keyboardResizesViewport": 1,
        "push": 1,
        "viewport": 1,
        "voice": 1,
    ]

    let shellVersion: String
    let userAgentVersion: String
    /// Whether this shell offers `direct` (docs/plans/ios-app.md section 4).
    let direct: Bool

    init(
        shellVersion: String,
        userAgentVersion: String,
        direct: Bool = false
    ) {
        self.shellVersion = shellVersion
        self.userAgentVersion = userAgentVersion
        self.direct = direct
    }

    func withDirect(_ direct: Bool) -> NativeShellMetadata {
        NativeShellMetadata(
            shellVersion: shellVersion,
            userAgentVersion: userAgentVersion,
            direct: direct
        )
    }

    var capabilities: [String: Int] {
        var capabilities = Self.capabilities
        if direct {
            capabilities["direct"] = 1
        }
        return capabilities
    }

    init(bundle: Bundle = .main) {
        let version = bundle.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
            ?? "0"
        let build = bundle.object(forInfoDictionaryKey: "CFBundleVersion") as? String
            ?? "0"
        self.init(
            shellVersion: "\(version) (\(build))",
            userAgentVersion: version
        )
    }

    var userAgentSuffix: String {
        "AvibeiOS/\(userAgentVersion)"
    }

    var jsonObject: [String: Any] {
        [
            "contract": Self.contract,
            "platform": Self.platform,
            "shellVersion": shellVersion,
            "capabilities": capabilities,
        ]
    }
}

/// The color scheme of what the primary instance document draws under the
/// status bar, as the shell's bridge script reads it.
enum PageColorScheme: String, Equatable {
    case light
    case dark
}

enum NativeBridgeMethod: String, CaseIterable {
    case appearanceColorSchemeChanged = "appearance.colorSchemeChanged"
    case appInfo = "app.info"
    case appOpenExternal = "app.openExternal"
    case appSetAccountActionAvailable = "app.setAccountActionAvailable"
    case appShowAccount = "app.showAccount"
    case badgeSet = "badge.set"
    case directStatus = "direct.status"
    case hapticsImpact = "haptics.impact"
    case hapticsNotify = "haptics.notify"
    case hapticsSelection = "haptics.selection"
    case navigationHistoryChanged = "navigation.historyChanged"
    case notificationsReconcile = "notifications.reconcile"
    case pushStatus = "push.status"
    case pushSubscribe = "push.subscribe"
    case pushUnsubscribe = "push.unsubscribe"
    case pushOpenedAck = "push.openedAck"
    case viewportSetOuterScrollLocked = "viewport.setOuterScrollLocked"
    case voicePermission = "voice.permission"
}

struct NativeBridgeRequest {
    let method: NativeBridgeMethod
    let params: [String: Any]

    static func parse(_ body: Any) -> Result<NativeBridgeRequest, NativeBridgeError> {
        guard
            let object = body as? [String: Any],
            let methodName = object["method"] as? String,
            !methodName.isEmpty
        else {
            return .failure(.invalidRequest)
        }
        guard let method = NativeBridgeMethod(rawValue: methodName) else {
            return .failure(.unsupported)
        }

        let rawParams = object["params"]
        guard
            rawParams == nil
                || rawParams is NSNull
                || rawParams is [String: Any]
        else {
            return .failure(.invalidRequest)
        }
        return .success(
            NativeBridgeRequest(
                method: method,
                params: rawParams as? [String: Any] ?? [:]
            )
        )
    }
}

enum NativeExternalURL {
    static func parse(_ rawValue: String) -> URL? {
        let value = rawValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard
            !value.isEmpty,
            let components = URLComponents(string: value),
            let scheme = components.scheme?.lowercased()
        else {
            return nil
        }

        switch scheme {
        case "http", "https":
            guard components.host?.isEmpty == false else {
                return nil
            }
        case "mailto", "tel":
            guard !components.path.isEmpty else {
                return nil
            }
        default:
            return nil
        }
        return components.url
    }
}

enum NativeBridgeError: Error, Equatable {
    case forbidden
    case invalidArgument(String)
    case invalidRequest
    case unsupported
    case failed(String)

    var code: String {
        switch self {
        case .forbidden:
            return "forbidden"
        case .invalidArgument:
            return "invalid_argument"
        case .invalidRequest:
            return "invalid_request"
        case .unsupported:
            return "unsupported"
        case .failed:
            return "failed"
        }
    }

    var message: String {
        switch self {
        case .forbidden:
            return "The native bridge is available only to the configured instance main frame."
        case .invalidArgument(let message):
            return message
        case .invalidRequest:
            return "The native bridge request is malformed."
        case .unsupported:
            return "The native bridge method is not supported by this shell."
        case .failed(let message):
            return message
        }
    }

    var jsonObject: [String: Any] {
        [
            "code": code,
            "message": message,
        ]
    }
}

enum NativeBridgeConfiguration {
    static let messageHandlerName = "avibeNative"

    static func make(
        instanceOrigin: WebOrigin,
        metadata: NativeShellMetadata = NativeShellMetadata(),
        websiteDataStore: WKWebsiteDataStore = .default(),
        setOuterScrollLocked: @escaping (WKWebView, Bool) -> Bool,
        setAccountActionAvailable: @escaping (WKWebView, Bool) -> Bool = { _, _ in false },
        historyDidChange: @escaping (WKWebView) -> Bool = { _ in false },
        colorSchemeDidChange: @escaping (WKWebView, PageColorScheme?) -> Bool = { _, _ in false },
        showAccount: @escaping () -> Void = {},
        directStatus: DirectStatusProvider? = nil,
        pushStatus: @escaping @MainActor (
            String?,
            WebOrigin
        ) async throws -> [String: Any] = { _, _ in
            ["state": "unavailable"]
        },
        pushSubscribe: @escaping @MainActor (
            String,
            Bool,
            WebOrigin
        ) async throws -> [String: Any] = { _, _, _ in
            throw NativePushError.unavailable
        },
        pushUnsubscribe: @escaping @MainActor (
            WebOrigin
        ) async throws -> [String: Any] = { _ in
            throw NativePushError.unavailable
        },
        pushOpenedAck: @escaping @MainActor (
            String,
            WebOrigin
        ) -> Bool = { _, _ in
            false
        },
        setBadgeCount: @escaping @MainActor (
            Int,
            WebOrigin
        ) -> Bool = { _, _ in
            false
        },
        reconcileNotifications: @escaping @MainActor (
            NotificationReconcile,
            WebOrigin
        ) -> Bool = { _, _ in
            false
        }
    ) -> WKWebViewConfiguration {
        // `direct` exists only with a provider: a shell with no direct
        // transport neither advertises it nor answers `direct.status`.
        let metadata = metadata.withDirect(directStatus != nil)
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = websiteDataStore
        let controller = WKUserContentController()
        let handler = NativeBridgeHandler(
            policy: NativeBridgePolicy(instanceOrigin: instanceOrigin),
            metadata: metadata,
            setOuterScrollLocked: setOuterScrollLocked,
            setAccountActionAvailable: setAccountActionAvailable,
            historyDidChange: historyDidChange,
            colorSchemeDidChange: colorSchemeDidChange,
            showAccount: showAccount,
            directStatus: directStatus,
            pushStatus: pushStatus,
            pushSubscribe: pushSubscribe,
            pushUnsubscribe: pushUnsubscribe,
            pushOpenedAck: pushOpenedAck,
            setBadgeCount: setBadgeCount,
            reconcileNotifications: reconcileNotifications
        )
        controller.addScriptMessageHandler(
            handler,
            contentWorld: .page,
            name: messageHandlerName
        )
        controller.addUserScript(
            WKUserScript(
                source: scriptSource(
                    instanceOrigin: instanceOrigin,
                    metadata: metadata
                ),
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true,
                in: .page
            )
        )
        configuration.userContentController = controller
        configuration.applicationNameForUserAgent = metadata.userAgentSuffix
        return configuration
    }

    static func scriptSource(
        instanceOrigin: WebOrigin,
        metadata: NativeShellMetadata
    ) -> String {
        let payload: [String: Any] = [
            "instanceOrigin": instanceOrigin.serialized,
            "metadata": metadata.jsonObject,
            "messageHandler": messageHandlerName,
        ]
        let data = try! JSONSerialization.data(
            withJSONObject: payload,
            options: [.sortedKeys]
        )
        let json = String(decoding: data, as: UTF8.self)

        return """
        (() => {
          "use strict";
          const config = \(json);
          if (window !== window.top || window.location.origin !== config.instanceOrigin) return;
          if (Object.prototype.hasOwnProperty.call(window, "avibeNative")) return;

          // A document holding the outer-scroll lock is a Workbench AppShell,
          // which reserves every safe-area edge through env(). Declaring
          // viewport-fit=cover for exactly that document lets WebKit report
          // the Home indicator inset to it and drop the automatic scroll
          // inset, while every other document keeps its own declaration.
          const coveredViewports = new WeakMap();
          const declaresViewportFit = (content) => content
            .split(/[,;]/)
            .some((entry) => entry.split("=")[0].trim().toLowerCase() === "viewport-fit");
          const setViewportFitCover = (covered) => {
            for (const meta of document.querySelectorAll('meta[name="viewport"]')) {
              const content = meta.getAttribute("content") ?? "";
              const applied = coveredViewports.get(meta);
              if (covered) {
                if (applied || declaresViewportFit(content)) continue;
                const next = content.trim() ? `${content}, viewport-fit=cover` : "viewport-fit=cover";
                coveredViewports.set(meta, { original: content, next });
                meta.setAttribute("content", next);
              } else if (applied) {
                coveredViewports.delete(meta);
                if (content === applied.next) meta.setAttribute("content", applied.original);
              }
            }
          };

          const call = (method, params = {}) => {
            const handler = window.webkit?.messageHandlers?.[config.messageHandler];
            if (!handler || typeof handler.postMessage !== "function") {
              return Promise.reject({
                code: "unavailable",
                message: "The native bridge transport is unavailable."
              });
            }
            if (method === "viewport.setOuterScrollLocked" && typeof params?.locked === "boolean") {
              setViewportFitCover(params.locked);
            }
            return Promise.resolve(handler.postMessage({ method, params })).then((reply) => {
              if (reply?.ok === true) return reply.result;
              return Promise.reject(reply?.error ?? {
                code: "failed",
                message: "The native bridge call failed."
              });
            });
          };

          const notifyHistoryChanged = () => {
            window.setTimeout(() => {
              void call("navigation.historyChanged").catch(() => {});
            }, 0);
          };
          for (const method of ["pushState", "replaceState"]) {
            const original = window.history[method];
            Object.defineProperty(window.history, method, {
              configurable: true,
              writable: true,
              value(...args) {
                const result = original.apply(this, args);
                notifyHistoryChanged();
                return result;
              }
            });
          }

          const pushOpenedType = "avibe:native:push.opened";
          const pushOpenedHandlers = new Set();
          const deliveredPushOpenedIds = new Set();
          let pendingPushOpened = null;
          const deliverPushOpened = (detail, handler) => {
            const id = detail?.id;
            if (typeof id !== "string" || id.length === 0 || deliveredPushOpenedIds.has(id)) return;
            deliveredPushOpenedIds.add(id);
            handler(detail);
            void call("push.openedAck", { id }).catch(() => {});
          };
          window.addEventListener(pushOpenedType, (nativeEvent) => {
            if (pushOpenedHandlers.size === 0) {
              pendingPushOpened = nativeEvent.detail;
              return;
            }
            for (const handler of Array.from(pushOpenedHandlers)) {
              deliverPushOpened(nativeEvent.detail, handler);
            }
          });

          const on = (event, handler) => {
            if (typeof event !== "string" || typeof handler !== "function") return () => {};
            const type = `avibe:native:${event}`;
            if (event === "push.opened") {
              pushOpenedHandlers.add(handler);
              const pending = pendingPushOpened;
              pendingPushOpened = null;
              if (pending) {
                queueMicrotask(() => {
                  if (pushOpenedHandlers.has(handler)) deliverPushOpened(pending, handler);
                });
              }
              return () => pushOpenedHandlers.delete(handler);
            }
            const listener = (nativeEvent) => handler(nativeEvent.detail);
            window.addEventListener(type, listener);
            return () => window.removeEventListener(type, listener);
          };

          const metadata = config.metadata;
          Object.freeze(metadata.capabilities);
          Object.defineProperty(window, "avibeNative", {
            configurable: false,
            enumerable: false,
            writable: false,
            value: Object.freeze({
              contract: metadata.contract,
              platform: metadata.platform,
              shellVersion: metadata.shellVersion,
              capabilities: metadata.capabilities,
              call,
              on
            })
          });

          // The status bar text follows the scheme of what the page draws
          // under it: the computed color-scheme of the element at the top of
          // the viewport. That inherits the root's, which the stylesheet sets
          // per theme, unless a layer such as a dark image viewer declares
          // its own. Reading it here keeps deployed instances working with
          // no Web change. An unknown scheme is reported only once the
          // document has loaded, so a reload keeps the previous page's
          // scheme until the new one is known.
          let reportedScheme;
          let documentLoaded = false;
          let reportFrame = 0;
          const prefersDark = window.matchMedia("(prefers-color-scheme: dark)");
          const effectiveScheme = () => {
            const root = document.documentElement;
            if (!root) return null;
            const surface = document.body
              ? document.elementFromPoint(window.innerWidth / 2, 0) ?? root
              : root;
            const tokens = getComputedStyle(surface).getPropertyValue("color-scheme").toLowerCase().split(/\\s+/);
            const light = tokens.includes("light");
            const dark = tokens.includes("dark");
            if (light && dark) return prefersDark.matches ? "dark" : "light";
            return dark ? "dark" : light ? "light" : null;
          };
          const reportScheme = (force = false) => {
            let scheme;
            try {
              scheme = effectiveScheme();
            } catch {
              return;
            }
            if (scheme === null && !documentLoaded) return;
            if (!force && scheme === reportedScheme) return;
            reportedScheme = scheme;
            void call("appearance.colorSchemeChanged", { scheme }).catch(() => {});
          };
          // Coalesced to one read per frame, after layout has caught up.
          const scheduleReport = () => {
            if (reportFrame) return;
            reportFrame = window.requestAnimationFrame(() => {
              reportFrame = 0;
              reportScheme();
            });
          };
          // The root's theme attributes, and the body's own children: the
          // layers a dialog, viewer, or palette portals in and out.
          const observer = new MutationObserver(scheduleReport);
          let observingRoot = false;
          let observingBody = false;
          const observeDocument = () => {
            if (!observingRoot && document.documentElement) {
              observer.observe(document.documentElement, {
                attributes: true,
                attributeFilter: ["class", "data-theme", "style"]
              });
              observingRoot = true;
            }
            if (!observingBody && document.body) {
              observer.observe(document.body, { childList: true });
              observingBody = true;
            }
            scheduleReport();
          };
          observeDocument();
          document.addEventListener("readystatechange", observeDocument);
          document.addEventListener("DOMContentLoaded", observeDocument);
          prefersDark.addEventListener("change", scheduleReport);
          // Stylesheets that finish loading can set the root's scheme.
          document.addEventListener("load", (event) => {
            if (event.target instanceof HTMLLinkElement) scheduleReport();
          }, true);
          window.addEventListener("load", () => {
            documentLoaded = true;
            reportScheme();
          });
          // A page restored from the back-forward cache reports again after
          // the commit that restored it.
          window.addEventListener("pageshow", (event) => {
            if (event.persisted) reportScheme(true);
          });
        })();
        """
    }
}

private final class NativeBridgeHandler: NSObject, WKScriptMessageHandlerWithReply {
    private let policy: NativeBridgePolicy
    private let metadata: NativeShellMetadata
    private let setOuterScrollLocked: (WKWebView, Bool) -> Bool
    private let setAccountActionAvailable: (WKWebView, Bool) -> Bool
    private let historyDidChange: (WKWebView) -> Bool
    private let colorSchemeDidChange: (WKWebView, PageColorScheme?) -> Bool
    private let showAccount: () -> Void
    private let directStatus: DirectStatusProvider?
    private let pushStatus: @MainActor (
        String?,
        WebOrigin
    ) async throws -> [String: Any]
    private let pushSubscribe: @MainActor (
        String,
        Bool,
        WebOrigin
    ) async throws -> [String: Any]
    private let pushUnsubscribe: @MainActor (
        WebOrigin
    ) async throws -> [String: Any]
    private let pushOpenedAck: @MainActor (
        String,
        WebOrigin
    ) -> Bool
    private let setBadgeCount: @MainActor (
        Int,
        WebOrigin
    ) -> Bool
    private let reconcileNotifications: @MainActor (
        NotificationReconcile,
        WebOrigin
    ) -> Bool

    init(
        policy: NativeBridgePolicy,
        metadata: NativeShellMetadata,
        setOuterScrollLocked: @escaping (WKWebView, Bool) -> Bool,
        setAccountActionAvailable: @escaping (WKWebView, Bool) -> Bool,
        historyDidChange: @escaping (WKWebView) -> Bool,
        colorSchemeDidChange: @escaping (WKWebView, PageColorScheme?) -> Bool,
        showAccount: @escaping () -> Void,
        directStatus: DirectStatusProvider?,
        pushStatus: @escaping @MainActor (
            String?,
            WebOrigin
        ) async throws -> [String: Any],
        pushSubscribe: @escaping @MainActor (
            String,
            Bool,
            WebOrigin
        ) async throws -> [String: Any],
        pushUnsubscribe: @escaping @MainActor (
            WebOrigin
        ) async throws -> [String: Any],
        pushOpenedAck: @escaping @MainActor (
            String,
            WebOrigin
        ) -> Bool,
        setBadgeCount: @escaping @MainActor (
            Int,
            WebOrigin
        ) -> Bool,
        reconcileNotifications: @escaping @MainActor (
            NotificationReconcile,
            WebOrigin
        ) -> Bool
    ) {
        self.policy = policy
        self.metadata = metadata
        self.setOuterScrollLocked = setOuterScrollLocked
        self.setAccountActionAvailable = setAccountActionAvailable
        self.historyDidChange = historyDidChange
        self.colorSchemeDidChange = colorSchemeDidChange
        self.showAccount = showAccount
        self.directStatus = directStatus
        self.pushStatus = pushStatus
        self.pushSubscribe = pushSubscribe
        self.pushUnsubscribe = pushUnsubscribe
        self.pushOpenedAck = pushOpenedAck
        self.setBadgeCount = setBadgeCount
        self.reconcileNotifications = reconcileNotifications
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage,
        replyHandler: @escaping (Any?, String?) -> Void
    ) {
        let origin = WebOrigin(
            scheme: message.frameInfo.securityOrigin.protocol,
            host: message.frameInfo.securityOrigin.host,
            port: message.frameInfo.securityOrigin.port == 0
                ? nil
                : message.frameInfo.securityOrigin.port
        )
        guard policy.allows(isMainFrame: message.frameInfo.isMainFrame, origin: origin) else {
            reply(replyHandler, error: .forbidden)
            return
        }

        switch NativeBridgeRequest.parse(message.body) {
        case .failure(let error):
            reply(replyHandler, error: error)
        case .success(let request):
            if request.method == .pushStatus
                || request.method == .pushSubscribe
                || request.method == .pushUnsubscribe
                || request.method == .pushOpenedAck
            {
                handlePush(
                    request,
                    instanceOrigin: policy.instanceOrigin,
                    replyHandler: replyHandler
                )
                return
            }
            handle(
                request,
                sourceWebView: message.webView,
                replyHandler: replyHandler
            )
        }
    }

    private func handlePush(
        _ request: NativeBridgeRequest,
        instanceOrigin: WebOrigin,
        replyHandler: @escaping (Any?, String?) -> Void
    ) {
        switch request.method {
        case .pushStatus:
            guard
                request.params.isEmpty
                    || (
                        request.params.count == 1
                            && request.params["vapidPublicKey"] is String
                    )
            else {
                reply(
                    replyHandler,
                    error: .invalidArgument(
                        "push.status accepts an optional vapidPublicKey string."
                    )
                )
                return
            }
            let vapidPublicKey = request.params["vapidPublicKey"] as? String
            Task { @MainActor [weak self] in
                guard let self else {
                    replyHandler(nil, nil)
                    return
                }
                do {
                    let result = try await self.pushStatus(
                        vapidPublicKey,
                        instanceOrigin
                    )
                    self.reply(replyHandler, result: result)
                } catch {
                    self.reply(
                        replyHandler,
                        error: .failed(
                            (error as? NativePushError)?.bridgeMessage
                                ?? "The native push status is unavailable."
                        )
                    )
                }
            }
        case .pushSubscribe:
            guard
                let vapidPublicKey = request.params["vapidPublicKey"] as? String,
                request.params.keys.allSatisfy({
                    $0 == "vapidPublicKey" || $0 == "forceResubscribe"
                }),
                request.params.count <= 2,
                request.params["forceResubscribe"] == nil
                    || request.params["forceResubscribe"] is Bool
            else {
                reply(
                    replyHandler,
                    error: .invalidArgument(
                        "push.subscribe requires a vapidPublicKey string and accepts an optional forceResubscribe boolean."
                    )
                )
                return
            }
            let forceResubscribe =
                request.params["forceResubscribe"] as? Bool ?? false
            Task { @MainActor [weak self] in
                guard let self else {
                    replyHandler(nil, nil)
                    return
                }
                do {
                    let result = try await self.pushSubscribe(
                        vapidPublicKey,
                        forceResubscribe,
                        instanceOrigin
                    )
                    self.reply(replyHandler, result: result)
                } catch {
                    self.reply(
                        replyHandler,
                        error: .failed(
                            (error as? NativePushError)?.bridgeMessage
                                ?? "The native push subscription is unavailable."
                        )
                    )
                }
            }
        case .pushUnsubscribe:
            guard request.params.isEmpty else {
                reply(
                    replyHandler,
                    error: .invalidArgument(
                        "push.unsubscribe does not accept parameters."
                    )
                )
                return
            }
            Task { @MainActor [weak self] in
                guard let self else {
                    replyHandler(nil, nil)
                    return
                }
                do {
                    let result = try await self.pushUnsubscribe(instanceOrigin)
                    self.reply(replyHandler, result: result)
                } catch {
                    self.reply(
                        replyHandler,
                        error: .failed(
                            (error as? NativePushError)?.bridgeMessage
                                ?? "The native push unsubscribe is unavailable."
                        )
                    )
                }
            }
        case .pushOpenedAck:
            guard
                request.params.count == 1,
                let id = request.params["id"] as? String,
                !id.isEmpty
            else {
                reply(
                    replyHandler,
                    error: .invalidArgument(
                        "push.openedAck requires a non-empty id string."
                    )
                )
                return
            }
            guard pushOpenedAck(id, instanceOrigin) else {
                reply(replyHandler, error: .forbidden)
                return
            }
            reply(replyHandler, result: [:])
        default:
            reply(replyHandler, error: .unsupported)
        }
    }

    private func handle(
        _ request: NativeBridgeRequest,
        sourceWebView: WKWebView?,
        replyHandler: @escaping (Any?, String?) -> Void
    ) {
        switch request.method {
        case .appearanceColorSchemeChanged:
            let rawScheme = request.params["scheme"]
            let scheme = (rawScheme as? String).flatMap(PageColorScheme.init(rawValue:))
            guard
                request.params.count == 1,
                scheme != nil || rawScheme is NSNull
            else {
                reply(
                    replyHandler,
                    error: .invalidArgument(
                        "appearance.colorSchemeChanged requires a light, dark, or null scheme."
                    )
                )
                return
            }
            guard
                let sourceWebView,
                colorSchemeDidChange(sourceWebView, scheme)
            else {
                reply(replyHandler, error: .forbidden)
                return
            }
            reply(replyHandler, result: NSNull())
        case .appInfo:
            reply(replyHandler, result: metadata.jsonObject)
        case .appOpenExternal:
            guard
                let rawURL = request.params["url"] as? String,
                let url = NativeExternalURL.parse(rawURL)
            else {
                reply(
                    replyHandler,
                    error: .invalidArgument(
                        "app.openExternal requires an absolute http, https, mailto, or tel URL."
                    )
                )
                return
            }
            UIApplication.shared.open(url, options: [:]) { opened in
                self.reply(replyHandler, result: ["opened": opened])
            }
        case .appSetAccountActionAvailable:
            guard let available = request.params["available"] as? Bool else {
                reply(
                    replyHandler,
                    error: .invalidArgument(
                        "app.setAccountActionAvailable requires a boolean available value."
                    )
                )
                return
            }
            guard
                let sourceWebView,
                setAccountActionAvailable(sourceWebView, available)
            else {
                reply(replyHandler, error: .forbidden)
                return
            }
            reply(replyHandler, result: NSNull())
        case .appShowAccount:
            reply(replyHandler, result: NSNull())
            DispatchQueue.main.async { [showAccount] in
                showAccount()
            }
        case .badgeSet:
            guard let count = NativeBadgeCount.parse(request.params) else {
                reply(
                    replyHandler,
                    error: .invalidArgument(
                        "badge.set requires a count number."
                    )
                )
                return
            }
            // The instance this WebView was made for, which the App checks
            // is still the selected one (as push.* does).
            guard setBadgeCount(count, policy.instanceOrigin) else {
                reply(replyHandler, error: .forbidden)
                return
            }
            reply(replyHandler, result: NSNull())
        case .notificationsReconcile:
            guard let reconcile = NotificationReconcile.parse(request.params) else {
                reply(
                    replyHandler,
                    error: .invalidArgument(
                        "notifications.reconcile requires unreadSessions, at most \(NotificationReconcile.maximumSessions) distinct Session ids, and an asOf time in milliseconds."
                    )
                )
                return
            }
            // The instance this WebView was made for, which the App checks
            // is still the selected one (as badge.set does).
            guard reconcileNotifications(reconcile, policy.instanceOrigin) else {
                reply(replyHandler, error: .forbidden)
                return
            }
            reply(replyHandler, result: NSNull())
        case .navigationHistoryChanged:
            guard request.params.isEmpty else {
                reply(
                    replyHandler,
                    error: .invalidArgument(
                        "navigation.historyChanged does not accept parameters."
                    )
                )
                return
            }
            guard
                let sourceWebView,
                historyDidChange(sourceWebView)
            else {
                reply(replyHandler, error: .forbidden)
                return
            }
            reply(replyHandler, result: NSNull())
        case .directStatus:
            guard let directStatus else {
                // No direct transport: no `direct` capability, no status.
                reply(replyHandler, error: .unsupported)
                return
            }
            guard request.params.isEmpty else {
                reply(
                    replyHandler,
                    error: .invalidArgument(
                        "direct.status does not accept parameters."
                    )
                )
                return
            }
            // The instance this WebView was made for. The answer may be held
            // while the App returns from the background, so it is checked
            // again when it comes: the WebView must still show a document of
            // that origin.
            let instanceOrigin = policy.instanceOrigin
            Task { @MainActor [weak self, weak sourceWebView] in
                guard let self else {
                    replyHandler(nil, nil)
                    return
                }
                directStatus(instanceOrigin) { [weak self] status in
                    guard let self else {
                        replyHandler(nil, nil)
                        return
                    }
                    guard
                        let status,
                        let url = sourceWebView?.url,
                        WebOrigin(url: url, requiresHTTPS: false) == instanceOrigin
                    else {
                        self.reply(
                            replyHandler,
                            error: .failed("The direct status is no longer current for this page.")
                        )
                        return
                    }
                    self.reply(replyHandler, result: status.jsonObject)
                }
            }
        case .pushStatus, .pushSubscribe, .pushUnsubscribe, .pushOpenedAck:
            reply(replyHandler, error: .unsupported)
        case .hapticsImpact:
            guard
                let rawStyle = request.params["style"] as? String,
                let style = impactStyle(rawStyle)
            else {
                reply(
                    replyHandler,
                    error: .invalidArgument(
                        "haptics.impact requires light, medium, heavy, soft, or rigid."
                    )
                )
                return
            }
            let generator = UIImpactFeedbackGenerator(style: style)
            generator.prepare()
            generator.impactOccurred()
            reply(replyHandler, result: NSNull())
        case .hapticsNotify:
            guard
                let rawType = request.params["type"] as? String,
                let type = notificationType(rawType)
            else {
                reply(
                    replyHandler,
                    error: .invalidArgument(
                        "haptics.notify requires success, warning, or error."
                    )
                )
                return
            }
            let generator = UINotificationFeedbackGenerator()
            generator.prepare()
            generator.notificationOccurred(type)
            reply(replyHandler, result: NSNull())
        case .hapticsSelection:
            let generator = UISelectionFeedbackGenerator()
            generator.prepare()
            generator.selectionChanged()
            reply(replyHandler, result: NSNull())
        case .viewportSetOuterScrollLocked:
            guard let locked = request.params["locked"] as? Bool else {
                reply(
                    replyHandler,
                    error: .invalidArgument(
                        "viewport.setOuterScrollLocked requires a boolean locked value."
                    )
                )
                return
            }
            guard
                let sourceWebView,
                setOuterScrollLocked(sourceWebView, locked)
            else {
                reply(replyHandler, error: .forbidden)
                return
            }
            reply(replyHandler, result: NSNull())
        case .voicePermission:
            reply(
                replyHandler,
                result: ["state": microphonePermissionState()]
            )
        }
    }

    private func reply(
        _ replyHandler: @escaping (Any?, String?) -> Void,
        result: Any
    ) {
        replyHandler(
            [
                "ok": true,
                "result": result,
            ],
            nil
        )
    }

    private func reply(
        _ replyHandler: @escaping (Any?, String?) -> Void,
        error: NativeBridgeError
    ) {
        replyHandler(
            [
                "ok": false,
                "error": error.jsonObject,
            ],
            nil
        )
    }

    private func impactStyle(_ rawValue: String) -> UIImpactFeedbackGenerator.FeedbackStyle? {
        switch rawValue {
        case "light":
            return .light
        case "medium":
            return .medium
        case "heavy":
            return .heavy
        case "soft":
            return .soft
        case "rigid":
            return .rigid
        default:
            return nil
        }
    }

    private func notificationType(
        _ rawValue: String
    ) -> UINotificationFeedbackGenerator.FeedbackType? {
        switch rawValue {
        case "success":
            return .success
        case "warning":
            return .warning
        case "error":
            return .error
        default:
            return nil
        }
    }

    private func microphonePermissionState() -> String {
        switch AVAudioApplication.shared.recordPermission {
        case .granted:
            return "granted"
        case .denied:
            return "denied"
        case .undetermined:
            return "prompt"
        @unknown default:
            return "prompt"
        }
    }
}
