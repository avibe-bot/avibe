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
        // With no route written, no listener is left behind. Retired first:
        // whatever its bind reports afterwards is not a failure.
        try await eventually { try newListeners(since: listening).isEmpty }
        XCTAssertEqual(harness.controller.listenerFailures, [])
        XCTAssertNil(harness.controller.listenerRecovery)

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

    /// Checks what the owner writes, not what WebKit does with the array.
    @MainActor
    func testHostPolicyRewriteKeepsTheOtherHostAndStoreIndependent() throws {
        let owner = DirectRouteOwner()
        let store = UUID()
        let data = WKWebsiteDataStore.nonPersistent()
        XCTAssertTrue(owner.add(one, store: store, dataStore: data, port: 40_000))
        XCTAssertTrue(owner.add(two, store: store, dataStore: data, port: 40_000))
        let before = try ProxyConfigurationWrites.count()
        XCTAssertTrue(owner.rewrite(one, store: store, dataStore: data, port: 40_000, allowsFailover: false))
        XCTAssertTrue(owner.rewrite(one, store: store, dataStore: data, port: 40_000, allowsFailover: false))
        XCTAssertEqual(try ProxyConfigurationWrites.count(), before + 1)
        XCTAssertEqual(data.proxyConfigurations.filter { !$0.allowFailover }.map(\.matchDomains), [[one]])
        XCTAssertEqual(data.proxyConfigurations.filter(\.allowFailover).map(\.matchDomains), [[two]])
        let fresh = WKWebsiteDataStore.nonPersistent()
        XCTAssertTrue(owner.add(one, store: UUID(), dataStore: fresh, port: 40_000))
        XCTAssertEqual(fresh.proxyConfigurations.count, 1)
        XCTAssertEqual(fresh.proxyConfigurations.first?.allowFailover, true)
        XCTAssertTrue(owner.rewrite(one, store: store, dataStore: data, port: 40_000, allowsFailover: true))
        XCTAssertEqual(data.proxyConfigurations.count, 1)
        XCTAssertEqual(data.proxyConfigurations.first?.matchDomains, [one, two])
        XCTAssertEqual(data.proxyConfigurations.first?.allowFailover, true)
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
        // Ordinary loss of the path is never a failure of the listener.
        XCTAssertEqual(harness.controller.listenerState, .ready)
        XCTAssertEqual(harness.controller.listenerFailures, [])
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

    @MainActor
    func testStrictBootstrapUsesTunnelThenFencesItAndAcceptsRelay() async throws {
        let harness = try Harness(test: self)
        let url = try XCTUnwrap(URL(string: "https://\(one)"))
        let authority = try XCTUnwrap(DirectAuthority(instanceURL: url))
        let controller = harness.controller
        controller.setDirectOnly(true, forInstanceURL: url)
        XCTAssertEqual(controller.loadGate(forInstanceURL: url), .held)
        XCTAssertFalse(controller.directOnlyNeedsRelaunch(forInstanceURL: url))
        try await harness.signIn()
        try await harness.select("instance-1")
        controller.noteInstanceLoad(forInstanceURL: url)
        let bootstrap = try XCTUnwrap(controller.plan(for: authority))
        XCTAssertNil(bootstrap.openDirect)
        XCTAssertNotNil(bootstrap.openPublic)
        let target = try XCTUnwrap(harness.transport.selections.last)
        harness.transport.currentPath = .relay
        harness.transport.report(.usable(generation: "g1"), for: target)
        try await eventually { controller.isDirectOnlyLinked(forInstanceURL: url) }
        XCTAssertFalse(bootstrap.isCurrent())
        let linked = try XCTUnwrap(controller.plan(for: authority))
        XCTAssertNotNil(linked.openDirect)
        XCTAssertNil(linked.openPublic)
        XCTAssertFalse(try XCTUnwrap(harness.currentRoutes.first).allowFailover)
        harness.transport.report(.lost, for: target)
        XCTAssertFalse(controller.isDirectOnlyLinked(forInstanceURL: url))
        let waiting = try XCTUnwrap(controller.plan(for: authority))
        XCTAssertNil(waiting.openDirect)
        XCTAssertNil(waiting.openPublic)
        XCTAssertEqual(controller.strictFailureCount(forInstanceURL: url), 0)
        harness.transport.report(.answeredWithoutLink, for: target)
        XCTAssertEqual(controller.unlinkedRendezvousCount(forInstanceURL: url), 1)
        controller.setDirectOnly(false, forInstanceURL: url)
        try await eventually { harness.currentRoutes.first?.allowFailover == true }
        XCTAssertFalse(waiting.isCurrent())
        XCTAssertNotNil(try XCTUnwrap(controller.plan(for: authority)).openPublic)
    }

    @MainActor
    func testOnlyAnActualUnroutedLoadNeedsRelaunch() throws {
        let harness = try Harness(test: self)
        let first = try XCTUnwrap(URL(string: "https://\(one)"))
        let other = try XCTUnwrap(URL(string: "https://\(two)"))
        let controller = harness.controller
        _ = controller.loadGate(forInstanceURL: other)
        controller.noteInstanceLoad(forInstanceURL: first)
        for url in [first, other] { controller.setDirectOnly(true, forInstanceURL: url) }
        XCTAssertTrue(controller.directOnlyNeedsRelaunch(forInstanceURL: first))
        XCTAssertFalse(controller.directOnlyNeedsRelaunch(forInstanceURL: other))
        controller.setDirectOnly(false, forInstanceURL: first)
        XCTAssertFalse(controller.directOnlyNeedsRelaunch(forInstanceURL: first))
    }

    // MARK: The path's kind, stickiness, and the race record

    /// `direct.status()` says whether the selected path's far end is on a
    /// local network, and its round-trip time, only when they belong to the
    /// path it names and only while the path is preferred. The event never
    /// carries them, and a change of them is not a change of the status.
    @MainActor
    func testTheStatusCarriesThePathsClassAndRoundTripOnlyForThePathItNames() async throws {
        let harness = try Harness(test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let target = try XCTUnwrap(harness.transport.selections.last)

        harness.transport.currentPath = .ipv6
        harness.transport.selectedPath = DirectSelectedPath(kind: .ipv6, local: true, rttMs: 4)
        harness.transport.report(.usable(generation: "g1"), for: target)
        let local = harness.status("instance-1")
        XCTAssertEqual(local.path, .ipv6)
        XCTAssertEqual(local.jsonObject["path_scope"] as? String, "local")
        XCTAssertEqual(local.jsonObject["rtt_ms"] as? Int, 4)
        XCTAssertNil(local.eventObject["path_scope"])
        XCTAssertNil(local.eventObject["rtt_ms"])

        // Another reading of the same path is not a change of the status.
        harness.transport.selectedPath = DirectSelectedPath(kind: .ipv6, local: false, rttMs: nil)
        let global = harness.status("instance-1")
        XCTAssertEqual(global, local)
        XCTAssertEqual(global.jsonObject["path_scope"] as? String, "global")
        XCTAssertNil(global.jsonObject["rtt_ms"], "not measured: not shown")

        // A reading of another kind of path than the one named: neither.
        harness.transport.selectedPath = DirectSelectedPath(kind: .ipv4, local: true, rttMs: 9)
        XCTAssertNil(harness.status("instance-1").jsonObject["path_scope"])
        XCTAssertNil(harness.status("instance-1").jsonObject["rtt_ms"])

        // The relay has a round trip and no class.
        harness.transport.currentPath = .relay
        harness.transport.selectedPath = DirectSelectedPath(kind: .relay, local: nil, rttMs: 120)
        harness.transport.report(.pathChanged(.relay), for: target)
        let relay = harness.status("instance-1")
        XCTAssertEqual(relay.path, .relay)
        XCTAssertNil(relay.jsonObject["path_scope"])
        XCTAssertEqual(relay.jsonObject["rtt_ms"] as? Int, 120)

        // Another host is never told this host's path.
        XCTAssertNil(harness.status("instance-2").jsonObject["rtt_ms"])

        // Lost: no path, so neither.
        harness.transport.report(.lost, for: target)
        XCTAssertNil(harness.status("instance-1").jsonObject["path_scope"])
        XCTAssertNil(harness.status("instance-1").jsonObject["rtt_ms"])
    }

    /// `direct.status()` carries how often the transport checked its path
    /// again, and why: counts only, for the selected host only, never in
    /// the event, and no change of the status.
    @MainActor
    func testTheStatusCarriesTheTransportsCountsForTheSelectedHostOnly() async throws {
        let harness = try Harness(test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let target = try XCTUnwrap(harness.transport.selections.last)
        harness.transport.report(.usable(generation: "g1"), for: target)

        // Nothing counted: nothing said.
        let quiet = harness.status("instance-1")
        XCTAssertNil(quiet.jsonObject["counts"])

        harness.transport.counts = DirectPathCounts(
            verifications: 2,
            probesAfterPathUpdate: 2,
            probesAfterCandidateFailure: 1,
            probesAfterRenewal: 0,
            dials: 3,
            pathUpdates: 2,
            pathUpdatesIgnored: 41
        )
        let counted = harness.status("instance-1")
        XCTAssertEqual(counted, quiet, "counts are no change of the status")
        XCTAssertNil(counted.eventObject["counts"])
        let counts = try XCTUnwrap(counted.jsonObject["counts"] as? [String: Any])
        XCTAssertEqual(counts["verifications"] as? Int, 2)
        XCTAssertEqual(counts["dials"] as? Int, 3)
        XCTAssertEqual(counts["path_updates"] as? Int, 2)
        XCTAssertEqual(counts["path_updates_ignored"] as? Int, 41)
        XCTAssertEqual(
            counts["probes"] as? [String: Int],
            ["path_update": 2, "candidate_failed": 1, "renewal": 0]
        )
        // Numbers and nothing else.
        XCTAssertEqual(
            Set(counts.keys),
            ["verifications", "probes", "dials", "path_updates", "path_updates_ignored"]
        )

        // Another host is told nothing of this host's path.
        XCTAssertNil(harness.status("instance-2").jsonObject["counts"])
    }

    @MainActor
    func testThePathKindIsPublishedAndADirectAddressMakesPlansStickyUntilAMiss() async throws {
        let harness = try Harness(test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let target = try XCTUnwrap(harness.transport.selections.last)
        let authority = try XCTUnwrap(DirectAuthority(host: one))

        // Usable through the relay: the page is told so, and plans hedge.
        harness.transport.currentPath = .relay
        harness.transport.report(.usable(generation: "g1"), for: target)
        var status = harness.status("instance-1")
        XCTAssertEqual(status.preferred, .direct)
        XCTAssertEqual(status.path, .relay)
        XCTAssertEqual(status.sequence, 1)
        XCTAssertEqual(status.eventObject["path"] as? String, "relay")
        // Without a reading of the path's class and round trip, the status
        // has neither.
        XCTAssertNil(status.jsonObject["path_scope"])
        XCTAssertNil(status.jsonObject["rtt_ms"])
        var plan = try XCTUnwrap(harness.controller.plan(for: authority))
        XCTAssertFalse(plan.sticky)
        XCTAssertEqual(plan.path, .relay)

        // The connection reaches a direct address: a new sequence, nothing
        // closed, and plans are sticky. The same path again is no change.
        harness.transport.report(.pathChanged(.ipv4), for: target)
        harness.transport.report(.pathChanged(.ipv4), for: target)
        // A CONNECT planned before the change is still current across it: it
        // is answered by whichever candidate is ready, never dropped.
        let stillCurrent = await plan.isCurrent()
        XCTAssertTrue(stillCurrent)
        status = harness.status("instance-1")
        XCTAssertEqual(status.preferred, .direct)
        XCTAssertEqual(status.path, .ipv4)
        XCTAssertEqual(status.sequence, 2)
        XCTAssertEqual(status.closure, 0)
        plan = try XCTUnwrap(harness.controller.plan(for: authority))
        XCTAssertTrue(plan.sticky)
        XCTAssertEqual(plan.path, .ipv4)

        // A sticky attempt got no answer within its bound: stickiness is
        // dropped at once, and the transport is NOT told, so nothing probes
        // the path or closes what it still carries.
        plan.stickyMissed?()
        try await eventually {
            try XCTUnwrap(harness.controller.plan(for: authority)).sticky == false
        }
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(harness.transport.failedTags.count, 0)
        // A CONNECT planned after the miss wins a hedged race on the direct
        // path: sticky again.
        let second = try XCTUnwrap(harness.controller.plan(for: authority))
        second.recordRace?(DirectRaceRecord(
            direct: .ready(40), publicOrigin: .none, winner: .direct, path: .ipv4, sticky: false
        ))
        try await eventually { harness.status("instance-1").races.count == 1 }
        XCTAssertTrue(try XCTUnwrap(harness.controller.plan(for: authority)).sticky)
        // Only now does the first CONNECT's race end (its public candidate
        // was slow). Its record is kept, and its old miss undoes nothing.
        plan.recordRace?(DirectRaceRecord(
            direct: .timedOut, publicOrigin: .ready(1_520), winner: .publicOrigin, path: .ipv4, sticky: true
        ))
        try await eventually { harness.status("instance-1").races.count == 2 }
        XCTAssertTrue(try XCTUnwrap(harness.controller.plan(for: authority)).sticky)

        // A real failure of a direct candidate (refused, stream or open
        // error): dropped at once too, and this one tells the transport.
        let third = try XCTUnwrap(harness.controller.plan(for: authority))
        third.directFailed()
        try await eventually { harness.transport.failedTags.count == 1 }
        XCTAssertFalse(try XCTUnwrap(harness.controller.plan(for: authority)).sticky)
        // A win of a plan made before that failure restores nothing; one
        // made after it does.
        third.recordRace?(DirectRaceRecord(
            direct: .ready(45), publicOrigin: .none, winner: .direct, path: .ipv4, sticky: true
        ))
        try await eventually { harness.status("instance-1").races.count == 3 }
        XCTAssertFalse(try XCTUnwrap(harness.controller.plan(for: authority)).sticky)
        let fourth = try XCTUnwrap(harness.controller.plan(for: authority))
        fourth.recordRace?(DirectRaceRecord(
            direct: .ready(38), publicOrigin: .none, winner: .direct, path: .ipv4, sticky: false
        ))
        try await eventually { harness.status("instance-1").races.count == 4 }
        XCTAssertTrue(try XCTUnwrap(harness.controller.plan(for: authority)).sticky)
        let connections = harness.status("instance-1").jsonObject["connections"] as? [[String: Any]]
        XCTAssertEqual(connections?.last?["direct"] as? Int, 38)
        XCTAssertEqual(connections?.last?["public"] as? String, "none")
        XCTAssertEqual(connections?[1]["direct"] as? String, "timeout")
        XCTAssertEqual(connections?[1]["winner"] as? String, "public")
        // Another host's page learns nothing of it.
        XCTAssertNil(harness.status("instance-2").path)
        XCTAssertEqual(harness.status("instance-2").races, [])

        // Only the last few races are kept.
        for _ in 0..<8 {
            plan.recordRace?(DirectRaceRecord(
                direct: .ready(40), publicOrigin: .none, winner: .direct, path: .ipv4, sticky: true
            ))
        }
        try await eventually {
            harness.status("instance-1").races.count == DirectPathController.raceLimit
        }

        // Back through the relay: told, and no longer sticky.
        harness.transport.report(.pathChanged(.relay), for: target)
        XCTAssertEqual(harness.status("instance-1").path, .relay)
        XCTAssertEqual(harness.status("instance-1").sequence, 3)
        XCTAssertFalse(try XCTUnwrap(harness.controller.plan(for: authority)).sticky)

        // Lost: no path is claimed, and the next selection starts clean.
        harness.transport.report(.pathChanged(.ipv6), for: target)
        harness.transport.report(.lost, for: target)
        XCTAssertEqual(harness.status("instance-1").preferred, .tunnel)
        XCTAssertNil(harness.status("instance-1").path)
        XCTAssertNil(harness.status("instance-1").eventObject["path"])
        let after = try XCTUnwrap(harness.controller.plan(for: authority))
        XCTAssertNil(after.openDirect)
        XCTAssertFalse(after.sticky)
        harness.controller.runtimeDidStop()
        XCTAssertEqual(harness.status("instance-1").races, [])
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
        harness.transport.currentPath = .ipv4
        harness.transport.report(.usable(generation: "g1"), for: target)
        harness.transport.report(.lost, for: target)
        try await eventually { aStream.isCancelled && bStream.isCancelled }
        try await eventually { harness.status("instance-1").closure == 1 }
        XCTAssertFalse(publicStream.isCancelled)
        // What was cut is recorded once for diagnosis: how many relays, how
        // long each had carried nothing, and the path they ran on.
        try await eventually { harness.status("instance-1").losses.count == 1 }
        let loss = try XCTUnwrap(harness.status("instance-1").losses.first)
        XCTAssertEqual(loss.cause, .lost)
        XCTAssertEqual(loss.relays, 2)
        XCTAssertEqual(loss.quietMs.count, 2)
        XCTAssertEqual(loss.quietMs, loss.quietMs.sorted())
        XCTAssertEqual(loss.path, .ipv4)
        let reported = harness.status("instance-1").jsonObject["losses"] as? [[String: Any]]
        XCTAssertEqual(reported?.first?["relays"] as? Int, 2)
        XCTAssertEqual(reported?.first?["cause"] as? String, "lost")
        XCTAssertEqual(reported?.first?["path"] as? String, "ipv4")
        XCTAssertEqual(harness.status("instance-2").losses, [])
        harness.transport.currentPath = nil

        // A new rendezvous generation closes the relays of the old one.
        harness.transport.report(.usable(generation: "g1"), for: target)
        let c = LoopbackClient(port: port)
        let cStream = try await establishDirect(c, harness: harness)
        harness.transport.report(.usable(generation: "g2"), for: target)
        try await eventually { cStream.isCancelled }
        try await eventually { harness.status("instance-1").closure == 2 }
        // Recorded with its own cause: not the sign of a cut request.
        try await eventually { harness.status("instance-1").losses.count == 2 }
        XCTAssertEqual(harness.status("instance-1").losses.last?.cause, .generationReplaced)
        XCTAssertEqual(harness.status("instance-1").losses.last?.relays, 1)
        // The replaced generation is never accepted again.
        let before = harness.preferences
        harness.transport.report(.verifying, for: target)
        harness.transport.report(.usable(generation: "g1"), for: target)
        XCTAssertEqual(harness.preferences, before + [.tunnel])
        harness.transport.report(.usable(generation: "g2"), for: target)
        XCTAssertEqual(harness.status("instance-1").preferred, .direct)

        // The runtime going away closes direct relays; that is no closure,
        // and no loss is recorded for it.
        let d = LoopbackClient(port: port)
        let dStream = try await establishDirect(d, harness: harness)
        harness.controller.runtimeDidStop()
        try await eventually { dStream.isCancelled }
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(harness.status("instance-1").closure, 2)
        XCTAssertEqual(harness.status("instance-1").losses, [])
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

        // Whether the same port can be re-bound at the first attempt with
        // its old connections' remains is not established. Either way the
        // App never pretends: the closure is published once a reconnect
        // can go somewhere, and the status says direct only once the
        // listener serves and the path is usable.
        print("B2 re-bind after background: \(harness.controller.listenerFailures.isEmpty ? "at the first attempt" : "after a failed attempt")")
        XCTAssertEqual(replies.values.first??.preferred, .tunnel)
        try await assertTheReturnPublishedOneClosure(heldAnswers: replies.values, harness: harness)
        XCTAssertNil(harness.controller.listenerRecovery)
        // The return selects again, under a new selection.
        let reselected = try XCTUnwrap(harness.transport.selections.last)
        XCTAssertGreaterThan(reselected.selection, target.selection)
        harness.transport.report(.usable(generation: "g1"), for: target)
        XCTAssertEqual(harness.status("instance-1").preferred, .tunnel)
        harness.transport.report(.usable(generation: "g1"), for: reselected)
        XCTAssertEqual(harness.status("instance-1").preferred, .direct)
        // What the background closed is still on record after the
        // return, with its own cause: it is not a lost path.
        try await eventually { harness.status("instance-1").losses.count == 1 }
        XCTAssertEqual(harness.status("instance-1").losses.first?.cause, .background)
        XCTAssertEqual(harness.status("instance-1").losses.first?.relays, 1)
        client.close()
    }

    // MARK: Failure and recovery

    /// The port is someone else's when the App returns: nothing is
    /// authorized or selected and the routed host's loads are refused, for
    /// as long as the stranger holds it; the same port is tried again and
    /// again, the failure is on record once, and when the stranger lets go
    /// the listener serves the same route again and direct comes back. No
    /// failure is the end.
    @MainActor
    func testAPortSomeoneElseHoldsOnTheReturnIsBoundAgainUntilItIsOursAndThenServes() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(timing: .recovering, test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let runtime = try XCTUnwrap(harness.lastOpened)
        let target = try XCTUnwrap(harness.transport.selections.last)
        let port = try harness.listenerPort(since: listening)
        harness.transport.report(.usable(generation: "g1"), for: target)
        let selections = harness.transport.selections.count
        XCTAssertNil(harness.controller.listenerRecovery)
        // A page's connection on the direct path: the background closes
        // it, which the page is to hear of as one closure.
        let relayed = LoopbackClient(port: port)
        let stream = try await establishDirect(relayed, harness: harness)

        harness.enterBackground()
        XCTAssertNil(harness.controller.listenerRecovery, "Nothing recovers in the background.")
        try await eventually { stream.isCancelled }
        relayed.close()
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: port.rawValue,
            reuseAddress: true
        )
        defer { squatter.close() }
        let routeWrites = try ProxyConfigurationWrites.count()
        let announced = harness.availabilityChanges
        harness.enterForeground()
        // The return's first attempt: recovering, with no failure yet, and
        // held like any return.
        let returned = try XCTUnwrap(harness.controller.listenerRecovery)
        XCTAssertNil(returned.lastFailure)
        XCTAssertFalse(returned.portMayBeForeign)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .held)
        XCTAssertEqual(harness.availabilityChanges, announced + 1)
        let replies = harness.requestStatus("instance-1")
        XCTAssertEqual(replies.values, [])

        try await eventually { harness.controller.listenerRecovery?.lastFailure != nil }
        let failed = try XCTUnwrap(harness.controller.listenerRecovery)
        XCTAssertEqual(failed.since, returned.since)
        XCTAssertEqual(failed.lastFailure?.step, .bind)
        XCTAssertEqual(failed.lastFailure?.code, EADDRINUSE)
        XCTAssertNotNil(failed.lastFailure?.backgroundSeconds)
        XCTAssertTrue(failed.portMayBeForeign)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .fault)
        XCTAssertGreaterThan(harness.availabilityChanges, announced + 1)
        // The held answer is given once the first attempt has failed, as
        // the public path; and so is a new one, at once. Neither has the
        // background's closure yet: a page that reconnected for it now
        // would send that connection to whoever holds the port.
        XCTAssertEqual(replies.values.count, 1)
        XCTAssertEqual(replies.values.first??.preferred, .tunnel)
        XCTAssertEqual(replies.values.first??.closure, 0)
        XCTAssertEqual(harness.requestStatus("instance-1").values.first??.preferred, .tunnel)
        XCTAssertEqual(harness.status("instance-1").closure, 0)
        // No new selection, no direct preference, no direct candidate.
        XCTAssertEqual(harness.transport.selections.count, selections)
        harness.transport.report(.usable(generation: "g1"), for: target)
        XCTAssertEqual(harness.status("instance-1").preferred, .tunnel)
        XCTAssertNil(harness.controller.plan(for: try XCTUnwrap(DirectAuthority(host: one)))?.openDirect)

        // Attempt after attempt, for as long as the stranger is there: the
        // gate refuses throughout (never open, and never the held state a
        // caller may wait out), the recovery is the same one, and the same
        // failure is on record once, where `direct.status()` shows it.
        var seen: Set<String> = []
        let until = ContinuousClock.now.advanced(by: .milliseconds(600))
        while ContinuousClock.now < until {
            seen.insert("\(harness.controller.loadGate(for: runtime))")
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertEqual(seen, ["fault"])
        XCTAssertEqual(harness.controller.listenerRecovery?.since, returned.since)
        XCTAssertEqual(harness.controller.listenerFailures.count, 1)
        XCTAssertEqual(harness.status("instance-1").closure, 0, "Still pending while the stranger is there.")
        XCTAssertEqual(harness.status("instance-1").listenerFaults, harness.controller.listenerFailures)
        XCTAssertEqual(
            harness.status("instance-1").jsonObject["listener_faults"] as? [NSDictionary],
            harness.controller.listenerFailures.map { $0.jsonObject as NSDictionary }
        )

        // No route is rewritten to another port. A host opened meanwhile
        // gets none, like one whose listener was not ready in time, and its
        // loads are not held: only a routed host's are.
        try await harness.select("instance-2")
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
        let second = try XCTUnwrap(harness.lastOpened)
        XCTAssertEqual(harness.controller.loadGate(for: second), .open)
        XCTAssertTrue(
            harness.controller.routeOwner.isExcluded(two, store: harness.websiteDataStore.identifier)
        )
        XCTAssertEqual(harness.requestStatus("instance-2").values.first??.preferred, .tunnel)
        XCTAssertEqual(harness.status("instance-2").listenerFaults, [])
        try await harness.select("instance-1")
        let reopened = try XCTUnwrap(harness.lastOpened)
        XCTAssertEqual(harness.controller.loadGate(for: reopened), .fault)

        // The stranger lets go: the next attempt finds the port ours, and
        // only now is the closure published and the pages told.
        let told = harness.announcements
        squatter.close()
        try await eventually { harness.controller.listenerState == .ready }
        XCTAssertNil(harness.controller.listenerRecovery)
        XCTAssertEqual(harness.status("instance-1").closure, 1)
        XCTAssertGreaterThan(harness.announcements, told)
        XCTAssertEqual(harness.controller.loadGate(for: reopened), .open)
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
        XCTAssertEqual(try harness.listenerPort(since: listening), port)
        // The record outlives the recovery.
        XCTAssertEqual(harness.controller.listenerFailures.count, 1)
        // Selected again, and direct once the path is usable; a CONNECT is
        // served on the same port.
        let reselected = try XCTUnwrap(harness.transport.selections.last)
        XCTAssertGreaterThan(reselected.selection, target.selection)
        harness.transport.report(.usable(generation: "g1"), for: reselected)
        XCTAssertEqual(harness.status("instance-1").preferred, .direct)
        let client = LoopbackClient(port: port)
        _ = try await establishDirect(client, harness: harness)
        client.close()
    }

    /// 「重试」: an attempt starts at once instead of at the next delay,
    /// unless one is running, and does nothing when nothing is recovering.
    @MainActor
    func testRetryListenerNowStartsAnAttemptAtOnceAndOnlyWhileRecovering() async throws {
        let listening = try listeningSockets()
        // The recovery's own next attempt is ten minutes away.
        let harness = try Harness(
            timing: DirectPathTiming(
                listenerWaitSeconds: 30,
                rebindSeconds: 10,
                foregroundSeconds: 600,
                retrySeconds: [600]
            ),
            test: self
        )
        try await harness.signIn()
        try await harness.select("instance-1")
        let runtime = try XCTUnwrap(harness.lastOpened)
        let port = try harness.listenerPort(since: listening)
        // Serving: nothing to retry.
        harness.controller.retryListenerNow()
        XCTAssertEqual(harness.controller.listenerState, .ready)

        harness.enterBackground()
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: port.rawValue,
            reuseAddress: true
        )
        defer { squatter.close() }
        // In the background nothing is bound, asked or not.
        harness.controller.retryListenerNow()
        XCTAssertEqual(harness.controller.listenerState, .closedForBackground)
        harness.enterForeground()
        try await eventually { harness.controller.listenerState == .failed }

        // The stranger is still there: the attempt starts at once, fails
        // like the first, and is announced once.
        let announced = harness.availabilityChanges
        harness.controller.retryListenerNow()
        XCTAssertEqual(harness.controller.listenerState, .rebinding)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .fault)
        // One running: asking again starts no second one.
        harness.controller.retryListenerNow()
        try await eventually { harness.controller.listenerState == .failed }
        XCTAssertEqual(harness.availabilityChanges, announced + 1)
        XCTAssertEqual(harness.controller.listenerFailures.count, 1)

        // Nothing is tried by itself within the delay, also once the port
        // is free; the retry finds it ours.
        squatter.close()
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(harness.controller.listenerState, .failed)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .fault)
        harness.controller.retryListenerNow()
        try await eventually { harness.controller.listenerState == .ready }
        XCTAssertNil(harness.controller.listenerRecovery)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .open)
    }

    /// The listening socket fails while it serves, in the foreground, as
    /// Network reports it: the port is closed, so the routed host's loads
    /// go on (WebKit fails over by itself), the recovery begins at the
    /// failure, and the same port serves again at the next attempt.
    @MainActor
    func testASocketThatFailsWhileServingIsBoundAgainAndLoadsAreNotHeld() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(timing: .recovering, test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let runtime = try XCTUnwrap(harness.lastOpened)
        let target = try XCTUnwrap(harness.transport.selections.last)
        let port = try harness.listenerPort(since: listening)
        harness.transport.report(.usable(generation: "g1"), for: target)
        XCTAssertEqual(harness.status("instance-1").preferred, .direct)
        let listener = try harness.listener()
        let lifecycle = await listener.lifecycle
        guard case .serving(let generation) = lifecycle.phase else {
            return XCTFail("Not serving: \(lifecycle.phase)")
        }
        let announced = harness.availabilityChanges
        let before = ContinuousClock.now

        // What Network's failure handler does: recorded first, then told.
        XCTAssertTrue(
            lifecycle.socketFailed(generation, DirectListenerFailureCause(.socket, code: ECONNABORTED))
        )
        // The gate reads the lifecycle: open before the controller hears.
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .open)
        // And a status asked now takes the failure in before it answers.
        XCTAssertEqual(harness.requestStatus("instance-1").values.first??.preferred, .tunnel)
        XCTAssertEqual(harness.controller.listenerRecovery?.lastFailure?.step, .socket)
        await listener.socketDidFail()

        let recovery = try XCTUnwrap(harness.controller.listenerRecovery)
        XCTAssertEqual(recovery.lastFailure?.step, .socket)
        XCTAssertEqual(recovery.lastFailure?.code, ECONNABORTED)
        XCTAssertNil(recovery.lastFailure?.backgroundSeconds, "No background came before it.")
        XCTAssertFalse(recovery.portMayBeForeign)
        XCTAssertGreaterThanOrEqual(recovery.since, before)
        XCTAssertEqual(recovery.since, recovery.lastFailure?.at)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .open)
        XCTAssertGreaterThan(harness.availabilityChanges, announced)
        // Nothing is authorized meanwhile.
        XCTAssertEqual(harness.status("instance-1").preferred, .tunnel)
        XCTAssertNil(harness.controller.plan(for: try XCTUnwrap(DirectAuthority(host: one)))?.openDirect)

        try await eventually { harness.controller.listenerState == .ready }
        XCTAssertNil(harness.controller.listenerRecovery)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .open)
        XCTAssertEqual(try harness.listenerPort(since: listening), port)
        XCTAssertEqual(harness.controller.listenerFailures.map(\.step), [.socket])
        // A failure in the foreground ends no selection (that would close
        // the transport's streams, and a failure closes no relay): the
        // path it reported usable is preferred again as soon as the
        // listener serves.
        XCTAssertEqual(harness.transport.selections.last, target)
        XCTAssertEqual(harness.status("instance-1").preferred, .direct)
        let client = LoopbackClient(port: port)
        _ = try await establishDirect(client, harness: harness)
        client.close()
    }

    /// No network when the App returns: the system leaves the bind waiting,
    /// which is a failed attempt like any other. The port is closed, not
    /// someone else's, so loads go on (WebKit fails over by itself), and
    /// the background's closure is published at once, because a page that
    /// reconnects for it reaches its normal path. The next attempt binds
    /// the port again, and the closure is not published a second time.
    @MainActor
    func testABindTheSystemLeavesWaitingIsAFailedAttemptThatHoldsNothingAndIsTriedAgain() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(timing: .recovering, test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let runtime = try XCTUnwrap(harness.lastOpened)
        let target = try XCTUnwrap(harness.transport.selections.last)
        let port = try harness.listenerPort(since: listening)
        harness.transport.report(.usable(generation: "g1"), for: target)
        let relayed = LoopbackClient(port: port)
        let stream = try await establishDirect(relayed, harness: harness)

        harness.enterBackground()
        try await eventually { stream.isCancelled }
        relayed.close()
        try await eventually { harness.controller.listenerState == .closedForBackground }
        XCTAssertEqual(harness.status("instance-1").closure, 0)

        // As Network reports a bind the system leaves waiting; or, if the
        // socket had just become ready, as it reports that socket failing.
        let (cause, held) = try await harness.enterForegroundFailingItsBind(
            binding: DirectListenerFailureCause(.waiting, code: ENETDOWN),
            ready: DirectListenerFailureCause(.socket, code: ENETDOWN)
        )
        // Decided at the lifecycle: the gate is open before anyone hears,
        // while the status request that return holds is still held.
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .open)
        XCTAssertEqual(held.values, [])

        try await eventually { held.values.count == 1 }
        XCTAssertEqual(held.values.first??.preferred, .tunnel)
        XCTAssertEqual(held.values.first??.closure, 1)
        XCTAssertEqual(harness.controller.listenerFailures.map(\.step), [cause.step])
        XCTAssertEqual(harness.controller.listenerFailures.first?.code, ENETDOWN)

        // The network is back: the next attempt binds and serves.
        try await eventually { harness.controller.listenerState == .ready }
        XCTAssertNil(harness.controller.listenerRecovery)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .open)
        XCTAssertEqual(try harness.listenerPort(since: listening), port)
        XCTAssertEqual(harness.status("instance-1").closure, 1)
        let reselected = try XCTUnwrap(harness.transport.selections.last)
        XCTAssertGreaterThan(reselected.selection, target.selection)
        harness.transport.report(.usable(generation: "g1"), for: reselected)
        XCTAssertEqual(harness.status("instance-1").preferred, .direct)
    }

    /// The return's check is not answered in time on the port the App has
    /// just bound (a phone just woken): a failed attempt, and not a
    /// stranger. The gate opens, because that attempt's failure closes its
    /// socket and WebKit fails over; the failure is on record as
    /// `ownership`; the background's closure is published; and the next
    /// attempt serves.
    @MainActor
    func testACheckNotAnsweredOnTheReturnLeavesTheGateOpenAndIsRecordedAsOwnership() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(timing: .recovering, test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let runtime = try XCTUnwrap(harness.lastOpened)
        let target = try XCTUnwrap(harness.transport.selections.last)
        let port = try harness.listenerPort(since: listening)
        harness.transport.report(.usable(generation: "g1"), for: target)
        let relayed = LoopbackClient(port: port)
        let stream = try await establishDirect(relayed, harness: harness)

        harness.enterBackground()
        try await eventually { stream.isCancelled }
        relayed.close()
        try await eventually { harness.controller.listenerState == .closedForBackground }

        // As the attempt's own completion does when the bound ends with
        // the port bound and the check unanswered.
        let unanswered = DirectListenerFailureCause(.ownership)
        let (cause, held) = try await harness.enterForegroundFailingItsBind(
            binding: nil,
            ready: unanswered
        )
        XCTAssertEqual(cause, unanswered)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .open)

        try await eventually { held.values.count == 1 }
        XCTAssertEqual(held.values.first??.preferred, .tunnel)
        XCTAssertEqual(held.values.first??.closure, 1)
        XCTAssertEqual(harness.controller.listenerFailures.map(\.step), [.ownership])
        XCTAssertEqual(harness.controller.listenerFailures.first?.portMayBeForeign, false)
        if let recovery = harness.controller.listenerRecovery {
            // The next attempt is not yet through.
            XCTAssertFalse(recovery.portMayBeForeign)
            XCTAssertEqual(harness.controller.loadGate(for: runtime), .open)
        }

        try await eventually { harness.controller.listenerState == .ready }
        XCTAssertNil(harness.controller.listenerRecovery)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .open)
        XCTAssertEqual(try harness.listenerPort(since: listening), port)
        XCTAssertEqual(harness.status("instance-1").closure, 1)
    }

    /// The App leaves while the port is still not ours: nothing is bound
    /// or tried while it is away, and the return is a new stay, with its
    /// own first attempt and its own `since`. What was known of the port
    /// stays known across both, until that attempt says otherwise.
    @MainActor
    func testLeavingWhileRecoveringStopsTheAttemptsAndTheReturnIsANewStay() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(timing: .recovering, test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let runtime = try XCTUnwrap(harness.lastOpened)
        let port = try harness.listenerPort(since: listening)

        harness.enterBackground()
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: port.rawValue,
            reuseAddress: true
        )
        defer { squatter.close() }
        harness.enterForeground()
        try await eventually { harness.controller.listenerRecovery?.lastFailure != nil }
        let first = try XCTUnwrap(harness.controller.listenerRecovery)

        harness.enterBackground()
        XCTAssertNil(harness.controller.listenerRecovery)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .fault)
        // An attempt that was running ends by itself; then nothing.
        try await eventually { harness.controller.listenerState == .failed }
        squatter.close()
        try await Task.sleep(for: .milliseconds(400))
        XCTAssertEqual(harness.controller.listenerState, .failed)
        XCTAssertTrue(try newListeners(since: listening).isEmpty)

        // The return does not make the port ours: until this stay's first
        // attempt has said so, it is still refused, not merely held.
        harness.enterForeground()
        let second = try XCTUnwrap(harness.controller.listenerRecovery)
        XCTAssertNil(second.lastFailure)
        XCTAssertTrue(second.portMayBeForeign)
        XCTAssertGreaterThan(second.since, first.since)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .fault)
        let held = harness.requestStatus("instance-1")
        XCTAssertEqual(held.values, [])
        try await eventually { harness.controller.listenerState == .ready }
        XCTAssertEqual(held.values.count, 1)
        XCTAssertNil(harness.controller.listenerRecovery)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .open)
    }

    @MainActor
    func testAFailureOfAListenerTheControllerDoesNotOwnChangesNothing() async throws {
        let harness = try Harness(test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let target = try XCTUnwrap(harness.transport.selections.last)
        harness.transport.report(.usable(generation: "g1"), for: target)

        // Another listener, whose own lifecycle has failed.
        let stranger = DirectConnectListener(planner: { _ in nil }, onListenerFailure: {})
        let lifecycle = await stranger.lifecycle
        let generation = try XCTUnwrap(lifecycle.beginBind())
        XCTAssertTrue(lifecycle.bindFailed(generation))
        harness.controller.listenerDidFail(stranger)

        XCTAssertEqual(harness.controller.listenerState, .ready)
        XCTAssertEqual(harness.controller.listenerFailures, [])
        XCTAssertNil(harness.controller.listenerRecovery)
        XCTAssertEqual(harness.status("instance-1").preferred, .direct)
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
    func testNoBindHappensInTheBackgroundAndAFirstBindUnderWayThereIsRetired() async throws {
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

        // A first bind under way when the App goes to the background ends
        // there, on purpose and not as a failure: its listener is retired,
        // no route is written, and the host is excluded.
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
        // With no route pointing at it, stopped: nothing listens until a
        // later open in the foreground binds again. Not a failure.
        XCTAssertEqual(harness.controller.listenerState, .none)
        XCTAssertEqual(harness.controller.listenerFailures, [])
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

    /// A second return before the first return's re-bind finished: the
    /// held answers and the pending closure stay held until that re-bind
    /// completes, and are then settled once.
    @MainActor
    func testASecondReturnDuringTheReBindKeepsTheResumeHeld() async throws {
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
        try await eventually { stream.isCancelled }
        try await eventually { await !LoopbackClient.canConnect(to: port) }

        // Return, ask, leave and return again, all before the re-bind can
        // finish on the main actor.
        harness.enterForeground()
        let first = harness.requestStatus("instance-1")
        harness.enterBackground()
        harness.enterForeground()
        let second = harness.requestStatus("instance-1")
        XCTAssertEqual(first.values, [])
        XCTAssertEqual(second.values, [])
        XCTAssertEqual(harness.status("instance-1").closure, 0)

        try await eventually { first.values.count == 1 && second.values.count == 1 }
        try await assertTheReturnPublishedOneClosure(
            heldAnswers: first.values + second.values,
            harness: harness
        )
        client.close()
    }

    /// The background counts the direct relays it closes before the
    /// transport is told to stop, which closes its streams.
    @MainActor
    func testTheBackgroundCountsItsClosureBeforeTheTransportClosesItsStreams() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let target = try XCTUnwrap(harness.transport.selections.last)
        let port = try harness.listenerPort(since: listening)
        harness.transport.report(.usable(generation: "g1"), for: target)
        let client = LoopbackClient(port: port)
        let stream = try await establishDirect(client, harness: harness)
        let deselected = harness.transport.deselectCount

        harness.enterBackground()
        // The transport is stopped only after the close was counted.
        XCTAssertEqual(harness.transport.deselectCount, deselected)
        try await eventually { harness.transport.deselectCount == deselected + 1 }
        try await eventually { stream.isCancelled }
        harness.enterForeground()
        let answer = harness.requestStatus("instance-1")
        try await eventually { answer.values.count == 1 }

        try await assertTheReturnPublishedOneClosure(heldAnswers: answer.values, harness: harness)
        client.close()
    }

    /// The transport hears the background first and says so: the
    /// controller enters its own background then, so the relay's closure is
    /// counted once, held while the App is away, and in the first status
    /// the return settles. Its own observer, when it comes, changes nothing.
    @MainActor
    func testABackgroundTheTransportReportsFirstIsTheControllersBackground() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let target = try XCTUnwrap(harness.transport.selections.last)
        let port = try harness.listenerPort(since: listening)
        harness.transport.report(.usable(generation: "g1"), for: target)
        let client = LoopbackClient(port: port)
        let stream = try await establishDirect(client, harness: harness)
        let deselected = harness.transport.deselectCount

        harness.transport.report(.lostToBackground, for: target)
        XCTAssertEqual(harness.status("instance-1").preferred, .tunnel)
        try await eventually { stream.isCancelled }
        try await eventually { harness.transport.deselectCount == deselected + 1 }
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        // Counted, not published while away.
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(harness.status("instance-1").closure, 0)

        // The controller's own observer: already entered.
        let announced = harness.availabilityChanges
        harness.enterBackground()
        XCTAssertEqual(harness.availabilityChanges, announced)
        XCTAssertEqual(harness.transport.deselectCount, deselected + 1)

        harness.enterForeground()
        let first = harness.requestStatus("instance-1")
        XCTAssertEqual(first.values, [])
        try await eventually { first.values.count == 1 }
        try await assertTheReturnPublishedOneClosure(heldAnswers: first.values, harness: harness)
        client.close()
    }

    // MARK: The load gate

    /// A caller with no runtime (the voice take's hidden WebView) asks by
    /// instance URL. The answer is the runtime gate's, and it keeps
    /// answering for a host that is still routed after its runtime closed.
    @MainActor
    func testTheLoadGateByInstanceURLNeedsNoRuntimeAndFollowsTheRouteAndTheListener() async throws {
        let harness = try Harness(test: self)
        try await harness.signIn()
        _ = try harness.coordinator.selectInstance(id: "instance-1")
        let unrouted = try XCTUnwrap(harness.lastOpened)
        // No route for this host yet: WebKit's normal path.
        XCTAssertEqual(harness.controller.loadGate(forInstanceURL: unrouted.instance.url), .open)

        try await harness.select("instance-1")
        let runtime = try XCTUnwrap(harness.lastOpened)
        let url = runtime.instance.url
        XCTAssertEqual(harness.controller.loadGate(forInstanceURL: url), .open)

        // The runtime closes. Its host stays routed to the listener, so the
        // gate still speaks for it: open while the listener serves, held as
        // soon as the background closes it.
        harness.controller.runtimeDidStop()
        XCTAssertEqual(harness.controller.loadGate(forInstanceURL: url), .open)
        harness.enterBackground()
        XCTAssertEqual(harness.controller.loadGate(forInstanceURL: url), .held)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .held)
        // A host that was never routed, and one that is not an instance
        // authority at all, are not the listener's.
        let other = try XCTUnwrap(URL(string: "https://never-routed.example"))
        XCTAssertEqual(harness.controller.loadGate(forInstanceURL: other), .open)
        let odd = try XCTUnwrap(URL(string: "https://instance-one.example:8443"))
        XCTAssertEqual(harness.controller.loadGate(forInstanceURL: odd), .open)
    }

    /// The gate the shell asks before it creates or reloads a WebView: open
    /// without a route or with a serving listener, held from the background
    /// until the return settles, and never decided by a wait. Each change
    /// is announced once.
    @MainActor
    func testTheLoadGateIsHeldFromTheBackgroundUntilTheReturnSettles() async throws {
        let harness = try Harness(test: self)
        try await harness.signIn()
        // No route for this host yet: WebKit's normal path.
        _ = try harness.coordinator.selectInstance(id: "instance-1")
        let unrouted = try XCTUnwrap(harness.lastOpened)
        XCTAssertEqual(harness.controller.loadGate(for: unrouted), .open)

        try await harness.select("instance-1")
        let runtime = try XCTUnwrap(harness.lastOpened)
        XCTAssertEqual(harness.controller.listenerState, .ready)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .open)
        let announced = harness.availabilityChanges

        // Closed for the background, in the same turn.
        harness.enterBackground()
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .held)
        XCTAssertEqual(harness.availabilityChanges, announced + 1)

        // The return starts its attempt; until that settles, still held.
        // The recovery it begins is announced.
        harness.enterForeground()
        XCTAssertEqual(harness.controller.listenerState, .rebinding)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .held)
        XCTAssertNotNil(harness.controller.listenerRecovery)
        XCTAssertEqual(harness.availabilityChanges, announced + 2)

        // Preparing a route that exists does not wait for the listener and
        // changes nothing: the gate is asked instead.
        await harness.controller.prepareRoute(for: runtime) { true }
        try await eventually { harness.controller.listenerState == .ready }
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .open)
        XCTAssertNil(harness.controller.listenerRecovery)
        if harness.controller.listenerFailures.isEmpty {
            // Served at the first attempt, as it does unless the simulator
            // could not bind the port at once.
            XCTAssertEqual(harness.availabilityChanges, announced + 3)
        }

        // An instance with no route on this store is open throughout.
        _ = try harness.coordinator.selectInstance(id: "instance-custom-port")
        let other = try XCTUnwrap(harness.lastOpened)
        harness.enterBackground()
        XCTAssertEqual(harness.controller.loadGate(for: other), .open)
        harness.enterForeground()
    }

    /// An attempt that only ran out of time found nothing about the port.
    /// After one that found the port in use, it leaves that known: the gate
    /// still refuses, and only the attempt that serves opens it.
    @MainActor
    func testAnAttemptThatRanOutOfTimeLeavesAPortLastFoundInUseRefused() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(timing: .recovering, test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let runtime = try XCTUnwrap(harness.lastOpened)
        let port = try harness.listenerPort(since: listening)

        harness.enterBackground()
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: port.rawValue,
            reuseAddress: true
        )
        harness.enterForeground()
        try await eventually { harness.controller.listenerRecovery?.portMayBeForeign == true }
        harness.enterBackground()
        try await eventually { harness.controller.listenerState == .failed }
        // The stranger goes while the App is away; nothing here knows.
        squatter.close()
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .fault)

        // The return's attempt ends at its bound, as its own deadline ends
        // it: no finding about the port.
        let out = DirectListenerFailureCause(.deadline)
        let (cause, held) = try await harness.enterForegroundFailingItsBind(binding: out, ready: out)
        XCTAssertEqual(cause, out)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .fault)
        try await eventually { held.values.count == 1 }
        if harness.controller.listenerState != .ready {
            // Heard, and the next attempt not yet through.
            XCTAssertEqual(harness.controller.listenerRecovery?.lastFailure?.step, .deadline)
            XCTAssertEqual(harness.controller.listenerRecovery?.portMayBeForeign, true)
            XCTAssertEqual(harness.controller.loadGate(for: runtime), .fault)
        }

        try await eventually { harness.controller.listenerState == .ready }
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .open)
        XCTAssertNil(harness.controller.listenerRecovery)
    }

    /// The port is taken while the App is in the background: the gate of the
    /// routed runtime goes from held (the return's first attempt) to the
    /// refusing value, never to open, and stays there for as long as the
    /// stranger has the port, in the background and across a return too:
    /// never again the held state, which a caller may wait out and then
    /// load. It opens when the port is ours again. A host without a route
    /// stays open throughout.
    @MainActor
    func testTheLoadGateOfARoutedRuntimeRefusesWhileAStrangerHoldsThePort() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(timing: .recovering, test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let runtime = try XCTUnwrap(harness.lastOpened)
        let port = try harness.listenerPort(since: listening)

        harness.enterBackground()
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: port.rawValue,
            reuseAddress: true
        )
        defer { squatter.close() }
        harness.enterForeground()
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .held)

        var first: Set<String> = []
        try await eventually {
            first.insert("\(harness.controller.loadGate(for: runtime))")
            return harness.controller.listenerRecovery?.lastFailure != nil
        }
        XCTAssertFalse(first.contains("open"))
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .fault)
        XCTAssertEqual(harness.controller.loadGate(forInstanceURL: runtime.instance.url), .fault)

        var seen: Set<String> = []
        harness.enterBackground()
        seen.insert("\(harness.controller.loadGate(for: runtime))")
        try await Task.sleep(for: .milliseconds(200))
        seen.insert("\(harness.controller.loadGate(for: runtime))")
        harness.enterForeground()
        let until = ContinuousClock.now.advanced(by: .milliseconds(400))
        while ContinuousClock.now < until {
            seen.insert("\(harness.controller.loadGate(for: runtime))")
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertEqual(seen, ["fault"])
        _ = try harness.coordinator.selectInstance(id: "instance-2")
        let unrouted = try XCTUnwrap(harness.lastOpened)
        XCTAssertEqual(harness.controller.loadGate(for: unrouted), .open)

        squatter.close()
        try await eventually { harness.controller.loadGate(for: runtime) == .open }
        XCTAssertEqual(harness.controller.listenerState, .ready)
    }

    /// What the Action Button's voice take does with that gate: its client
    /// waits out a held gate (five seconds) and then loads, so the gate of
    /// a port that may be someone else's must never read as held. It reads
    /// as the refusing value, and the client settles on it in the background
    /// at once and in front after at most `gateWait` (the listener re-binds
    /// in front, #4241), with no load, and says so.
    @MainActor
    func testAVoiceTakeLoadsNothingWhileAStrangerHoldsThePort() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(timing: .recovering, test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let runtime = try XCTUnwrap(harness.lastOpened)
        let origin = harness.origin("instance-1")
        let store = harness.websiteDataStore.identifier
        let port = try harness.listenerPort(since: listening)
        let controller = harness.controller
        // The gate as the voice take asks it: by its own host, on the
        // current store, with no runtime.
        let gate: () -> DirectLoadGate = {
            VoiceCaptureLoadGate.gate(for: origin, store: store, currentStore: store) {
                controller.loadGate(forInstanceURL: $0)
            }
        }
        XCTAssertEqual(gate(), .open)

        harness.enterBackground()
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: port.rawValue,
            reuseAddress: true
        )
        defer { squatter.close() }
        harness.enterForeground()
        try await eventually { harness.controller.listenerRecovery?.portMayBeForeign == true }
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .fault)

        for inFront in [true, false] {
            if !inFront {
                harness.enterBackground()
            }
            // In the background settled at once; in front after the client's
            // whole wait (a clock the steps advance), and on the fault, never
            // on a value that would load.
            var slept = 0
            var clock = ContinuousClock.now
            let settled = await VoiceCaptureInstanceClient.settledGate(
                gate: gate,
                isActive: { inFront },
                sleep: { step in
                    slept += 1
                    clock += step
                },
                now: { clock }
            )
            XCTAssertEqual(settled, .fault, "in front: \(inFront)")
            XCTAssertEqual(
                slept,
                inFront ? Int(VoiceCaptureInstanceClient.gateWait / VoiceCaptureInstanceClient.gateStep) : 0,
                "in front: \(inFront)"
            )

            let client = VoiceCaptureInstanceClient(
                target: VoiceCaptureTarget(
                    accountID: "account-1",
                    instanceID: "instance-1",
                    origin: origin,
                    storeIdentifier: store
                ),
                gate: gate,
                isActive: { inFront }
            )
            do {
                _ = try await client.status()
                XCTFail("A voice take loaded against a port that may be someone else's.")
            } catch {
                XCTAssertEqual(error as? VoiceCaptureInstanceClient.Failure, .directFault, "in front: \(inFront)")
            }
            XCTAssertEqual(client.failures.count, 1, "in front: \(inFront)")
            XCTAssertTrue(
                client.failures.first?.contains("direct_fault") == true,
                "in front: \(inFront), \(client.failures)"
            )
        }
    }

    /// Backgrounds and returns in quick succession, including a return
    /// before the previous close or re-bind has finished: a routed listener
    /// in the foreground always has a current attempt or the next one
    /// scheduled, so it serves again every time, and nothing is stranded.
    @MainActor
    func testRapidBackgroundsAndReturnsNeverStrandARoutedListener() async throws {
        let harness = try Harness(test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let runtime = try XCTUnwrap(harness.lastOpened)

        for round in 0..<4 {
            // A return before the close has run, then another round trip
            // before that return's re-bind can finish.
            harness.enterBackground()
            harness.enterForeground()
            harness.enterBackground()
            harness.enterForeground()
            if round.isMultiple(of: 2) {
                await Task.yield()
                harness.enterBackground()
                harness.enterForeground()
            }
            let held = harness.requestStatus("instance-1")
            try await eventually { harness.controller.listenerState == .ready }
            // The held answer is settled with the return.
            try await eventually { held.values.count == 1 }
            XCTAssertEqual(harness.controller.loadGate(for: runtime), .open)
            XCTAssertNil(harness.controller.listenerRecovery)
        }
    }

    // MARK: The foreground watchdog

    /// The watchdog covers the first bind's stay too. With no time at all
    /// in the foreground, the first bind is past its deadline before it can
    /// serve. No route points at that listener, so it is not kept: it is
    /// stopped, the failure is on record, no route is written, and the page
    /// loads on WebKit's normal path. Nothing is recovering, because there
    /// is nothing a route needs back; the next open makes another listener.
    @MainActor
    func testAFirstBindPastTheForegroundDeadlineLeavesNoListenerAndNoRoute() async throws {
        let listening = try listeningSockets()
        let routeWrites = try ProxyConfigurationWrites.count()
        let harness = try Harness(
            timing: DirectPathTiming(
                listenerWaitSeconds: 30,
                rebindSeconds: 10,
                foregroundSeconds: 0
            ),
            test: self
        )
        try await harness.signIn()

        try await harness.select("instance-1")

        XCTAssertEqual(harness.controller.listenerState, .none)
        XCTAssertNil(harness.controller.listenerRecovery)
        XCTAssertEqual(harness.controller.listenerFailures.map(\.step), [.deadline])
        let first = try XCTUnwrap(harness.lastOpened)
        XCTAssertEqual(harness.controller.loadGate(for: first), .open)
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
        XCTAssertTrue(
            harness.controller.routeOwner.isExcluded(one, store: harness.websiteDataStore.identifier)
        )
        XCTAssertEqual(harness.requestStatus("instance-1").values.first??.preferred, .tunnel)
        // Not a routed host's failure: its status does not carry it.
        XCTAssertEqual(harness.status("instance-1").listenerFaults, [])
        try await eventually { try newListeners(since: listening).isEmpty }

        // A return has nothing to bind again. Another host's open makes a
        // new listener, which the same bound fails the same way.
        harness.enterBackground()
        harness.enterForeground()
        XCTAssertNil(harness.controller.listenerRecovery)
        try await harness.select("instance-2")
        XCTAssertEqual(harness.controller.listenerState, .none)
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
        try await eventually { try newListeners(since: listening).isEmpty }
        XCTAssertEqual(harness.transport.selections.count, 0)
    }

    /// With the standard bound far away, stays in the foreground and in the
    /// background come and go without the watchdog ever failing a listener
    /// that serves in time: each stay's episode ends when it serves, and
    /// the background ends it too.
    @MainActor
    func testTheWatchdogNeverFailsAListenerThatServesInTime() async throws {
        let harness = try Harness(
            timing: DirectPathTiming(
                listenerWaitSeconds: 30,
                rebindSeconds: 10,
                foregroundSeconds: 5
            ),
            test: self
        )
        try await harness.signIn()
        try await harness.select("instance-1")
        let runtime = try XCTUnwrap(harness.lastOpened)

        for _ in 0..<3 {
            harness.enterBackground()
            harness.enterForeground()
            try await eventually { harness.controller.loadGate(for: runtime) != .held }
            guard harness.controller.listenerFailures.filter({ $0.step != .deadline }).isEmpty else {
                // The simulator could not bind the port again at once: not
                // this test's subject.
                return
            }
        }
        XCTAssertEqual(harness.controller.listenerState, .ready)
        XCTAssertEqual(harness.controller.listenerFailures, [])
    }

    // MARK: A failure before any route

    /// A first bind that failed, and then the retirement its abandoned
    /// preparation asks for: no route points at the listener, so it is
    /// stopped, failed or not, and nothing is left listening. (A retirement
    /// that comes first records no failure at all:
    /// `testAListenerThatIsNotReadyInTimeExcludesTheHostForThatStore`.)
    @MainActor
    func testAListenerThatFailedBeforeAnyRouteIsStoppedAndTheNextOpenMakesAnother() async throws {
        let listening = try listeningSockets()
        let routeWrites = try ProxyConfigurationWrites.count()
        let harness = try Harness(
            timing: DirectPathTiming(
                listenerWaitSeconds: 30,
                rebindSeconds: 0,
                foregroundSeconds: 600
            ),
            test: self
        )
        try await harness.signIn()
        _ = try harness.coordinator.selectInstance(id: "instance-1")
        let runtime = try XCTUnwrap(harness.lastOpened)

        // Nobody wants this open any more: the preparation ends by retiring
        // the listener, which by then has failed (its ownership check got
        // no time at all).
        await harness.controller.prepareRoute(for: runtime) { false }

        XCTAssertEqual(harness.controller.listenerState, .none)
        XCTAssertNil(harness.controller.listenerRecovery)
        XCTAssertEqual(harness.controller.listenerFailures.map(\.step), [.ownership])
        try await eventually { try newListeners(since: listening).isEmpty }
        // An open that was superseded excludes nothing.
        XCTAssertFalse(
            harness.controller.routeOwner.isExcluded(one, store: harness.websiteDataStore.identifier)
        )

        // The next open binds a new listener; under this bound it fails
        // the same way, and is stopped the same way.
        try await harness.select("instance-2")
        XCTAssertEqual(harness.controller.listenerState, .none)
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
        XCTAssertTrue(
            harness.controller.routeOwner.isExcluded(two, store: harness.websiteDataStore.identifier)
        )
        try await eventually { try newListeners(since: listening).isEmpty }
        // The same failure again adds nothing to the record.
        XCTAssertEqual(harness.controller.listenerFailures.map(\.step), [.ownership])
    }

    /// The first bind is not confirmed (its ownership check gets no time at
    /// all), before any route. No page is interrupted: the host loads on
    /// WebKit's normal path, its status is an ordinary `tunnel`, nothing is
    /// held, and nothing is selected or planned for it.
    @MainActor
    func testAFirstBindThatIsNotConfirmedInterruptsNoPage() async throws {
        let listening = try listeningSockets()
        let routeWrites = try ProxyConfigurationWrites.count()
        let harness = try Harness(
            timing: DirectPathTiming(listenerWaitSeconds: 30, rebindSeconds: 0),
            test: self
        )
        try await harness.signIn()

        try await harness.select("instance-1")

        XCTAssertEqual(harness.controller.listenerState, .none)
        XCTAssertNil(harness.controller.listenerRecovery)
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
        let first = try XCTUnwrap(harness.lastOpened)
        XCTAssertEqual(harness.controller.loadGate(for: first), .open)
        XCTAssertEqual(harness.requestStatus("instance-1").values.first??.preferred, .tunnel)
        XCTAssertEqual(harness.transport.selections.count, 0)
        try await eventually { try newListeners(since: listening).isEmpty }

        // A background and a return: nothing to bind again, nothing held.
        harness.enterBackground()
        XCTAssertEqual(harness.controller.loadGate(for: first), .open)
        harness.enterForeground()
        XCTAssertNil(harness.controller.listenerRecovery)
        XCTAssertEqual(harness.requestStatus("instance-1").values.first??.preferred, .tunnel)
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(try newListeners(since: listening), [])
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
        XCTAssertEqual(harness.transport.selections.count, 0)
        XCTAssertNil(harness.controller.plan(for: try XCTUnwrap(DirectAuthority(host: one))))
    }

    /// A routed re-bind's bind fails, and the App goes back to the
    /// background before that attempt's completion runs: the failure is
    /// still recorded, once; nothing is tried while the App is away; and
    /// the next return binds again, as a new stay.
    @MainActor
    func testAReBindThatFailedIsRecordedInTheBackgroundAndTheNextReturnBindsAgain() async throws {
        let listening = try listeningSockets()
        let harness = try Harness(timing: .recovering, test: self)
        try await harness.signIn()
        try await harness.select("instance-1")
        let runtime = try XCTUnwrap(harness.lastOpened)
        let port = try harness.listenerPort(since: listening)
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
        // Return, then leave again in the same turn: the re-bind's
        // completion can only run afterwards, in the background.
        harness.enterForeground()
        harness.enterBackground()

        try await eventually { harness.controller.listenerFailures.count == 1 }
        XCTAssertEqual(harness.controller.listenerFailures.first?.code, EADDRINUSE)
        XCTAssertEqual(harness.controller.listenerState, .failed)
        XCTAssertNil(harness.controller.listenerRecovery)
        XCTAssertEqual(harness.controller.loadGate(for: runtime), .fault)
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(harness.controller.listenerState, .failed, "Nothing is bound in the background.")

        // The return: held for its first attempt, which fails again while
        // the stranger is there; the held answer is then the public path.
        harness.enterForeground()
        let answer = harness.requestStatus("instance-1")
        XCTAssertEqual(answer.values, [])
        XCTAssertNil(try XCTUnwrap(harness.controller.listenerRecovery).lastFailure)
        try await eventually { answer.values.count == 1 }
        XCTAssertEqual(answer.values.first??.preferred, .tunnel)
        XCTAssertEqual(harness.controller.listenerRecovery?.portMayBeForeign, true)
        XCTAssertEqual(harness.transport.selections.count, selections)

        squatter.close()
        try await eventually { harness.controller.listenerState == .ready }
        XCTAssertGreaterThan(harness.transport.selections.count, selections)
    }

    /// One attempt's bound is spent on the bind and then on the ownership
    /// check, and the record says which part failed. A check that is not
    /// answered on the port this listener has just bound is `ownership`
    /// when the bound ends: not `deadline` (the bind was in time), and not
    /// a stranger, because the bind is exclusive and only this listener
    /// accepts there. The same listener, asked for its own nonce, serves;
    /// with no time at all, the bound is what ended the attempt.
    func testACheckNotAnsweredOnTheListenersOwnPortIsAFailedCheckAndNotAStranger() async throws {
        let listener = DirectConnectListener(
            limits: .patient,
            planner: { _ in nil },
            onListenerFailure: {}
        )
        addTeardownBlock {
            await listener.stop()
        }
        let lifecycle = await listener.lifecycle
        let nonce = await listener.ownershipNonce
        let first = try await listener.bind(port: nil)
        XCTAssertTrue(lifecycle.confirm(first.generation))
        XCTAssertTrue(lifecycle.beginClose())

        // The port is bound and accepts, and the nonce asked for is never
        // answered: what a listener too slow to answer looks like.
        let started = ContinuousClock.now
        let unanswered = await DirectPathController.bindAndCheck(
            listener,
            port: first.port,
            nonce: "never-answered",
            within: 1
        )
        let took = started.duration(to: ContinuousClock.now)
        XCTAssertEqual(unanswered.failure, DirectListenerFailureCause(.ownership))
        XCTAssertEqual(unanswered.failure?.portMayBeForeign, false)
        // The bind returned, so its generation is known and can be failed.
        let generation = try XCTUnwrap(unanswered.generation)
        XCTAssertEqual(lifecycle.phase, .verifying(generation: generation, rebind: true))
        // The check had the rest of the bound, and ended with it.
        XCTAssertGreaterThanOrEqual(took, .milliseconds(900))
        XCTAssertLessThan(took, .seconds(3))
        XCTAssertTrue(lifecycle.failUnconfirmed(try XCTUnwrap(unanswered.failure)))

        // The next attempt, asking for this listener's own nonce: owned.
        let owned = await DirectPathController.bindAndCheck(
            listener,
            port: first.port,
            nonce: nonce,
            within: 10
        )
        XCTAssertNil(owned.failure)
        XCTAssertTrue(lifecycle.confirm(try XCTUnwrap(owned.generation)))

        // No time at all: the bound ends the attempt before any check.
        XCTAssertTrue(lifecycle.beginClose())
        let none = await DirectPathController.bindAndCheck(
            listener,
            port: first.port,
            nonce: nonce,
            within: 0
        )
        XCTAssertEqual(none.failure?.step, .deadline)
    }

    /// Only the current re-bind attempt's completion settles the return: an
    /// older attempt completing after a newer one began, or after the return
    /// was settled by a failure, changes nothing.
    func testOnlyTheCurrentReBindAttemptSettlesTheReturn() {
        var attempts = DirectRebindAttempts()
        XCTAssertFalse(attempts.inFlight)

        let first = attempts.begin()
        let second = attempts.begin()
        XCTAssertNotEqual(first, second)
        XCTAssertFalse(attempts.end(first), "A stale completion settles nothing.")
        XCTAssertTrue(attempts.inFlight)
        XCTAssertEqual(attempts.current, second)
        XCTAssertTrue(attempts.end(second))
        XCTAssertFalse(attempts.inFlight)
        XCTAssertFalse(attempts.end(second), "A completion settles once.")

        let third = attempts.begin()
        attempts.abandon()
        XCTAssertFalse(attempts.end(third), "Settled by a failure first.")
        XCTAssertFalse(attempts.inFlight)
    }

    // MARK: Helpers

    /// The background's one closure for instance-1 is published by the
    /// return: in the held answers themselves when the port was bound at
    /// the first attempt, as it is unless the simulator could not; in any
    /// case once the listener serves, and once only.
    @MainActor
    private func assertTheReturnPublishedOneClosure(
        heldAnswers: [DirectPathStatus?],
        harness: Harness,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async throws {
        if harness.controller.listenerFailures.isEmpty {
            for answer in heldAnswers {
                XCTAssertEqual(answer?.closure, 1, file: file, line: line)
            }
        }
        try await eventually(file: file, line: line) {
            harness.controller.listenerState == .ready
        }
        XCTAssertEqual(harness.status("instance-1").closure, 1, file: file, line: line)
    }

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
    /// How many times the controller said a load gate may have changed.
    private(set) var availabilityChanges = 0
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
        onStatusChanged: { [unowned self] in
            self.announcements += 1
            let status = self.status("instance-1")
            if self.preferences.last != status.preferred {
                self.preferences.append(status.preferred)
            }
        },
        onAvailabilityChanged: { [unowned self] in
            self.availabilityChanges += 1
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
        // The watchdog's bound is far away on the real clock: these tests
        // are about what a loaded simulator does within its other bounds.
        timing: DirectPathTiming = DirectPathTiming(
            listenerWaitSeconds: 30,
            rebindSeconds: 10,
            foregroundSeconds: 600
        ),
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

    /// The controller's listener, which it keeps to itself: read through
    /// reflection, so that a test can do to its lifecycle what Network does
    /// when a serving socket fails, without the App exposing it.
    func listener() throws -> DirectConnectListener {
        try XCTUnwrap(
            Mirror(reflecting: controller).descendant("listener") as? DirectConnectListener
        )
    }

    /// Returns to the foreground and fails the bind that return makes, by
    /// telling the lifecycle what Network, or the attempt's own bound,
    /// tells it, before anyone else hears: `binding` if the bind is caught
    /// while it is binding (`nil`: not then), `ready` once its socket has
    /// become ready.
    /// The attempt binds off the main actor and can only be confirmed on
    /// it, so this watches the lifecycle and gives the main actor up only
    /// while the bind has not begun. Returns the cause it recorded and the
    /// status request it made right after the return, which that return
    /// holds.
    func enterForegroundFailingItsBind(
        binding: DirectListenerFailureCause?,
        ready: DirectListenerFailureCause
    ) async throws -> (cause: DirectListenerFailureCause, held: Recorder<DirectPathStatus?>) {
        let lifecycle = try await listener().lifecycle
        for attempt in 1...5 {
            enterForeground()
            let held = requestStatus("instance-1")
            let deadline = ContinuousClock.now.advanced(by: .seconds(10))
            watching: while ContinuousClock.now < deadline {
                switch lifecycle.phase {
                case .binding(let generation, true):
                    // With no cause for a bind still binding, wait here for
                    // its socket to be ready, without giving the main actor
                    // up: the attempt cannot be confirmed meanwhile.
                    if let binding, lifecycle.reject(generation, binding) {
                        return (binding, held)
                    }
                case .verifying(let generation, true):
                    if lifecycle.reject(generation, ready) {
                        return (ready, held)
                    }
                case .serving, .stopped:
                    // Confirmed before this looked.
                    break watching
                case .idle, .closing, .closed, .failed, .binding, .verifying:
                    await Task.yield()
                }
            }
            print("[iOS fixture] stage=the return's bind was not caught, attempt=\(attempt), phase=\(lifecycle.phase)")
            enterBackground()
            let closed = ContinuousClock.now.advanced(by: .seconds(10))
            while controller.listenerState != .closedForBackground, ContinuousClock.now < closed {
                try await Task.sleep(for: .milliseconds(20))
            }
        }
        XCTFail("The return's bind was never caught while it was under way: \(lifecycle.phase)")
        throw DirectTestTimeout()
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

extension DirectPathTiming {
    /// Patient bounds, and the next attempt a tenth of a second after a
    /// failed one, so that a test of the recovery does not wait seconds.
    static let recovering = DirectPathTiming(
        listenerWaitSeconds: 30,
        rebindSeconds: 10,
        foregroundSeconds: 600,
        retrySeconds: [0.1]
    )
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

    func signInMethods() async -> MobileSignInMethods? {
        MobileSignInMethods(providers: [], passkey: false)
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

    func appleExchange(
        transactionID: String,
        identityToken: String,
        authorizationCode: String,
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
        throw MobileAccountError.network
    }

    func primaryPasskeyStart() async throws -> MobilePrimaryPasskeyStart {
        throw MobileAccountError.providerUnavailable
    }

    func primaryPasskeyVerify(
        flow: String,
        response: [String: Any]
    ) async throws {
        throw MobileAccountError.network
    }

    func primaryPasskeyCancel(flow: String) async {}

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
    /// The kind of path the stand-in says its connection runs on.
    @MainActor var currentPath: DirectPathKind?
    /// That path's class and round-trip time, as the transport reads them.
    @MainActor var selectedPath: DirectSelectedPath?
    /// How often the stand-in says it checked its path again.
    @MainActor var counts = DirectPathCounts.zero

    @MainActor
    var pathDiagnostics: DirectPathDiagnostics {
        DirectPathDiagnostics(current: currentPath, attempts: [], selected: selectedPath)
    }

    @MainActor
    var pathCounts: DirectPathCounts {
        counts
    }

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
