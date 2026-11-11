import Network
import UIKit
import WebKit
import XCTest
@testable import Avibe

/// The transport state and the tag rules, driven through the App's own
/// `MobileSessionCoordinator` and `AccountWebsiteDataStore`, with stand-ins
/// only for the account transport and the direct transport.
final class DirectPathControllerTests: XCTestCase {
    private let tunnel = DirectPathStatus.Transport.tunnel
    private let direct = DirectPathStatus.Transport.direct

    // MARK: Inactive without a transport

    /// Counted, not inferred from the end state: no listening socket is
    /// ever created and `proxyConfigurations` is never written, on any
    /// store, at any step. `testTheRouteIsInstalledOnlyWhile...` shows the
    /// same counters and the same observer do move when a route is used.
    @MainActor
    func testWithoutATransportNoListenerIsCreatedAndNoRouteIsEverWritten() async throws {
        try assertSocketScanSeesListeners()
        let listening = try listeningSockets()
        let harness = try Harness(transport: nil, test: self)

        func assertUntouched(_ step: String) throws {
            XCTAssertEqual(harness.routeWrites, 0, step)
            XCTAssertEqual(harness.observedRouteWrites, 0, step)
            XCTAssertEqual(harness.listenersCreated, 0, step)
            XCTAssertEqual(try newListeners(since: listening), [], step)
            XCTAssertEqual(harness.routedStoreCount, 0, step)
            XCTAssertEqual(harness.changes, [], step)
            XCTAssertEqual(
                harness.controller.status(for: harness.origin("instance-1")),
                .initial,
                step
            )
        }

        try assertUntouched("the controller exists")
        try await harness.signIn()
        try harness.select("instance-1")
        try assertUntouched("a runtime opened")
        harness.enterBackground()
        try assertUntouched("background")
        harness.enterForeground()
        try assertUntouched("foreground")
        try harness.select("instance-2")
        try assertUntouched("the instance was switched")
        _ = try await harness.coordinator.refresh()
        try assertUntouched("the account was refreshed")
        try harness.select("instance-1")
        await harness.websiteDataStore.rotate()
        try assertUntouched("the store rotated under an open runtime")
        _ = try await harness.coordinator.logout()
        try assertUntouched("signed out")
        try await Task.sleep(for: .milliseconds(200))
        try assertUntouched("afterwards")
        // Every store the account ever had was watched, the rotated ones
        // included.
        XCTAssertGreaterThan(harness.observedStoreCount, 2)
    }

    // MARK: Activation and fallback

    @MainActor
    func testTheRouteIsInstalledOnlyWhileTheTransportReportsAUsablePath() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(test: self)
        try await harness.signIn()
        try harness.select("instance-1")

        // The transport is told exactly what it is asked to reach.
        let target = try XCTUnwrap(harness.transport?.selections.last)
        XCTAssertEqual(target.runtime.account.id, "account-1")
        XCTAssertEqual(target.runtime.instance.id, "instance-1")
        XCTAssertEqual(target.authority.connectTarget, "instance-one.example:443")
        XCTAssertEqual(target.storeIdentifier, harness.websiteDataStore.identifier)
        // Selecting is not activating.
        XCTAssertEqual(harness.routedStoreCount, 0)
        XCTAssertEqual(try newListeners(since: listening), [])
        XCTAssertEqual(harness.routeWrites, 0)
        XCTAssertEqual(harness.observedRouteWrites, 0)
        XCTAssertEqual(harness.listenersCreated, 0)

        harness.transport?.report(.usable(generation: "g1"), for: target)
        try await eventually { harness.changes == [self.direct] }
        XCTAssertEqual(harness.currentStoreRouteCount, 1)
        XCTAssertEqual(try newListeners(since: listening).map(\.address), ["127.0.0.1"])
        // One listener, and one write that both the App's counter and the
        // observer of the real setter saw.
        XCTAssertEqual(harness.listenersCreated, 1)
        XCTAssertEqual(harness.routeWrites, 1)
        XCTAssertEqual(harness.observedRouteWrites, 1)
        XCTAssertEqual(
            harness.controller.status(for: harness.origin("instance-1")),
            DirectPathStatus(transport: .direct, sequence: 1)
        )
        // Another origin is never on the direct path.
        XCTAssertEqual(
            harness.controller.status(for: harness.origin("instance-2")),
            DirectPathStatus(transport: .tunnel, sequence: 1)
        )

        // The route is gone before the report returns.
        harness.transport?.report(.lost, for: target)
        XCTAssertEqual(harness.routedStoreCount, 0)
        XCTAssertEqual(harness.changes, [direct, tunnel])
        XCTAssertEqual(harness.routeWrites, 2)
        XCTAssertEqual(harness.observedRouteWrites, 2)
        XCTAssertEqual(
            harness.controller.status(for: harness.origin("instance-1")),
            DirectPathStatus(transport: .tunnel, sequence: 2)
        )
        try await eventually { try newListeners(since: listening).isEmpty }

        // The same path, found again, is accepted again.
        harness.transport?.report(.usable(generation: "g1"), for: target)
        try await eventually {
            harness.changes == [self.direct, self.tunnel, self.direct]
        }
        XCTAssertEqual(harness.currentStoreRouteCount, 1)
        XCTAssertEqual(harness.listenersCreated, 2)
        XCTAssertEqual(harness.routeWrites, 3)
        XCTAssertEqual(harness.observedRouteWrites, 3)
    }

    @MainActor
    func testAPathThatCannotServeAConnectionReturnsToTheTunnelAtOnce() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(test: self)
        let transport = try XCTUnwrap(harness.transport)
        try await harness.signIn()
        try harness.select("instance-1")
        let target = try XCTUnwrap(transport.selections.last)
        transport.report(.usable(generation: "g1"), for: target)
        try await eventually { harness.changes == [self.direct] }
        let port = try XCTUnwrap(
            try newListeners(since: listening).first
                .flatMap { NWEndpoint.Port(rawValue: $0.port) }
        )
        let head = "CONNECT instance-one.example:443 HTTP/1.1\r\nHost: instance-one.example:443\r\n\r\n"

        // A connection WebKit would make is carried on a stream opened for
        // the route's own tags.
        let served = LoopbackClient(port: port)
        try await served.send(head)
        let stream = try await transport.streams.waitForStream()
        stream.instanceSends(Data("HTTP/1.1 200 Connection Established\r\n\r\n".utf8))
        _ = try await served.receive(count: 39)
        let tag = DirectRouteTag(target: target, rendezvousGeneration: "g1")
        XCTAssertEqual(transport.openedTags.values, [tag])
        XCTAssertEqual(harness.changes, [direct])

        // The next one cannot be opened.
        transport.streams.setMode(.fail)
        let refused = LoopbackClient(port: port)
        try await refused.send(head)
        let answer = try await refused.receiveUntilClosed()
        XCTAssertTrue(
            String(decoding: answer, as: UTF8.self).hasPrefix("HTTP/1.1 502 ")
        )
        try await eventually { harness.changes == [self.direct, self.tunnel] }
        XCTAssertEqual(harness.routedStoreCount, 0)
        XCTAssertEqual(transport.failedTags, [tag])
        // The listener and the connection it was still carrying are closed.
        try await eventually { try newListeners(since: listening).isEmpty }
        try await eventually { stream.isCancelled }
        let rest = try await served.receiveUntilClosed()
        XCTAssertEqual(rest, Data())
        refused.close()
    }

    // MARK: Tags

    @MainActor
    func testAReportForAnEndedSelectionNeverInstallsARoute() async throws {
        struct Transition {
            let name: String
            /// Whether a runtime is open afterwards, which can then activate.
            let reselects: Bool
            let apply: @MainActor (Harness) async throws -> Void
        }
        let transitions = [
            Transition(name: "the instance was switched", reselects: true) {
                try $0.select("instance-2")
            },
            Transition(name: "the same instance was opened again", reselects: true) {
                try $0.select("instance-1")
            },
            Transition(name: "the App went to the background", reselects: false) {
                $0.enterBackground()
            },
            Transition(name: "the App came back from the background", reselects: true) {
                $0.enterBackground()
                $0.enterForeground()
            },
            Transition(name: "the account was refreshed", reselects: false) {
                _ = try await $0.coordinator.refresh()
            },
            Transition(name: "the account signed out", reselects: false) {
                _ = try await $0.coordinator.logout()
            },
            Transition(name: "the session ended on the server", reselects: false) {
                $0.account.session = nil
                _ = try await $0.coordinator.refresh()
            },
            Transition(name: "another account is signed in", reselects: false) {
                $0.account.session = Harness.session(accountID: "account-2")
                _ = try await $0.coordinator.refresh()
            },
        ]

        for transition in transitions {
            let listening = try listeningSockets()
            let harness = try Harness(test: self)
            let transport = try XCTUnwrap(harness.transport)
            try await harness.signIn()
            try harness.select("instance-1")
            let ended = try XCTUnwrap(transport.selections.last)

            try await transition.apply(harness)
            transport.report(.usable(generation: "g1"), for: ended)
            try await Task.sleep(for: .milliseconds(200))

            XCTAssertEqual(harness.routedStoreCount, 0, transition.name)
            XCTAssertEqual(harness.changes, [], transition.name)
            XCTAssertEqual(try newListeners(since: listening), [], transition.name)

            // The report was dropped for its tags, not because nothing can
            // activate: the selection that replaced it still can.
            let current = transport.selections.last
            XCTAssertEqual(current != ended, transition.reselects, transition.name)
            if transition.reselects, let current {
                XCTAssertGreaterThan(current.selection, ended.selection)
                transport.report(.usable(generation: "g1"), for: current)
                try await eventually { harness.changes == [self.direct] }
                XCTAssertEqual(harness.currentStoreRouteCount, 1, transition.name)
            }
            harness.controller.runtimeDidStop()
            try await eventually { try newListeners(since: listening).isEmpty }
        }
    }

    @MainActor
    func testAListenerThatIsReadyAfterItsSelectionEndedInstallsNothing() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(test: self)
        let transport = try XCTUnwrap(harness.transport)
        try await harness.signIn()
        try harness.select("instance-1")
        let target = try XCTUnwrap(transport.selections.last)

        // The path is accepted, and the selection ends in the same turn,
        // before the listener it started has bound its socket.
        transport.report(.usable(generation: "g1"), for: target)
        try harness.select("instance-2")
        try await Task.sleep(for: .milliseconds(200))

        XCTAssertEqual(harness.routedStoreCount, 0)
        XCTAssertEqual(harness.changes, [])
        try await eventually { try newListeners(since: listening).isEmpty }

        // The selection that replaced it activates once, for its own host,
        // and the abandoned listener is not the one left listening.
        let current = try XCTUnwrap(transport.selections.last)
        transport.report(.usable(generation: "g1"), for: current)
        try await eventually { harness.changes == [self.direct] }
        XCTAssertEqual(
            harness.controller.status(for: harness.origin("instance-2")).transport,
            .direct
        )
        XCTAssertEqual(
            harness.controller.status(for: harness.origin("instance-1")).transport,
            .tunnel
        )
        try await eventually {
            try newListeners(since: listening).count == 1
        }
        XCTAssertEqual(harness.changes, [direct])
    }

    /// `isCurrentRuntime` is also false while the coordinator runs a request
    /// of its own, such as push management, with no account transition. A
    /// route that is ready in that window is not installed.
    @MainActor
    func testARouteThatIsReadyWhileTheAccountCoordinatorIsBusyIsNotInstalled() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(test: self)
        let transport = try XCTUnwrap(harness.transport)
        try await harness.signIn()
        try harness.select("instance-1")
        let target = try XCTUnwrap(transport.selections.last)
        let tag = DirectRouteTag(target: target, rendezvousGeneration: "g1")

        let request = harness.account.holdPushManagement()
        let pushManagement = Task { @MainActor in
            _ = try await harness.coordinator.pushManagementRequest(
                PushManagementRequest(
                    method: "GET",
                    path: "/api/push/status",
                    headers: [:],
                    body: nil
                )
            )
        }
        try await eventually { harness.account.isHoldingPushManagement }
        XCTAssertFalse(harness.coordinator.isCurrentRuntime(target.runtime))

        transport.report(.usable(generation: "g1"), for: target)
        try await eventually { transport.failedTags == [tag] }
        XCTAssertEqual(harness.routedStoreCount, 0)
        XCTAssertEqual(harness.changes, [])
        try await eventually {
            try newListeners(since: listening).isEmpty
        }

        // The request ends; the transport reports again and is admitted.
        request.resume()
        try await pushManagement.value
        transport.report(.usable(generation: "g1"), for: target)
        try await eventually { harness.changes == [self.direct] }
        XCTAssertEqual(harness.currentStoreRouteCount, 1)
    }

    @MainActor
    func testTheRouteLeavesWithTheStoreItWasInstalledOn() async throws {
        let harness = try Harness(test: self)
        let transport = try XCTUnwrap(harness.transport)
        try await harness.signIn()
        try harness.select("instance-1")
        let target = try XCTUnwrap(transport.selections.last)
        transport.report(.usable(generation: "g1"), for: target)
        try await eventually { harness.changes == [self.direct] }
        let routed = harness.websiteDataStore.dataStore
        XCTAssertEqual(routed.proxyConfigurations.count, 1)

        // The store rotates with nothing having stopped the runtime first.
        await harness.websiteDataStore.rotate()

        XCTAssertEqual(routed.proxyConfigurations.count, 0)
        XCTAssertFalse(harness.websiteDataStore.dataStore === routed)
        XCTAssertEqual(harness.routedStoreCount, 0)
        XCTAssertEqual(harness.changes, [direct, tunnel])
        XCTAssertEqual(transport.deselectCount, 1)

        // Neither a late report nor the old lease reaches the new store.
        transport.report(.usable(generation: "g1"), for: target)
        harness.controller.runtimeDidOpen(target.runtime)
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(transport.selections.count, 1)
        XCTAssertEqual(harness.routedStoreCount, 0)
    }

    @MainActor
    func testTheRouteOwnerRefusesATagMadeForAnotherStore() async throws {
        let harness = try Harness(test: self)
        try await harness.signIn()
        try harness.select("instance-1")
        let target = try XCTUnwrap(harness.transport?.selections.last)
        let tag = DirectRouteTag(target: target, rendezvousGeneration: "g1")
        let owner = DirectRouteOwner(websiteDataStore: harness.websiteDataStore)
        let port = try XCTUnwrap(NWEndpoint.Port(rawValue: 9))

        XCTAssertTrue(owner.install(tag, proxyPort: port))
        XCTAssertEqual(owner.installedTag, tag)
        XCTAssertEqual(harness.currentStoreRouteCount, 1)
        owner.remove()
        XCTAssertNil(owner.installedTag)
        XCTAssertEqual(harness.routedStoreCount, 0)

        await harness.websiteDataStore.rotate()
        XCTAssertFalse(owner.install(tag, proxyPort: port))
        XCTAssertNil(owner.installedTag)
        XCTAssertEqual(harness.routedStoreCount, 0)
    }

    @MainActor
    func testANewRendezvousGenerationReplacesTheRouteAndTheOldOneIsNotAcceptedAgain() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(test: self)
        let transport = try XCTUnwrap(harness.transport)
        try await harness.signIn()
        try harness.select("instance-1")
        let target = try XCTUnwrap(transport.selections.last)
        transport.report(.usable(generation: "g1"), for: target)
        try await eventually { harness.changes == [self.direct] }

        // A repeat of the route's own generation changes nothing.
        transport.report(.usable(generation: "g1"), for: target)
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(harness.changes, [direct])

        // The instance's endpoint or addresses changed.
        transport.report(.usable(generation: "g2"), for: target)
        try await eventually {
            harness.changes == [self.direct, self.tunnel, self.direct]
        }
        // The first listener is closed; only the new route's remains.
        try await eventually {
            try newListeners(since: listening).count == 1
        }
        let port = try XCTUnwrap(
            try newListeners(since: listening).first
                .flatMap { NWEndpoint.Port(rawValue: $0.port) }
        )
        let client = LoopbackClient(port: port)
        try await client.send(
            "CONNECT instance-one.example:443 HTTP/1.1\r\n\r\n"
        )
        _ = try await transport.streams.waitForStream()
        XCTAssertEqual(
            transport.openedTags.values.map(\.rendezvousGeneration),
            ["g2"]
        )
        client.close()

        // A late result for the generation that was replaced.
        transport.report(.usable(generation: "g1"), for: target)
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(harness.changes, [direct, tunnel, direct])
        XCTAssertEqual(harness.currentStoreRouteCount, 1)
    }

    @MainActor
    func testTheBackgroundReturnsToTheTunnelAndTheForegroundSelectsAgain() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(test: self)
        let transport = try XCTUnwrap(harness.transport)
        try await harness.signIn()
        try harness.select("instance-1")
        let first = try XCTUnwrap(transport.selections.last)
        transport.report(.usable(generation: "g1"), for: first)
        try await eventually { harness.changes == [self.direct] }

        // The route is gone before the notification returns: the listening
        // socket does not survive suspension.
        harness.enterBackground()
        XCTAssertEqual(harness.routedStoreCount, 0)
        XCTAssertEqual(harness.changes, [direct, tunnel])
        XCTAssertEqual(transport.deselectCount, 1)
        try await eventually { try newListeners(since: listening).isEmpty }

        // A runtime that opens in the background selects nothing yet.
        try harness.select("instance-2")
        XCTAssertEqual(transport.selections.count, 1)

        harness.enterForeground()
        let second = try XCTUnwrap(transport.selections.last)
        XCTAssertEqual(transport.selections.count, 2)
        XCTAssertEqual(second.runtime.instance.id, "instance-2")
        XCTAssertGreaterThan(second.selection, first.selection)
        transport.report(.usable(generation: "g1"), for: second)
        try await eventually {
            harness.changes == [self.direct, self.tunnel, self.direct]
        }

        // Once the runtime has stopped, a foreground selects nothing.
        harness.controller.runtimeDidStop()
        harness.enterBackground()
        harness.enterForeground()
        XCTAssertEqual(transport.selections.count, 2)
    }

    // MARK: WebKit

    /// WebKit itself, through the installed route. The page is plain HTTP
    /// on port 443 of an unqualified host: that makes WebKit ask the proxy
    /// for the route's exact authority without needing a certificate, and
    /// App Transport Security does not apply to unqualified names. So this
    /// shows that WebKit sends its CONNECT for the matched host to the
    /// listener and that a page loads through the relay. It shows nothing
    /// about TLS, certificates, or a real network.
    @MainActor
    func testWebKitLoadsThroughTheInstalledRoute() async throws {
        let harness = try Harness(test: self)
        let transport = try XCTUnwrap(harness.transport)
        try await harness.signIn()
        try harness.select("instance-unqualified")
        let target = try XCTUnwrap(transport.selections.last)
        transport.report(.usable(generation: "g1"), for: target)
        try await eventually { harness.changes == [self.direct] }

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = harness.websiteDataStore.dataStore
        let webView = WKWebView(
            frame: CGRect(x: 0, y: 0, width: 320, height: 480),
            configuration: configuration
        )
        let page = PageLoad()
        webView.navigationDelegate = page
        webView.load(
            URLRequest(url: URL(string: "http://avibe-direct-test:443/hello")!)
        )

        try await eventually(timeout: .seconds(30)) {
            !transport.streams.opened.isEmpty || page.failure != nil
        }
        if let failure = page.failure {
            XCTFail("WebKit did not use the route: \(failure)")
            return
        }
        let stream = try await transport.streams.waitForStream()
        stream.instanceSends(
            Data("HTTP/1.1 200 Connection Established\r\n\r\n".utf8)
        )
        // What WebKit sends inside the tunnel is its own request.
        try await eventually(timeout: .seconds(30)) {
            String(decoding: stream.written, as: UTF8.self)
                .contains("GET /hello HTTP/1.1\r\n")
                && stream.written.suffix(4) == Data("\r\n\r\n".utf8)
        }
        let body = "<!doctype html><title>direct</title>"
        stream.instanceSends(
            Data(
                "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: \(body.utf8.count)\r\nConnection: close\r\n\r\n\(body)".utf8
            )
        )
        stream.instanceFinishes()
        try await eventually(timeout: .seconds(30)) {
            page.finished || page.failure != nil
        }
        XCTAssertNil(page.failure)
        let title = try await webView.evaluateJavaScript("document.title")
        XCTAssertEqual(title as? String, "direct")
        // WebKit decides how many connections a load takes (a second one
        // has been seen). Every stream was opened for this route.
        let opened = transport.openedTags.values
        XCTAssertFalse(opened.isEmpty)
        XCTAssertEqual(
            opened.filter {
                $0 != DirectRouteTag(target: target, rendezvousGeneration: "g1")
            },
            []
        )
    }

    @MainActor
    func testAnInstanceThatIsNotOnTheDefaultHTTPSPortIsNeverSelected() async throws {
        let harness = try Harness(test: self)
        try await harness.signIn()

        try harness.select("instance-custom-port")

        XCTAssertEqual(harness.transport?.selections.count, 0)
    }
}

// MARK: - Harness

/// The same wiring `AppModel` has, around stand-ins.
@MainActor
private final class Harness {
    let websiteDataStore: AccountWebsiteDataStore
    let account = StandInAccountTransport()
    let transport: StandInDirectTransport?
    private let stores = InMemoryAccountStores()
    private let notifications = NotificationCenter()
    private let routeWritesAtStart = DirectRouteOwner.writeCount
    private let listenersAtStart = DirectConnectListener.socketsCreated
    private(set) var statuses: [DirectPathStatus] = []

    private(set) lazy var controller: DirectPathController = DirectPathController(
        websiteDataStore: websiteDataStore,
        transport: transport,
        isCurrentRuntime: { [unowned self] in
            self.coordinator.isCurrentRuntime($0)
        },
        limits: .patient,
        notificationCenter: notifications,
        isInBackground: false,
        onStatusChanged: { [unowned self] in
            self.statuses.append($0)
            // The route has changed by the time the change is announced,
            // so a connection opened on the event uses the new path.
            XCTAssertEqual(
                self.currentStoreRouteCount,
                $0.transport == .direct ? 1 : 0
            )
        }
    )

    private(set) lazy var coordinator: MobileSessionCoordinator = MobileSessionCoordinator(
        defaults: defaults,
        controlPlane: MobileControlPlaneConfiguration(
            testBaseURL: URL(string: "http://127.0.0.1:43127")!
        ),
        websiteDataStore: websiteDataStore,
        transportFactory: { [account] _, _ in
            account
        },
        onOpenRuntime: { [unowned self] in
            self.controller.runtimeDidOpen($0)
        },
        onPrepareAccountTransition: { [unowned self] in
            self.controller.runtimeDidStop()
            return []
        }
    )

    private let defaults: UserDefaults

    init(
        transport: StandInDirectTransport? = StandInDirectTransport(),
        test: XCTestCase
    ) throws {
        let suiteName = "DirectPathControllerTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        test.addTeardownBlock {
            defaults.removePersistentDomain(forName: suiteName)
        }
        self.defaults = defaults
        self.transport = transport
        websiteDataStore = AccountWebsiteDataStore(
            defaults: defaults,
            operations: stores.operations
        )
        account.session = Self.session(accountID: "account-1")
        _ = controller
        test.addTeardownBlock { [self] in
            await self.stop()
        }
    }

    /// Closes whatever listener the test left open.
    func stop() {
        controller.runtimeDidStop()
    }

    /// The transports the bridge event would have carried, in order.
    var changes: [DirectPathStatus.Transport] {
        // Each change has the next sequence number.
        XCTAssertEqual(statuses.map(\.sequence), Array(1..<(statuses.count + 1)))
        return statuses.map(\.transport)
    }

    /// Writes of `proxyConfigurations` since this harness was made, as the
    /// App counts them at its one assignment.
    var routeWrites: Int {
        DirectRouteOwner.writeCount - routeWritesAtStart
    }

    /// The same writes as the real setter saw them, on every store this
    /// account has had.
    var observedRouteWrites: Int {
        stores.observer.writes
    }

    var observedStoreCount: Int {
        stores.created.count
    }

    /// Listening sockets the App has created since this harness was made.
    var listenersCreated: Int {
        DirectConnectListener.socketsCreated - listenersAtStart
    }

    /// How many account stores, current or rotated away, have a route.
    var routedStoreCount: Int {
        stores.created.filter { !$0.proxyConfigurations.isEmpty }.count
    }

    var currentStoreRouteCount: Int {
        websiteDataStore.dataStore.proxyConfigurations.count
    }

    func origin(_ instanceID: String) -> WebOrigin {
        let instance = Self.session(accountID: "account-1").instances
            .first { $0.id == instanceID }!
        return WebOrigin(url: instance.url)!
    }

    func signIn() async throws {
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
    }

    func select(_ instanceID: String) throws {
        _ = try coordinator.selectInstance(id: instanceID)
    }

    func enterBackground() {
        notifications.post(
            name: UIApplication.didEnterBackgroundNotification,
            object: nil
        )
    }

    func enterForeground() {
        notifications.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
    }

    static func session(accountID: String) -> MobileSessionSnapshot {
        func instance(_ id: String, _ url: String) -> MobileInstance {
            let url = URL(string: url)!
            return MobileInstance(
                id: id,
                name: id,
                host: url.host!,
                url: url,
                relationship: "owner",
                ownerEmail: "max@example.com",
                available: true
            )
        }
        return MobileSessionSnapshot(
            account: MobileAccount(id: accountID, email: "max@example.com"),
            instances: [
                instance("instance-1", "https://instance-one.example"),
                instance("instance-2", "https://instance-two.example"),
                instance("instance-custom-port", "https://instance-three.example:8443"),
                instance("instance-unqualified", "https://avibe-direct-test"),
            ]
        )
    }
}

/// How one navigation ended.
@MainActor
private final class PageLoad: NSObject, WKNavigationDelegate {
    private(set) var finished = false
    private(set) var failure: Error?

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation?) {
        finished = true
    }

    func webView(
        _ webView: WKWebView,
        didFail navigation: WKNavigation?,
        withError error: Error
    ) {
        failure = error
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation?,
        withError error: Error
    ) {
        failure = error
    }
}

/// Account stores that exist only in memory, all kept so a test can look at
/// one after it was rotated away.
@MainActor
private final class InMemoryAccountStores {
    private(set) var created: [WKWebsiteDataStore] = []
    let observer = RouteWriteObserver()

    var operations: AccountWebsiteDataStoreOperations {
        AccountWebsiteDataStoreOperations(
            makeDataStore: { [self] _ in
                let store = WKWebsiteDataStore.nonPersistent()
                created.append(store)
                observer.observe(store)
                return store
            },
            removeIdentifier: { _ in
                nil
            }
        )
    }
}

/// The account transport, reduced to the session it answers with.
@MainActor
private final class StandInAccountTransport: MobileAuthTransporting {
    var session: MobileSessionSnapshot?

    func requestOTP(email: String) async throws -> Int {
        0
    }

    func verifyOTP(
        email: String,
        code: String
    ) async throws -> MobileSessionDiscovery {
        throw MobileAccountError.network
    }

    func signInWithPassword(
        email: String,
        password: String
    ) async throws -> MobilePasswordSignInDiscovery {
        throw MobileAccountError.network
    }

    func fetchSession() async throws -> MobileSessionDiscovery? {
        session.map(MobileSessionDiscovery.signedIn)
    }

    func passkeyChallenge() async throws -> MobilePasskeyChallenge {
        throw MobileAccountError.passkeyUnavailable
    }

    func verifySecondFactor(
        _ proof: MobileSecondFactorProof
    ) async throws -> MobileSessionDiscovery {
        throw MobileAccountError.network
    }

    func logout() async throws {}

    func pushManagementRequest(
        _ request: PushManagementRequest
    ) async throws -> PushManagementResponse {
        if let pushManagementHold {
            isHoldingPushManagement = true
            await pushManagementHold.wait()
            isHoldingPushManagement = false
        }
        return PushManagementResponse(statusCode: 200, body: [:])
    }

    func cancelAll() async {}

    func invalidate() -> WebsiteDataStoreReleaseBarrier? {
        nil
    }

    private var pushManagementHold: Hold?
    private(set) var isHoldingPushManagement = false

    /// The next push management request waits until the returned hold is
    /// resumed.
    func holdPushManagement() -> Hold {
        let hold = Hold()
        pushManagementHold = hold
        return hold
    }

    @MainActor
    final class Hold {
        private var continuation: CheckedContinuation<Void, Never>?
        private var resumed = false

        func wait() async {
            guard !resumed else {
                return
            }
            await withCheckedContinuation { continuation = $0 }
        }

        func resume() {
            resumed = true
            continuation?.resume()
            continuation = nil
        }
    }
}

/// Counts real calls of the `proxyConfigurations` setter on the stores it is
/// given, by key-value observing them. It sees a write wherever it comes
/// from, including one that sets the value the store already had.
final class RouteWriteObserver: NSObject {
    private(set) var writes = 0
    private var stores: [WKWebsiteDataStore] = []

    func observe(_ store: WKWebsiteDataStore) {
        stores.append(store)
        store.addObserver(
            self,
            forKeyPath: "proxyConfigurations",
            options: [],
            context: nil
        )
    }

    override func observeValue(
        forKeyPath keyPath: String?,
        of object: Any?,
        change: [NSKeyValueChangeKey: Any]?,
        context: UnsafeMutableRawPointer?
    ) {
        writes += 1
    }

    deinit {
        for store in stores {
            store.removeObserver(self, forKeyPath: "proxyConfigurations")
        }
    }
}

/// A direct transport the test speaks for. Shared with the App-level tests.
final class StandInDirectTransport: DirectTransporting, @unchecked Sendable {
    let streams = StandInStreamSource()
    let openedTags = Recorder<DirectRouteTag>()
    @MainActor private(set) var selections: [DirectTarget] = []
    @MainActor private(set) var deselectCount = 0
    @MainActor private(set) var failedTags: [DirectRouteTag] = []
    @MainActor private var reporters: [Int: @MainActor (DirectPathReport) -> Void] = [:]

    @MainActor
    func select(
        _ target: DirectTarget,
        report: @escaping @MainActor (DirectPathReport) -> Void
    ) {
        selections.append(target)
        reporters[target.selection] = report
    }

    @MainActor
    func deselect() {
        deselectCount += 1
    }

    @MainActor
    func pathDidFail(for tag: DirectRouteTag) {
        failedTags.append(tag)
    }

    func openStream(for tag: DirectRouteTag) async throws -> any DirectByteStream {
        openedTags.record(tag)
        return try await streams.open()
    }

    /// The transport reports about a target it was given, whether or not
    /// that target is still the selected one.
    @MainActor
    func report(_ report: DirectPathReport, for target: DirectTarget) {
        reporters[target.selection]?(report)
    }
}
