import Network
import UIKit
import WebKit
import XCTest
@testable import Avibe

/// The direct path inside the App's own composition: `AppModel`, the
/// runtime controller it registers, and the bridge a page talks to.
final class DirectPathAppTests: XCTestCase {
    private let instance = WebOrigin.parseConfigured(
        "https://instance-one.example"
    )!

    // MARK: The App as shipped

    @MainActor
    func testTheAppAsShippedBindsNoListenerAndWritesNoRoute() async throws {
        // No transport is passed, as in `AvibeApp`.
        let app = try await makeApp(directTransport: nil)
        let listening = listeningLoopbackPorts()
        let firstStore = app.runtimeWebsiteDataStore

        let runtime = try select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        let status = try await page.directStatus()
        XCTAssertEqual(status, #"{"sequence":0,"transport":"tunnel"}"#)
        page.close()

        _ = try select("instance-2", in: app)
        _ = try await app.mobileSessionCoordinator.logout()
        trackStore(of: app)
        try await Task.sleep(for: .milliseconds(200))

        XCTAssertEqual(firstStore.proxyConfigurations.count, 0)
        XCTAssertEqual(app.runtimeWebsiteDataStore.proxyConfigurations.count, 0)
        XCTAssertEqual(
            app.directPaths.status(for: instance),
            DirectPathStatus(transport: .tunnel, sequence: 0)
        )
        XCTAssertEqual(listeningLoopbackPorts().subtracting(listening), [])
    }

    // MARK: The App with a transport

    @MainActor
    func testATransportChangeReachesThePageAndTheRouteFollowsTheRuntime() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let listening = listeningLoopbackPorts()
        let firstStore = app.runtimeWebsiteDataStore

        let runtime = try select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        let target = try XCTUnwrap(transport.selections.last)
        XCTAssertEqual(target.runtime, runtime)
        XCTAssertEqual(target.storeIdentifier, app.websiteDataStore.identifier)
        let before = try await page.directStatus()
        XCTAssertEqual(before, #"{"sequence":0,"transport":"tunnel"}"#)

        // The transport finds a path: the store gets the route, and the
        // page is told after it has.
        try await reportUsable(transport, for: target) {
            firstStore.proxyConfigurations.count == 1
        }
        try await eventually {
            try await page.changes() == #"[{"sequence":1,"transport":"direct"}]"#
        }
        XCTAssertEqual(listeningLoopbackPorts().subtracting(listening).count, 1)
        let after = try await page.directStatus()
        XCTAssertEqual(after, #"{"sequence":1,"transport":"direct"}"#)
        page.close()

        // Another instance is selected: the route leaves with the runtime,
        // and the next one starts on the tunnel.
        let second = try select("instance-2", in: app)
        XCTAssertEqual(firstStore.proxyConfigurations.count, 0)
        let secondTarget = try XCTUnwrap(transport.selections.last)
        XCTAssertEqual(secondTarget.runtime, second)
        XCTAssertEqual(
            secondTarget.authority.connectTarget,
            "instance-two.example:443"
        )
        // A late report for the first instance changes nothing.
        transport.report(.usable(generation: "g1"), for: target)
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(firstStore.proxyConfigurations.count, 0)

        try await reportUsable(transport, for: secondTarget) {
            firstStore.proxyConfigurations.count == 1
        }

        // Signing out rotates the store: the route leaves with the old
        // one, and the new one starts with none.
        _ = try await app.mobileSessionCoordinator.logout()
        trackStore(of: app)
        XCTAssertEqual(firstStore.proxyConfigurations.count, 0)
        XCTAssertFalse(app.runtimeWebsiteDataStore === firstStore)
        XCTAssertEqual(app.runtimeWebsiteDataStore.proxyConfigurations.count, 0)
        transport.report(.usable(generation: "g1"), for: secondTarget)
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(app.runtimeWebsiteDataStore.proxyConfigurations.count, 0)
        try await eventually { listeningLoopbackPorts().subtracting(listening).isEmpty }
    }

    // MARK: The bridge

    @MainActor
    func testDirectStatusIsAdvertisedReadOnlyAndTakesNoParameters() async throws {
        var askedFor: WebOrigin?
        let webView = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: instance,
                setOuterScrollLocked: { _, _ in false },
                directStatus: { origin in
                    askedFor = origin
                    return DirectPathStatus(transport: .direct, sequence: 7)
                }
            )
        )
        let loader = PageLoader()
        webView.navigationDelegate = loader
        try await loader.loadHTML(
            "<!doctype html><title>instance</title>",
            baseURL: instance.url,
            in: webView
        )

        let result = try await webView.callAsyncJavaScript(
            """
            const info = await window.avibeNative.call("app.info");
            const status = await window.avibeNative.call("direct.status");
            const refused = [];
            for (const params of [{address: "192.168.1.20:443"}, {probe: true}, {answer: {}}]) {
              try {
                await window.avibeNative.call("direct.status", params);
                refused.push("accepted");
              } catch (error) {
                refused.push(error.code);
              }
            }
            return JSON.stringify({
              capability: info.capabilities.direct,
              status: [status.transport, status.sequence, Object.keys(status).length],
              refused
            });
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )

        XCTAssertEqual(
            result as? String,
            #"{"capability":1,"status":["direct",7,2],"refused":["invalid_argument","invalid_argument","invalid_argument"]}"#
        )
        XCTAssertEqual(askedFor, instance)
        // The whole surface: one call, and nothing that takes an address,
        // starts a probe, or accepts a rendezvous answer.
        XCTAssertEqual(
            NativeBridgeMethod.allCases
                .map(\.rawValue)
                .filter { $0.hasPrefix("direct.") },
            ["direct.status"]
        )
    }

    @MainActor
    func testATransportChangeIsDeliveredOnlyToInstanceDocumentsOfALiveRuntime() async throws {
        let controller = WebViewController(
            instanceOrigin: instance,
            websiteDataStore: .nonPersistent(),
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        let page = try RuntimePage(controller: controller)
        defer { page.close() }

        try await page.load(
            RuntimePage.recordingDocument,
            baseURL: instance.url
        )
        controller.deliverDirectTransportChanged(
            DirectPathStatus(transport: .direct, sequence: 3)
        )
        try await eventually {
            try await page.changes() == #"[{"sequence":3,"transport":"direct"}]"#
        }

        // A document on another origin has no bridge and is told nothing.
        try await page.load(
            """
            <!doctype html>
            <script>
              window.changes = [];
              window.addEventListener(
                "avibe:native:direct.transportChanged",
                (event) => window.changes.push(event.detail)
              );
            </script>
            """,
            baseURL: URL(string: "https://example.com/")!
        )
        controller.deliverDirectTransportChanged(
            DirectPathStatus(transport: .tunnel, sequence: 4)
        )
        try await Task.sleep(for: .milliseconds(300))
        let elsewhere = try await page.changes()
        XCTAssertEqual(elsewhere, "[]")

        // A released runtime is told nothing either.
        _ = controller.invalidateForAccountTransition()
        controller.deliverDirectTransportChanged(
            DirectPathStatus(transport: .direct, sequence: 5)
        )
    }

    // MARK: Fixture

    private var storeIdentifiers: Set<UUID> = []

    /// An `AppModel` signed in through a local control plane, past its
    /// bootstrap. `nil` builds it exactly as the App does, with no
    /// transport argument at all.
    @MainActor
    private func makeApp(
        directTransport: DirectTransporting?
    ) async throws -> AppModel {
        let server = try LocalHTTPServer { request in
            switch (request.method, request.path) {
            case ("GET", "/api/config/auth"):
                return .json("{}")
            case ("GET", "/api/mobile/session"):
                return .json(
                    """
                    {
                      "account": {"id": "account-1", "email": "max@example.com"},
                      "instances": [
                        {
                          "id": "instance-1",
                          "name": "One",
                          "host": "instance-one.example",
                          "url": "https://instance-one.example",
                          "relationship": "owner",
                          "owner_email": "max@example.com",
                          "available": true
                        },
                        {
                          "id": "instance-2",
                          "name": "Two",
                          "host": "instance-two.example",
                          "url": "https://instance-two.example",
                          "relationship": "owner",
                          "owner_email": "max@example.com",
                          "available": true
                        }
                      ]
                    }
                    """
                )
            default:
                return .status(404)
            }
        }
        let baseURL = try await server.start()
        addTeardownBlock {
            server.stop()
        }
        let suiteName = "DirectPathAppTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        addTeardownBlock {
            defaults.removePersistentDomain(forName: suiteName)
        }
        addTeardownBlock { @MainActor [self] in
            await self.removeTrackedStores()
        }

        let controlPlane = MobileControlPlaneConfiguration(testBaseURL: baseURL)
        let app: AppModel
        if let directTransport {
            app = AppModel(
                defaults: defaults,
                controlPlane: controlPlane,
                directTransport: directTransport,
                pushKeychain: InMemoryPushKeychainStore(),
                pushGenerationStore: InMemoryPushGenerationStore(),
                pushInstallationMarker: InMemoryPushInstallationMarkerStore(),
                pushNotificationCenter: SilentPushNotificationCenter()
            )
        } else {
            app = AppModel(
                defaults: defaults,
                controlPlane: controlPlane,
                pushKeychain: InMemoryPushKeychainStore(),
                pushGenerationStore: InMemoryPushGenerationStore(),
                pushInstallationMarker: InMemoryPushInstallationMarkerStore(),
                pushNotificationCenter: SilentPushNotificationCenter()
            )
        }
        trackStore(of: app)
        try await eventually(timeout: .seconds(30)) {
            if case .onboarding(.instances) = app.route {
                return true
            }
            return false
        }
        return app
    }

    /// Reports the path usable until the route is installed. The App's own
    /// push recovery can hold the account coordinator busy for a moment
    /// after a runtime opens, and a route that is ready then is refused; a
    /// transport reports again after `pathDidFail`, and so does this.
    @MainActor
    private func reportUsable(
        _ transport: StandInDirectTransport,
        for target: DirectTarget,
        until installed: @escaping @MainActor () -> Bool
    ) async throws {
        try await eventually {
            if installed() {
                return true
            }
            transport.report(.usable(generation: "g1"), for: target)
            return false
        }
    }

    @MainActor
    private func select(
        _ instanceID: String,
        in app: AppModel
    ) throws -> MobileRuntimeContext {
        _ = try app.mobileSessionCoordinator.selectInstance(id: instanceID)
        guard case .browser(let runtime) = app.route else {
            XCTFail("Selecting \(instanceID) opened no runtime.")
            throw DirectTestTimeout()
        }
        XCTAssertEqual(runtime.instance.id, instanceID)
        return runtime
    }

    /// The runtime controller the root view would create for `runtime`,
    /// registered with the App, showing an instance document that records
    /// the transport changes it is told about.
    @MainActor
    private func showRuntimePage(
        _ app: AppModel,
        _ runtime: MobileRuntimeContext
    ) async throws -> RuntimePage {
        let origin = try XCTUnwrap(WebOrigin(url: runtime.instance.url))
        let controller = WebViewController(
            instanceOrigin: origin,
            websiteDataStore: app.runtimeWebsiteDataStore,
            directStatus: app.directPaths.status(for:),
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        app.registerRuntimeController(controller, for: runtime)
        let page = try RuntimePage(controller: controller)
        try await page.load(
            RuntimePage.recordingDocument,
            baseURL: runtime.instance.url
        )
        return page
    }

    /// The App made real named stores; remove exactly those.
    @MainActor
    private func trackStore(of app: AppModel) {
        storeIdentifiers.insert(app.websiteDataStore.identifier)
    }

    @MainActor
    private func removeTrackedStores() async {
        for identifier in storeIdentifiers {
            for _ in 0..<50 {
                _ = await AccountWebsiteDataStoreOperations.live
                    .removeIdentifier(identifier)
                let registered = await withCheckedContinuation { continuation in
                    WKWebsiteDataStore.fetchAllDataStoreIdentifiers {
                        continuation.resume(returning: Set($0))
                    }
                }
                if !registered.contains(identifier) {
                    break
                }
                try? await Task.sleep(for: .milliseconds(100))
            }
        }
        storeIdentifiers.removeAll()
    }
}

/// One runtime controller in a window, and its primary WebView.
@MainActor
private final class RuntimePage {
    /// Records each transport change the bridge delivers.
    static let recordingDocument = """
        <!doctype html>
        <title>instance</title>
        <script>
          window.changes = [];
          window.avibeNative.on("direct.transportChanged", (status) => {
            window.changes.push(status);
          });
        </script>
        """

    private var window: UIWindow?
    private var webView: WKWebView?
    private let loader = PageLoader()

    init(controller: WebViewController) throws {
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 700))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        webView.navigationDelegate = loader
        self.window = window
        self.webView = webView
    }

    func load(_ html: String, baseURL: URL) async throws {
        let webView = try XCTUnwrap(webView)
        try await loader.loadHTML(html, baseURL: baseURL, in: webView)
    }

    /// The changes the document has recorded, as JSON.
    func changes() async throws -> String? {
        try await webView?.evaluateJavaScript(
            "JSON.stringify(window.changes ?? null)"
        ) as? String
    }

    /// `direct.status()` as the page gets it, as JSON with sorted keys.
    func directStatus() async throws -> String? {
        try await webView?.callAsyncJavaScript(
            """
            const status = await window.avibeNative.call("direct.status");
            return JSON.stringify(status, Object.keys(status).sort());
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        ) as? String
    }

    /// Lets go of the WebView and its window, as the App does when the
    /// runtime is released.
    func close() {
        webView?.navigationDelegate = nil
        webView = nil
        window?.rootViewController = nil
        window?.isHidden = true
        window = nil
    }
}

/// Loads a document and waits for it. The cancellation of a navigation
/// that was in flight when this one started is not this load's failure.
@MainActor
private final class PageLoader: NSObject, WKNavigationDelegate {
    private var continuation: CheckedContinuation<Void, Error>?

    func loadHTML(
        _ html: String,
        baseURL: URL,
        in webView: WKWebView
    ) async throws {
        try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            webView.loadHTMLString(html, baseURL: baseURL)
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation?) {
        finish(with: .success(()))
    }

    func webView(
        _ webView: WKWebView,
        didFail navigation: WKNavigation?,
        withError error: Error
    ) {
        finish(with: .failure(error))
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation?,
        withError error: Error
    ) {
        finish(with: .failure(error))
    }

    private func finish(with result: Result<Void, Error>) {
        if case .failure(let error as NSError) = result,
           error.domain == NSURLErrorDomain,
           error.code == NSURLErrorCancelled
        {
            return
        }
        continuation?.resume(with: result)
        continuation = nil
    }
}

@MainActor
private final class SilentPushNotificationCenter: PushNotificationCentering {
    func deliveredNotifications() async -> [PushNotificationCenterEntry] {
        []
    }

    func pendingNotificationRequests() async -> [PushNotificationCenterEntry] {
        []
    }

    func removeDeliveredNotifications(withIdentifiers identifiers: [String]) {}

    func removePendingNotificationRequests(withIdentifiers identifiers: [String]) {}
}
