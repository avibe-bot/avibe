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
private final class SheetFixtureRootViewController: UIViewController,
    UIAdaptivePresentationControllerDelegate
{
    private var sheetController: MobileOnboardingViewController?
    private var hasStarted = false
    private var documentReady = false
    private var fetchCount = 0
    private var runtimeWebView: WKWebView?
    private var runtimeRouteTask: Task<Void, Never>?
    private var documentReadinessTask: Task<Void, Never>?
    private var selectionReceiptObserver: FixtureSelectionReceiptObserver?
    private var selectionEvidence: [String: Any] = ["bridgeStage": "selection-none"]
    /// What a dismissing gesture did: whether its touch reached the window,
    /// and what UIKit's sheet did with it. Passive; published on the sheet's
    /// surface while it is up and on the root view once it is gone.
    private var dismissalEvidence: [String: Any] = ["stages": [String]()]
    private var dismissalTouchObserver: FixtureTouchObserver?
    private var selectionDisplayLink: CADisplayLink?
    private var geometryDisplayLink: CADisplayLink?
    private var lastGeometrySignature: String?
    private var geometrySequence = 0
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
            precondition(["default", "offline-open", "open-failed", "stale", "account", "first-run", "instances-named", "account-root", "page-switching"].contains(captureState))
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
            // `page-switching`: the full page with a selection, as after an
            // instance failed to load.
            startScreen: startScreen ?? (showsFullPage ? .instances : .sheet),
            resourceURL: resourceURL,
            returnToRuntime: { [weak self] in
                self?.dismissFixtureSheet() ?? false
            },
            dismissSurface: { [weak self] in
                self?.dismissFixtureSheet() ?? false
            }
        )
        controller.view.accessibilityIdentifier = "fixture-document-surface"
        if captureState == "open-failed" || captureState == "page-switching" {
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
        geometryDisplayLink = CADisplayLink(target: self, selector: #selector(recordGeometryTick))
        geometryDisplayLink?.add(to: .main, forMode: .common)
        controller.modalPresentationStyle = .pageSheet
        sheetController = controller
        publishReadiness()
        observeDocument(in: controller)
        // Only the plain sheet state: the selection and stall states time
        // touches, and reading the page's text forces layout.
        if realBundle, captureState == "default" { observePageRender(in: controller) }
        controller.overrideUserInterfaceStyle = overrideUserInterfaceStyle
        if showsFullPage {
            addChild(controller)
            controller.view.frame = view.bounds
            controller.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            view.addSubview(controller.view)
            controller.didMove(toParent: self)
        } else {
            presentationStartedUptime = ProcessInfo.processInfo.systemUptime
            // Notification-only delegate methods: the fixture never answers
            // whether the sheet may dismiss.
            controller.presentationController?.delegate = self
            present(controller, animated: false) { [weak self, weak controller] in
                self?.presentationCompletedUptime = ProcessInfo.processInfo.systemUptime
                self?.observeDismissalTouches(of: controller)
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
    /// The full-screen page rather than the sheet: first use, or the page
    /// with a selection.
    private var showsFullPage: Bool {
        captureState == "first-run" || captureState == "page-switching"
    }

    /// Page-side receipts for the real bundle, logged when they change:
    /// readiness, body text length, `#root` children, window errors and
    /// rejections, and when "You're in" first rendered after presentation.
    /// A slow first paint and a page that never renders read differently.
    private var pageReceiptTask: Task<Void, Never>?

    private func observePageRender(in controller: MobileOnboardingViewController) {
        guard let webView = controller.view.subviews.compactMap({ $0 as? WKWebView }).first else { return }
        webView.configuration.userContentController.addUserScript(WKUserScript(
            source: """
            window.__fixtureErrors = [];
            window.addEventListener('error', e => window.__fixtureErrors.push(String(e.message || e.type)));
            window.addEventListener('unhandledrejection', e => window.__fixtureErrors.push('rejection: ' + String(e.reason)));
            """,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        ))
        let started = ProcessInfo.processInfo.systemUptime
        pageReceiptTask?.cancel()
        pageReceiptTask = Task { @MainActor [weak self, weak webView] in
            var last = ""
            var rendered = false
            while !Task.isCancelled, let webView,
                  ProcessInfo.processInfo.systemUptime - started < 150 {
                let probe = (try? await webView.evaluateJavaScript(
                    """
                    JSON.stringify({
                      ready: document.readyState,
                      text: document.body ? document.body.innerText.length : -1,
                      youreIn: !!(document.body && /You’re in|正在使用/.test(document.body.innerText)),
                      root: document.getElementById('root') ? document.getElementById('root').childElementCount : -1,
                      errors: (window.__fixtureErrors || []).slice(0, 5)
                    })
                    """
                ) as? String) ?? "unavailable"
                let elapsed = ProcessInfo.processInfo.systemUptime - started
                if !rendered, probe.contains("\"youreIn\":true") {
                    rendered = true
                    NSLog("[page receipt] first render %.2f s after presentation; documentReady=%ld",
                          elapsed, (self?.documentReady ?? false) ? 1 : 0)
                }
                if probe != last {
                    NSLog("[page receipt] %.2f s %@", elapsed, probe)
                    last = probe
                }
                if rendered { return }
                try? await Task.sleep(for: .milliseconds(250))
            }
        }
    }

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

    private func observeDismissalTouches(of controller: MobileOnboardingViewController?) {
        guard let window = controller?.view.window, dismissalTouchObserver == nil else { return }
        let observer = FixtureTouchObserver { [weak self] event in
            self?.recordDismissal(event)
        }
        window.addGestureRecognizer(observer)
        dismissalTouchObserver = observer
    }

    func presentationControllerWillDismiss(_ presentationController: UIPresentationController) {
        recordDismissal(["kind": "sheet-will-dismiss"])
    }

    func presentationControllerDidDismiss(_ presentationController: UIPresentationController) {
        recordDismissal(["kind": "sheet-did-dismiss"])
    }

    func presentationControllerDidAttemptToDismiss(_ presentationController: UIPresentationController) {
        recordDismissal(["kind": "sheet-dismiss-refused"])
    }

    private func recordDismissal(_ event: [String: Any]) {
        guard let kind = event["kind"] as? String else { return }
        var stages = dismissalEvidence["stages"] as? [String] ?? []
        stages.append(kind)
        dismissalEvidence["stages"] = Array(stages.suffix(16))
        var detail = event
        detail["uptime"] = ProcessInfo.processInfo.systemUptime
        if kind == "native-touch-began",
           let webView = sheetController?.view.subviews.compactMap({ $0 as? WKWebView }).first {
            detail["scrollOffsetY"] = Double(webView.scrollView.contentOffset.y)
        }
        dismissalEvidence[kind] = detail
        selectionEvidence["dismissal"] = dismissalEvidence
        guard let data = try? JSONSerialization.data(withJSONObject: selectionEvidence, options: [.sortedKeys]),
              let text = String(data: data, encoding: .utf8) else { return }
        sheetController?.view.accessibilityLabel = text
        view.accessibilityLabel = text
    }

    @objc private func recordGeometryTick() {
        guard let controller = sheetController,
              let webView = controller.view.subviews.compactMap({ $0 as? WKWebView }).first else { return }
        let frame = controller.view.convert(controller.view.bounds, to: nil)
        let webFrame = webView.convert(webView.bounds, to: nil)
        let detent = controller.sheetPresentationController?.selectedDetentIdentifier?.rawValue ?? "none"
        let orientation = frame.width > frame.height ? "landscape" : "portrait"
        let signature = "\(orientation)|\(frame.integral)|\(webFrame.integral)|\(detent)"
        guard signature != lastGeometrySignature else { return }
        lastGeometrySignature = signature
        geometrySequence += 1
        selectionEvidence["geometry"] = [
            "stage": "native-geometry",
            "sequence": geometrySequence,
            "orientation": orientation,
            "sheetWindowFrame": frameEvidence(frame),
            "webViewWindowFrame": frameEvidence(webFrame),
            "detent": detent,
        ]
        if let data = try? JSONSerialization.data(withJSONObject: selectionEvidence, options: [.sortedKeys]),
           let text = String(data: data, encoding: .utf8) {
            controller.view.accessibilityLabel = text
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
        if kind == "bridge", let id = event["id"] as? String {
            var ids = selectionEvidence["selectionIDs"] as? [String] ?? []
            ids.append(id)
            selectionEvidence["selectionIDs"] = ids
        }
        if kind == "bridge", let stage = event["stage"] as? String {
            selectionEvidence["bridgeStage"] = stage
            var stages = selectionEvidence["bridgeStages"] as? [String] ?? []
            if !stages.contains(stage) { stages.append(stage) }
            selectionEvidence["bridgeStages"] = stages
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
        geometryDisplayLink?.invalidate()
        geometryDisplayLink = nil
        lastGeometrySignature = nil
        runtimeRouteTask?.cancel()
        pageReceiptTask?.cancel()
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

// Observe only the synthetic open-failed and page-switching fixtures' real
// WebKit request/reply.
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
      let rowErrorReported = false;
      const reportRowError = () => {
        if (rowErrorReported) return;
        const isVisible = (element) => {
          for (let node = element; node; node = node.parentElement) {
            const style = getComputedStyle(node);
            if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0"
                || node.getAttribute("aria-hidden") === "true") return false;
          }
          const rect = element.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0;
        };
        const alert = [...document.querySelectorAll('[role="alert"]')].find(element => {
          const chinese = navigator.language.toLowerCase().startsWith("zh");
          const expectedError = chinese ? "没能打开 Lab，再试一次。" : "Couldn’t open Lab. Try again.";
          const expectedRetry = chinese ? "重试" : "Retry";
          const row = element.closest("div");
          const buttons = row ? [...row.querySelectorAll("button")] : [];
          const retry = buttons.find(button => button.textContent.trim() === expectedRetry);
          const hasLabButton = buttons.some(button => button.textContent.includes("Lab"));
          return isVisible(element) && element.textContent.trim() === expectedError && buttons.length === 1
            && !!retry && isVisible(retry) && !hasLabButton;
        });
        if (!alert) return;
        rowErrorReported = true;
        receipt("row-error-rendered");
      };
      const observerRoot = document.documentElement || document;
      new MutationObserver(reportRowError).observe(observerRoot, {
        subtree: true, childList: true, characterData: true, attributes: true
      });
      reportRowError();
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
        window.webkit.messageHandlers.fixtureSelectionReceipt.postMessage({
          kind: "bridge", stage: "selection-submitted", id: String(request?.params?.id ?? "")
        });
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

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent) {
        if let touch = touches.first {
            let point = touch.location(in: nil)
            publish(["kind": "native-touch-ended", "timestamp": touch.timestamp,
                     "windowX": Double(point.x), "windowY": Double(point.y)])
        }
        state = .failed
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent) {
        if let touch = touches.first {
            publish(["kind": "native-touch-cancelled", "timestamp": touch.timestamp])
        }
        state = .failed
    }
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

    func providerStart(
        provider: MobileSignInProvider,
        challenge: String,
        state: String
    ) async throws -> MobileProviderStart {
        throw MobileAccountError.network
    }

    func providerExchange(
        transactionID: String,
        code: String,
        verifier: String
    ) async throws -> MobileProviderExchange {
        throw MobileAccountError.network
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
