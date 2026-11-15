import UIKit
import WebKit

@main
final class SheetFixtureAppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [
            UIApplication.LaunchOptionsKey: Any
        ]? = nil
    ) -> Bool {
        let window = UIWindow(frame: UIScreen.main.bounds)
        window.rootViewController = SheetFixtureRootViewController()
        window.makeKeyAndVisible()
        self.window = window
        return true
    }
}

@MainActor
private final class SheetFixtureRootViewController: UIViewController {
    private var sheetController: MobileOnboardingViewController?
    private var hasStarted = false
    private var documentReady = false
    private var fetchCount = 0
    private var runtimeWebView: WKWebView?
    private var runtimeRouteTask: Task<Void, Never>?
    private var documentReadinessTask: Task<Void, Never>?
    private var selectionReceiptObserver: FixtureSelectionReceiptObserver?
    // These controls exist only in the simulator fixture app, never Avibe.
    private let captureState = ProcessInfo.processInfo.environment["AVIBE_FIXTURE_CAPTURE_STATE"]
    private let captureTheme = ProcessInfo.processInfo.environment["AVIBE_FIXTURE_CAPTURE_THEME"]
    private lazy var coordinator: MobileSessionCoordinator = {
        let defaults = UserDefaults(suiteName: "avibe.ios.sheet-fixture")!
        defaults.removePersistentDomain(forName: "avibe.ios.sheet-fixture")
        let store = AccountWebsiteDataStore(defaults: defaults)
        let transport = FixtureMobileAuthTransport(
            snapshot: captureState == nil ? Self.snapshot : captureSnapshot,
            failRefresh: captureState == "stale",
            didFetch: { [weak self] count in
                self?.fetchCount = count
                self?.publishReadiness()
            }
        )
        return MobileSessionCoordinator(
            defaults: defaults,
            websiteDataStore: store,
            transportFactory: { _, _ in
                transport
            },
            onOpenRuntime: { _ in },
            onPrepareAccountTransition: { [] }
        )
    }()

    private static let snapshot = MobileSessionSnapshot(
        account: MobileAccount(id: "fixture-account", email: "fixture@example.com"),
        instances: (1...32).map { index in
            MobileInstance(
                id: "fixture-\(index)",
                name: "Fixture instance \(index)",
                host: "instance-\(index).avibe.bot",
                url: URL(string: "https://instance-\(index).avibe.bot")!,
                relationship: index.isMultiple(of: 3) ? "shared" : "owner",
                ownerEmail: index.isMultiple(of: 3) ? "owner@example.com" : nil,
                available: !index.isMultiple(of: 11)
            )
        }
    )

    private var captureSnapshot: MobileSessionSnapshot {
        MobileSessionSnapshot(
            account: MobileAccount(id: "capture-account", email: "alex@example.invalid"),
            instances: [
                captureInstance("current", name: "Studio", available: true),
                captureInstance("lab", name: "Lab", available: true),
                captureInstance("offline", name: "Home", available: false),
            ]
        )
    }

    private func captureInstance(_ id: String, name: String, available: Bool) -> MobileInstance {
        MobileInstance(
            id: id, name: name, host: "\(id).example.invalid",
            // A genuine native selection rejection produces the row failure;
            // no replacement bridge or fabricated DOM/UI is used.
            url: URL(string: captureState == "open-failed" && id == "lab"
                ? "ftp://lab.example.invalid" : "https://\(id).example.invalid")!,
            relationship: "owner", ownerEmail: nil, available: available
        )
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        view.accessibilityIdentifier = "fixture-root"
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        guard !hasStarted else { return }
        hasStarted = true
        if let captureState {
            precondition(["default", "offline-open", "open-failed", "stale", "account", "first-run", "instances-named", "account-root"].contains(captureState))
            overrideUserInterfaceStyle = captureTheme == "dark" ? .dark : .light
            Task { @MainActor in
                do {
                    coordinator.activateAccountSurface()
                    _ = try await coordinator.bootstrap()
                    if captureState != "first-run" {
                        _ = try coordinator.selectInstance(id: "current")
                    }
                    if captureState == "instances-named" || captureState == "account-root" {
                        self.presentRuntimeFixture()
                    } else {
                        self.presentBundledPage(realBundle: true)
                    }
                } catch {
                    preconditionFailure("Capture fixture bootstrap failed: \(error)")
                }
            }
        } else {
            presentBundledPage(realBundle: false)
        }
    }

    private func presentBundledPage(
        realBundle: Bool,
        startScreen: MobileOnboardingStartScreen? = nil
    ) {
        let resourceURL = Bundle.main.url(
            forResource: "mobile",
            withExtension: "html",
            subdirectory: realBundle ? "MobileApp" : "FixtureMobile"
        )!
        let controller = MobileOnboardingViewController(
            coordinator: coordinator,
            startScreen: startScreen ?? (captureState == "first-run" ? .instances : .sheet),
            resourceURL: resourceURL,
            returnToRuntime: { [weak self] in
                self?.dismissFixtureSheet() ?? false
            },
            dismissSurface: { [weak self] in
                self?.dismissFixtureSheet() ?? false
            }
        )
        controller.view.accessibilityIdentifier = "fixture-document-surface"
        if captureState == "open-failed" {
            controller.view.accessibilityLabel = "selection-none"
            let observer = FixtureSelectionReceiptObserver { [weak controller] stage in
                controller?.view.accessibilityLabel = stage
            }
            selectionReceiptObserver = observer
            let webView = controller.view.subviews.compactMap { $0 as? WKWebView }.first!
            webView.configuration.userContentController.add(observer, name: "fixtureSelectionReceipt")
            webView.configuration.userContentController.addUserScript(WKUserScript(
                source: FixtureSelectionReceiptObserver.script,
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true
            ))
        }
        controller.modalPresentationStyle = .pageSheet
        sheetController = controller
        publishReadiness()
        observeDocument(in: controller)
        controller.overrideUserInterfaceStyle = overrideUserInterfaceStyle
        if captureState == "first-run" {
            addChild(controller)
            controller.view.frame = view.bounds
            controller.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            view.addSubview(controller.view)
            controller.didMove(toParent: self)
        } else {
            present(controller, animated: false)
        }
    }

    /// The WKWebView can exist before its WebContent process has loaded a
    /// document. Publish a native-only gate; do not replace the production
    /// navigation delegate or treat a rendered page as proof of its content.
    private func observeDocument(in controller: MobileOnboardingViewController) {
        let webView = controller.view.subviews.compactMap { $0 as? WKWebView }.first!
        documentReadinessTask = Task { @MainActor [weak self, weak controller, weak webView] in
            let clock = ContinuousClock()
            let deadline = clock.now.advanced(by: .seconds(120))
            while clock.now < deadline {
                guard let self, let controller, let webView,
                      self.sheetController === controller, !Task.isCancelled else { return }
                let loaded = try? await webView.evaluateJavaScript(
                    """
                    document.readyState === "complete"
                      && location.protocol === "avibe-app:"
                      && location.host === "bundle"
                      && location.pathname === "/mobile.html"
                    """
                ) as? Bool
                guard self.sheetController === controller, !Task.isCancelled else { return }
                if loaded == true {
                    self.documentReady = true
                    self.publishReadiness()
                    return
                }
                try? await Task.sleep(for: .milliseconds(50))
            }
        }
    }

    /// The B capture states start from a fixture runtime document with the
    /// production `avibeNative` configuration. Its native callbacks present
    /// the same MobileOnboardingViewController sheet owner used by Runtime;
    /// the bundled page is never asked to open its own route.
    private func presentRuntimeFixture() {
        guard let origin = WebOrigin(scheme: "https", host: "current.example.invalid", port: nil) else {
            preconditionFailure("The fixed Runtime fixture origin must be valid")
        }
        let runtime = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: origin,
                metadata: NativeShellMetadata(bundle: .main),
                setOuterScrollLocked: { _, _ in true },
                showAccountRoot: { [weak self] in
                    self?.presentBundledPage(realBundle: true, startScreen: .sheetAccount)
                },
                showInstances: { [weak self] in
                    self?.presentBundledPage(realBundle: true, startScreen: .sheetInstances)
                },
                instanceName: { "Studio" }
            )
        )
        runtime.isHidden = true
        view.addSubview(runtime)
        runtimeWebView = runtime
        let html = """
        <!doctype html><html><head><meta name="viewport" content="width=device-width"></head>
        <body><main id="runtime-document">Runtime fixture</main></body></html>
        """
        runtime.loadHTMLString(html, baseURL: URL(string: "https://current.example.invalid/runtime.html")!)
        runtimeRouteTask = Task { @MainActor [weak self, weak runtime] in
            let clock = ContinuousClock()
            let deadline = clock.now.advanced(by: .seconds(30))
            while clock.now < deadline {
                guard let self, let runtime, self.runtimeWebView === runtime,
                      !Task.isCancelled else { return }
                let invoked = (try? await runtime.evaluateJavaScript(
                    """
                    (() => {
                      if (document.readyState !== "complete" || typeof window.avibeNative?.call !== "function") return false;
                      let method = \(self.captureState == "account-root" ? String(reflecting: "app.showAccount") : String(reflecting: "app.showInstances"));
                      window.avibeNative.call(
                        method,
                        \(self.captureState == "account-root" ? #"{"start":"account"}"# : "{}")
                      );
                      return true;
                    })()
                    """
                ) as? Bool) == true
                if invoked { return }
                try? await Task.sleep(for: .milliseconds(50))
            }
        }
    }

    /// The B capture states exercise the real capable-shell bridge routes. They
    /// intentionally do not set a URL hash or replace the production page.
    /// The fixture-only invocation happens after the real bundled document is ready.
    private func publishReadiness() {
        sheetController?.view.accessibilityValue =
            "\(documentReady ? "document-ready" : "document-loading");fetch-\(fetchCount)"
    }

    @discardableResult
    private func dismissFixtureSheet() -> Bool {
        runtimeRouteTask?.cancel()
        runtimeWebView?.removeFromSuperview()
        runtimeWebView = nil
        guard let controller = sheetController else { return false }
        controller.dismiss(animated: true) { [weak self] in
            self?.documentReadinessTask?.cancel()
            self?.sheetController = nil
        }
        return true
    }
}

// Observe only the synthetic open-failed fixture's real WebKit request/reply.
// Do not replace the frozen avibeMobile API, the native handler, or its result.
@MainActor
private final class FixtureSelectionReceiptObserver: NSObject, WKScriptMessageHandler {
    private let publish: (String) -> Void

    init(publish: @escaping (String) -> Void) {
        self.publish = publish
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame,
              let stage = message.body as? String,
              ["selection-clicked", "selection-submitted", "selection-accepted",
               "selection-rejected-invalid_instance", "selection-rejected-other",
               "selection-transport-rejected"].contains(stage) else { return }
        publish(stage)
    }

    static let script = #"""
    (() => {
      const handler = window.webkit.messageHandlers.avibeMobile;
      const original = handler.postMessage.bind(handler);
      const receipt = (stage) => window.webkit.messageHandlers.fixtureSelectionReceipt.postMessage(stage);
      document.addEventListener("click", (event) => {
        const button = event.target.closest?.("button");
        if (event.isTrusted && button?.textContent.includes("Lab")) receipt("selection-clicked");
      }, true);
      handler.postMessage = (request) => {
        if (request?.method !== "session.selectInstance") return original(request);
        receipt("selection-submitted");
        const result = original(request);
        Promise.resolve(result).then((reply) => {
          receipt(reply?.ok === true ? "selection-accepted"
            : reply?.error?.code === "invalid_instance" ? "selection-rejected-invalid_instance"
            : "selection-rejected-other");
        }, () => receipt("selection-transport-rejected"));
        return result;
      };
    })();
    """#
}

// Data-only transport shapes keep the fixture independent of the production
// push coordinator, notification center, and entitlements. Their field shapes
// match NativePushCoordinator.swift; the fixture never registers for push.
struct PushManagementRequest {
    let method: String
    let path: String
    let headers: [String: String]
    let body: Data?
}

struct PushManagementResponse {
    let statusCode: Int
    let body: [String: Any]
}

@MainActor
private final class FixtureMobileAuthTransport: MobileAuthTransporting {
    private let session: MobileSessionSnapshot
    private let failRefresh: Bool
    private let didFetch: (Int) -> Void
    private var fetches = 0

    init(
        snapshot: MobileSessionSnapshot,
        failRefresh: Bool = false,
        didFetch: @escaping (Int) -> Void = { _ in }
    ) {
        session = snapshot
        self.failRefresh = failRefresh
        self.didFetch = didFetch
    }

    func requestOTP(email: String) async throws -> Int {
        throw MobileAccountError.invalidRequest
    }

    func verifyOTP(
        email: String,
        code: String
    ) async throws -> MobileSessionDiscovery {
        throw MobileAccountError.invalidRequest
    }

    func signInWithPassword(
        email: String,
        password: String
    ) async throws -> MobilePasswordSignInDiscovery {
        throw MobileAccountError.invalidRequest
    }

    func fetchSession() async throws -> MobileSessionDiscovery? {
        fetches += 1
        didFetch(fetches)
        if failRefresh && fetches > 1 { throw MobileAccountError.network }
        return .signedIn(session)
    }

    func passkeyChallenge() async throws -> MobilePasskeyChallenge {
        throw MobileAccountError.passkeyUnavailable
    }

    func verifySecondFactor(
        _ proof: MobileSecondFactorProof
    ) async throws -> MobileSessionDiscovery {
        throw MobileAccountError.invalidRequest
    }

    func logout() async throws {}

    func pushManagementRequest(
        _ request: PushManagementRequest
    ) async throws -> PushManagementResponse {
        PushManagementResponse(statusCode: 200, body: [:])
    }

    func cancelAll() async {}

    func invalidate() -> WebsiteDataStoreReleaseBarrier? {
        nil
    }
}
