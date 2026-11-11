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

    /// Observed from before the App exists, and checked at every step:
    /// WebKit's `proxyConfigurations` setter is never called, on any store,
    /// and the App has no listening socket. The test with a transport,
    /// below, shows the count and the scan do move.
    @MainActor
    func testTheAppAsShippedCreatesNoListenerAndNeverWritesARoute() async throws {
        try assertSocketScanSeesListeners()
        let routeWrites = try ProxyConfigurationWrites.count()
        var listening: Set<ListeningSocket> = []

        func assertUntouched(_ step: String) throws {
            XCTAssertEqual(
                try ProxyConfigurationWrites.count(),
                routeWrites,
                step
            )
            XCTAssertEqual(try newListeners(since: listening), [], step)
        }

        // No transport is passed, as in `AvibeApp`. The baseline is taken
        // once this test's own control plane listens and before the App
        // exists.
        let app = try await makeApp(directTransport: nil) {
            listening = try listeningSockets()
        }
        let firstStore = app.runtimeWebsiteDataStore
        try assertUntouched("launched and signed in")

        let runtime = try select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        let status = try await page.directStatus()
        XCTAssertEqual(status, #"{"sequence":0,"transport":"tunnel"}"#)
        try assertUntouched("a runtime opened and its page asked")

        NotificationCenter.default.post(
            name: UIApplication.didEnterBackgroundNotification,
            object: nil
        )
        try assertUntouched("background")
        NotificationCenter.default.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        try assertUntouched("foreground")
        page.close()

        _ = try select("instance-2", in: app)
        try assertUntouched("the instance was switched")

        // Signing out rotates the account's store.
        _ = try await app.mobileSessionCoordinator.logout()
        trackStore(of: app)
        XCTAssertFalse(app.runtimeWebsiteDataStore === firstStore)
        try assertUntouched("signed out and the store rotated")
        try await Task.sleep(for: .milliseconds(200))
        try assertUntouched("afterwards")

        XCTAssertEqual(firstStore.proxyConfigurations.count, 0)
        XCTAssertEqual(app.runtimeWebsiteDataStore.proxyConfigurations.count, 0)
        XCTAssertEqual(app.directPaths.status(for: instance), .initial)
    }

    // MARK: The App with a transport

    @MainActor
    func testATransportChangeReachesThePageAndTheRouteFollowsTheRuntime() async throws {
        let transport = StandInDirectTransport()
        let routeWrites = try ProxyConfigurationWrites.count()
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(directTransport: transport) {
            listening = try listeningSockets()
        }
        let firstStore = app.runtimeWebsiteDataStore

        let runtime = try select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        let target = try XCTUnwrap(transport.selections.last)
        XCTAssertEqual(target.runtime, runtime)
        XCTAssertEqual(target.storeIdentifier, app.websiteDataStore.identifier)
        let before = try await page.directStatus()
        XCTAssertEqual(before, #"{"sequence":0,"transport":"tunnel"}"#)
        // Selecting is not activating: what the shipped-App test counts is
        // still untouched here, with a transport present.
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
        XCTAssertEqual(try newListeners(since: listening), [])

        // The transport finds a path: the store gets the route, and the
        // page is told after it has.
        try await reportUsable(transport, for: target) {
            firstStore.proxyConfigurations.count == 1
        }
        try await eventually {
            try await page.changes() == #"[{"sequence":1,"transport":"direct"}]"#
        }
        XCTAssertEqual(try newListeners(since: listening).map(\.address), ["127.0.0.1"])
        // The same count and the same scan do move once a route is used.
        XCTAssertGreaterThan(try ProxyConfigurationWrites.count(), routeWrites)
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
        try await eventually { try newListeners(since: listening).isEmpty }
    }

    // MARK: A runtime abandoned when its WebView registers

    @MainActor
    func testARuntimeAbandonedAtRegistrationEndsItsDirectSelection() async throws {
        let transport = StandInDirectTransport()
        let firstHold = PushHold(path: "/api/push/hold-1")
        let secondHold = PushHold(path: "/api/push/hold-2")
        let routeWrites = try ProxyConfigurationWrites.count()
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(
            directTransport: transport,
            pushHolds: [firstHold, secondHold]
        ) {
            listening = try listeningSockets()
        }
        let store = app.runtimeWebsiteDataStore

        let runtime = try select("instance-1", in: app)
        let target = try XCTUnwrap(transport.selections.last)
        XCTAssertEqual(target.runtime, runtime)

        // An ordinary push-management request is in flight when the root
        // view's WebView arrives: the App refuses it and returns to the
        // instance picker, without the usual stop of a runtime.
        let management = try await holdPushManagement(firstHold, in: app)
        XCTAssertFalse(app.mobileSessionCoordinator.isCurrentRuntime(runtime))
        let deselected = transport.deselectCount
        app.registerRuntimeController(
            try makeRuntimeController(app, runtime),
            for: runtime
        )
        XCTAssertTrue(isOnInstancePicker(app))
        // The selection ended with the browser.
        XCTAssertEqual(transport.deselectCount, deselected + 1)

        // The request ends. The account still has its instance, so only the
        // ended selection stands between a report and a route.
        firstHold.release.signal()
        try await management.value
        try await reportUsableWhileTheAccountIsIdle(
            transport,
            for: target,
            generation: "g1",
            runtime: runtime,
            in: app
        )
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
        XCTAssertEqual(store.proxyConfigurations.count, 0)
        XCTAssertEqual(try newListeners(since: listening), [])

        // A foreground does not select the abandoned runtime again.
        let selections = transport.selections.count
        NotificationCenter.default.post(
            name: UIApplication.didEnterBackgroundNotification,
            object: nil
        )
        NotificationCenter.default.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        XCTAssertEqual(transport.selections.count, selections)

        // The same reports do install a route for a runtime that is open.
        let second = try select("instance-2", in: app)
        let secondTarget = try XCTUnwrap(transport.selections.last)
        XCTAssertEqual(secondTarget.runtime, second)
        try await reportUsable(transport, for: secondTarget) {
            store.proxyConfigurations.count == 1
        }
        try await eventually { try newListeners(since: listening).count == 1 }

        // Abandoned the same way with its route installed: the route is
        // gone before registration returns, and the listener follows.
        let secondManagement = try await holdPushManagement(secondHold, in: app)
        app.registerRuntimeController(
            try makeRuntimeController(app, second),
            for: second
        )
        XCTAssertTrue(isOnInstancePicker(app))
        XCTAssertEqual(store.proxyConfigurations.count, 0)
        secondHold.release.signal()
        try await secondManagement.value
        try await eventually { try newListeners(since: listening).isEmpty }
        let writes = try ProxyConfigurationWrites.count()
        try await reportUsableWhileTheAccountIsIdle(
            transport,
            for: secondTarget,
            generation: "g2",
            runtime: second,
            in: app
        )
        XCTAssertEqual(try ProxyConfigurationWrites.count(), writes)
        XCTAssertEqual(store.proxyConfigurations.count, 0)
        XCTAssertEqual(try newListeners(since: listening), [])
    }

    @MainActor
    func testALateControllerForAnOlderRuntimeLeavesTheNewerSelectionAlone() async throws {
        let transport = StandInDirectTransport()
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(directTransport: transport) {
            listening = try listeningSockets()
        }
        let store = app.runtimeWebsiteDataStore

        let older = try select("instance-1", in: app)
        let newer = try select("instance-2", in: app)
        let target = try XCTUnwrap(transport.selections.last)
        XCTAssertEqual(target.runtime, newer)

        // The WebView made for the first runtime arrives after the second
        // opened. It is refused; the browser and its selection stay.
        let deselected = transport.deselectCount
        app.registerRuntimeController(
            try makeRuntimeController(app, older),
            for: older
        )
        guard case .browser(let shown) = app.route else {
            return XCTFail("The newer runtime's browser was closed.")
        }
        XCTAssertEqual(shown, newer)
        XCTAssertEqual(transport.deselectCount, deselected)
        try await reportUsable(transport, for: target) {
            store.proxyConfigurations.count == 1
        }

        _ = try await app.mobileSessionCoordinator.logout()
        trackStore(of: app)
        XCTAssertEqual(store.proxyConfigurations.count, 0)
        try await eventually { try newListeners(since: listening).isEmpty }
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

    /// The event is sent after a check of the WebView's URL, and runs later,
    /// in whatever document the WebView shows by then. The script checks the
    /// origin of the document it runs in.
    @MainActor
    func testTheEventScriptDispatchesOnlyInADocumentOfItsOrigin() async throws {
        let elsewhere = try XCTUnwrap(
            WebOrigin.parseConfigured("https://example.com")
        )
        let status = DirectPathStatus(transport: .direct, sequence: 3)
        let forInstance = try XCTUnwrap(status.eventScript(for: instance))
        let forElsewhere = try XCTUnwrap(status.eventScript(for: elsewhere))
        let recorded = #"[{"sequence":3,"transport":"direct"}]"#
        let webView = WKWebView(frame: .zero)
        let loader = PageLoader()
        webView.navigationDelegate = loader
        func changes() async throws -> String? {
            try await webView.evaluateJavaScript(
                "JSON.stringify(window.changes)"
            ) as? String
        }

        try await loader.loadHTML(
            RuntimePage.listeningDocument(title: "Instance"),
            baseURL: instance.url,
            in: webView
        )
        webView.evaluateJavaScript(forInstance, completionHandler: nil)
        try await eventually { try await changes() == recorded }

        // The document a navigation put in its place, on another origin,
        // gets the script that was meant for the instance.
        try await loader.loadHTML(
            RuntimePage.listeningDocument(title: "Elsewhere"),
            baseURL: elsewhere.url,
            in: webView
        )
        webView.evaluateJavaScript(forInstance, completionHandler: nil)
        try await Task.sleep(for: .milliseconds(300))
        let afterForeignScript = try await changes()
        XCTAssertEqual(afterForeignScript, "[]")
        // Its listener works: only the origin check stopped the event.
        webView.evaluateJavaScript(forElsewhere, completionHandler: nil)
        try await eventually { try await changes() == recorded }
    }

    @MainActor
    func testATransportChangeReachesAnInstanceChildWindowUntilItLeavesTheInstance() async throws {
        let instanceServer = try LocalHTTPServer { request in
            switch request.path {
            case "/child":
                return .html(RuntimePage.listeningDocument(title: "Child"))
            default:
                return .html(
                    RuntimePage.listeningDocument(
                        title: "Instance",
                        body: """
                            <button id="open-child" onclick="window.open('/child', '_blank')">Open</button>
                            """
                    )
                )
            }
        }
        let elsewhereServer = try LocalHTTPServer { _ in
            .html(RuntimePage.listeningDocument(title: "Elsewhere"))
        }
        let instanceURL = try await instanceServer.start()
        let elsewhereURL = try await elsewhereServer.start()
        defer {
            instanceServer.stop()
            elsewhereServer.stop()
        }
        let origin = try XCTUnwrap(
            WebOrigin(url: instanceURL, requiresHTTPS: false)
        )
        let elsewhere = try XCTUnwrap(
            WebOrigin(url: elsewhereURL, requiresHTTPS: false)
        )
        XCTAssertNotEqual(origin, elsewhere)
        let controller = WebViewController(
            instanceOrigin: origin,
            websiteDataStore: .nonPersistent(),
            navigationPolicy: NavigationPolicy(
                instanceOrigins: [origin],
                authenticationOrigins: []
            ),
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 700))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        controller.view.frame = window.bounds
        controller.view.layoutIfNeeded()
        defer {
            controller.presentedViewController?.dismiss(animated: false)
            window.rootViewController = nil
            window.isHidden = true
        }
        let primary = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        func title(of webView: WKWebView) async -> String? {
            try? await webView.evaluateJavaScript("document.title") as? String
        }
        func changes(in webView: WKWebView) async throws -> String? {
            try await webView.evaluateJavaScript(
                "JSON.stringify(window.changes ?? null)"
            ) as? String
        }

        // The instance document opens a child window on the instance.
        try await eventually { await title(of: primary) == "Instance" }
        _ = try await primary.evaluateJavaScript(
            "document.querySelector('#open-child').click(); true"
        )
        var child: WKWebView?
        try await eventually {
            let popup = controller.presentedViewController
                as? UINavigationController
            child = popup?.topViewController?.viewIfLoaded?
                .subviews.compactMap { $0 as? WKWebView }.first
            return child != nil
        }
        let childWebView = try XCTUnwrap(child)
        try await eventually { await title(of: childWebView) == "Child" }

        // Both are told.
        controller.deliverDirectTransportChanged(
            DirectPathStatus(transport: .direct, sequence: 3)
        )
        let first = #"[{"sequence":3,"transport":"direct"}]"#
        try await eventually { try await changes(in: primary) == first }
        try await eventually { try await changes(in: childWebView) == first }

        // The child leaves the instance. The primary is still told; the
        // child is not.
        childWebView.load(URLRequest(url: elsewhereURL))
        try await eventually { await title(of: childWebView) == "Elsewhere" }
        controller.deliverDirectTransportChanged(
            DirectPathStatus(transport: .tunnel, sequence: 4)
        )
        try await eventually {
            try await changes(in: primary)
                == #"[{"sequence":3,"transport":"direct"},{"sequence":4,"transport":"tunnel"}]"#
        }
        try await Task.sleep(for: .milliseconds(300))
        let untold = try await changes(in: childWebView)
        XCTAssertEqual(untold, "[]")

        // An event that was already on its way when the child left runs in
        // the new document, and dispatches nothing there.
        let late = try XCTUnwrap(
            DirectPathStatus(transport: .direct, sequence: 5)
                .eventScript(for: origin)
        )
        childWebView.evaluateJavaScript(late, completionHandler: nil)
        try await Task.sleep(for: .milliseconds(300))
        let afterLateEvent = try await changes(in: childWebView)
        XCTAssertEqual(afterLateEvent, "[]")
        // The document does listen: an event for its own origin arrives.
        let own = try XCTUnwrap(
            DirectPathStatus(transport: .direct, sequence: 5)
                .eventScript(for: elsewhere)
        )
        childWebView.evaluateJavaScript(own, completionHandler: nil)
        try await eventually {
            try await changes(in: childWebView)
                == #"[{"sequence":5,"transport":"direct"}]"#
        }
    }

    // MARK: Fixture

    private var storeIdentifiers: Set<UUID> = []

    /// An `AppModel` signed in through a local control plane, past its
    /// bootstrap. `nil` builds it exactly as the App does, with no
    /// transport argument at all. `beforeTheAppExists` runs once the control
    /// plane listens and before the `AppModel` is made, which is where a
    /// test takes the baseline of what the App itself does.
    @MainActor
    private func makeApp(
        directTransport: DirectTransporting?,
        pushHolds: [PushHold] = [],
        beforeTheAppExists: () throws -> Void = {}
    ) async throws -> AppModel {
        let server = try LocalHTTPServer { request in
            if request.method == "POST",
               let hold = pushHolds.first(where: { $0.path == request.path })
            {
                return hold.response
            }
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

        try beforeTheAppExists()

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
                pushNotificationCenter: SilentPushNotificationCenter(),
                badgeCenter: SilentBadgeCenter()
            )
        } else {
            app = AppModel(
                defaults: defaults,
                controlPlane: controlPlane,
                pushKeychain: InMemoryPushKeychainStore(),
                pushGenerationStore: InMemoryPushGenerationStore(),
                pushInstallationMarker: InMemoryPushInstallationMarkerStore(),
                pushNotificationCenter: SilentPushNotificationCenter(),
                badgeCenter: SilentBadgeCenter()
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

    /// Starts a push-management request and returns once the control plane
    /// is answering it. Until `hold.release` is signaled the request stays
    /// in flight, and the account coordinator is busy with it.
    @MainActor
    private func holdPushManagement(
        _ hold: PushHold,
        in app: AppModel
    ) async throws -> Task<Void, Error> {
        let request = Task { @MainActor in
            _ = try await app.mobileSessionCoordinator.pushManagementRequest(
                PushManagementRequest(
                    method: "POST",
                    path: hold.path,
                    headers: [:],
                    body: nil
                )
            )
        }
        await fulfillment(of: [hold.inFlight.expectation], timeout: 30)
        return request
    }

    /// Reports a usable path ten times, each at a moment the account
    /// coordinator is idle and still has `runtime` as its instance: the
    /// moments at which the App's lease check would admit a route, so that
    /// only an ended selection can be what refuses these.
    @MainActor
    private func reportUsableWhileTheAccountIsIdle(
        _ transport: StandInDirectTransport,
        for target: DirectTarget,
        generation: String,
        runtime: MobileRuntimeContext,
        in app: AppModel
    ) async throws {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(30))
        var reported = 0
        while reported < 10 {
            guard clock.now < deadline else {
                XCTFail("The account coordinator never became idle.")
                throw DirectTestTimeout()
            }
            // Checked and reported in one turn of the main actor.
            if app.mobileSessionCoordinator.isCurrentRuntime(runtime) {
                transport.report(.usable(generation: generation), for: target)
                reported += 1
            }
            try await Task.sleep(for: .milliseconds(50))
        }
    }

    @MainActor
    private func isOnInstancePicker(_ app: AppModel) -> Bool {
        if case .onboarding(.instances) = app.route {
            return true
        }
        return false
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
        let controller = try makeRuntimeController(app, runtime)
        app.registerRuntimeController(controller, for: runtime)
        let page = try RuntimePage(controller: controller)
        try await page.load(
            RuntimePage.recordingDocument,
            baseURL: runtime.instance.url
        )
        return page
    }

    /// The runtime controller the root view would create for `runtime`.
    @MainActor
    private func makeRuntimeController(
        _ app: AppModel,
        _ runtime: MobileRuntimeContext
    ) throws -> WebViewController {
        let origin = try XCTUnwrap(WebOrigin(url: runtime.instance.url))
        return WebViewController(
            instanceOrigin: origin,
            websiteDataStore: app.runtimeWebsiteDataStore,
            directStatus: app.directPaths.status(for:),
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
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

/// A push-management answer the control plane starts and does not finish
/// until `release` is signaled, so the request stays in flight until then.
private struct PushHold: Sendable {
    let path: String
    let inFlight: FixtureEvent
    let release = DispatchSemaphore(value: 0)

    init(path: String) {
        self.path = path
        inFlight = FixtureEvent(description: "\(path) in flight")
    }

    /// Larger than what the server sends before `release`.
    var response: LocalHTTPResponse {
        LocalHTTPResponse(
            status: 200,
            headers: [("Content-Type", "application/json")],
            body: Data(
                ("{\"pad\":\"" + String(repeating: "x", count: 100_000) + "\"}").utf8
            ),
            headersSentEvent: inFlight,
            bodyRelease: release
        )
    }
}

/// One runtime controller in a window, and its primary WebView.
@MainActor
private final class RuntimePage {
    /// Records each transport change dispatched in it, with no use of the
    /// bridge, so it works in any document.
    nonisolated static func listeningDocument(
        title: String,
        body: String = ""
    ) -> String {
        """
        <!doctype html>
        <title>\(title)</title>
        <script>
          window.changes = [];
          window.addEventListener(
            "avibe:native:direct.transportChanged",
            (event) => window.changes.push(event.detail)
          );
        </script>
        \(body)
        """
    }

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

/// The App icon badge is not this suite's subject, and a test sets none.
private final class SilentBadgeCenter: AppBadgeCentering {
    func badgeAllowed() async -> Bool {
        false
    }

    func setBadgeCount(_ count: Int) async throws {}
}
