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
    case authSignInWithPassword = "auth.signInWithPassword"
    case authVerifySecondFactor = "auth.verifySecondFactor"
    case authVerifyPasskey = "auth.verifyPasskey"
    case sessionRefresh = "session.refresh"
    case sessionSelectInstance = "session.selectInstance"
    case sessionLogout = "session.logout"
    case appOpenExternal = "app.openExternal"
    case appDismiss = "app.dismiss"
    case appReturnToInstance = "app.returnToInstance"
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

// A request may carry a password or a code, so its descriptions, dumps, and any
// log line built from it name the parameters and never their values.
extension MobileBridgeRequest: CustomStringConvertible,
    CustomDebugStringConvertible,
    CustomReflectable
{
    var description: String {
        "MobileBridgeRequest(method: \(method.rawValue), params: [\(parameterNames.joined(separator: ", "))])"
    }

    var debugDescription: String {
        description
    }

    var customMirror: Mirror {
        Mirror(
            self,
            children: [
                "method": method.rawValue,
                "params": parameterNames,
            ],
            displayStyle: .struct
        )
    }

    private var parameterNames: [String] {
        params.keys.sorted()
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
    private let returnToRuntime: () -> Bool
    private let dismissSurface: () -> Bool
    private let sheetMethodsOnly: Bool

    init(
        coordinator: MobileSessionCoordinator,
        returnToRuntime: @escaping () -> Bool,
        dismissSurface: @escaping () -> Bool,
        sheetMethodsOnly: Bool
    ) {
        self.coordinator = coordinator
        self.returnToRuntime = returnToRuntime
        self.dismissSurface = dismissSurface
        self.sheetMethodsOnly = sheetMethodsOnly
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
                if sheetMethodsOnly {
                    switch request.method {
                    case .authRequestOTP,
                         .authVerifyOTP,
                         .authSignInWithPassword,
                         .authVerifySecondFactor,
                         .authVerifyPasskey:
                        throw MobileAccountError.forbidden
                    default:
                        break
                    }
                }
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
                case .authSignInWithPassword:
                    guard
                        let email = request.params["email"] as? String,
                        let password = request.params["password"] as? String
                    else {
                        throw MobileAccountError.invalidRequest
                    }
                    let result = try await coordinator.signInWithPassword(
                        email: email,
                        password: password
                    )
                    reply(replyHandler, result: result.jsonObject)
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
                case .appDismiss:
                    guard request.params.isEmpty else {
                        throw MobileAccountError.invalidRequest
                    }
                    guard dismissSurface() else {
                        throw MobileAccountError.runtimeUnavailable
                    }
                    reply(replyHandler, result: NSNull())
                case .appReturnToInstance:
                    guard returnToRuntime() else {
                        throw MobileAccountError.runtimeUnavailable
                    }
                    reply(replyHandler, result: NSNull())
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

final class MobileOnboardingViewController: UIViewController, WKNavigationDelegate,
    UIGestureRecognizerDelegate
{
    private let coordinator: MobileSessionCoordinator
    private let startScreen: MobileOnboardingStartScreen
    /// Full-page account surfaces and the runtime-preserving sheet activate
    /// the hidden account transport when they appear. The legacy runtime
    /// account page starts after its caller has prepared the transition.
    private let startsAccountSurfaceOnAppear: Bool
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
        resourceURL: URL? = nil,
        returnToRuntime: @escaping () -> Bool = { false },
        dismissSurface: @escaping () -> Bool = { false }
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
        self.startScreen = startScreen
        startsAccountSurfaceOnAppear = startScreen != .runtimeAccount
        self.resourceURL = resolvedResourceURL
        self.schemeHandler = schemeHandler
        handler = MobileBridgeHandler(
            coordinator: coordinator,
            returnToRuntime: returnToRuntime,
            dismissSurface: dismissSurface,
            sheetMethodsOnly: startScreen == .sheet
        )
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
        // The pages change screens in place, not in the WebView's history,
        // so WebKit's own back swipe has nothing to go back to. A swipe in
        // from the left edge presses the page's Back button instead, where
        // one is shown; it starts only at the edge, so the pages' own
        // horizontal scrolling keeps its touches.
        let backSwipe = UIScreenEdgePanGestureRecognizer(
            target: self,
            action: #selector(handleBackSwipe(_:))
        )
        backSwipe.edges = .left
        backSwipe.delegate = self
        view.addGestureRecognizer(backSwipe)
        let keyboardViewportLayoutController =
            KeyboardViewportLayoutController(
                contentView: webView,
                containerView: view,
                bottomEdge: .container
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
        configureSheetPresentation()
        if startsAccountSurfaceOnAppear {
            coordinator.activateAccountSurface()
        }
        guard !hasLoadedBundledPage else {
            return
        }
        hasLoadedBundledPage = true
        webView.load(URLRequest(url: resourceURL))
    }

    private func configureSheetPresentation() {
        guard
            startScreen == .sheet,
            let sheet = (presentationController ?? parent?.presentationController)
                as? UISheetPresentationController
        else {
            return
        }
        sheet.detents = [.large()]
        sheet.prefersScrollingExpandsWhenScrolledToEdge = false
        sheet.prefersGrabberVisible = true
        let contentOwner = parent ?? self
        contentOwner.setContentScrollView(webView.scrollView, for: .all)
        contentOwner.view.accessibilityViewIsModal = true
    }

    @objc
    private func handleBackSwipe(_ gesture: UIScreenEdgePanGestureRecognizer) {
        guard gesture.state == .ended else {
            return
        }
        let travel = gesture.translation(in: view).x
        let speed = gesture.velocity(in: view).x
        guard travel > Self.backSwipeDistance || speed > Self.backSwipeSpeed else {
            return
        }
        pressPageBackFromSwipe()
    }

    /// How far, or how fast, a swipe from the left edge must go to mean Back.
    static let backSwipeDistance: CGFloat = 60
    static let backSwipeSpeed: CGFloat = 500

    /// Presses the page's Back button if it shows an enabled one; otherwise
    /// does nothing. Reports whether there was one.
    @discardableResult
    func pressPageBack() async -> Bool {
        let pressed = try? await webView.evaluateJavaScript(Self.pressBackScript)
        return pressed as? Bool == true
    }

    private func pressPageBackFromSwipe() {
        Task { @MainActor [weak self] in
            await self?.pressPageBack()
        }
    }

    static let pressBackScript = """
        (() => {
          const back = document.querySelector("[data-mobile-back]");
          if (!(back instanceof HTMLButtonElement) || back.disabled) return false;
          back.click();
          return true;
        })()
        """

    func gestureRecognizer(
        _ gestureRecognizer: UIGestureRecognizer,
        shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer
    ) -> Bool {
        true
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
