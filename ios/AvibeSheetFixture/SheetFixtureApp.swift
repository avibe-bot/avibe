import UIKit
import UIKit.UIGestureRecognizerSubclass
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
    private var selectionEvidence: [String: Any] = ["bridgeStage": "selection-none"]
    private var selectionDisplayLink: CADisplayLink?
    private var lastDisplayTick: TimeInterval?
    private var maximumDisplayGap: TimeInterval = 0
    private var readinessJavaScriptPending = false
    private var presentationStartedUptime: TimeInterval?
    private var presentationCompletedUptime: TimeInterval?
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
            let webView = controller.view.subviews.compactMap { $0 as? WKWebView }.first!
            let observer = FixtureSelectionReceiptObserver { [weak self, weak controller, weak webView] event in
                guard let self, let controller, let webView else { return }
                self.recordSelectionEvidence(event, in: controller, webView: webView)
            }
            selectionReceiptObserver = observer
            recordSelectionEvidence(["kind": "observer-installed"], in: controller, webView: webView)
            let touches = FixtureTouchObserver { [weak self, weak controller, weak webView] event in
                guard let self, let controller, let webView else { return }
                self.recordSelectionEvidence(event, in: controller, webView: webView)
            }
            webView.addGestureRecognizer(touches)
            lastDisplayTick = ProcessInfo.processInfo.systemUptime
            selectionDisplayLink = CADisplayLink(target: self, selector: #selector(recordDisplayTick))
            selectionDisplayLink?.add(to: .main, forMode: .common)
            webView.configuration.userContentController.add(observer, name: "fixtureSelectionReceipt")
            webView.configuration.userContentController.addUserScript(WKUserScript(
                source: FixtureSelectionReceiptObserver.script,
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true
            ))
            if ProcessInfo.processInfo.environment["AVIBE_FIXTURE_STALL_DIAGNOSTIC"] == "1" {
                let arm = UIButton(type: .system)
                arm.setTitle("Arm diagnostic stall", for: .normal)
                arm.accessibilityIdentifier = "fixture-arm-stall"
                arm.frame = CGRect(x: 16, y: 10, width: 180, height: 36)
                arm.addTarget(self, action: #selector(armDiagnosticStall), for: .touchUpInside)
                controller.view.addSubview(arm)
            }
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
            presentationStartedUptime = ProcessInfo.processInfo.systemUptime
            present(controller, animated: false) { [weak self] in
                self?.presentationCompletedUptime = ProcessInfo.processInfo.systemUptime
            }
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
                self.readinessJavaScriptPending = true
                let loaded = try? await webView.evaluateJavaScript(
                    """
                    document.readyState === "complete"
                      && location.protocol === "avibe-app:"
                      && location.host === "bundle"
                      && location.pathname === "/mobile.html"
                    """
                ) as? Bool
                self.readinessJavaScriptPending = false
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

    @objc private func armDiagnosticStall() {
        // On-demand CI only. This is deliberately a stall, never a readiness wait.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { [weak self] in
            guard let self, let controller = self.sheetController,
                  let webView = controller.view.subviews.compactMap({ $0 as? WKWebView }).first else { return }
            self.recordSelectionEvidence(["kind": "stall-start"], in: controller, webView: webView)
            let end = ProcessInfo.processInfo.systemUptime + 1.5
            while ProcessInfo.processInfo.systemUptime < end {}
            self.recordSelectionEvidence(["kind": "stall-end"], in: controller, webView: webView)
        }
    }

    @objc private func recordDisplayTick() {
        let now = ProcessInfo.processInfo.systemUptime
        defer { lastDisplayTick = now }
        guard let lastDisplayTick else { return }
        let gap = now - lastDisplayTick
        guard gap > maximumDisplayGap else { return }
        maximumDisplayGap = gap
        // Publish only new material gaps, not every display tick/AX snapshot.
        if gap > 0.25, let controller = sheetController,
           let webView = controller.view.subviews.compactMap({ $0 as? WKWebView }).first {
            recordSelectionEvidence(["kind": "display-gap", "gap": gap], in: controller, webView: webView)
        }
    }

    private func frameEvidence(_ rect: CGRect) -> [String: Double] {
        ["x": Double(rect.origin.x), "y": Double(rect.origin.y),
         "width": Double(rect.size.width), "height": Double(rect.size.height)]
    }

    private func recordSelectionEvidence(
        _ event: [String: Any], in controller: MobileOnboardingViewController, webView: WKWebView
    ) {
        let kind = event["kind"] as? String ?? "unknown"
        if kind == "bridge", let stage = event["stage"] as? String {
            selectionEvidence["bridgeStage"] = stage
        }
        var events = selectionEvidence["events"] as? [[String: Any]] ?? []
        var observation = event
        observation["receivedUptime"] = ProcessInfo.processInfo.systemUptime
        observation["sheetWindowFrame"] = frameEvidence(controller.view.convert(controller.view.bounds, to: nil))
        observation["sheetLayerFrame"] = frameEvidence(controller.view.layer.frame)
        observation["presentationLayerFrame"] = controller.view.layer.presentation().map { frameEvidence($0.frame) } ?? [:]
        observation["transitionCoordinator"] = controller.transitionCoordinator != nil
        observation["presenterTransitionCoordinator"] = controller.presentingViewController?.transitionCoordinator != nil
        observation["isBeingPresented"] = controller.isBeingPresented
        observation["detent"] = controller.sheetPresentationController?.selectedDetentIdentifier?.rawValue ?? "none"
        observation["webViewWindowFrame"] = frameEvidence(webView.convert(webView.bounds, to: nil))
        observation["readinessJavaScriptPending"] = readinessJavaScriptPending
        observation["documentReady"] = documentReady
        observation["fetchCount"] = fetchCount
        observation["maximumDisplayGap"] = maximumDisplayGap
        observation["presentationStartedUptime"] = presentationStartedUptime
        observation["presentationCompletedUptime"] = presentationCompletedUptime
        observation["presentationAnimated"] = false
        events.append(observation)
        selectionEvidence["events"] = Array(events.suffix(32))
        if let data = try? JSONSerialization.data(withJSONObject: selectionEvidence, options: [.sortedKeys]),
           let text = String(data: data, encoding: .utf8) {
            controller.view.accessibilityLabel = text
        }
    }

    @discardableResult
    private func dismissFixtureSheet() -> Bool {
        selectionDisplayLink?.invalidate()
        selectionDisplayLink = nil
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
    private let publish: ([String: Any]) -> Void

    init(publish: @escaping ([String: Any]) -> Void) {
        self.publish = publish
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame,
              let event = message.body as? [String: Any] else { return }
        publish(event)
    }

    static let script = #"""
    (() => {
      const handler = window.webkit.messageHandlers.avibeMobile;
      const original = handler.postMessage.bind(handler);
      const receipt = (stage) => window.webkit.messageHandlers.fixtureSelectionReceipt.postMessage({kind: "bridge", stage});
      for (const kind of ["pointerdown", "pointerup", "pointercancel", "click"]) {
        document.addEventListener(kind, (event) => {
          if (!event.isTrusted) return;
          const row = [...document.querySelectorAll("button")].find(button => button.textContent.includes("Lab"));
          const hit = document.elementFromPoint(event.clientX, event.clientY);
          const rect = row?.getBoundingClientRect();
          window.webkit.messageHandlers.fixtureSelectionReceipt.postMessage({
            kind, timestamp: event.timeStamp, clientX: event.clientX, clientY: event.clientY,
            screenX: event.screenX, screenY: event.screenY,
            onLab: !!row && row.contains(event.target), hitOnLab: !!row && row.contains(hit),
            targetTag: event.target.tagName ?? "none", hitTag: hit?.tagName ?? "none",
            rowRect: rect ? {x: rect.x, y: rect.y, width: rect.width, height: rect.height} : null,
            rowDisabled: row?.disabled ?? null,
            viewport: {width: innerWidth, height: innerHeight, scale: visualViewport?.scale ?? 1}
          });
        }, true);
      }
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

// A passive recognizer: it never recognizes, prevents another recognizer, or
// delays/cancels touches. Its window point is an observation, never a forced tap.
@MainActor
private final class FixtureTouchObserver: UIGestureRecognizer {
    private let publish: ([String: Any]) -> Void

    init(publish: @escaping ([String: Any]) -> Void) {
        self.publish = publish
        super.init(target: nil, action: nil)
        cancelsTouchesInView = false
        delaysTouchesBegan = false
        delaysTouchesEnded = false
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("Fixture-only touch observer") }

    override func canPrevent(_ preventedGestureRecognizer: UIGestureRecognizer) -> Bool { false }
    override func canBePrevented(by preventingGestureRecognizer: UIGestureRecognizer) -> Bool { false }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent) {
        guard let touch = touches.first else { return }
        let point = touch.location(in: nil)
        publish(["kind": "native-touch-began", "timestamp": touch.timestamp,
                 "windowX": Double(point.x), "windowY": Double(point.y)])
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent) { state = .failed }
    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent) { state = .failed }
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
