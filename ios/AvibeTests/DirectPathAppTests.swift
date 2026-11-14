import Combine
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

    /// Built exactly as `AvibeApp` builds it, with no transport argument.
    /// Observed from before the App exists and at every step: no `direct`
    /// capability, `direct.status` unsupported, no event, WebKit's
    /// `proxyConfigurations` setter never called on any store, and no
    /// listening socket. The next test, with a stand-in transport, is the
    /// positive control: the same capability, count, and scan do move.
    @MainActor
    func testTheAppAsShippedHasNoDirectCapabilityNoListenerAndNoRouteWrite() async throws {
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

        let app = try await makeApp(directTransport: nil) {
            listening = try listeningSockets()
        }
        XCTAssertNil(app.directStatusProvider)
        let firstStore = app.runtimeWebsiteDataStore
        try assertUntouched("launched and signed in")

        // A runtime opens at once, as before: there is no route to prepare.
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        guard case .browser(let runtime) = app.route else {
            return XCTFail("The runtime did not open at once.")
        }
        let page = try await showRuntimePage(app, runtime)
        let surface = try await page.directSurface()
        XCTAssertEqual(surface, #"{"capability":null,"status":"unsupported"}"#)
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
        try await Task.sleep(for: .milliseconds(200))
        let changes = try await page.changes()
        XCTAssertEqual(changes, "[]")
        page.close()

        _ = try await select("instance-2", in: app)
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
        // Nothing of the listener fault exists either: no marker in the
        // App's (isolated) preferences, and no fault state.
        XCTAssertFalse(app.directListenerFault)
        XCTAssertNil(
            try XCTUnwrap(appDefaults).object(forKey: UserDefaultsDirectFaultMarker.key)
        )
    }

    // MARK: The listener fault

    @MainActor
    func testWithoutATransportTheFaultMarkerIsNeverReadOrWritten() async throws {
        let marker = FaultMarkerSpy(marked: true)
        let app = try await makeApp(directTransport: nil, directFaultMarker: marker)

        _ = try await select("instance-1", in: app)

        XCTAssertNil(app.directStatusProvider)
        XCTAssertEqual(marker.takes, 0)
        XCTAssertEqual(marker.marks, 0)
        XCTAssertTrue(marker.isMarked)
    }

    /// The launch after a fault is decided in `init`, before any route or
    /// first load: no controller, so no capability, no listener, no route
    /// write, and a runtime that opens at once. It consumes the marker, so
    /// the launch after it uses the transport again.
    @MainActor
    func testTheLaunchAfterAFaultUsesNoListenerAndTheOneAfterThatUsesTheTransport() async throws {
        try assertSocketScanSeesListeners()
        let marker = FaultMarkerSpy(marked: true)
        let transport = StandInDirectTransport()
        let routeWrites = try ProxyConfigurationWrites.count()
        var listening: Set<ListeningSocket> = []
        let bypassed = try await makeApp(
            directTransport: transport,
            directFaultMarker: marker
        ) {
            listening = try listeningSockets()
        }

        XCTAssertNil(bypassed.directStatusProvider)
        XCTAssertEqual(marker.takes, 1)
        XCTAssertFalse(marker.isMarked)
        _ = try bypassed.mobileSessionCoordinator.selectInstance(id: "instance-1")
        guard case .browser = bypassed.route else {
            return XCTFail("The runtime did not open at once.")
        }
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
        XCTAssertEqual(try newListeners(since: listening), [])
        XCTAssertEqual(transport.selections.count, 0)

        // The next launch, with the same preferences, uses the transport.
        let next = try await makeApp(
            directTransport: transport,
            directFaultMarker: marker
        )
        XCTAssertNotNil(next.directStatusProvider)
        XCTAssertEqual(marker.takes, 2)
        _ = try await select("instance-1", in: next)
        XCTAssertEqual(transport.selections.count, 1)
    }

    func testTheFaultMarkerIsOneVersionedKeyAndAnyStoredValueCounts() throws {
        let suiteName = "DirectPathAppTests.marker.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let marker = UserDefaultsDirectFaultMarker(defaults: defaults)

        XCTAssertFalse(marker.take())
        marker.mark()
        XCTAssertEqual(defaults.object(forKey: UserDefaultsDirectFaultMarker.key) as? Int, 1)
        XCTAssertTrue(marker.take())
        XCTAssertNil(defaults.object(forKey: UserDefaultsDirectFaultMarker.key))
        XCTAssertFalse(marker.take())

        // A value this version cannot read still means one launch without
        // the listener, and is cleared.
        defaults.set("unreadable", forKey: UserDefaultsDirectFaultMarker.key)
        XCTAssertTrue(marker.take())
        XCTAssertNil(defaults.object(forKey: UserDefaultsDirectFaultMarker.key))
    }

    @MainActor
    func testAListenerFaultCoversOnlyARoutedRuntimeOnceAndIsMarkedOnce() async throws {
        let marker = FaultMarkerSpy(marked: false)
        let transport = StandInDirectTransport()
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(directTransport: transport, directFaultMarker: marker) {
            listening = try listeningSockets()
        }
        let first = try await select("instance-1", in: app)
        let socket = try XCTUnwrap(try newListeners(since: listening).first)
        let port = try XCTUnwrap(NWEndpoint.Port(rawValue: socket.port))
        XCTAssertFalse(app.directFaultCovers(first))
        var shownWhileMarking: [Bool] = []
        // The App owns this spy; its observer must not retain the App back.
        marker.whileMarking = { [weak app] in
            guard let app else {
                return XCTFail("The App was released before the fault was marked")
            }
            shownWhileMarking.append(app.directListenerFault)
        }
        addTeardownBlock { @MainActor [weak app] in
            XCTAssertNil(app, "The fault-marker observer must not keep the fixture App alive")
        }

        // The App's port is taken while it is in the background: the return
        // cannot re-bind it, which is the listener fault.
        NotificationCenter.default.post(
            name: UIApplication.didEnterBackgroundNotification,
            object: nil
        )
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: socket.port,
            reuseAddress: true
        )
        defer { squatter.close() }
        NotificationCenter.default.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        try await eventually { app.directListenerFault }
        // The marker was written before the fault was shown.
        XCTAssertEqual(shownWhileMarking, [false])
        // Retry does not rebuild under the reopen message, and the runtime
        // that was open keeps its WebView under it.
        XCTAssertEqual(app.directLoadGate(for: first), .fault)
        XCTAssertFalse(app.mayReload(first))
        XCTAssertFalse(app.authorizeReload(of: first))
        XCTAssertFalse(app.presentsDirectFaultOnly(first))
        // Another return does not report it again.
        NotificationCenter.default.post(
            name: UIApplication.didEnterBackgroundNotification,
            object: nil
        )
        NotificationCenter.default.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        try await Task.sleep(for: .milliseconds(300))

        XCTAssertTrue(app.directListenerFault)
        XCTAssertEqual(marker.marks, 1)
        XCTAssertTrue(app.directFaultCovers(first))
        // A page behind the fault gets the bridge's `failed`, never a
        // status that reads as ready.
        let page = try await showRuntimePage(app, first)
        let surface = try await page.directSurface()
        XCTAssertEqual(surface, #"{"capability":1,"status":"failed"}"#)
        page.close()

        // An instance the fault left unrouted opens and is not covered: an
        // ordinary page, with its WebView and its Retry.
        let second = try await select("instance-2", in: app)
        XCTAssertFalse(app.directFaultCovers(second))
        XCTAssertFalse(app.presentsDirectFaultOnly(second))
        XCTAssertTrue(app.mayReload(second))
        XCTAssertTrue(app.directListenerFault)
        XCTAssertEqual(marker.marks, 1)

        // The routed instance opened again under the fault: the reopen
        // message alone, with no WebView made for it and no Retry.
        let reopened = try await select("instance-1", in: app)
        XCTAssertTrue(app.directFaultCovers(reopened))
        XCTAssertTrue(app.presentsDirectFaultOnly(reopened))
        XCTAssertFalse(app.mayReload(reopened))
        XCTAssertEqual(marker.marks, 1)
    }

    // MARK: The creation and reload boundaries

    private func enterBackground() {
        NotificationCenter.default.post(
            name: UIApplication.didEnterBackgroundNotification,
            object: nil
        )
    }

    private func enterForeground() {
        NotificationCenter.default.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
    }

    /// Lets the main actor run what is already scheduled, then a little
    /// longer: for checks that something did not happen.
    @MainActor
    private func settle() async throws {
        for _ in 0..<200 {
            await Task.yield()
        }
        try await Task.sleep(for: .milliseconds(200))
    }

    /// A routed instance while the listener is closed for the background or
    /// being bound again: Retry is not allowed and an open makes no browser,
    /// both decided at once and by no wait. The held open stays on the
    /// loading route; when the return settles, it becomes the browser if
    /// the listener serves, or the reopen message alone if it failed.
    @MainActor
    func testARoutedOpenAndRetryAreHeldUntilTheReturnSettles() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let runtime = try await select("instance-1", in: app)
        XCTAssertEqual(app.directLoadGate(for: runtime), .open)
        XCTAssertTrue(app.mayReload(runtime))

        enterBackground()
        XCTAssertEqual(app.directLoadGate(for: runtime), .held)
        XCTAssertFalse(app.mayReload(runtime))

        // The instance is opened again (a push tap): nothing is created
        // against the closed port.
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await settle()
        XCTAssertEqual(app.route, .loading)

        // The return starts the re-bind; in that same turn, still held.
        enterForeground()
        XCTAssertEqual(app.route, .loading)
        XCTAssertFalse(app.mayReload(runtime))

        let reopened = try await waitForBrowser("instance-1", in: app)
        if app.directListenerFault {
            XCTAssertTrue(app.presentsDirectFaultOnly(reopened))
            XCTAssertFalse(app.mayReload(reopened))
        } else {
            XCTAssertEqual(app.directLoadGate(for: reopened), .open)
            XCTAssertFalse(app.presentsDirectFaultOnly(reopened))
            XCTAssertTrue(app.mayReload(reopened))
        }
    }

    /// A held open belongs to the runtime identity it was made under: an
    /// open that replaces it, or a sign-out, ends it, and the return then
    /// creates nothing for it.
    @MainActor
    func testAHeldOpenIsEndedByANewerOpenOrASignOut() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let first = try await select("instance-1", in: app)

        enterBackground()
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await settle()
        XCTAssertEqual(app.route, .loading)

        // A newer open, of an instance with no route on this store: it
        // loads on WebKit's normal path at once, background or not.
        let second = try await select("instance-2", in: app)
        XCTAssertEqual(app.directLoadGate(for: second), .open)
        enterForeground()
        try await eventually { app.directLoadGate(for: first) != .held }
        try await settle()
        XCTAssertEqual(app.route, .browser(second))

        // Held again, then signed out: the return leaves the account page.
        guard !app.directListenerFault else {
            return
        }
        enterBackground()
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await settle()
        XCTAssertEqual(app.route, .loading)
        _ = try await app.mobileSessionCoordinator.logout()
        trackStore(of: app)
        // The sign-out itself ends the held open: the spinner does not wait
        // for the return.
        XCTAssertNil(app.route.runtime)
        XCTAssertNotEqual(app.route, .loading)
        enterForeground()
        try await settle()
        XCTAssertNil(app.route.runtime)
        XCTAssertNotEqual(app.route, .loading)
    }

    /// A held open whose return fails: the loading route ends as the reopen
    /// message alone, with no WebView and no Retry, the fault is marked
    /// once, and an instance without a route still opens as an ordinary
    /// page.
    @MainActor
    func testAHeldOpenEndsAsTheReopenMessageWhenTheReBindFails() async throws {
        let marker = FaultMarkerSpy(marked: false)
        let transport = StandInDirectTransport()
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(directTransport: transport, directFaultMarker: marker) {
            listening = try listeningSockets()
        }
        _ = try await select("instance-1", in: app)
        let socket = try XCTUnwrap(try newListeners(since: listening).first)
        let port = try XCTUnwrap(NWEndpoint.Port(rawValue: socket.port))

        enterBackground()
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: socket.port,
            reuseAddress: true
        )
        defer { squatter.close() }
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await settle()
        XCTAssertEqual(app.route, .loading)

        enterForeground()
        try await eventually { app.directListenerFault }
        let runtime = try await waitForBrowser("instance-1", in: app)
        XCTAssertTrue(app.presentsDirectFaultOnly(runtime))
        XCTAssertEqual(app.directLoadGate(for: runtime), .fault)
        XCTAssertFalse(app.mayReload(runtime))
        XCTAssertEqual(marker.marks, 1)

        let second = try await select("instance-2", in: app)
        XCTAssertFalse(app.presentsDirectFaultOnly(second))
        XCTAssertTrue(app.mayReload(second))
        XCTAssertEqual(marker.marks, 1)
    }

    /// Without a transport nothing is gated: Retry reloads at once, and the
    /// runtime's WebView is given no gate at all, as before.
    @MainActor
    func testWithoutATransportNothingIsGated() async throws {
        let app = try await makeApp(directTransport: nil)
        let runtime = try await select("instance-1", in: app)
        XCTAssertTrue(app.mayReload(runtime))
        XCTAssertTrue(app.authorizeReload(of: runtime))
        XCTAssertEqual(app.directLoadGate(for: runtime), .open)
        XCTAssertNil(app.directLoadGateProvider(for: runtime))
        XCTAssertFalse(app.presentsDirectFaultOnly(runtime))
        enterBackground()
        XCTAssertTrue(app.mayReload(runtime))
        enterForeground()
        XCTAssertTrue(app.mayReload(runtime))
    }

    /// The WebView's own automatic loads (its first load, and the reload
    /// after its web content process ended) pass the same gate where they
    /// are made. Held: nothing is loaded, and one pending load is kept
    /// however often it is asked for; it is made once when the gate opens.
    /// The fault, or the controller's invalidation, drops it.
    @MainActor
    func testAnAutomaticLoadThatMeetsAHeldGateIsMadeOnceWhenTheGateOpens() async throws {
        let server = try LocalHTTPServer { _ in
            .html(RuntimePage.listeningDocument(title: "Instance"))
        }
        let url = try await server.start()
        defer { server.stop() }
        let origin = try XCTUnwrap(WebOrigin(url: url, requiresHTTPS: false))
        var gate = DirectLoadGate.held
        var starts = 0
        var finishes = 0
        let controller = WebViewController(
            instanceOrigin: origin,
            websiteDataStore: .nonPersistent(),
            navigationPolicy: NavigationPolicy(
                instanceOrigins: [origin],
                authenticationOrigins: []
            ),
            directLoadGate: { gate },
            didStartLoading: { starts += 1 },
            didFinishLoading: { finishes += 1 },
            didFailLoading: {}
        )
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 700))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        defer {
            window.rootViewController = nil
            window.isHidden = true
        }
        let primary = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )

        // The first load met a held gate: nothing was loaded, and a change
        // that leaves the gate held loads nothing either.
        controller.directAvailabilityDidChange()
        try await settle()
        XCTAssertEqual(starts, 0)
        XCTAssertNil(primary.url)

        // The gate opens: the one load, once.
        gate = .open
        controller.directAvailabilityDidChange()
        try await eventually { finishes == 1 }
        controller.directAvailabilityDidChange()
        try await settle()
        XCTAssertEqual(starts, 1)

        // The web content process ends while held, reported more than once:
        // nothing is reloaded until the gate opens, and then once.
        gate = .held
        controller.webViewWebContentProcessDidTerminate(primary)
        controller.webViewWebContentProcessDidTerminate(primary)
        try await settle()
        XCTAssertEqual(starts, 1)
        gate = .open
        controller.directAvailabilityDidChange()
        try await eventually { starts == 2 }
        controller.directAvailabilityDidChange()
        try await settle()
        XCTAssertEqual(starts, 2)

        // With an open gate the reload is made at once, as before.
        controller.webViewWebContentProcessDidTerminate(primary)
        try await eventually { starts == 3 }

        // The fault drops a held load for good.
        gate = .held
        controller.webViewWebContentProcessDidTerminate(primary)
        gate = .fault
        controller.directAvailabilityDidChange()
        gate = .open
        controller.directAvailabilityDidChange()
        try await settle()
        XCTAssertEqual(starts, 3)

        // So does the controller's invalidation.
        gate = .held
        controller.webViewWebContentProcessDidTerminate(primary)
        _ = controller.invalidateForAccountTransition()
        gate = .open
        controller.directAvailabilityDidChange()
        try await settle()
        XCTAssertEqual(starts, 3)
    }

    /// The App allowed the open, and the App went to the background before
    /// the WebView's first load: that load is held where it is made, the
    /// return makes it once, and a later round trip makes no other. A
    /// reload held when the runtime is replaced is never made.
    @MainActor
    func testAHeldFirstLoadIsMadeOnceByTheReturnAndNeverForAReplacedRuntime() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let runtime = try await select("instance-1", in: app)
        let origin = try XCTUnwrap(WebOrigin(url: runtime.instance.url))
        var starts = 0
        let controller = WebViewController(
            instanceOrigin: origin,
            websiteDataStore: app.runtimeWebsiteDataStore,
            directStatus: app.directStatusProvider,
            directLoadGate: app.directLoadGateProvider(for: runtime),
            didStartLoading: { starts += 1 },
            didFinishLoading: {},
            didFailLoading: {}
        )
        app.registerRuntimeController(controller, for: runtime)

        enterBackground()
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 700))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        defer {
            window.rootViewController = nil
            window.isHidden = true
        }
        try await settle()
        XCTAssertEqual(starts, 0)

        enterForeground()
        try await eventually { app.directLoadGate(for: runtime) != .held }
        guard !app.directListenerFault else {
            // The fault dropped the held load.
            try await settle()
            XCTAssertEqual(starts, 0)
            return
        }
        try await eventually { starts == 1 }

        // Consumed: another background and return loads nothing more.
        enterBackground()
        enterForeground()
        try await eventually { app.directLoadGate(for: runtime) != .held }
        try await settle()
        guard !app.directListenerFault else {
            return
        }
        XCTAssertEqual(starts, 1)

        // A replacement selection ends the held runtime before the return.
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-2")
        enterForeground()
        try await eventually { app.directLoadGate(for: runtime) != .held }
        try await settle()
        XCTAssertEqual(starts, 1)
    }

    @MainActor
    func testFailureInstancePickerCoversTheSelectedRuntimeWithoutStoppingIt()
        async throws
    {
        let app = try await makeApp(directTransport: nil)
        let runtime = try await select("instance-1", in: app)
        let identity = app.runtimeIdentity

        // The native failure action uses the same sheet as the avatar and
        // leaves the selected route available for Close/current-instance.
        app.showInstancePicker()
        XCTAssertEqual(app.route, .accountOverBrowser(runtime))
        XCTAssertEqual(app.runtimeIdentity, identity)
        XCTAssertTrue(app.mobileSessionCoordinator.isCurrentRuntime(runtime))
        XCTAssertTrue(app.returnToRuntime())
        XCTAssertEqual(app.route, .browser(runtime))
    }

    /// the App is in the background with a route, open again once the
    /// listener serves.
    @MainActor
    func testTheRuntimeWebViewIsGivenTheAppsLoadGate() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let runtime = try await select("instance-1", in: app)
        let provider = try XCTUnwrap(app.directLoadGateProvider(for: runtime))
        XCTAssertEqual(provider(), .open)
        enterBackground()
        XCTAssertEqual(provider(), .held)
        enterForeground()
        XCTAssertEqual(provider(), .held)
        try await eventually { provider() != .held }
        XCTAssertEqual(provider(), app.directListenerFault ? .fault : .open)
    }

    /// A child window is presented above the view the reopen message
    /// covers; the fault takes it away.
    @MainActor
    func testTheListenerFaultTakesAwayAPresentedChildWindow() async throws {
        let server = try LocalHTTPServer { request in
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
        let url = try await server.start()
        defer { server.stop() }
        let origin = try XCTUnwrap(WebOrigin(url: url, requiresHTTPS: false))
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
        try await eventually {
            (try? await primary.evaluateJavaScript("document.title") as? String) == "Instance"
        }
        _ = try await primary.evaluateJavaScript(
            "document.querySelector('#open-child').click(); true"
        )
        try await eventually { controller.presentedViewController != nil }

        controller.discardPopupsForListenerFault()

        try await eventually { controller.presentedViewController == nil }
    }

    func testTheListenerFaultCoversTheRuntimeWithoutRetryOrSpinner() {
        XCTAssertEqual(
            BrowserOverlay(isLoading: true, failed: true, directFault: true),
            .directFault
        )
        XCTAssertEqual(
            BrowserOverlay(isLoading: true, failed: false, directFault: true),
            .directFault
        )
        XCTAssertEqual(
            BrowserOverlay(isLoading: false, failed: true, directFault: false),
            .loadFailure
        )
        XCTAssertEqual(
            BrowserOverlay(isLoading: true, failed: false, directFault: false),
            .loading
        )
        XCTAssertEqual(
            BrowserOverlay(isLoading: false, failed: false, directFault: false),
            .none
        )
    }

    func testTheReopenMessageIsTheApprovedCopyInBothLocales() throws {
        func localized(_ language: String) throws -> String {
            let path = try XCTUnwrap(Bundle.main.path(forResource: language, ofType: "lproj"))
            let bundle = try XCTUnwrap(Bundle(path: path))
            return bundle.localizedString(
                forKey: "browser.direct_fault.title",
                value: "missing",
                table: nil
            )
        }

        XCTAssertEqual(try localized("en"), "Connection lost — reopen the app")
        XCTAssertEqual(try localized("zh-Hans"), "连接中断，请重新打开 App")
    }

    // MARK: The App with a transport

    @MainActor
    func testWithATransportTheRouteIsWrittenBeforeTheFirstLoadAndTheStatusReachesThePage() async throws {
        let transport = StandInDirectTransport()
        let routeWrites = try ProxyConfigurationWrites.count()
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(directTransport: transport) {
            listening = try listeningSockets()
        }
        let firstStore = app.runtimeWebsiteDataStore
        // What the store's route is each time a browser route is set.
        var routesAtOpen: [[String]] = []
        let opened = app.$route.sink { route in
            if case .browser = route {
                routesAtOpen.append(
                    firstStore.proxyConfigurations.flatMap(\.matchDomains)
                )
            }
        }
        defer { opened.cancel() }

        // The runtime waits for its route.
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        XCTAssertEqual(app.route, .loading)
        let runtime = try await waitForBrowser("instance-1", in: app)
        XCTAssertEqual(routesAtOpen, [["instance-one.example"]])
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites + 1)
        XCTAssertEqual(try newListeners(since: listening).map(\.address), ["127.0.0.1"])

        let page = try await showRuntimePage(app, runtime)
        let surface = try await page.directSurface()
        XCTAssertEqual(
            surface,
            #"{"capability":1,"status":{"closure":0,"preferred":"tunnel","sequence":0}}"#
        )
        let target = try XCTUnwrap(transport.selections.last)
        XCTAssertEqual(target.runtime, runtime)
        XCTAssertEqual(target.storeIdentifier, app.websiteDataStore.identifier)

        // The transport finds a path: the page is told, and asks.
        transport.report(.usable(generation: "g1"), for: target)
        try await eventually {
            try await page.changes()
                == #"[{"closure":0,"preferred":"direct","sequence":1}]"#
        }
        let after = try await page.directStatus()
        XCTAssertEqual(after, #"{"closure":0,"preferred":"direct","sequence":1}"#)
        page.close()

        // Another instance joins the same route; nothing is removed.
        _ = try await select("instance-2", in: app)
        XCTAssertEqual(
            firstStore.proxyConfigurations.first?.matchDomains,
            ["instance-one.example", "instance-two.example"]
        )
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites + 2)
        // A late report for the first instance changes nothing.
        transport.report(.lost, for: target)
        XCTAssertEqual(directStatus(app, instance)?.sequence, 2)

        // Signing out rotates the store; the new one starts with no route
        // and the old one keeps its own until it is discarded.
        let writes = try ProxyConfigurationWrites.count()
        _ = try await app.mobileSessionCoordinator.logout()
        trackStore(of: app)
        XCTAssertFalse(app.runtimeWebsiteDataStore === firstStore)
        XCTAssertEqual(app.runtimeWebsiteDataStore.proxyConfigurations.count, 0)
        XCTAssertEqual(try ProxyConfigurationWrites.count(), writes)
    }

    @MainActor
    func testAnOpenReplacedWhileItsRouteWasPreparedNeverShowsItsBrowser() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        var browsers: [String] = []
        let opened = app.$route.sink { route in
            if case .browser(let runtime) = route {
                browsers.append(runtime.instance.id)
            }
        }
        defer { opened.cancel() }

        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-2")
        _ = try await waitForBrowser("instance-2", in: app)
        try await Task.sleep(for: .milliseconds(300))

        XCTAssertEqual(browsers, ["instance-2"])
        XCTAssertEqual(
            transport.selections.map(\.runtime.instance.id),
            ["instance-2"]
        )
    }

    // MARK: Account over the runtime while a push request runs

    /// The avatar while a push request the App made on its own is running:
    /// Account covers the runtime (it used to close it and show the plain
    /// account page), and Back returns to it.
    @MainActor
    func testAccountCoversTheRuntimeAndReturnsToItWhileAPushRequestRuns() async throws {
        let hold = PushHold(path: "/api/push/hold-account")
        let app = try await makeApp(directTransport: nil, pushHolds: [hold])
        let runtime = try await select("instance-1", in: app)
        // The App holds its runtime's controller weakly; the view would.
        let controller = try makeRuntimeController(app, runtime)
        app.registerRuntimeController(controller, for: runtime)
        XCTAssertEqual(app.route, .browser(runtime))

        let management = try await holdPushManagement(hold, in: app)
        app.showAccount()
        XCTAssertEqual(app.route, .accountOverBrowser(runtime))
        XCTAssertTrue(app.returnToRuntime())
        XCTAssertEqual(app.route, .browser(runtime))

        hold.release.signal()
        try await management.value
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertFalse(app.mobileSessionCoordinator.isAccountSurfaceActive)
        withExtendedLifetime(controller) {}
    }

    // MARK: A runtime abandoned when its WebView registers

    @MainActor
    func testARuntimeAbandonedAtRegistrationEndsItsDirectSelection() async throws {
        let transport = StandInDirectTransport()
        let hold = PushHold(path: "/api/push/hold-1")
        let app = try await makeApp(
            directTransport: transport,
            pushHolds: [hold]
        )

        let runtime = try await select("instance-1", in: app)
        let target = try XCTUnwrap(transport.selections.last)
        XCTAssertEqual(target.runtime, runtime)

        // An ordinary push-management request is in flight when the root
        // view's WebView arrives: the App refuses it and returns to the
        // instance picker, without the usual stop of a runtime.
        let management = try await holdPushManagement(hold, in: app)
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
        // ended selection stands between a report and the direct path.
        hold.release.signal()
        try await management.value
        try await reportUsableWhileTheAccountIsIdle(
            transport,
            for: target,
            generation: "g1",
            runtime: runtime,
            in: app
        )
        XCTAssertEqual(directStatus(app, instance)?.preferred, .tunnel)

        // A return from the background does not select it again.
        let selections = transport.selections.count
        NotificationCenter.default.post(
            name: UIApplication.didEnterBackgroundNotification,
            object: nil
        )
        NotificationCenter.default.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertEqual(transport.selections.count, selections)
    }

    @MainActor
    func testALateControllerForAnOlderRuntimeLeavesTheNewerSelectionAlone() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let second = try XCTUnwrap(WebOrigin.parseConfigured("https://instance-two.example"))

        let older = try await select("instance-1", in: app)
        let newer = try await select("instance-2", in: app)
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
        try await eventually {
            if app.mobileSessionCoordinator.isCurrentRuntime(newer) {
                transport.report(.usable(generation: "g1"), for: target)
            }
            return self.directStatus(app, second)?.preferred == .direct
        }
    }

    // MARK: The transport's rendezvous WebView

    /// The rendezvous request runs in the primary WebView of the runtime
    /// the controller selected, and only for the controller's current
    /// target.
    @MainActor
    func testTheRendezvousWebViewIsTheSelectedRuntimesPrimaryAndNothingElse() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let runtime = try await select("instance-1", in: app)
        let target = try XCTUnwrap(transport.selections.last)
        let selection = DirectIrohSelection(target)
        XCTAssertEqual(selection.number, target.selection)
        XCTAssertEqual(selection.instanceID, "instance-1")
        XCTAssertEqual(selection.host, "instance-one.example")
        XCTAssertEqual(selection.storeIdentifier, app.websiteDataStore.identifier)

        // Selected, but the runtime's WebView is not made yet.
        XCTAssertNil(app.directRendezvousWebView(for: selection))

        let controller = try makeRuntimeController(app, runtime)
        app.registerRuntimeController(controller, for: runtime)
        let page = try RuntimePage(controller: controller)
        let primary = try XCTUnwrap(controller.livePrimaryWebView)
        XCTAssertTrue(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first === primary
        )
        XCTAssertTrue(app.directRendezvousWebView(for: selection) === primary)

        // A selection that differs from the target in any field is refused.
        let others = [
            DirectIrohSelection(
                number: selection.number + 1,
                instanceID: selection.instanceID,
                host: selection.host,
                storeIdentifier: selection.storeIdentifier
            ),
            DirectIrohSelection(
                number: selection.number,
                instanceID: "instance-2",
                host: selection.host,
                storeIdentifier: selection.storeIdentifier
            ),
            DirectIrohSelection(
                number: selection.number,
                instanceID: selection.instanceID,
                host: "instance-two.example",
                storeIdentifier: selection.storeIdentifier
            ),
            DirectIrohSelection(
                number: selection.number,
                instanceID: selection.instanceID,
                host: selection.host,
                storeIdentifier: UUID()
            ),
        ]
        for other in others {
            XCTAssertNil(app.directRendezvousWebView(for: other), "\(other)")
        }

        // Covered by Account, the runtime keeps its document and its WebView.
        app.showAccount()
        XCTAssertEqual(app.route, .accountOverBrowser(runtime))
        XCTAssertTrue(app.directRendezvousWebView(for: selection) === primary)
        XCTAssertTrue(app.returnToRuntime())
        XCTAssertTrue(app.directRendezvousWebView(for: selection) === primary)

        // Another instance: the first target is over, its WebView is
        // released, and the second runtime has no WebView yet.
        page.close()
        _ = try await select("instance-2", in: app)
        let second = DirectIrohSelection(try XCTUnwrap(transport.selections.last))
        XCTAssertNotEqual(second, selection)
        XCTAssertNil(controller.livePrimaryWebView)
        XCTAssertNil(app.directRendezvousWebView(for: selection))
        XCTAssertNil(app.directRendezvousWebView(for: second))

        // The instance picker now opens the instances sheet over the
        // selected runtime; it does not end the selection by itself.
        app.showInstancePicker()
        guard case .accountOverBrowser(let covered) = app.route else {
            return XCTFail("The instance picker did not open the sheet")
        }
        XCTAssertEqual(covered.instance.id, "instance-2")
    }

    /// Without a transport there is no target, so no WebView is handed
    /// out, and a source bound to the App asks nothing.
    @MainActor
    func testWithoutATransportNoRendezvousWebViewIsHandedOut() async throws {
        let app = try await makeApp(directTransport: nil)
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        guard case .browser(let runtime) = app.route else {
            return XCTFail("The runtime did not open at once.")
        }
        let controller = try makeRuntimeController(app, runtime)
        app.registerRuntimeController(controller, for: runtime)
        let page = try RuntimePage(controller: controller)
        defer { page.close() }
        XCTAssertNotNil(controller.livePrimaryWebView)

        let unbound = DirectRendezvousWebViewSource()
        let bound = DirectRendezvousWebViewSource()
        bound.appModel = app
        let client = try XCTUnwrap(DirectEndpointID(bytes: Data(repeating: 0xAB, count: 32)))
        for number in 0..<4 {
            let selection = DirectIrohSelection(
                number: number,
                instanceID: "instance-1",
                host: "instance-one.example",
                storeIdentifier: app.websiteDataStore.identifier
            )
            XCTAssertNil(app.directRendezvousWebView(for: selection))
            for source in [unbound, bound] {
                do {
                    _ = try await source.fetch(selection, client)
                    XCTFail("A rendezvous request was made.")
                } catch {
                    XCTAssertEqual(error as? DirectRendezvousFetchError, .pageNotReady)
                }
            }
        }
    }

    /// The iroh transport, over a stand-in for the binding, as the App's
    /// transport: the controller's target reaches it as its selection, its
    /// rendezvous request gets the runtime's primary WebView, its report
    /// reaches the page, a stream and a failed candidate pass through the
    /// seam, and signing out closes its connection.
    @MainActor
    func testTheIrohTransportBehindTheSeamWorksForTheControllersTarget() async throws {
        let log = DirectIrohEventLog()
        let engine = DirectIrohFakeEngine(log: log)
        let scheduler = DirectIrohManualScheduler()
        let source = DirectRendezvousWebViewSource()
        let asked = RendezvousRequests()
        let transport = DirectIrohTransport(
            engine: engine,
            fetchRendezvous: { selection, clientEndpointID in
                let webView = source.webView(for: selection)
                asked.record(selection, webView: webView)
                guard webView != nil else {
                    throw DirectRendezvousFetchError.pageNotReady
                }
                return DirectIrohFixtures.ready(
                    generation: "g1",
                    client: clientEndpointID,
                    instanceID: selection.instanceID,
                    hostname: selection.host
                )
            },
            network: DirectIrohFakeNetwork(),
            scheduler: scheduler,
            timing: Self.patientIrohTiming,
            isInBackground: false
        )
        let app = try await makeApp(directTransport: transport)
        source.appModel = app
        let connection = DirectIrohFakeConnection(
            log: log,
            remote: DirectIrohFixtures.instanceEndpoint,
            answers: [
                DirectIrohFakeConnection.established,
                Data("hello".utf8),
                DirectIrohFakeConnection.established,
            ]
        )
        engine.prepare().connection.resolve(connection)

        // The runtime's WebView is not made yet: the request is refused
        // before anything is bound, and asked again soon.
        let runtime = try await select("instance-1", in: app)
        try await eventually { asked.requests.count == 1 }
        let selection = try XCTUnwrap(asked.requests.first?.selection)
        XCTAssertEqual(selection.instanceID, "instance-1")
        XCTAssertEqual(selection.host, "instance-one.example")
        XCTAssertEqual(selection.storeIdentifier, app.websiteDataStore.identifier)
        XCTAssertNil(asked.requests.first?.webView)
        XCTAssertEqual(log.all, ["key"])
        XCTAssertEqual(scheduler.pending, [Self.patientIrohTiming.pageRetrySeconds])

        let controller = try makeRuntimeController(app, runtime)
        app.registerRuntimeController(controller, for: runtime)
        let page = try RuntimePage(controller: controller)
        try await page.load(RuntimePage.recordingDocument, baseURL: runtime.instance.url)
        let primary = try XCTUnwrap(controller.livePrimaryWebView)

        // Asked again, the request gets the primary WebView; the path comes
        // up and the page is told.
        XCTAssertTrue(scheduler.fire(Self.patientIrohTiming.pageRetrySeconds))
        try await eventually {
            try await page.changes()
                == #"[{"closure":0,"preferred":"direct","sequence":1}]"#
        }
        XCTAssertEqual(asked.requests.count, 2)
        XCTAssertEqual(asked.requests.last?.selection, selection)
        XCTAssertEqual(asked.requests.last?.webView, ObjectIdentifier(primary))
        XCTAssertEqual(connection.streams.count, 1)

        // A stream through the seam, for the controller's target.
        let target = DirectTarget(
            selection: selection.number,
            runtime: runtime,
            storeIdentifier: app.websiteDataStore.identifier,
            authority: try XCTUnwrap(DirectAuthority(instanceURL: runtime.instance.url))
        )
        XCTAssertEqual(DirectIrohSelection(target), selection)
        let tag = DirectStreamTag(target: target, rendezvousGeneration: "g1", pathGeneration: 1)
        let seam: any DirectTransporting = transport
        let stream = try await seam.openStream(for: tag)
        XCTAssertTrue(stream is DirectIrohByteStream)
        try await stream.write(Data("ping".utf8))
        let read = try await stream.read(maxLength: 1_024)
        XCTAssertEqual(read, Data("hello".utf8))
        XCTAssertEqual(connection.streams.count, 2)
        XCTAssertEqual(connection.streams.last?.send.written, Data("ping".utf8))
        stream.cancel()

        // A failed candidate is probed; the path answers and stays.
        seam.pathDidFail(for: tag)
        try await eventually { connection.streams.count == 3 }
        try await Task.sleep(for: .milliseconds(200))
        let changes = try await page.changes()
        XCTAssertEqual(changes, #"[{"closure":0,"preferred":"direct","sequence":1}]"#)
        page.close()

        // Signing out ends the selection: the connection closes, and no
        // WebView is handed out for it.
        _ = try await app.mobileSessionCoordinator.logout()
        trackStore(of: app)
        try await eventually { log.count(of: "connection.close") == 1 }
        XCTAssertNil(source.webView(for: selection))
    }

    // MARK: A loss of the iroh transport's path

    /// The App goes to the background with a direct relay committed on the
    /// iroh transport's path. The transport and the controller both hear
    /// it, in either order. The relay is counted once, and the first status
    /// the return settles already includes that closure.
    @MainActor
    func testTheBackgroundCountsTheDirectRelayOnTheIrohTransportsPathOnceInEitherOrder() async throws {
        enum Order: CaseIterable {
            case transportFirst
            case controllerFirst
        }
        for order in Order.allCases {
            // The transport hears the App's lifecycle on its own center, so
            // that the test decides which of the two hears it first.
            let transportNotifications = NotificationCenter()
            let fixture = try await makeAppWithACommittedDirectRelay(
                transportNotifications: transportNotifications
            )
            let centers = order == .transportFirst
                ? [transportNotifications, NotificationCenter.default]
                : [NotificationCenter.default, transportNotifications]

            for center in centers {
                center.post(name: UIApplication.didEnterBackgroundNotification, object: nil)
            }
            // Closed by the transport in its own turn, after the claim.
            XCTAssertEqual(fixture.log.count(of: "connection.close"), 1, "\(order)")
            // Counted, and not published while the App is away: no document
            // is told to reconnect while the listener cannot be reached.
            XCTAssertEqual(directStatus(fixture.app, instance)?.closure, 0, "\(order)")
            try await Task.sleep(for: .milliseconds(300))
            XCTAssertEqual(directStatus(fixture.app, instance)?.closure, 0, "\(order)")
            for center in centers {
                center.post(name: UIApplication.willEnterForegroundNotification, object: nil)
            }
            // The first status the return settles.
            let first = Recorder<DirectPathStatus?>()
            fixture.app.directStatusProvider?(instance) { first.record($0) }
            try await eventually { first.values.count == 1 }
            if let status = first.values.first ?? nil {
                XCTAssertEqual(status.closure, 1, "\(order)")
                try await Task.sleep(for: .milliseconds(300))
                XCTAssertEqual(directStatus(fixture.app, instance)?.closure, 1, "\(order)")
            } else {
                // The simulator refused the re-bind: the held answer is the
                // bridge's `failed`, and the App shows the reopen message.
                // This run covers no successful return.
                XCTAssertTrue(fixture.app.directListenerFault, "\(order)")
            }
            fixture.close()
        }
    }

    /// The instance answers that it offers no direct path while a relay is
    /// committed on it: the relay is counted as closed by that answer,
    /// once.
    @MainActor
    func testAnUnavailableAnswerCountsTheDirectRelayOnTheIrohTransportsPathOnce() async throws {
        let fixture = try await makeAppWithACommittedDirectRelay()
        defer { fixture.close() }

        fixture.rendezvous.next.append(
            DirectIrohFixtures.unavailable(
                retryAfterSeconds: 60,
                instanceID: "instance-1",
                hostname: "instance-one.example"
            )
        )
        try fixture.fireRefresh()
        try await eventually { self.directStatus(fixture.app, self.instance)?.closure == 1 }
        XCTAssertEqual(fixture.log.count(of: "connection.close"), 1)
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(directStatus(fixture.app, instance)?.closure, 1)
        XCTAssertEqual(directStatus(fixture.app, instance)?.preferred, .tunnel)
    }

    /// The instance answers with a new endpoint while a relay is committed
    /// on the old one: the relay is counted as closed by the replacement,
    /// once, and the new path comes up.
    @MainActor
    func testAReplacementAnswerCountsTheDirectRelayOnTheIrohTransportsPathOnce() async throws {
        let fixture = try await makeAppWithACommittedDirectRelay()
        defer { fixture.close() }

        fixture.engine.prepare().connection.resolve(
            DirectIrohFakeConnection(
                log: fixture.log,
                remote: DirectIrohFixtures.instanceEndpoint,
                answers: [DirectIrohFakeConnection.established]
            )
        )
        fixture.rendezvous.generation = "g2"
        try fixture.fireRefresh()
        try await eventually {
            let status = self.directStatus(fixture.app, self.instance)
            return status?.closure == 1 && status?.preferred == .direct
        }
        XCTAssertEqual(fixture.log.count(of: "connection.close"), 1)
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(directStatus(fixture.app, instance)?.closure, 1)
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
                directStatus: { origin, reply in
                    askedFor = origin
                    reply(DirectPathStatus(preferred: .direct, sequence: 7, closure: 2))
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
              advertised: window.avibeNative.capabilities.direct,
              status: [status.preferred, status.sequence, status.closure, Object.keys(status).length],
              refused
            });
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )

        XCTAssertEqual(
            result as? String,
            #"{"capability":1,"advertised":1,"status":["direct",7,2,3],"refused":["invalid_argument","invalid_argument","invalid_argument"]}"#
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
    func testAStatusWithNothingCurrentToAnswerForIsAnErrorNotATunnel() async throws {
        let webView = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: instance,
                setOuterScrollLocked: { _, _ in false },
                directStatus: { _, reply in
                    reply(nil)
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
            try {
              await window.avibeNative.call("direct.status");
              return "answered";
            } catch (error) {
              return error.code;
            }
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )

        XCTAssertEqual(result as? String, "failed")
    }

    @MainActor
    func testATransportChangeIsDeliveredOnlyToInstanceDocumentsOfALiveRuntime() async throws {
        let controller = WebViewController(
            instanceOrigin: instance,
            websiteDataStore: .nonPersistent(),
            directStatus: { _, reply in
                reply(.initial)
            },
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
        var asked: [WebOrigin] = []
        controller.deliverDirectTransportChanged { origin in
            asked.append(origin)
            return DirectPathStatus(preferred: .direct, sequence: 3, closure: 1)
        }
        XCTAssertEqual(asked, [instance])
        try await eventually {
            try await page.changes()
                == #"[{"closure":1,"preferred":"direct","sequence":3}]"#
        }

        // A document on another origin has no bridge and is told nothing.
        try await page.load(
            RuntimePage.listeningDocument(title: "Elsewhere"),
            baseURL: URL(string: "https://example.com/")!
        )
        controller.deliverDirectTransportChanged { _ in
            DirectPathStatus(preferred: .tunnel, sequence: 4, closure: 1)
        }
        try await Task.sleep(for: .milliseconds(300))
        let elsewhere = try await page.changes()
        XCTAssertEqual(elsewhere, "[]")

        // A released runtime is not even asked.
        _ = controller.invalidateForAccountTransition()
        asked.removeAll()
        controller.deliverDirectTransportChanged { origin in
            asked.append(origin)
            return DirectPathStatus(preferred: .direct, sequence: 5, closure: 1)
        }
        XCTAssertEqual(asked, [])
    }

    /// The event is sent after a check of the WebView's URL, and runs later,
    /// in whatever document the WebView shows by then. The script checks the
    /// origin of the document it runs in.
    @MainActor
    func testTheEventScriptDispatchesOnlyInADocumentOfItsOrigin() async throws {
        let elsewhere = try XCTUnwrap(
            WebOrigin.parseConfigured("https://example.com")
        )
        let status = DirectPathStatus(preferred: .direct, sequence: 3, closure: 0)
        let forInstance = try XCTUnwrap(status.eventScript(for: instance))
        let forElsewhere = try XCTUnwrap(status.eventScript(for: elsewhere))
        let recorded = #"[{"closure":0,"preferred":"direct","sequence":3}]"#
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
        controller.deliverDirectTransportChanged { _ in
            DirectPathStatus(preferred: .direct, sequence: 3, closure: 0)
        }
        let first = #"[{"closure":0,"preferred":"direct","sequence":3}]"#
        try await eventually { try await changes(in: primary) == first }
        try await eventually { try await changes(in: childWebView) == first }

        // The child leaves the instance. The primary is still told; the
        // child is not.
        childWebView.load(URLRequest(url: elsewhereURL))
        try await eventually { await title(of: childWebView) == "Elsewhere" }
        controller.deliverDirectTransportChanged { _ in
            DirectPathStatus(preferred: .tunnel, sequence: 4, closure: 1)
        }
        try await eventually {
            try await changes(in: primary)
                == #"[{"closure":0,"preferred":"direct","sequence":3},{"closure":1,"preferred":"tunnel","sequence":4}]"#
        }
        try await Task.sleep(for: .milliseconds(300))
        let untold = try await changes(in: childWebView)
        XCTAssertEqual(untold, "[]")

        // An event that was already on its way when the child left runs in
        // the new document, and dispatches nothing there.
        let late = try XCTUnwrap(
            DirectPathStatus(preferred: .direct, sequence: 5, closure: 1)
                .eventScript(for: origin)
        )
        childWebView.evaluateJavaScript(late, completionHandler: nil)
        try await Task.sleep(for: .milliseconds(300))
        let afterLateEvent = try await changes(in: childWebView)
        XCTAssertEqual(afterLateEvent, "[]")
        // The document does listen: an event for its own origin arrives.
        let own = try XCTUnwrap(
            DirectPathStatus(preferred: .direct, sequence: 5, closure: 1)
                .eventScript(for: elsewhere)
        )
        childWebView.evaluateJavaScript(own, completionHandler: nil)
        try await eventually {
            try await changes(in: childWebView)
                == #"[{"closure":1,"preferred":"direct","sequence":5}]"#
        }
    }

    // MARK: Fixture

    private var storeIdentifiers: Set<UUID> = []
    private var releaseFixtureApps: [@MainActor () -> Void] = []
    /// What `direct.status()` answers for `origin`, through the provider
    /// the App gives its WebViews. `nil` when it answers later or not at all.
    @MainActor
    private func directStatus(_ app: AppModel, _ origin: WebOrigin) -> DirectPathStatus? {
        var answer: DirectPathStatus?
        app.directStatusProvider?(origin) { answer = $0 }
        return answer
    }

    /// Bounds no test of the iroh transport should reach, so that a slow
    /// simulator does not turn a stand-in that answers at once into a
    /// timeout.
    private static let patientIrohTiming: DirectIrohTiming = {
        var timing = DirectIrohTiming()
        timing.fetchSeconds = 120
        timing.bindSeconds = 120
        timing.dialSeconds = 120
        timing.probeSeconds = 120
        return timing
    }()

    /// An App whose transport is the iroh transport over a stand-in for the
    /// binding, with its path to instance-1 up through the runtime's
    /// primary WebView, and one direct relay committed through the App's
    /// own listener on that path.
    @MainActor
    private func makeAppWithACommittedDirectRelay(
        transportNotifications: NotificationCenter = .default
    ) async throws -> CommittedDirectRelay {
        let log = DirectIrohEventLog()
        let engine = DirectIrohFakeEngine(log: log)
        let scheduler = DirectIrohManualScheduler()
        let source = DirectRendezvousWebViewSource()
        let rendezvous = ScriptedRendezvous()
        let transport = DirectIrohTransport(
            engine: engine,
            fetchRendezvous: { selection, clientEndpointID in
                guard source.webView(for: selection) != nil else {
                    throw DirectRendezvousFetchError.pageNotReady
                }
                return rendezvous.answer(for: selection, client: clientEndpointID)
            },
            network: DirectIrohFakeNetwork(),
            scheduler: scheduler,
            timing: Self.patientIrohTiming,
            notificationCenter: transportNotifications,
            isInBackground: false
        )
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(directTransport: transport) {
            listening = try listeningSockets()
        }
        source.appModel = app
        // The probe's stream, then the relay's.
        let connection = DirectIrohFakeConnection(
            log: log,
            remote: DirectIrohFixtures.instanceEndpoint,
            answers: [DirectIrohFakeConnection.established, DirectIrohFakeConnection.established]
        )
        engine.prepare().connection.resolve(connection)

        let runtime = try await select("instance-1", in: app)
        let controller = try makeRuntimeController(app, runtime)
        app.registerRuntimeController(controller, for: runtime)
        let page = try RuntimePage(controller: controller)
        // A document of the instance's origin, loaded here before the path
        // comes up: the runtime's own load of the instance would otherwise
        // send WebKit's own CONNECT through the route, and could take the
        // relay's stream below.
        try await page.load(RuntimePage.recordingDocument, baseURL: runtime.instance.url)
        try await eventually {
            // A request made before the WebView existed is asked again.
            scheduler.fire(Self.patientIrohTiming.pageRetrySeconds)
            return self.directStatus(app, self.instance)?.preferred == .direct
        }

        let socket = try XCTUnwrap(try newListeners(since: listening).first)
        let port = try XCTUnwrap(NWEndpoint.Port(rawValue: socket.port))
        let client = LoopbackClient(port: port)
        try await client.send(
            "CONNECT instance-one.example:443 HTTP/1.1\r\nHost: instance-one.example:443\r\n\r\n"
        )
        let established = "HTTP/1.1 200 Connection Established\r\n\r\n"
        let answer = try await client.receive(count: established.utf8.count)
        XCTAssertEqual(String(decoding: answer, as: UTF8.self), established)
        XCTAssertEqual(connection.streams.count, 2)
        XCTAssertEqual(log.count(of: "connection.close"), 0)
        XCTAssertEqual(directStatus(app, instance)?.closure, 0)
        return CommittedDirectRelay(
            app: app,
            engine: engine,
            scheduler: scheduler,
            rendezvous: rendezvous,
            log: log,
            page: page,
            client: client
        )
    }

    /// The isolated preferences of the last App `makeApp` built.
    private var appDefaults: UserDefaults?

    /// An `AppModel` signed in through a local control plane, past its
    /// bootstrap. `nil` builds it exactly as the App does, with no
    /// transport argument at all. `beforeTheAppExists` runs once the control
    /// plane listens and before the `AppModel` is made, which is where a
    /// test takes the baseline of what the App itself does.
    @MainActor
    private func makeApp(
        directTransport: DirectTransporting?,
        directFaultMarker: DirectFaultMarkerStoring? = nil,
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
        appDefaults = defaults
        addTeardownBlock {
            defaults.removePersistentDomain(forName: suiteName)
        }
        addTeardownBlock { @MainActor [self] in
            await self.removeTrackedStores()
        }

        try beforeTheAppExists()

        let controlPlane = MobileControlPlaneConfiguration(testBaseURL: baseURL)
        let app: AppModel
        if let directFaultMarker {
            app = AppModel(
                defaults: defaults,
                controlPlane: controlPlane,
                directTransport: directTransport,
                directFaultMarker: directFaultMarker,
                pushKeychain: InMemoryPushKeychainStore(),
                pushGenerationStore: InMemoryPushGenerationStore(),
                pushInstallationMarker: InMemoryPushInstallationMarkerStore(),
                pushNotificationCenter: SilentPushNotificationCenter(),
                badgeCenter: SilentBadgeCenter()
            )
        } else if let directTransport {
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
        releaseFixtureApps.append { [weak app] in
            app?.showInstancePicker()
            app?.mobileSessionCoordinator.deactivateAccountSurface()
        }
        addTeardownBlock { @MainActor [self] in
            server.requests.attach(to: self, name: "direct App bootstrap requests")
        }
        try await WebKitReadiness.waitForSignedInFixture(
            app,
            accountID: "account-1",
            instanceIDs: ["instance-1", "instance-2"],
            requests: server.requests
        )
        return app
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
    /// moments at which the App's lease check would admit direct, so that
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

    /// Selects an instance and waits for its browser. With a transport the
    /// runtime opens once its route is prepared; without one, at once.
    @MainActor
    private func select(
        _ instanceID: String,
        in app: AppModel
    ) async throws -> MobileRuntimeContext {
        _ = try app.mobileSessionCoordinator.selectInstance(id: instanceID)
        return try await waitForBrowser(instanceID, in: app)
    }

    @MainActor
    private func waitForBrowser(
        _ instanceID: String,
        in app: AppModel
    ) async throws -> MobileRuntimeContext {
        var opened: MobileRuntimeContext?
        try await eventually {
            if case .browser(let runtime) = app.route,
               runtime.instance.id == instanceID
            {
                opened = runtime
                return true
            }
            return false
        }
        return try XCTUnwrap(opened)
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
            directStatus: app.directStatusProvider,
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
        // Release every App's runtime/account views before removing any
        // store (some tests create more than one App). RuntimePage owners
        // close their windows before this cleanup.
        for release in releaseFixtureApps { release() }
        releaseFixtureApps.removeAll()
        print("[iOS fixture] stage=App views invalidated before store removal")
        for identifier in storeIdentifiers {
            var removed = false
            var lastError: Error?
            for _ in 0..<50 {
                lastError = await AccountWebsiteDataStoreOperations.live
                    .removeIdentifier(identifier)
                let registered = await withCheckedContinuation { continuation in
                    WKWebsiteDataStore.fetchAllDataStoreIdentifiers {
                        continuation.resume(returning: Set($0))
                    }
                }
                if !registered.contains(identifier) {
                    removed = true
                    break
                }
                try? await Task.sleep(for: .milliseconds(100))
            }
            XCTAssertTrue(removed, "Fixture store still registered: \(identifier); error=\(String(describing: lastError))")
            print("[iOS fixture] stage=store removal, identifier=\(identifier), removed=\(removed)")
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

/// What the instance answers the iroh transport's rendezvous requests: a
/// ready answer of `generation` for the selection, unless a scripted
/// answer is next.
@MainActor
private final class ScriptedRendezvous {
    var generation = "g1"
    var next: [Data] = []

    func answer(for selection: DirectIrohSelection, client: DirectEndpointID) -> Data {
        guard next.isEmpty else {
            return next.removeFirst()
        }
        return DirectIrohFixtures.ready(
            generation: generation,
            client: client,
            instanceID: selection.instanceID,
            hostname: selection.host
        )
    }
}

/// An App with one direct relay committed on the iroh transport's path.
@MainActor
private struct CommittedDirectRelay {
    let app: AppModel
    let engine: DirectIrohFakeEngine
    let scheduler: DirectIrohManualScheduler
    let rendezvous: ScriptedRendezvous
    let log: DirectIrohEventLog
    let page: RuntimePage
    let client: LoopbackClient

    /// Fires the transport's refresh of its rendezvous answer: the one
    /// timer that is neither the stability timer nor the answer's deadline.
    func fireRefresh() throws {
        let stable = DirectIrohTiming.standard.stableSeconds
        let pending = scheduler.pending.filter { $0 != stable }.sorted()
        XCTAssertEqual(pending.count, 2, "\(scheduler.pending)")
        let refresh = try XCTUnwrap(pending.first)
        XCTAssertTrue(scheduler.fire(refresh))
    }

    func close() {
        client.close()
        page.close()
    }
}

/// The rendezvous requests a stand-in fetch was asked for, with the WebView
/// it was given for each.
@MainActor
private final class RendezvousRequests {
    struct Request {
        let selection: DirectIrohSelection
        let webView: ObjectIdentifier?
    }

    private(set) var requests: [Request] = []

    func record(_ selection: DirectIrohSelection, webView: WKWebView?) {
        requests.append(Request(selection: selection, webView: webView.map(ObjectIdentifier.init)))
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

    /// The `direct` capability as the page sees it, and what
    /// `direct.status()` answers: the status, or the error's code.
    func directSurface() async throws -> String? {
        try await webView?.callAsyncJavaScript(
            """
            let status;
            try {
              const answer = await window.avibeNative.call("direct.status");
              status = Object.fromEntries(Object.keys(answer).sort().map((key) => [key, answer[key]]));
            } catch (error) {
              status = error.code;
            }
            return JSON.stringify({
              capability: window.avibeNative.capabilities.direct ?? null,
              status
            });
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
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

/// Script terminal callbacks independently of WebContent timing. Navigation
/// tokens still come from WebKit: bare WKNavigation() instances lack the native
/// storage their deallocator requires. Token creation uses only local HTML.
final class PageLoaderTests: XCTestCase {
    @MainActor
    func testUnrelatedCallbacksCannotSettleTheRequestedDocument() async throws {
        enum Callback: CaseIterable {
            case staleFinish, staleFailure, staleProvisionalFailure
            case nilFinish, nilFailure, nilProvisionalFailure
            case wrongViewFinish, wrongViewFailure, wrongViewProvisionalFailure
            case wrongViewTermination
        }
        let view = makeWebView()
        let otherView = makeWebView()
        let loader = PageLoader()
        let staleNavigation = try view.makeNavigation()
        let expected = NSError(domain: "PageLoaderTests", code: 123)
        let unrelated = NSError(domain: "PageLoaderTests", code: 456)

        for callback in Callback.allCases {
            let navigation = try view.makeNavigation()
            view.nextNavigation = navigation
            view.onLoad = {
                switch callback {
                case .staleFinish:
                    loader.webView(view, didFinish: staleNavigation)
                case .staleFailure:
                    loader.webView(view, didFail: staleNavigation, withError: unrelated)
                case .staleProvisionalFailure:
                    loader.webView(view, didFailProvisionalNavigation: staleNavigation, withError: unrelated)
                case .nilFinish:
                    loader.webView(view, didFinish: nil)
                case .nilFailure:
                    loader.webView(view, didFail: nil, withError: unrelated)
                case .nilProvisionalFailure:
                    loader.webView(view, didFailProvisionalNavigation: nil, withError: unrelated)
                case .wrongViewFinish:
                    loader.webView(otherView, didFinish: navigation)
                case .wrongViewFailure:
                    loader.webView(otherView, didFail: navigation, withError: unrelated)
                case .wrongViewProvisionalFailure:
                    loader.webView(otherView, didFailProvisionalNavigation: navigation, withError: unrelated)
                case .wrongViewTermination:
                    loader.webViewWebContentProcessDidTerminate(otherView)
                }
                // A terminal fallback makes an incorrectly accepted callback
                // fail an assertion rather than leave this test hanging.
                loader.webView(view, didFail: navigation, withError: expected)
            }
            do {
                try await loader.loadHTML("fixture", baseURL: baseURL, in: view)
                XCTFail("Unrelated callback completed the load: \(callback)")
            } catch {
                XCTAssertEqual(error as NSError, expected, "\(callback)")
            }
        }
    }

    @MainActor
    func testMatchingFailuresIncludingCancellationAreNotIgnored() async throws {
        let view = makeWebView()
        let loader = PageLoader()
        for provisional in [false, true] {
            for code in [NSURLErrorCancelled, NSURLErrorCannotFindHost] {
                let navigation = try view.makeNavigation()
                let expected = NSError(domain: NSURLErrorDomain, code: code)
                view.nextNavigation = navigation
                view.onLoad = {
                    if provisional {
                        loader.webView(view, didFailProvisionalNavigation: navigation, withError: expected)
                    } else {
                        loader.webView(view, didFail: navigation, withError: expected)
                    }
                    loader.webView(view, didFinish: navigation)
                }
                do {
                    try await loader.loadHTML("fixture", baseURL: baseURL, in: view)
                    XCTFail("Matching failure was ignored: \(code), provisional=\(provisional)")
                } catch {
                    XCTAssertEqual(error as NSError, expected)
                }
            }
        }
    }

    @MainActor
    func testProcessTerminationFailsOnceAndLoaderCanBeReused() async throws {
        let view = makeWebView()
        let loader = PageLoader()
        let terminatedNavigation = try view.makeNavigation()
        view.nextNavigation = terminatedNavigation
        view.onLoad = {
            loader.webViewWebContentProcessDidTerminate(view)
            loader.webViewWebContentProcessDidTerminate(view)
            loader.webView(view, didFinish: terminatedNavigation)
        }
        do {
            try await loader.loadHTML("fixture", baseURL: baseURL, in: view)
            XCTFail("Process termination must fail, not accept a fallback document")
        } catch {
            XCTAssertEqual(error as? PageLoader.LoadError, .webContentProcessTerminated)
        }

        let nextNavigation = try view.makeNavigation()
        view.nextNavigation = nextNavigation
        view.onLoad = {
            loader.webView(
                view, didFail: terminatedNavigation,
                withError: NSError(domain: NSURLErrorDomain, code: NSURLErrorCancelled)
            )
            loader.webView(view, didFinish: nextNavigation)
            // Success must also clear the owner before a later termination.
            loader.webViewWebContentProcessDidTerminate(view)
        }
        try await loader.loadHTML("next fixture", baseURL: baseURL, in: view)
    }

    @MainActor
    func testMissingNavigationFailsAndClearsThePendingLoad() async throws {
        let view = makeWebView()
        let loader = PageLoader()
        view.nextNavigation = nil
        // A broken loader must fail an assertion, not suspend this test forever.
        view.onLoad = { loader.webView(view, didFinish: nil) }
        do {
            try await loader.loadHTML("fixture", baseURL: baseURL, in: view)
            XCTFail("A load without a navigation token cannot be observed")
        } catch {
            XCTAssertEqual(error as? PageLoader.LoadError, .navigationNotCreated)
        }
        let navigation = try view.makeNavigation()
        view.nextNavigation = navigation
        view.onLoad = { loader.webView(view, didFinish: navigation) }
        try await loader.loadHTML("next fixture", baseURL: baseURL, in: view)
    }

    private var baseURL: URL { URL(string: "https://fixture.example")! }

    @MainActor
    private func makeWebView() -> ScriptedPageLoaderWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        return ScriptedPageLoaderWebView(frame: .zero, configuration: configuration)
    }
}

@MainActor
private final class ScriptedPageLoaderWebView: WKWebView {
    var nextNavigation: WKNavigation?
    var onLoad: (() -> Void)?

    func makeNavigation() throws -> WKNavigation {
        // Bypass the scripted override to obtain a fully initialized token.
        // No navigationDelegate is installed, so WebKit cannot deliver these
        // real load callbacks to PageLoader; only onLoad does that.
        let navigation = try XCTUnwrap(super.loadHTMLString("<!doctype html>", baseURL: nil))
        stopLoading()
        return navigation
    }

    override func loadHTMLString(_ string: String, baseURL: URL?) -> WKNavigation? {
        let callback = onLoad
        onLoad = nil
        // Match WebKit's asynchronous delegate delivery: the loader must have
        // received the navigation token before these callbacks run.
        Task { @MainActor in callback?() }
        return nextNavigation
    }
}

/// Observes only the synthetic document requested by this loader. A runtime
/// controller can already have a URL navigation in flight when this delegate
/// is installed; its callbacks must not complete the synthetic document wait.
@MainActor
private final class PageLoader: NSObject, WKNavigationDelegate {
    enum LoadError: Error, Equatable, LocalizedError {
        case alreadyLoading
        case navigationNotCreated
        case webContentProcessTerminated

        var errorDescription: String? {
            switch self {
            case .alreadyLoading:
                return "PageLoader already has a pending fixture document load"
            case .navigationNotCreated:
                return "PageLoader could not create a navigation for the fixture document"
            case .webContentProcessTerminated:
                return "PageLoader WebContent process terminated before the fixture document finished loading"
            }
        }
    }

    private var continuation: CheckedContinuation<Void, Error>?
    private var navigation: WKNavigation?
    private weak var loadingWebView: WKWebView?

    func loadHTML(
        _ html: String,
        baseURL: URL,
        in webView: WKWebView
    ) async throws {
        guard continuation == nil else {
            throw LoadError.alreadyLoading
        }
        try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            loadingWebView = webView
            navigation = webView.loadHTMLString(html, baseURL: baseURL)
            if navigation == nil {
                finish(with: .failure(LoadError.navigationNotCreated))
            }
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation?) {
        finish(in: webView, navigation: navigation, with: .success(()))
    }

    func webView(
        _ webView: WKWebView,
        didFail navigation: WKNavigation?,
        withError error: Error
    ) {
        finish(in: webView, navigation: navigation, with: .failure(error))
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation?,
        withError error: Error
    ) {
        finish(in: webView, navigation: navigation, with: .failure(error))
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        guard webView === loadingWebView else { return }
        // The HTML document is gone. Do not let WebKit's fallback URL reload
        // masquerade as the requested fixture or wait for its DNS failure.
        finish(with: .failure(LoadError.webContentProcessTerminated))
    }

    private func finish(
        in webView: WKWebView,
        navigation: WKNavigation?,
        with result: Result<Void, Error>
    ) {
        guard webView === loadingWebView,
              let navigation, navigation === self.navigation
        else { return }
        // Cancellation of this navigation is a real failure. Only callbacks
        // for a different navigation are ignored, regardless of error code.
        finish(with: result)
    }

    private func finish(with result: Result<Void, Error>) {
        let pending = continuation
        continuation = nil
        navigation = nil
        loadingWebView = nil
        pending?.resume(with: result)
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

/// The fault marker as a test sees it, in memory.
private final class FaultMarkerSpy: DirectFaultMarkerStoring {
    private(set) var isMarked: Bool
    private(set) var marks = 0
    private(set) var takes = 0
    /// Called inside `mark()`, before it returns.
    var whileMarking: () -> Void = {}

    init(marked: Bool) {
        isMarked = marked
    }

    func mark() {
        marks += 1
        whileMarking()
        isMarked = true
    }

    func take() -> Bool {
        takes += 1
        defer { isMarked = false }
        return isMarked
    }
}
