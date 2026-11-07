import UIKit
import WebKit

final class MobileBundleSchemeHandler: NSObject, WKURLSchemeHandler {
    static let scheme = "avibe-app"
    static let host = "bundle"

    let rootDirectoryURL: URL

    init(rootDirectoryURL: URL) {
        self.rootDirectoryURL = rootDirectoryURL.standardizedFileURL
    }

    func webView(
        _ webView: WKWebView,
        start urlSchemeTask: WKURLSchemeTask
    ) {
        do {
            guard let requestURL = urlSchemeTask.request.url else {
                throw URLError(.unsupportedURL)
            }
            let resourceURL = try resolveResourceURL(requestURL)
            let data = try Data(contentsOf: resourceURL)
            let response = URLResponse(
                url: requestURL,
                mimeType: Self.mimeType(for: resourceURL),
                expectedContentLength: data.count,
                textEncodingName: Self.textEncodingName(for: resourceURL)
            )
            urlSchemeTask.didReceive(response)
            urlSchemeTask.didReceive(data)
            urlSchemeTask.didFinish()
        } catch {
            urlSchemeTask.didFailWithError(error)
        }
    }

    func webView(
        _ webView: WKWebView,
        stop urlSchemeTask: WKURLSchemeTask
    ) {}

    static func isTrustedBundleURL(_ url: URL?) -> Bool {
        guard
            let url,
            let components = URLComponents(
                url: url,
                resolvingAgainstBaseURL: false
            )
        else {
            return false
        }
        return components.scheme?.lowercased() == scheme
            && components.host?.lowercased() == host
            && components.user == nil
            && components.password == nil
            && components.port == nil
    }

    func resolveResourceURL(_ requestURL: URL) throws -> URL {
        guard
            Self.isTrustedBundleURL(requestURL),
            let decodedPath = requestURL.path.removingPercentEncoding,
            !decodedPath.isEmpty
        else {
            throw URLError(.unsupportedURL)
        }

        let components = decodedPath.split(separator: "/", omittingEmptySubsequences: true)
        guard !components.contains("..") else {
            throw URLError(.noPermissionsToReadFile)
        }

        let resourceURL = components.reduce(rootDirectoryURL) {
            $0.appendingPathComponent(String($1), isDirectory: false)
        }.standardizedFileURL
        let rootPath = rootDirectoryURL.path.hasSuffix("/")
            ? rootDirectoryURL.path
            : rootDirectoryURL.path + "/"
        guard resourceURL.path.hasPrefix(rootPath) else {
            throw URLError(.noPermissionsToReadFile)
        }
        return resourceURL
    }

    private static func mimeType(for url: URL) -> String {
        switch url.pathExtension.lowercased() {
        case "css":
            return "text/css"
        case "html":
            return "text/html"
        case "js":
            return "text/javascript"
        case "json":
            return "application/json"
        case "png":
            return "image/png"
        case "svg":
            return "image/svg+xml"
        case "webp":
            return "image/webp"
        default:
            return "application/octet-stream"
        }
    }

    private static func textEncodingName(for url: URL) -> String? {
        switch url.pathExtension.lowercased() {
        case "css", "html", "js", "json", "svg":
            return "utf-8"
        default:
            return nil
        }
    }
}

enum MobileBridgeMethod: String, CaseIterable {
    case sessionBootstrap = "session.bootstrap"
    case authRequestOTP = "auth.requestOtp"
    case authVerifyOTP = "auth.verifyOtp"
    case authVerifySecondFactor = "auth.verifySecondFactor"
    case authVerifyPasskey = "auth.verifyPasskey"
    case sessionRefresh = "session.refresh"
    case sessionSelectInstance = "session.selectInstance"
    case sessionLogout = "session.logout"
    case appOpenExternal = "app.openExternal"
}

struct MobileBridgeRequest {
    let method: MobileBridgeMethod
    let params: [String: Any]

    static func parse(_ body: Any) throws -> MobileBridgeRequest {
        guard
            let object = body as? [String: Any],
            let rawMethod = object["method"] as? String,
            let method = MobileBridgeMethod(rawValue: rawMethod)
        else {
            throw MobileAccountError.invalidRequest
        }
        let rawParams = object["params"]
        guard
            rawParams == nil
                || rawParams is NSNull
                || rawParams is [String: Any]
        else {
            throw MobileAccountError.invalidRequest
        }
        return MobileBridgeRequest(
            method: method,
            params: rawParams as? [String: Any] ?? [:]
        )
    }
}

enum MobileBridgeConfiguration {
    static let messageHandlerName = "avibeMobile"

    static func make(
        handler: WKScriptMessageHandlerWithReply,
        schemeHandler: WKURLSchemeHandler
    ) -> WKWebViewConfiguration {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.setURLSchemeHandler(
            schemeHandler,
            forURLScheme: MobileBundleSchemeHandler.scheme
        )
        let controller = WKUserContentController()
        controller.addScriptMessageHandler(
            handler,
            contentWorld: .page,
            name: messageHandlerName
        )
        controller.addUserScript(
            WKUserScript(
                source: scriptSource,
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true,
                in: .page
            )
        )
        configuration.userContentController = controller
        return configuration
    }

    // Custom-scheme documents still expose about:blank at document start.
    // Install the page API eagerly; the native handler owns the origin boundary.
    static let scriptSource = """
    (() => {
      "use strict";
      if (Object.prototype.hasOwnProperty.call(window, "avibeMobile")) return;
      const call = (method, params = {}) => {
        const handler = window.webkit?.messageHandlers?.avibeMobile;
        if (!handler || typeof handler.postMessage !== "function") {
          return Promise.reject({
            code: "unavailable",
            message: "The mobile account bridge is unavailable."
          });
        }
        return Promise.resolve(handler.postMessage({method, params})).then((reply) => {
          if (reply?.ok === true) return reply.result;
          return Promise.reject(reply?.error ?? {
            code: "failed",
            message: "The mobile account operation failed."
          });
        });
      };
      Object.defineProperty(window, "avibeMobile", {
        configurable: false,
        enumerable: false,
        writable: false,
        value: Object.freeze({contract: 1, call})
      });
    })();
    """
}

private final class MobileBridgeHandler: NSObject, WKScriptMessageHandlerWithReply {
    weak var trustedWebView: WKWebView?
    private let coordinator: MobileSessionCoordinator

    init(coordinator: MobileSessionCoordinator) {
        self.coordinator = coordinator
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage,
        replyHandler: @escaping (Any?, String?) -> Void
    ) {
        guard
            message.webView === trustedWebView,
            message.frameInfo.isMainFrame,
            MobileBundleSchemeHandler.isTrustedBundleURL(
                message.frameInfo.request.url
            ),
            message.frameInfo.securityOrigin.protocol
                == MobileBundleSchemeHandler.scheme,
            message.frameInfo.securityOrigin.host
                == MobileBundleSchemeHandler.host
        else {
            reply(replyHandler, error: .forbidden)
            return
        }

        let request: MobileBridgeRequest
        do {
            request = try MobileBridgeRequest.parse(message.body)
        } catch let error as MobileAccountError {
            reply(replyHandler, error: error)
            return
        } catch {
            reply(replyHandler, error: .invalidRequest)
            return
        }

        Task { @MainActor [weak self] in
            guard let self else {
                replyHandler(
                    [
                        "ok": false,
                        "error": MobileAccountError.forbidden.jsonObject,
                    ],
                    nil
                )
                return
            }
            do {
                switch request.method {
                case .sessionBootstrap:
                    let state = try await coordinator.bootstrap()
                    reply(replyHandler, result: state.jsonObject)
                case .authRequestOTP:
                    guard let email = request.params["email"] as? String else {
                        throw MobileAccountError.invalidRequest
                    }
                    let cooldown = try await coordinator.requestOTP(email: email)
                    reply(
                        replyHandler,
                        result: ["cooldown_seconds": cooldown]
                    )
                case .authVerifyOTP:
                    guard
                        let email = request.params["email"] as? String,
                        let code = request.params["code"] as? String
                    else {
                        throw MobileAccountError.invalidRequest
                    }
                    let state = try await coordinator.verifyOTP(
                        email: email,
                        code: code
                    )
                    reply(replyHandler, result: state.jsonObject)
                case .authVerifySecondFactor:
                    guard
                        let rawMethod = request.params["method"] as? String,
                        let method = MobileSecondFactorCodeMethod(rawValue: rawMethod),
                        let code = request.params["code"] as? String
                    else {
                        throw MobileAccountError.invalidRequest
                    }
                    let state = try await coordinator.verifySecondFactor(
                        method: method,
                        code: code
                    )
                    reply(replyHandler, result: state.jsonObject)
                case .authVerifyPasskey:
                    guard let window = trustedWebView?.window else {
                        throw MobileAccountError.passkeyUnavailable
                    }
                    let state = try await coordinator.verifyPasskey(
                        presentationAnchor: window
                    )
                    reply(replyHandler, result: state.jsonObject)
                case .sessionRefresh:
                    let state = try await coordinator.refresh()
                    reply(replyHandler, result: state.jsonObject)
                case .sessionSelectInstance:
                    guard let id = request.params["id"] as? String else {
                        throw MobileAccountError.invalidRequest
                    }
                    let instance = try coordinator.selectInstance(id: id)
                    reply(
                        replyHandler,
                        result: ["instance": instance.jsonObject]
                    )
                case .sessionLogout:
                    let state = try await coordinator.logout()
                    reply(replyHandler, result: state.jsonObject)
                case .appOpenExternal:
                    guard
                        let rawURL = request.params["url"] as? String,
                        let url = URL(string: rawURL),
                        MobileExternalURLPolicy.allows(url)
                    else {
                        throw MobileAccountError(
                            code: "invalid_argument",
                            message: "The external URL is not in the mobile app allowlist."
                        )
                    }
                    let opened = await UIApplication.shared.open(
                        url,
                        options: [:]
                    )
                    reply(replyHandler, result: ["opened": opened])
                }
            } catch let error as MobileAccountError {
                reply(replyHandler, error: error)
            } catch {
                reply(replyHandler, error: .network)
            }
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
        error: MobileAccountError
    ) {
        replyHandler(
            [
                "ok": false,
                "error": error.jsonObject,
            ],
            nil
        )
    }
}

final class MobileOnboardingViewController: UIViewController, WKNavigationDelegate {
    private let coordinator: MobileSessionCoordinator
    private let resourceURL: URL
    private let schemeHandler: MobileBundleSchemeHandler
    private let handler: MobileBridgeHandler
    private let webView: WKWebView
    private var hasLoadedBundledPage = false
    private var keyboardViewportLayoutController:
        KeyboardViewportLayoutController?

    init(
        coordinator: MobileSessionCoordinator,
        startScreen: MobileOnboardingStartScreen = .instances,
        resourceURL: URL? = nil
    ) {
        let resourceDirectoryURL = resourceURL?.deletingLastPathComponent()
            ?? Bundle.main.resourceURL!.appendingPathComponent(
                "MobileApp",
                isDirectory: true
            )
        let schemeHandler = MobileBundleSchemeHandler(
            rootDirectoryURL: resourceDirectoryURL
        )
        var components = URLComponents()
        components.scheme = MobileBundleSchemeHandler.scheme
        components.host = MobileBundleSchemeHandler.host
        components.path = "/mobile.html"
        components.fragment = startScreen.rawValue
        let resolvedResourceURL = components.url!
        self.coordinator = coordinator
        self.resourceURL = resolvedResourceURL
        self.schemeHandler = schemeHandler
        handler = MobileBridgeHandler(coordinator: coordinator)
        webView = WKWebView(
            frame: .zero,
            configuration: MobileBridgeConfiguration.make(
                handler: handler,
                schemeHandler: schemeHandler
            )
        )
        super.init(nibName: nil, bundle: nil)
        handler.trustedWebView = webView
        webView.navigationDelegate = self
        webView.translatesAutoresizingMaskIntoConstraints = false
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        view.addSubview(webView)
        let keyboardViewportLayoutController =
            KeyboardViewportLayoutController(
                contentView: webView,
                containerView: view
            )
        self.keyboardViewportLayoutController =
            keyboardViewportLayoutController
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.topAnchor),
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            keyboardViewportLayoutController.bottomConstraint,
        ])
        keyboardViewportLayoutController.refreshForContainerGeometryChange()
    }

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        keyboardViewportLayoutController?
            .refreshForContainerGeometryChange()
    }

    override func viewSafeAreaInsetsDidChange() {
        super.viewSafeAreaInsetsDidChange()
        keyboardViewportLayoutController?
            .refreshForContainerGeometryChange()
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        coordinator.activateAccountSurface()
        guard !hasLoadedBundledPage else {
            return
        }
        hasLoadedBundledPage = true
        webView.load(URLRequest(url: resourceURL))
    }

    deinit {
        webView.configuration.userContentController.removeScriptMessageHandler(
            forName: MobileBridgeConfiguration.messageHandlerName,
            contentWorld: .page
        )
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if MobileBundleSchemeHandler.isTrustedBundleURL(url) {
            decisionHandler(.allow)
        } else {
            decisionHandler(.cancel)
        }
    }
}

private extension MobileInstance {
    var jsonObject: [String: Any] {
        [
            "id": id,
            "name": name,
            "host": host,
            "url": url.absoluteString,
            "relationship": relationship,
            "owner_email": ownerEmail ?? NSNull(),
            "available": available,
        ]
    }
}
