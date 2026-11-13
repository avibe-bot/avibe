import Network
import ObjectiveC
import UIKit
import WebKit
import XCTest
@testable import Avibe

/// The path controller, driven through the App's own
/// `MobileSessionCoordinator` and `AccountWebsiteDataStore`, with stand-ins
/// only for the account transport, the direct transport, and the public
/// origin. Connections are real loopback sockets to the real listener.
final class DirectPathControllerTests: XCTestCase {
    private let one = "instance-one.example"
    private let two = "instance-two.example"
    private let established = "HTTP/1.1 200 Connection Established\r\n\r\n"

    // MARK: The route

    @MainActor
    func testTheFirstRouteBindsOneLoopbackListenerAndTheRouteOnlyEverGrows() async throws {
        try assertSocketScanSeesListeners()
        let listening = try listeningSockets()
        let routeWrites = try ProxyConfigurationWrites.count()
        let harness = try Harness(test: self)
        try await harness.signIn()
        // Nothing exists before a runtime opens.
        XCTAssertEqual(try newListeners(since: listening), [])
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)

        try await harness.select("instance-1")

        let listener = try XCTUnwrap(try newListeners(since: listening).first)
        XCTAssertEqual(try newListeners(since: listening), [listener])
        XCTAssertEqual(listener.address, "127.0.0.1")
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites + 1)
        let route = try XCTUnwrap(harness.currentRoutes.first)
        XCTAssertEqual(harness.currentRoutes.count, 1)
        XCTAssertEqual(route.matchDomains, [one])
        XCTAssertTrue(route.allowFailover)
        // The transport is told exactly what it is asked to reach.
        let target = try XCTUnwrap(harness.transport.selections.last)
        XCTAssertEqual(target.runtime.instance.id, "instance-1")
        XCTAssertEqual(target.authority.connectTarget, "instance-one.example:443")
        XCTAssertEqual(target.storeIdentifier, harness.websiteDataStore.identifier)
        // Selecting is not preferring.
        XCTAssertEqual(harness.status("instance-1"), .initial)

        // Another instance joins the same route on the same listener.
        try await harness.select("instance-2")
        XCTAssertEqual(try newListeners(since: listening), [listener])
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites + 2)
        XCTAssertEqual(harness.currentRoutes.first?.matchDomains, [one, two])

        // Going back writes nothing: the host is already routed.
        try await harness.select("instance-1")
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites + 2)
        XCTAssertEqual(harness.currentRoutes.first?.matchDomains, [one, two])
    }

    @MainActor
    func testAnInstanceThatIsNotOnTheDefaultHTTPSPortGetsNoRouteAndNoListener() async throws {
        let listening = try listeningSockets()
        let routeWrites = try ProxyConfigurationWrites.count()
        let harness = try Harness(test: self)
        try await harness.signIn()

        try await harness.select("instance-custom-port")

        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
        XCTAssertEqual(try newListeners(since: listening), [])
        XCTAssertEqual(harness.transport.selections.count, 0)
    }

    @MainActor
    func testAListenerThatIsNotReadyInTimeExcludesTheHostForThatStore() async throws {
        let listening = try listeningSockets()
        let routeWrites = try ProxyConfigurationWrites.count()
        // No time at all for the listener: the first open cannot wait.
        let harness = try Harness(
            timing: DirectPathTiming(listenerWaitSeconds: 0, rebindSeconds: 10),
            test: self
        )
        try await harness.signIn()

        try await harness.select("instance-1")

        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
        XCTAssertTrue(
            harness.controller.routeOwner.isExcluded(one, store: harness.websiteDataStore.identifier)
        )
        XCTAssertEqual(harness.transport.selections.count, 0)
        // With no route written, no listener is left behind.
        try await eventually { try newListeners(since: listening).isEmpty }

        // The host loaded without a route on this store, so it never gets
        // one there: WebKit would keep its existing connections.
        try await harness.select("instance-2")
        try await harness.select("instance-1")
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
    }

    @MainActor
    func testAnOpenThatIsNoLongerWantedAddsNothingAndLeavesNoListener() async throws {
        let listening = try listeningSockets()
        let routeWrites = try ProxyConfigurationWrites.count()
        let harness = try Harness(test: self)
        try await harness.signIn()
        _ = try harness.coordinator.selectInstance(id: "instance-1")
        let runtime = try XCTUnwrap(harness.lastOpened)

        await harness.controller.prepareRoute(for: runtime, stillWanted: { false })

        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
        XCTAssertFalse(
            harness.controller.routeOwner.isExcluded(one, store: harness.websiteDataStore.identifier)
        )
        try await eventually { try newListeners(since: listening).isEmpty }
    }

    @MainActor
    func testTheRouteOwnerOnlyAddsAndStopsAtSixteenHosts() throws {
        let owner = DirectRouteOwner()
        let store = UUID()
        let dataStore = WKWebsiteDataStore.nonPersistent()
        let hosts = (1...17).map { "host-\($0).example" }

        for host in hosts.prefix(16) {
            XCTAssertTrue(owner.add(host, store: store, dataStore: dataStore, port: 40_000))
        }
        XCTAssertFalse(owner.add(hosts[16], store: store, dataStore: dataStore, port: 40_000))
        XCTAssertEqual(dataStore.proxyConfigurations.first?.matchDomains, Array(hosts.prefix(16)))
        XCTAssertEqual(dataStore.proxyConfigurations.count, 1)
        // A host already routed is still routed; nothing is ever removed.
        XCTAssertTrue(owner.add(hosts[0], store: store, dataStore: dataStore, port: 40_000))

        // An excluded host is never added to its store.
        let other = UUID()
        let otherStore = WKWebsiteDataStore.nonPersistent()
        owner.exclude(hosts[1], store: other)
        XCTAssertFalse(owner.add(hosts[1], store: other, dataStore: otherStore, port: 40_000))
        XCTAssertTrue(owner.add(hosts[0], store: other, dataStore: otherStore, port: 40_000))
        XCTAssertEqual(otherStore.proxyConfigurations.first?.matchDomains, [hosts[0]])
        // Routing a host makes a later exclusion of it on that store moot.
        owner.exclude(hosts[0], store: other)
        XCTAssertFalse(owner.isExcluded(hosts[0], store: other))
    }

    // MARK: The preference

    @MainActor
    func testThePreferenceFollowsTheTransportAndNoRouteIsEverWrittenForIt() async throws {
        let routeWrites = try ProxyConfigurationWrites.count()
        let harness = try Harness(test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let target = try XCTUnwrap(harness.transport.selections.last)
        let afterRoute = try ProxyConfigurationWrites.count()
        XCTAssertEqual(afterRoute, routeWrites + 1)

        harness.transport.report(.usable(generation: "g1"), for: target)
        XCTAssertEqual(harness.status("instance-1"), DirectPathStatus(preferred: .direct, sequence: 1, closure: 0))
        // Only the selected instance's origin is ever preferred direct.
        XCTAssertEqual(harness.status("instance-2").preferred, .tunnel)

        harness.transport.report(.verifying, for: target)
        XCTAssertEqual(harness.status("instance-1"), DirectPathStatus(preferred: .tunnel, sequence: 2, closure: 0))
        harness.transport.report(.usable(generation: "g1"), for: target)
        XCTAssertEqual(harness.status("instance-1").preferred, .direct)
        harness.transport.report(.lost, for: target)
        XCTAssertEqual(harness.status("instance-1"), DirectPathStatus(preferred: .tunnel, sequence: 4, closure: 0))
        harness.controller.runtimeDidStop()
        XCTAssertEqual(harness.status("instance-1").preferred, .tunnel)

        XCTAssertEqual(harness.preferences, [.direct, .tunnel, .direct, .tunnel])
        // Ordinary loss of the path is never the listener fault.
        XCTAssertFalse(harness.controller.isFaulted)
        XCTAssertEqual(harness.faults, 0)
        XCTAssertEqual(try ProxyConfigurationWrites.count(), afterRoute)
        XCTAssertEqual(harness.currentRoutes.first?.matchDomains, [one])
    }

    @MainActor
    func testAReportForAnEndedSelectionChangesNothing() async throws {
        let harness = try Harness(test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let first = try XCTUnwrap(harness.transport.selections.last)
        try await harness.select("instance-2")
        let second = try XCTUnwrap(harness.transport.selections.last)
        XCTAssertNotEqual(first, second)

        harness.transport.report(.usable(generation: "g1"), for: first)
        XCTAssertEqual(harness.preferences, [])
        XCTAssertEqual(harness.status("instance-1").preferred, .tunnel)

        await harness.websiteDataStore.rotate()
        harness.transport.report(.usable(generation: "g1"), for: second)
        XCTAssertEqual(harness.preferences, [])
        XCTAssertEqual(harness.status("instance-2").preferred, .tunnel)
    }

    @MainActor
    func testAPlanOffersDirectOnlyWhileAuthorizedAndAnOlderPlanIsFenced() async throws {
        let harness = try Harness(test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let target = try XCTUnwrap(harness.transport.selections.last)
        let authority = try XCTUnwrap(DirectAuthority(host: one))

        // A host that was never routed is refused.
        XCTAssertNil(harness.controller.plan(for: try XCTUnwrap(DirectAuthority(host: two))))
        let tunnelPlan = try XCTUnwrap(harness.controller.plan(for: authority))
        XCTAssertNil(tunnelPlan.openDirect)
        let tunnelCurrent = await tunnelPlan.isCurrent()
        XCTAssertTrue(tunnelCurrent)

        harness.transport.report(.usable(generation: "g1"), for: target)
        let directPlan = try XCTUnwrap(harness.controller.plan(for: authority))
        XCTAssertNotNil(directPlan.openDirect)
        let directCurrent = await directPlan.isCurrent()
        XCTAssertTrue(directCurrent)

        // Any event of the path-generation table fences both.
        harness.transport.report(.lost, for: target)
        let directAfter = await directPlan.isCurrent()
        let tunnelAfter = await tunnelPlan.isCurrent()
        XCTAssertFalse(directAfter)
        XCTAssertFalse(tunnelAfter)
        XCTAssertNil(harness.controller.plan(for: authority)?.openDirect)
    }

    // MARK: Closing relays

    @MainActor
    func testEachEventClosesOnlyWhatItsRowSaysAndCountsOneClosurePerHost() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let target = try XCTUnwrap(harness.transport.selections.last)
        let port = try harness.listenerPort(since: listening)

        // Before the path is usable, a connection goes to the public origin.
        let viaPublic = LoopbackClient(port: port)
        let publicStream = try await establishPublic(viaPublic, harness: harness)

        harness.transport.report(.usable(generation: "g1"), for: target)
        let a = LoopbackClient(port: port)
        let aStream = try await establishDirect(a, harness: harness)
        let b = LoopbackClient(port: port)
        let bStream = try await establishDirect(b, harness: harness)
        XCTAssertEqual(
            harness.transport.openedTags.values.map(\.rendezvousGeneration),
            ["g1", "g1"]
        )

        // Verifying closes nothing.
        harness.transport.report(.verifying, for: target)
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertFalse(aStream.isCancelled || bStream.isCancelled || publicStream.isCancelled)
        XCTAssertEqual(harness.status("instance-1").closure, 0)

        // Lost closes the direct relays only, and counts once for the host.
        harness.transport.report(.usable(generation: "g1"), for: target)
        harness.transport.report(.lost, for: target)
        try await eventually { aStream.isCancelled && bStream.isCancelled }
        try await eventually { harness.status("instance-1").closure == 1 }
        XCTAssertFalse(publicStream.isCancelled)

        // A new rendezvous generation closes the relays of the old one.
        harness.transport.report(.usable(generation: "g1"), for: target)
        let c = LoopbackClient(port: port)
        let cStream = try await establishDirect(c, harness: harness)
        harness.transport.report(.usable(generation: "g2"), for: target)
        try await eventually { cStream.isCancelled }
        try await eventually { harness.status("instance-1").closure == 2 }
        // The replaced generation is never accepted again.
        let before = harness.preferences
        harness.transport.report(.verifying, for: target)
        harness.transport.report(.usable(generation: "g1"), for: target)
        XCTAssertEqual(harness.preferences, before + [.tunnel])
        harness.transport.report(.usable(generation: "g2"), for: target)
        XCTAssertEqual(harness.status("instance-1").preferred, .direct)

        // The runtime going away closes direct relays; that is no closure.
        let d = LoopbackClient(port: port)
        let dStream = try await establishDirect(d, harness: harness)
        harness.controller.runtimeDidStop()
        try await eventually { dStream.isCancelled }
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(harness.status("instance-1").closure, 2)
        XCTAssertFalse(publicStream.isCancelled)

        // An account transition closes everything; no closure either.
        await harness.websiteDataStore.rotate()
        try await eventually { publicStream.isCancelled }
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(harness.status("instance-1").closure, 2)

        for client in [viaPublic, a, b, c, d] {
            client.close()
        }
    }

    // MARK: Background

    @MainActor
    func testTheBackgroundClosesDirectRelaysAndTheClosureAndStatusWaitForTheReturn() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let target = try XCTUnwrap(harness.transport.selections.last)
        let port = try harness.listenerPort(since: listening)
        harness.transport.report(.usable(generation: "g1"), for: target)
        let client = LoopbackClient(port: port)
        let stream = try await establishDirect(client, harness: harness)

        harness.enterBackground()
        XCTAssertEqual(harness.status("instance-1").preferred, .tunnel)
        try await eventually { stream.isCancelled }
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        // Counted, but not published while in the background.
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(harness.status("instance-1").closure, 0)

        harness.enterForeground()
        // A status request on the return is held until the listener is
        // ready again, or until the re-bind has failed.
        let replies = harness.requestStatus("instance-1")
        XCTAssertEqual(replies.values, [])
        try await eventually { replies.values.count == 1 }

        // Whether the same port can be re-bound with its old connections'
        // remains is not established. Either way the App never pretends.
        if harness.controller.isFaulted {
            print("B2 re-bind after background: listener fault")
            // The held answer is settled without a status.
            XCTAssertEqual(replies.values, [nil])
            XCTAssertEqual(harness.faults, 1)
            harness.transport.report(.usable(generation: "g1"), for: target)
            XCTAssertEqual(harness.status("instance-1").preferred, .tunnel)
        } else {
            print("B2 re-bind after background: listener ready")
            XCTAssertEqual(replies.values.first??.closure, 1)
            XCTAssertEqual(harness.status("instance-1").closure, 1)
            XCTAssertEqual(harness.faults, 0)
            XCTAssertEqual(harness.controller.listenerState, .ready)
            // The return selects again, under a new selection.
            let reselected = try XCTUnwrap(harness.transport.selections.last)
            XCTAssertGreaterThan(reselected.selection, target.selection)
            harness.transport.report(.usable(generation: "g1"), for: target)
            XCTAssertEqual(harness.status("instance-1").preferred, .tunnel)
            harness.transport.report(.usable(generation: "g1"), for: reselected)
            XCTAssertEqual(harness.status("instance-1").preferred, .direct)
        }
        client.close()
    }

    @MainActor
    func testAPortSomeoneElseTookOnTheReturnMeansDirectIsNeverAuthorizedAgain() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let target = try XCTUnwrap(harness.transport.selections.last)
        let port = try harness.listenerPort(since: listening)
        harness.transport.report(.usable(generation: "g1"), for: target)
        let selections = harness.transport.selections.count

        harness.enterBackground()
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: port.rawValue,
            reuseAddress: true
        )
        defer { squatter.close() }
        let routeWrites = try ProxyConfigurationWrites.count()
        harness.enterForeground()
        let replies = harness.requestStatus("instance-1")
        XCTAssertEqual(replies.values, [])

        try await eventually { harness.controller.isFaulted }
        try await eventually { replies.values.count == 1 }
        // The held answer is settled without a status, and so is a new one.
        XCTAssertEqual(replies.values, [nil])
        XCTAssertEqual(harness.requestStatus("instance-1").values, [nil])
        XCTAssertEqual(harness.faults, 1)
        let announced = harness.announcements
        // No new selection, no direct preference, no direct candidate.
        XCTAssertEqual(harness.transport.selections.count, selections)
        harness.transport.report(.usable(generation: "g1"), for: target)
        XCTAssertEqual(harness.status("instance-1").preferred, .tunnel)
        XCTAssertNil(harness.controller.plan(for: try XCTUnwrap(DirectAuthority(host: one)))?.openDirect)

        // Nothing undoes it in this process: not another background and
        // return, not another failure callback.
        squatter.close()
        harness.enterBackground()
        harness.enterForeground()
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertTrue(harness.controller.isFaulted)
        XCTAssertEqual(harness.transport.selections.count, selections)
        if let listener = harness.controller.listener {
            harness.controller.listenerDidFail(listener)
        }
        XCTAssertEqual(harness.faults, 1)
        XCTAssertEqual(harness.announcements, announced)

        // No route is rewritten to another port, and no new host is added.
        // The fault covers only a runtime whose host is routed.
        try await harness.select("instance-2")
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
        let second = try XCTUnwrap(harness.lastOpened)
        XCTAssertFalse(harness.controller.faultCovers(second))
        XCTAssertEqual(harness.requestStatus("instance-2").values.first??.preferred, .tunnel)
        try await harness.select("instance-1")
        XCTAssertTrue(harness.controller.faultCovers(try XCTUnwrap(harness.lastOpened)))
    }

    @MainActor
    func testAListenerSocketFailureAfterARouteIsOneTerminalFaultAndAStaleOneIsNothing() async throws {
        let harness = try Harness(test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let target = try XCTUnwrap(harness.transport.selections.last)
        harness.transport.report(.usable(generation: "g1"), for: target)
        let listener = try XCTUnwrap(harness.controller.listener)

        // A listener this controller does not own fails: nothing happens.
        let stranger = DirectConnectListener(planner: { _ in nil }, onListenerFailure: {})
        harness.controller.listenerDidFail(stranger)
        XCTAssertFalse(harness.controller.isFaulted)
        XCTAssertEqual(harness.status("instance-1").preferred, .direct)

        let announced = harness.announcements
        harness.controller.listenerDidFail(listener)
        harness.controller.listenerDidFail(listener)

        XCTAssertTrue(harness.controller.isFaulted)
        XCTAssertEqual(harness.faults, 1)
        // No event presents anything after the fault.
        XCTAssertEqual(harness.announcements, announced)
        XCTAssertEqual(harness.requestStatus("instance-1").values, [nil])
        XCTAssertNil(harness.controller.plan(for: try XCTUnwrap(DirectAuthority(host: one)))?.openDirect)
        XCTAssertTrue(harness.controller.faultCovers(try XCTUnwrap(harness.lastOpened)))
    }

    @MainActor
    func testHeldStatusAnswersEndWithTheRuntimeOrAccountTheyWereAskedUnder() async throws {
        let harness = try Harness(test: self)
        try await harness.signIn()
        try await harness.select("instance-1")

        harness.enterBackground()
        harness.enterForeground()
        let held = harness.requestStatus("instance-1")
        XCTAssertEqual(held.values, [])
        // The runtime goes while the answer is held: no status reaches it.
        harness.controller.runtimeDidStop()
        XCTAssertEqual(held.values, [nil])
        try await eventually { harness.controller.listenerState == .ready }

        harness.enterBackground()
        harness.enterForeground()
        let heldAgain = harness.requestStatus("instance-1")
        XCTAssertEqual(heldAgain.values, [])
        // The account changes while the answer is held.
        await harness.websiteDataStore.rotate()
        XCTAssertEqual(heldAgain.values, [nil])
    }

    @MainActor
    func testNoBindHappensInTheBackgroundAndABindThatEndsThereIsParked() async throws {
        let listening = try listeningSockets()
        let routeWrites = try ProxyConfigurationWrites.count()
        let harness = try Harness(test: self)
        try await harness.signIn()

        // In the background no listener is bound and no route is written:
        // the host loads without one, so it never gets one on this store.
        harness.enterBackground()
        try await harness.select("instance-1")
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
        XCTAssertEqual(try newListeners(since: listening), [])
        XCTAssertEqual(harness.controller.listenerState, .none)
        XCTAssertTrue(
            harness.controller.routeOwner.isExcluded(one, store: harness.websiteDataStore.identifier)
        )
        harness.enterForeground()

        // A bind under way when the App goes to the background ends parked:
        // its socket closed, no route written, and the host excluded.
        _ = try harness.coordinator.selectInstance(id: "instance-2")
        let runtime = try XCTUnwrap(harness.lastOpened)
        let coordinator = harness.coordinator
        let controller = harness.controller
        let preparing = Task {
            await controller.prepareRoute(for: runtime) {
                coordinator.isCurrentRuntime(runtime)
            }
        }
        var yields = 0
        while harness.controller.listenerState != .binding, yields < 10_000 {
            await Task.yield()
            yields += 1
        }
        XCTAssertEqual(harness.controller.listenerState, .binding)
        harness.enterBackground()
        await preparing.value

        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
        // Parked, and then, with no route pointing at it, stopped: nothing
        // listens until a later open in the foreground binds again.
        XCTAssertEqual(harness.controller.listenerState, .none)
        XCTAssertTrue(
            harness.controller.routeOwner.isExcluded(two, store: harness.websiteDataStore.identifier)
        )
        try await eventually { try newListeners(since: listening).isEmpty }
    }

    // MARK: Races with events

    @MainActor
    func testANetworkChangeCancelsAnOlderDirectRaceAndTheConnectGoesToThePublicOrigin() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let target = try XCTUnwrap(harness.transport.selections.last)
        let port = try harness.listenerPort(since: listening)
        harness.transport.report(.usable(generation: "g1"), for: target)
        // The direct stream never opens, and the hedge is a minute long.
        harness.transport.streams.setMode(.hang)
        let client = LoopbackClient(port: port)
        try await client.send("CONNECT \(one):443 HTTP/1.1\r\nHost: \(one):443\r\n\r\n")
        try await eventually { harness.transport.openedTags.values.count == 1 }

        // The network changes: the old race ends now and the CONNECT is
        // planned again, for the public origin.
        harness.transport.report(.verifying, for: target)
        let stream = try await harness.publicOrigin.streams.waitForStream()
        let answer = try await client.receive(count: established.utf8.count)

        XCTAssertEqual(String(decoding: answer, as: UTF8.self), established)
        XCTAssertEqual(stream.written, Data())
        XCTAssertEqual(harness.transport.openedTags.values.count, 1)
        // Cancelled by the event: no failure reported to the transport.
        XCTAssertEqual(harness.transport.failedTags, [])
        client.close()
    }

    /// The listener fails while the first route is being prepared, at a
    /// different moment each time: whenever it is ready and no route exists
    /// yet, no route may be written then, and the host keeps WebKit's
    /// normal path. Route publication checks the listener again, with no
    /// suspension, after its last wait.
    @MainActor
    func testAListenerFailureWhileTheFirstRouteIsPreparedNeverLeavesARouteToIt() async throws {
        var hitBeforeRoute = 0
        for attempt in 0..<30 {
            let harness = try Harness(test: self)
            try await harness.signIn()
            let controller = harness.controller
            let writes = try ProxyConfigurationWrites.count()
            let selecting = Task { try await harness.select("instance-1") }
            // Wait for the listener, then fail it a little later each time.
            let deadline = ContinuousClock.now.advanced(by: .seconds(10))
            while controller.listenerState != .ready, ContinuousClock.now < deadline {
                try await Task.sleep(for: .milliseconds(1))
            }
            for _ in 0..<attempt {
                await Task.yield()
            }
            let routed = controller.routeOwner.routes(one, store: harness.websiteDataStore.identifier)
            let failed = controller.listener
            if let failed, controller.listenerState == .ready {
                controller.listenerDidFail(failed)
            }
            try await selecting.value

            guard failed != nil else {
                continue
            }
            if routed {
                XCTAssertTrue(controller.isFaulted, "attempt \(attempt)")
            } else {
                hitBeforeRoute += 1
                XCTAssertEqual(try ProxyConfigurationWrites.count(), writes, "attempt \(attempt)")
                XCTAssertTrue(
                    controller.routeOwner.isExcluded(one, store: harness.websiteDataStore.identifier),
                    "attempt \(attempt)"
                )
                XCTAssertFalse(controller.isFaulted, "attempt \(attempt)")
                XCTAssertNil(controller.listener, "attempt \(attempt)")
                XCTAssertEqual(harness.transport.selections.count, 0, "attempt \(attempt)")
            }
            harness.stop()
        }
        print("Listener failures before the first route: \(hitBeforeRoute) of 30")
        XCTAssertGreaterThan(hitBeforeRoute, 0)
    }

    @MainActor
    func testAListenerFailureWhileALaterRouteIsPreparedIsTheFaultAndWritesNoRoute() async throws {
        var hitBeforeRoute = 0
        for attempt in 0..<30 {
            let harness = try Harness(test: self)
            try await harness.signIn()
            try await harness.select("instance-1")
            let controller = harness.controller
            let writes = try ProxyConfigurationWrites.count()
            let selecting = Task { try await harness.select("instance-2") }
            for _ in 0..<attempt {
                await Task.yield()
            }
            let routed = controller.routeOwner.routes(two, store: harness.websiteDataStore.identifier)
            if let listener = controller.listener {
                controller.listenerDidFail(listener)
            }
            try await selecting.value

            XCTAssertTrue(controller.isFaulted, "attempt \(attempt)")
            XCTAssertEqual(harness.faults, 1, "attempt \(attempt)")
            if !routed {
                hitBeforeRoute += 1
                XCTAssertEqual(try ProxyConfigurationWrites.count(), writes, "attempt \(attempt)")
                XCTAssertEqual(harness.currentRoutes.first?.matchDomains, [one], "attempt \(attempt)")
            }
            harness.stop()
        }
        XCTAssertGreaterThan(hitBeforeRoute, 0)
    }

    /// A push tap opens an instance whose route exists while the listener
    /// is still being bound again after the background: the open waits for
    /// it rather than load against a closed port.
    @MainActor
    func testOpeningARoutedInstanceDuringTheReturnWaitsForTheListener() async throws {
        let harness = try Harness(test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        harness.enterBackground()
        harness.enterForeground()
        XCTAssertEqual(harness.controller.listenerState, .rebinding)

        let runtime = try XCTUnwrap(harness.lastOpened)
        await harness.controller.prepareRoute(for: runtime) { true }

        XCTAssertNotEqual(harness.controller.listenerState, .rebinding)
    }

    // MARK: Helpers

    @MainActor
    private func establishDirect(
        _ client: LoopbackClient,
        harness: Harness
    ) async throws -> StandInDirectStream {
        let before = harness.transport.streams.opened.count
        try await client.send(
            "CONNECT \(one):443 HTTP/1.1\r\nHost: \(one):443\r\n\r\n"
        )
        let stream = try await harness.transport.streams.waitForStream(count: before + 1)
        stream.instanceSends(Data(established.utf8))
        let answer = try await client.receive(count: established.utf8.count)
        XCTAssertEqual(String(decoding: answer, as: UTF8.self), established)
        return stream
    }

    @MainActor
    private func establishPublic(
        _ client: LoopbackClient,
        harness: Harness
    ) async throws -> StandInDirectStream {
        let before = harness.publicOrigin.streams.opened.count
        try await client.send(
            "CONNECT \(one):443 HTTP/1.1\r\nHost: \(one):443\r\n\r\n"
        )
        let stream = try await harness.publicOrigin.streams.waitForStream(count: before + 1)
        let answer = try await client.receive(count: established.utf8.count)
        XCTAssertEqual(String(decoding: answer, as: UTF8.self), established)
        return stream
    }
}

// MARK: - Harness

/// The same wiring `AppModel` has, around stand-ins.
@MainActor
private final class Harness {
    let websiteDataStore: AccountWebsiteDataStore
    let account = StandInAccountTransport()
    let transport = StandInDirectTransport()
    let publicOrigin = StandInPublicOrigin()
    private let stores = InMemoryAccountStores()
    private let notifications = NotificationCenter()
    private let timing: DirectPathTiming
    /// `preferred` for instance-1 each time the controller announced a change.
    private(set) var preferences: [DirectPathStatus.Preference] = []
    /// Every announcement, whatever changed.
    private(set) var announcements = 0
    /// How many times the controller reported the listener fault.
    private(set) var faults = 0
    private(set) var lastOpened: MobileRuntimeContext?

    private(set) lazy var controller: DirectPathController = DirectPathController(
        websiteDataStore: websiteDataStore,
        transport: transport,
        isCurrentRuntime: { [unowned self] in
            self.coordinator.isCurrentRuntime($0)
        },
        publicBackend: DirectPublicBackend(resolver: publicOrigin, dialer: publicOrigin),
        limits: .patient,
        timing: timing,
        notificationCenter: notifications,
        isInBackground: false,
        onStatusChanged: { [unowned self] in
            self.announcements += 1
            let status = self.status("instance-1")
            if self.preferences.last != status.preferred {
                self.preferences.append(status.preferred)
            }
        },
        onFault: { [unowned self] in
            self.faults += 1
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
            self.lastOpened = $0
        },
        onPrepareAccountTransition: { [unowned self] in
            self.controller.runtimeDidStop()
            return []
        }
    )

    private let defaults: UserDefaults

    init(
        timing: DirectPathTiming = DirectPathTiming(listenerWaitSeconds: 30, rebindSeconds: 10),
        test: XCTestCase
    ) throws {
        let suiteName = "DirectPathControllerTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        test.addTeardownBlock {
            defaults.removePersistentDomain(forName: suiteName)
        }
        self.defaults = defaults
        self.timing = timing
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

    /// Closes what the test left open: the account transition closes every
    /// relay, and the controller's listener goes with the controller.
    func stop() {
        controller.accountWillChange()
    }

    var currentRoutes: [ProxyConfiguration] {
        websiteDataStore.dataStore.proxyConfigurations
    }

    func status(_ instanceID: String) -> DirectPathStatus {
        controller.currentStatus(for: origin(instanceID))
    }

    /// Asks `direct.status` the way the bridge does; the answer comes later
    /// when it is held.
    func requestStatus(_ instanceID: String) -> Recorder<DirectPathStatus?> {
        let replies = Recorder<DirectPathStatus?>()
        controller.status(for: origin(instanceID)) { replies.record($0) }
        return replies
    }

    func listenerPort(since baseline: Set<ListeningSocket>) throws -> NWEndpoint.Port {
        let socket = try XCTUnwrap(try newListeners(since: baseline).first)
        return try XCTUnwrap(NWEndpoint.Port(rawValue: socket.port))
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

    /// Selects an instance and opens its runtime the way `AppModel` does:
    /// the previous runtime stops, the route is prepared, then it opens.
    func select(_ instanceID: String) async throws {
        _ = try coordinator.selectInstance(id: instanceID)
        let runtime = try XCTUnwrap(lastOpened)
        controller.runtimeDidStop()
        let coordinator = coordinator
        await controller.prepareRoute(for: runtime) {
            coordinator.isCurrentRuntime(runtime)
        }
        guard coordinator.isCurrentRuntime(runtime) else {
            return
        }
        controller.runtimeDidOpen(runtime)
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

/// The public origin in a test: every host resolves to one public address,
/// and every dial hands back a stand-in stream. Shared with the App-level
/// tests.
final class StandInPublicOrigin: DirectHostResolving, DirectPublicDialing, @unchecked Sendable {
    let streams = StandInStreamSource()

    func resolve(_ host: String) async throws -> [DirectResolvedAddress] {
        [DirectResolvedAddress(ipv4: [104, 16, 132, 229])!]
    }

    func dial(_ address: DirectResolvedAddress, port: UInt16) async throws -> any DirectByteStream {
        try await streams.open()
    }
}

/// Account stores that exist only in memory, all kept so a test can look at
/// one after it was rotated away.
@MainActor
private final class InMemoryAccountStores {
    private(set) var created: [WKWebsiteDataStore] = []

    var operations: AccountWebsiteDataStoreOperations {
        AccountWebsiteDataStoreOperations(
            makeDataStore: { [self] _ in
                let store = WKWebsiteDataStore.nonPersistent()
                created.append(store)
                return store
            },
            removeIdentifier: { _ in
                nil
            },
            // Nothing here is a named store on disk.
            listIdentifiers: {
                []
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

/// Counts every call of WebKit's `proxyConfigurations` setter in this
/// process, on any store, from the first time it is asked. It replaces the
/// setter's implementation with one that counts and then calls the original,
/// so it sees a write wherever it comes from, including one that sets the
/// value the store already had, and on stores made later. Shared with the
/// App-level tests.
enum ProxyConfigurationWrites {
    struct SetterNotFound: Error {}

    private static let lock = NSLock()
    private static var calls = 0
    private static let installed: Bool = {
        let selector = NSSelectorFromString("setProxyConfigurations:")
        guard
            let method = class_getInstanceMethod(
                WKWebsiteDataStore.self,
                selector
            )
        else {
            return false
        }
        typealias Setter = @convention(c) (AnyObject, Selector, AnyObject?) -> Void
        let original = unsafeBitCast(
            method_getImplementation(method),
            to: Setter.self
        )
        let counting: @convention(block) (AnyObject, AnyObject?) -> Void = {
            store, configurations in
            lock.withLock { calls += 1 }
            original(store, selector, configurations)
        }
        method_setImplementation(method, imp_implementationWithBlock(counting))
        return true
    }()

    /// The calls so far. Throws if the setter cannot be found, so a count
    /// that cannot see is never read as "no writes".
    static func count() throws -> Int {
        guard installed else {
            throw SetterNotFound()
        }
        return lock.withLock { calls }
    }
}

/// A direct transport the test speaks for. Shared with the App-level tests.
final class StandInDirectTransport: DirectTransporting, @unchecked Sendable {
    let streams = StandInStreamSource()
    let openedTags = Recorder<DirectStreamTag>()
    @MainActor private(set) var selections: [DirectTarget] = []
    @MainActor private(set) var deselectCount = 0
    @MainActor private(set) var failedTags: [DirectStreamTag] = []
    @MainActor private var reporters: [Int: @MainActor (DirectPathReport) -> Void] = [:]

    @MainActor
    func select(
        _ target: DirectTarget,
        report: @escaping @MainActor (DirectPathReport) -> Void
    ) {
        selections.append(target)
        reporters[target.selection] = report
    }

    /// As the protocol says: stop, and close every stream.
    @MainActor
    func deselect() {
        deselectCount += 1
        for stream in streams.opened {
            stream.cancel()
        }
    }

    @MainActor
    func pathDidFail(for tag: DirectStreamTag) {
        failedTags.append(tag)
    }

    func openStream(for tag: DirectStreamTag) async throws -> any DirectByteStream {
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
