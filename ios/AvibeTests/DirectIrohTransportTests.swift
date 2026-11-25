import Network
import UIKit
import XCTest
@testable import Avibe

/// The direct transport against a stand-in for the iroh binding
/// (docs/plans/remote-direct-ios-transport.md). These are tests of the
/// App's logic; they do not qualify iroh, a network, or a device.
@MainActor
final class DirectIrohTransportTests: XCTestCase {
    // MARK: Before a path

    func testNothingExistsBeforeAnAcceptedReadyAnswer() async throws {
        let harness = Harness()
        XCTAssertEqual(harness.log.all, [])
        XCTAssertFalse(harness.network.isWatching)

        // The instance has no endpoint: the key is made, nothing is bound.
        harness.rendezvous.answer(DirectIrohFixtures.unavailable(retryAfterSeconds: 60))
        harness.select()
        try await eventually("the answer is taken") { harness.scheduler.pending == [60] }
        XCTAssertEqual(
            harness.rendezvous.requests,
            [.init(selection: harness.selection, clientEndpointID: harness.engine.key.endpointID)]
        )
        XCTAssertTrue(harness.network.isWatching)

        // An answer for another instance.
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(client: harness.engine.key.endpointID, instanceID: "inst_2")
        )
        XCTAssertTrue(harness.scheduler.fire(60))
        try await eventually("the answer is rejected") { harness.scheduler.pending == [0.25] }

        // An answer whose only addresses the App may not dial.
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(
                client: harness.engine.key.endpointID,
                addresses: [
                    ["kind": "lan", "address": "8.8.8.8:53"],
                    ["kind": "lan", "address": "127.0.0.1:51234"],
                    ["kind": "ipv6", "address": "[fe80::1]:51234"],
                ]
            )
        )
        XCTAssertTrue(harness.scheduler.fire(0.25))
        try await eventually("the answer is rejected") { harness.scheduler.pending == [1] }

        // An answer admitted for another client's endpoint.
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(client: DirectIrohFixtures.instanceEndpoint)
        )
        XCTAssertTrue(harness.scheduler.fire(1))
        try await eventually("the answer is rejected") { harness.scheduler.pending == [5] }

        XCTAssertEqual(harness.rendezvous.requests.count, 4)
        XCTAssertEqual(harness.log.all, ["key"])
        XCTAssertEqual(harness.reports, [])
    }

    func testAHostThatIsNotADNSNameIsNeverAskedFor() async throws {
        let harness = Harness()
        for host in ["192.168.1.20", "alice-app.avibe.bot:443", "Alice-app.avibe.bot", "a b", ""] {
            harness.transport.select(
                DirectIrohSelection(
                    number: 9,
                    instanceID: DirectIrohFixtures.instanceID,
                    host: host,
                    storeIdentifier: UUID()
                )
            ) { _ in }
        }
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(harness.rendezvous.requests, [])
        XCTAssertEqual(harness.log.all, [])
        XCTAssertEqual(harness.scheduler.pending, [])
    }

    func testFailuresBackOffAndAPageThatIsStillLoadingIsAskedAgainSoon() async throws {
        let harness = Harness()
        harness.rendezvous.fail(DirectRendezvousFetchError.pageNotReady)
        harness.select()
        try await eventually("the page is asked again soon") { harness.scheduler.pending == [0.5] }

        var waits: [TimeInterval] = []
        var last: TimeInterval = 0.5
        let failures: [Error] = [
            DirectRendezvousFetchError.failed,
            DirectRendezvousFetchError.refused(status: 401),
            DirectRendezvousFetchError.failed,
            DirectIrohTestError.scripted,
            DirectRendezvousFetchError.refused(status: 403),
            DirectRendezvousFetchError.failed,
            DirectRendezvousFetchError.failed,
        ]
        for failure in failures {
            let asked = harness.rendezvous.requests.count
            harness.rendezvous.fail(failure)
            XCTAssertTrue(harness.scheduler.fire(last))
            try await eventually("the next attempt is set") {
                harness.rendezvous.requests.count == asked + 1 && harness.scheduler.pending.count == 1
            }
            last = harness.scheduler.pending[0]
            waits.append(last)
        }
        XCTAssertEqual(waits, [0.25, 1, 5, 15, 60, 300, 300])
        XCTAssertEqual(harness.log.all, ["key"])
    }

    // MARK: Finding a path

    func testAReadyAnswerIsDialledProbedAndOnlyThenReportedUsable() async throws {
        let harness = Harness()
        let endpoint = harness.engine.prepare()
        let connection = harness.makeConnection(answers: [DirectIrohFakeConnection.established])
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(generation: "boot:7", client: harness.engine.key.endpointID)
        )

        harness.select()
        try await eventually("the instance is dialled") { endpoint.dials.count == 1 }
        // Bound and dialling, and not usable before the probe is answered.
        XCTAssertEqual(harness.reports, [])
        XCTAssertEqual(harness.engine.secrets, [harness.engine.key.secret])
        XCTAssertEqual(
            endpoint.dials,
            [
                .init(
                    endpointID: DirectIrohFixtures.instanceEndpoint,
                    addresses: ["192.168.1.20:51234", "[fd12:0:0:0:0:0:0:1]:51234"],
                    relayURL: nil,
                    alpn: Data("avibe-direct/1".utf8)
                ),
            ]
        )

        endpoint.connection.resolve(connection)
        try await eventually("the path is reported") {
            harness.reports == [.usable(generation: "boot:7")]
        }
        XCTAssertEqual(
            connection.streams[0].send.written,
            Data("CONNECT alice-app.avibe.bot:443 HTTP/1.1\r\nHost: alice-app.avibe.bot:443\r\n\r\n".utf8)
        )
        // The probe's stream carried nothing and is abandoned.
        try await eventually("the probe's stream is abandoned") {
            harness.log.count(of: "reset") == 1 && harness.log.count(of: "stop") == 1
        }
        XCTAssertEqual(Array(harness.log.all.prefix(4)), ["key", "bind", "connect", "open"])
        // The answer is asked again before its admission ends, it runs out
        // by itself if never renewed, and a path that stays up clears the
        // failure count.
        XCTAssertEqual(harness.scheduler.pending.sorted(), [30, 240, 300])
        XCTAssertEqual(harness.log.count(of: "connection.close"), 0)
        XCTAssertEqual(harness.log.count(of: "endpoint.close"), 0)
    }

    func testAnotherIdentityOrARefusedProbeIsClosedAndNeverUsable() async throws {
        let harness = Harness()

        // A peer that proved another identity.
        let first = harness.engine.prepare()
        first.connection.resolve(
            DirectIrohFakeConnection(
                log: harness.log,
                remote: harness.engine.key.endpointID,
                answers: [DirectIrohFakeConnection.established]
            )
        )
        harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
        harness.select()
        try await eventually("the wrong peer is closed") {
            harness.log.count(of: "connection.close") == 1
                && harness.log.count(of: "endpoint.close") == 1
                && harness.scheduler.pending == [0.25]
        }
        XCTAssertEqual(harness.log.count(of: "open"), 0)

        // The right peer, whose ingress refuses this host.
        let second = harness.engine.prepare()
        second.connection.resolve(harness.makeConnection(answers: [DirectIrohFakeConnection.refused]))
        harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
        XCTAssertTrue(harness.scheduler.fire(0.25))
        try await eventually("the refused path is closed") {
            harness.log.count(of: "connection.close") == 2
                && harness.log.count(of: "endpoint.close") == 2
                && harness.scheduler.pending == [1]
        }

        // A peer that ends the stream without an answer.
        let third = harness.engine.prepare()
        third.connection.resolve(harness.makeConnection(answers: [Data()]))
        harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
        XCTAssertTrue(harness.scheduler.fire(1))
        try await eventually("the silent path is closed") {
            harness.log.count(of: "connection.close") == 3 && harness.scheduler.pending == [5]
        }
        XCTAssertEqual(harness.reports, [])
    }

    func testADialThatDoesNotAnswerEndsAtItsBoundAndALateConnectionIsClosed() async throws {
        var timing = Harness.patientTiming
        timing.dialSeconds = 0.05
        let harness = Harness(timing: timing)
        let endpoint = harness.engine.prepare()
        harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))

        harness.select()
        // Closing the endpoint is what ends the dial the binding still runs.
        try await eventually("the endpoint is closed at the bound") {
            harness.log.count(of: "endpoint.close") == 1 && harness.scheduler.pending == [0.25]
        }

        // The binding's dial succeeds after all: too late.
        endpoint.connection.resolve(harness.makeConnection(answers: [DirectIrohFakeConnection.established]))
        try await eventually("the late connection is closed") {
            harness.log.count(of: "connection.close") == 1
        }
        XCTAssertEqual(harness.log.count(of: "open"), 0)
        XCTAssertEqual(harness.reports, [])
    }

    func testDeselectEndsADialAtOnceAndWhatArrivesLateIsClosed() async throws {
        let harness = Harness()
        let endpoint = harness.engine.prepare()
        harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
        harness.select()
        try await eventually("the instance is dialled") { endpoint.dials.count == 1 }

        harness.transport.deselect()
        try await eventually("the endpoint is closed") {
            harness.log.count(of: "endpoint.close") == 1
        }
        XCTAssertFalse(harness.network.isWatching)
        XCTAssertEqual(harness.scheduler.pending, [])

        endpoint.connection.resolve(harness.makeConnection(answers: [DirectIrohFakeConnection.established]))
        try await eventually("the late connection is closed") {
            harness.log.count(of: "connection.close") == 1
        }
        XCTAssertEqual(harness.log.count(of: "open"), 0)
        XCTAssertEqual(harness.reports, [])
        XCTAssertEqual(harness.scheduler.pending, [])
    }

    func testAnAnswerThatArrivesAfterDeselectChangesNothing() async throws {
        let harness = Harness()
        harness.select()
        try await eventually("the rendezvous is asked") { harness.rendezvous.held.count == 1 }

        harness.transport.deselect()
        harness.rendezvous.held[0].resolve(
            DirectIrohFixtures.ready(client: harness.engine.key.endpointID)
        )
        try await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(harness.log.all, ["key"])
        XCTAssertEqual(harness.scheduler.pending, [])

        // The same for an answer to the previous selection.
        harness.select(DirectIrohFixtures.selection(2))
        try await eventually("the rendezvous is asked again") { harness.rendezvous.held.count == 2 }
        harness.rendezvous.answer(DirectIrohFixtures.unavailable(retryAfterSeconds: 120))
        harness.select(DirectIrohFixtures.selection(3))
        try await eventually("the new selection is answered") { harness.scheduler.pending == [120] }
        harness.rendezvous.held[1].resolve(
            DirectIrohFixtures.ready(client: harness.engine.key.endpointID)
        )
        try await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(harness.log.all, ["key"])
        XCTAssertEqual(harness.scheduler.pending, [120])
    }

    // MARK: A live path

    func testAClosedConnectionIsLostItsStreamsEndAndTheRendezvousIsAskedAgain() async throws {
        let harness = Harness()
        let connection = try await harness.bringUp(generation: "boot:1")
        let stream = try await harness.transport.openStream(for: harness.tag("boot:1"))
        let reading = Task<Data?, Error> {
            try await stream.read(maxLength: 16)
        }
        try await eventually("the relay is reading") { harness.log.count(of: "read") == 2 }

        // The instance's idle timeout, or the sidecar ending the admission.
        connection.drop()
        try await eventually("the loss is reported") {
            harness.reports == [.usable(generation: "boot:1"), .lost]
        }
        let outcome = await directIrohOutcome(of: reading)
        XCTAssertEqual(Self.failure(of: outcome) as? DirectIrohError, .cancelled)
        XCTAssertEqual(harness.log.count(of: "connection.close"), 1)
        try await eventually("the endpoint is closed") {
            harness.log.count(of: "endpoint.close") == 1
        }
        // The refresh and the stability timer went with the path.
        XCTAssertEqual(harness.scheduler.pending, [0.25])
        await assertStale { _ = try await harness.transport.openStream(for: harness.tag("boot:1", path: 9)) }

        // A path that keeps failing soon after it comes up backs off: the
        // failure count is cleared only by a path that stays up.
        let again = try await harness.bringUp(generation: "boot:1", firing: 0.25)
        again.drop()
        try await eventually("the second loss is reported") { harness.reports.count == 4 }
        XCTAssertEqual(harness.scheduler.pending, [1])

        let stable = try await harness.bringUp(generation: "boot:1", firing: 1)
        XCTAssertTrue(harness.scheduler.fire(30))
        stable.drop()
        try await eventually("the third loss is reported") { harness.reports.count == 6 }
        XCTAssertEqual(harness.scheduler.pending, [0.25])
    }

    func testANetworkChangeIsVerifyingThenUsableOnTheSamePathOrLost() async throws {
        let harness = Harness()
        // What each stream of the connection reads first, in the order the
        // streams open: the dial's probe, a relay, the first verification,
        // another relay, and the second verification.
        let connection = try await harness.bringUp(
            generation: "boot:1",
            answers: [
                DirectIrohFakeConnection.established,
                DirectIrohFakeConnection.established,
                DirectIrohFakeConnection.established,
                DirectIrohFakeConnection.established,
                DirectIrohFakeConnection.refused,
            ]
        )
        let relay = try await harness.transport.openStream(for: harness.tag("boot:1", path: 4))

        // Updates the monitor ignored as no change are counted with it.
        harness.network.ignoredUpdates = 7
        XCTAssertEqual(
            harness.transport.pathCounts,
            DirectPathCounts(dials: 1, pathUpdatesIgnored: 7)
        )

        harness.network.change()
        harness.network.change()
        // Reported at once and once; probed when the network is quiet.
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1"), .verifying])
        XCTAssertEqual(
            harness.transport.pathCounts,
            DirectPathCounts(verifications: 1, dials: 1, pathUpdates: 2, pathUpdatesIgnored: 7)
        )
        XCTAssertEqual(harness.scheduler.pending.filter { $0 == 0.5 }, [0.5])
        XCTAssertEqual(connection.streams.count, 2)
        // Nothing new opens while the path is being verified.
        await assertStale { _ = try await harness.transport.openStream(for: harness.tag("boot:1", path: 5)) }

        // The probe answers on the same connection: usable again, nothing
        // closed, and the rendezvous asked again.
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(generation: "boot:1", client: harness.engine.key.endpointID)
        )
        let asked = harness.rendezvous.requests.count
        XCTAssertTrue(harness.scheduler.fire(0.5))
        try await eventually("the path is usable again") {
            harness.reports == [
                .usable(generation: "boot:1"),
                .verifying,
                .usable(generation: "boot:1"),
            ] && harness.rendezvous.requests.count == asked + 1
                && harness.scheduler.pending.contains(240)
        }
        XCTAssertEqual(harness.log.count(of: "bind"), 1)
        XCTAssertEqual(harness.log.count(of: "connection.close"), 0)
        XCTAssertEqual(
            harness.transport.pathCounts,
            DirectPathCounts(
                verifications: 1, probesAfterPathUpdate: 1, dials: 1, pathUpdates: 2, pathUpdatesIgnored: 7
            )
        )
        // A tag from before the change is stale; a newer one opens.
        await assertStale { _ = try await harness.transport.openStream(for: harness.tag("boot:1", path: 4)) }
        _ = try await harness.transport.openStream(for: harness.tag("boot:1", path: 5))
        // The relay opened before the change is still there.
        try await relay.write(Data("x".utf8))

        // The next change finds the path dead.
        harness.network.change()
        XCTAssertTrue(harness.scheduler.fire(0.5))
        try await eventually("the loss is reported and the rendezvous asked at once") {
            harness.reports.last == .lost && harness.rendezvous.requests.count == asked + 2
        }
        XCTAssertEqual(harness.reports.count, 5)
        XCTAssertEqual(harness.reports[3], .verifying)
        XCTAssertEqual(harness.log.count(of: "connection.close"), 1)
        await assertCancelled { try await relay.write(Data("x".utf8)) }
        let counts = harness.transport.pathCounts
        XCTAssertEqual(counts.verifications, 2)
        XCTAssertEqual(counts.probesAfterPathUpdate, 2)
        XCTAssertEqual(counts.pathUpdates, 3)

        // The counts speak for one selection: a new one, or none, starts
        // from nothing, and from what the monitor had ignored by then.
        harness.transport.deselect()
        XCTAssertEqual(harness.transport.pathCounts, .zero)
    }

    // MARK: Which network path updates are changes

    private func snapshot(
        status: DirectIrohNetworkSnapshot.Status = .satisfied,
        interfaces: [(String, DirectIrohNetworkSnapshot.InterfaceKind)] = [("en0", .wifi), ("pdp_ip0", .cellular)],
        ipv4: Bool = true,
        ipv6: Bool = true,
        dns: Bool = true,
        gateways: [String] = ["192.0.2.1"],
        addresses: [DirectIrohInterfaceAddress]
    ) -> DirectIrohNetworkSnapshot {
        DirectIrohNetworkSnapshot(
            status: status,
            interfaces: interfaces.map { DirectIrohNetworkSnapshot.Interface(name: $0.0, kind: $0.1) },
            supportsIPv4: ipv4,
            supportsIPv6: ipv6,
            supportsDNS: dns,
            gateways: gateways,
            addresses: addresses
        )
    }

    private func address(_ interface: String, _ bytes: [UInt8]) -> DirectIrohInterfaceAddress {
        DirectIrohInterfaceAddress(interface: interface, family: bytes.count == 4 ? .ipv4 : .ipv6, bytes: bytes)
    }

    /// Sixteen bytes: `prefix`, zeros, then `last`.
    private func ipv6(_ prefix: [UInt8], _ last: UInt8) -> [UInt8] {
        var bytes = prefix
        bytes.append(contentsOf: [UInt8](repeating: 0, count: 15 - prefix.count))
        bytes.append(last)
        return bytes
    }

    /// An IPv6 link-local address (fe80::/10).
    private func linkLocal(_ last: UInt8) -> [UInt8] {
        ipv6([0xFE, 0x80], last)
    }

    /// Every change that can matter to the path is seen, and what cannot is
    /// not. Addresses here are from the documentation ranges.
    func testANetworkSnapshotDiffersForEveryChangeThatMattersAndForNoOther() {
        let wifi = address("en0", [192, 0, 2, 10])
        let wifiV6 = address("en0", ipv6([0x20, 0x01, 0x0D, 0xB8], 1))
        let cellular = address("pdp_ip0", [198, 51, 100, 7])
        // The system's own tunnel: a link-local address only.
        let systemTunnel = address("utun0", linkLocal(9))
        let base = snapshot(addresses: [wifi, wifiV6, cellular, systemTunnel])

        // The same facts read again, in another order: the same network.
        XCTAssertEqual(snapshot(addresses: [systemTunnel, cellular, wifiV6, wifi, wifi]), base)
        // Interfaces that carry nothing of the path: an AirDrop link, a
        // second cellular context, another system tunnel.
        XCTAssertEqual(
            snapshot(addresses: [
                wifi, wifiV6, cellular, systemTunnel,
                address("awdl0", linkLocal(4)),
                address("llw0", linkLocal(5)),
                address("pdp_ip1", [203, 0, 113, 9]),
                address("utun3", linkLocal(6)),
            ]),
            base
        )

        // Wi-Fi to cellular, and back.
        let onCellular = snapshot(interfaces: [("pdp_ip0", .cellular)], addresses: [cellular, systemTunnel])
        XCTAssertNotEqual(onCellular, base)
        XCTAssertEqual(snapshot(addresses: [wifi, wifiV6, cellular, systemTunnel]), base)
        // The order of preference alone: cellular took the default route.
        XCTAssertNotEqual(
            snapshot(interfaces: [("pdp_ip0", .cellular), ("en0", .wifi)], addresses: [wifi, wifiV6, cellular, systemTunnel]),
            base
        )
        // An address added to the same interface, removed from it, replaced.
        XCTAssertNotEqual(
            snapshot(addresses: [wifi, wifiV6, cellular, systemTunnel, address("en0", [192, 0, 2, 11])]),
            base
        )
        XCTAssertNotEqual(snapshot(addresses: [wifi, cellular, systemTunnel]), base)
        XCTAssertNotEqual(
            snapshot(addresses: [address("en0", [192, 0, 2, 99]), wifiV6, cellular, systemTunnel]),
            base
        )
        // The same address on another interface is another network.
        XCTAssertNotEqual(
            snapshot(addresses: [address("pdp_ip0", [192, 0, 2, 10]), wifiV6, cellular, systemTunnel]),
            base
        )
        // A VPN on: a tunnel interface with a routable address, whether or
        // not it takes the default route. And off again.
        let splitVPN = snapshot(addresses: [wifi, wifiV6, cellular, systemTunnel, address("utun4", [100, 64, 0, 2])])
        XCTAssertNotEqual(splitVPN, base)
        let fullVPN = snapshot(
            interfaces: [("utun4", .other), ("en0", .wifi), ("pdp_ip0", .cellular)],
            addresses: [wifi, wifiV6, cellular, systemTunnel, address("utun4", [10, 8, 0, 2])]
        )
        XCTAssertNotEqual(fullVPN, base)
        XCTAssertNotEqual(fullVPN, splitVPN)
        XCTAssertNotEqual(
            snapshot(addresses: [wifi, wifiV6, cellular, systemTunnel, address("ipsec0", [198, 51, 100, 40])]),
            base
        )
        // No path, a gateway, and what the path carries.
        XCTAssertNotEqual(snapshot(status: .unsatisfied, interfaces: [], addresses: []), base)
        XCTAssertNotEqual(snapshot(gateways: ["192.0.2.254"], addresses: [wifi, wifiV6, cellular, systemTunnel]), base)
        XCTAssertNotEqual(snapshot(ipv6: false, addresses: [wifi, wifiV6, cellular, systemTunnel]), base)
        XCTAssertNotEqual(snapshot(ipv4: false, addresses: [wifi, wifiV6, cellular, systemTunnel]), base)
        XCTAssertNotEqual(snapshot(dns: false, addresses: [wifi, wifiV6, cellular, systemTunnel]), base)

        // Only digests are kept: 32 bytes each, whatever was read.
        XCTAssertEqual(base.addressDigest.count, 32)
        XCTAssertEqual(base.gatewayDigest.count, 32)
        XCTAssertEqual(snapshot(status: .unsatisfied, interfaces: [], gateways: [], addresses: []).addressDigest.count, 32)
    }

    /// The first reading is the path as it is; after it, only a reading
    /// that differs from the one before is a change.
    func testOnlyAReadingThatDiffersFromTheOneBeforeIsANetworkChange() {
        let wifi = address("en0", [192, 0, 2, 10])
        let cellular = address("pdp_ip0", [198, 51, 100, 7])
        let onWifi = snapshot(addresses: [wifi, cellular])
        let onCellular = snapshot(interfaces: [("pdp_ip0", .cellular)], addresses: [cellular])
        var filter = DirectIrohNetworkChangeFilter()

        XCTAssertEqual(filter.observe(onWifi), .baseline)
        for _ in 0..<20 {
            XCTAssertEqual(filter.observe(onWifi), .unchanged)
        }
        XCTAssertEqual(filter.observe(onCellular), .changed)
        XCTAssertEqual(filter.observe(onCellular), .unchanged)
        XCTAssertEqual(filter.observe(onWifi), .changed)
        // A watch started again begins from what it then reads.
        filter.reset()
        XCTAssertEqual(filter.observe(onCellular), .baseline)
        XCTAssertEqual(filter.observe(onCellular), .unchanged)
    }

    /// On this host's real network, with the system's own monitor: reading
    /// the same path twice gives the same snapshot, and a monitor that is
    /// started reports no change for the path as it finds it. This shows
    /// the reading is stable on a real system; it is not a network switch.
    func testTheRealPathReadTwiceIsTheSameNetworkAndStartingReportsNoChange() async throws {
        XCTAssertFalse(DirectIrohInterfaceAddress.system().isEmpty, "the loopback at least")

        let first = Recorder<DirectIrohNetworkSnapshot>()
        let system = NWPathMonitor()
        system.pathUpdateHandler = { path in
            first.record(DirectIrohNetworkSnapshot(path: path, addresses: DirectIrohInterfaceAddress.system()))
            first.record(DirectIrohNetworkSnapshot(path: path, addresses: DirectIrohInterfaceAddress.system()))
        }
        system.start(queue: .main)
        defer {
            system.cancel()
        }
        try await eventually("the system reports its path") { first.values.count >= 2 }
        XCTAssertEqual(first.values[0], first.values[1])

        let monitor = DirectIrohPathMonitor()
        let changes = Recorder<Int>()
        monitor.start { changes.record(1) }
        defer {
            monitor.stop()
        }
        // Long enough for the monitor's first update, which the system
        // delivered above within the same wait. Nothing can be awaited for
        // "no change was reported", so this is a wait and not a barrier.
        try await Task.sleep(nanoseconds: 500_000_000)
        // A second, identical update from the system in that time would be
        // ignored and counted, which is the behaviour and not a failure.
        XCTAssertEqual(changes.values, [])
    }

    func testANetworkChangeBeforeAnyPathStartsOver() async throws {
        let harness = Harness()
        let endpoint = harness.engine.prepare()
        harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
        harness.select()
        try await eventually("the instance is dialled") { endpoint.dials.count == 1 }

        harness.network.change()
        try await eventually("the dial of the old network is closed") {
            harness.log.count(of: "endpoint.close") == 1
        }
        XCTAssertEqual(harness.scheduler.pending, [0.5])
        XCTAssertEqual(harness.reports, [])

        harness.rendezvous.answer(DirectIrohFixtures.unavailable(retryAfterSeconds: 90))
        XCTAssertTrue(harness.scheduler.fire(0.5))
        try await eventually("the rendezvous is asked on the new network") {
            harness.rendezvous.requests.count == 2 && harness.scheduler.pending == [90]
        }
    }

    func testAFailedCandidateProbesThePathAndOnlyADeadPathIsLost() async throws {
        let harness = Harness()
        let connection = try await harness.bringUp(
            generation: "boot:1",
            answers: [
                DirectIrohFakeConnection.established,
                DirectIrohFakeConnection.established,
                DirectIrohFakeConnection.refused,
            ]
        )

        // A failure for another selection or generation is not this path's.
        harness.transport.pathDidFail(
            for: DirectIrohStreamTag(
                selection: DirectIrohFixtures.selection(2),
                rendezvousGeneration: "boot:1",
                pathGeneration: 1
            )
        )
        harness.transport.pathDidFail(for: harness.tag("boot:0"))
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(connection.streams.count, 1)

        // The path answers: the candidate was refused or slow. Nothing is
        // reported, and further failures do not probe again at once.
        harness.transport.pathDidFail(for: harness.tag("boot:1"))
        harness.transport.pathDidFail(for: harness.tag("boot:1"))
        try await eventually("the path is probed once") {
            connection.streams.count == 2 && harness.scheduler.pending.contains(1)
        }
        harness.transport.pathDidFail(for: harness.tag("boot:1"))
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(connection.streams.count, 2)
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1")])

        // Later the path is dead.
        XCTAssertTrue(harness.scheduler.fire(1))
        harness.transport.pathDidFail(for: harness.tag("boot:1"))
        try await eventually("the loss is reported") {
            harness.reports == [.usable(generation: "boot:1"), .lost]
        }
        XCTAssertEqual(connection.streams.count, 3)
        XCTAssertEqual(harness.log.count(of: "connection.close"), 1)
        XCTAssertEqual(harness.scheduler.pending, [0.25])
    }

    func testARefreshKeepsTheSameGenerationAndRetiresAReplacedOne() async throws {
        let harness = Harness()
        _ = try await harness.bringUp(generation: "boot:1")
        let relay = try await harness.transport.openStream(for: harness.tag("boot:1"))

        // The same generation: the admission is extended, nothing changes.
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(generation: "boot:1", client: harness.engine.key.endpointID)
        )
        XCTAssertTrue(harness.scheduler.fire(240))
        try await eventually("the answer is asked again later") {
            harness.rendezvous.requests.count == 2 && harness.scheduler.pending.contains(240)
        }
        XCTAssertEqual(harness.log.count(of: "bind"), 1)
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1")])

        // A refresh that fails keeps the path and is tried again soon.
        harness.rendezvous.fail(DirectRendezvousFetchError.failed)
        XCTAssertTrue(harness.scheduler.fire(240))
        try await eventually("the refresh is tried again soon") {
            harness.rendezvous.requests.count == 3 && harness.scheduler.pending.contains(15)
        }
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1")])

        // A new generation: the old path is retired first, then the new one
        // is dialled and reported.
        let endpoint = harness.engine.prepare()
        endpoint.connection.resolve(harness.makeConnection(answers: [DirectIrohFakeConnection.established]))
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(generation: "boot:2", client: harness.engine.key.endpointID)
        )
        XCTAssertTrue(harness.scheduler.fire(15))
        try await eventually("the new generation is reported") {
            harness.reports == [
                .usable(generation: "boot:1"),
                .lost,
                .usable(generation: "boot:2"),
            ]
        }
        XCTAssertEqual(harness.log.count(of: "bind"), 2)
        XCTAssertEqual(harness.log.count(of: "connection.close"), 1)
        await assertCancelled { try await relay.write(Data("x".utf8)) }
        await assertStale { _ = try await harness.transport.openStream(for: harness.tag("boot:1", path: 9)) }
        _ = try await harness.transport.openStream(for: harness.tag("boot:2", path: 9))

        // The instance withdraws its endpoint.
        harness.rendezvous.answer(DirectIrohFixtures.unavailable(retryAfterSeconds: 60))
        XCTAssertTrue(harness.scheduler.fire(240))
        try await eventually("the withdrawal is reported") {
            harness.reports.count == 4 && harness.reports.last == .lost
        }
        XCTAssertEqual(harness.log.count(of: "connection.close"), 2)
        XCTAssertEqual(harness.scheduler.pending, [60])
    }

    // MARK: The public-relay owner trial

    private static let relayA = ["policy": "public-trial-v1", "url": "https://aps1-1.relay.n0.iroh.link./"]
    private static let relayB = ["policy": "public-trial-v1", "url": "https://use1-1.relay.n0.iroh.link./"]

    func testARelayOnlyAnswerIsDialledThroughThePinnedMapAndNothingElse() async throws {
        let harness = Harness()
        let endpoint = harness.engine.prepare()
        endpoint.connection.resolve(harness.makeConnection(answers: [DirectIrohFakeConnection.established]))
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(
                generation: "boot:1", client: harness.engine.key.endpointID, addresses: [], relay: Self.relayA
            )
        )
        harness.select()
        try await eventually("the relayed path is reported") {
            harness.reports == [.usable(generation: "boot:1")]
        }
        // The endpoint is bound with the pinned map, and the instance's
        // address is the answer's relay and nothing else.
        XCTAssertEqual(harness.engine.relayMaps, [DirectTrialRelay.publicTrialRelayURLs])
        XCTAssertEqual(
            endpoint.dials,
            [.init(
                endpointID: DirectIrohFixtures.instanceEndpoint,
                addresses: [],
                relayURL: "https://aps1-1.relay.n0.iroh.link./",
                alpn: Data("avibe-direct/1".utf8)
            )]
        )
    }

    func testEachAttemptIsRecordedByFamilyAndTheLivePathIsRead() async throws {
        let harness = Harness()
        // A failed attempt: the endpoint is bound, its own candidates read
        // before it closes.
        let failed = harness.engine.prepare()
        failed.localFamilies = DirectAddressFamilies(ipv4: 1, ipv6: 2)
        failed.connection.resolve(harness.makeConnection(answers: [DirectIrohFakeConnection.refused]))
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(generation: "boot:1", client: harness.engine.key.endpointID, relay: Self.relayA)
        )
        harness.select()
        try await eventually("the attempt fails") { harness.scheduler.pending == [0.25] }
        XCTAssertEqual(harness.transport.pathDiagnostics, DirectPathDiagnostics(current: nil, attempts: [
            DirectDialAttempt(offered: DirectAddressFamilies(ipv4: 1, ipv6: 1), offeredRelay: true,
                              local: DirectAddressFamilies(ipv4: 1, ipv6: 2), path: nil),
        ]))

        // A usable one, through the relay, later upgraded to IPv6.
        let endpoint = harness.engine.prepare()
        endpoint.localFamilies = DirectAddressFamilies(ipv4: 1, ipv6: 1)
        let connection = harness.makeConnection(answers: [DirectIrohFakeConnection.established])
        connection.selectedPath = .relay
        endpoint.connection.resolve(connection)
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(
                generation: "boot:1", client: harness.engine.key.endpointID, addresses: [], relay: Self.relayA
            )
        )
        XCTAssertTrue(harness.scheduler.fire(0.25))
        try await eventually("the path comes up") { harness.reports == [.usable(generation: "boot:1")] }
        XCTAssertEqual(harness.transport.pathDiagnostics.attempts.last, DirectDialAttempt(
            offered: DirectAddressFamilies(), offeredRelay: true,
            local: DirectAddressFamilies(ipv4: 1, ipv6: 1), path: .relay
        ))
        XCTAssertEqual(harness.transport.pathDiagnostics.current, .relay)
        connection.selectedPath = .ipv6
        XCTAssertEqual(harness.transport.pathDiagnostics.current, .ipv6)
        XCTAssertEqual(
            harness.transport.pathDiagnostics.attempts.last?.jsonObject["result"] as? String,
            "relay"
        )

        // Only the last few are kept.
        XCTAssertLessThanOrEqual(harness.transport.pathDiagnostics.attempts.count, DirectPathDiagnostics.attemptLimit)

        // The history speaks for one selection: a new one, or none, starts empty.
        harness.transport.deselect()
        XCTAssertEqual(harness.transport.pathDiagnostics, .empty)
    }

    /// The selected path's class and round trip are read with its kind, and
    /// kept only while the two readings agree.
    func testTheSelectedPathsClassAndRoundTripAreReadWithItsKind() async throws {
        let harness = Harness()
        let endpoint = harness.engine.prepare()
        let connection = harness.makeConnection(answers: [DirectIrohFakeConnection.established])
        connection.selectedPath = .ipv4
        connection.selectedPathDetail = DirectSelectedPath(kind: .ipv4, local: false, rttMs: 85)
        endpoint.connection.resolve(connection)
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(generation: "boot:1", client: harness.engine.key.endpointID, relay: Self.relayA)
        )
        harness.select()
        try await eventually("the path comes up") { harness.reports == [.usable(generation: "boot:1")] }
        XCTAssertEqual(harness.transport.pathDiagnostics.current, .ipv4)
        XCTAssertEqual(
            harness.transport.pathDiagnostics.selected,
            DirectSelectedPath(kind: .ipv4, local: false, rttMs: 85)
        )

        // The two readings disagree (the path moved between them): no class.
        connection.selectedPath = .ipv6
        XCTAssertEqual(harness.transport.pathDiagnostics.current, .ipv6)
        XCTAssertNil(harness.transport.pathDiagnostics.selected)

        harness.transport.deselect()
        XCTAssertNil(harness.transport.pathDiagnostics.selected)
    }

    /// Brings a relayed path up on a stand-in connection whose selected
    /// path is named `relay one`.
    private func relayedPath() async throws -> (Harness, DirectIrohFakeConnection) {
        let harness = Harness()
        let endpoint = harness.engine.prepare()
        let connection = harness.makeConnection(answers: [DirectIrohFakeConnection.established])
        connection.selectedPath = .relay
        connection.selectedPathIdentity = "relay one"
        endpoint.connection.resolve(connection)
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(
                generation: "boot:1", client: harness.engine.key.endpointID, addresses: [], relay: Self.relayA
            )
        )
        harness.select()
        try await eventually("the path comes up") { harness.reports == [.usable(generation: "boot:1")] }
        return (harness, connection)
    }

    /// The mark a measurement fences on (`DirectPathMark`), at the
    /// transport's side of the binding. A report of the watch that leaves
    /// the selected path as it was changes nothing: the connection opens
    /// and closes other paths while it looks for a better one. A report, or
    /// a read, that finds another path selected changes the mark, another
    /// path of the same kind included, and it stays changed if the path
    /// changes back.
    func testThePathsMarkChangesOnlyWhenAnotherPathIsFoundSelected() async throws {
        // No connection: nothing to mark.
        XCTAssertNil(Harness().transport.pathMark)
        let (harness, connection) = try await relayedPath()

        // Read again with nothing in between: the same mark.
        let first = try XCTUnwrap(harness.transport.pathMark)
        XCTAssertEqual(harness.transport.pathMark, first)

        // Another path is opened, and closed again; the selected one stays.
        connection.reportOtherPaths()
        XCTAssertEqual(harness.transport.pathMark, first)
        connection.reportOtherPaths()
        connection.reportOtherPaths()
        XCTAssertEqual(harness.transport.pathMark, first)

        // The watch reports another relay selected: the same kind, another
        // path. The mark has changed at once, before the main actor has
        // heard of the report.
        connection.movePath(to: .relay, identity: "relay two")
        let afterReport = try XCTUnwrap(harness.transport.pathMark)
        XCTAssertNotEqual(afterReport, first)
        XCTAssertEqual(harness.transport.pathMark, afterReport)

        // Each of two changes reported one after the other counts: out to
        // an address and back, the path is the one it was and the mark is
        // not.
        connection.movePath(to: .ipv6, identity: "address a")
        connection.movePath(to: .relay, identity: "relay two")
        let outAndBack = try XCTUnwrap(harness.transport.pathMark)
        XCTAssertNotEqual(outAndBack, afterReport)
        XCTAssertEqual(outAndBack.moves, afterReport.moves + 2)

        // Found by a read, with no report yet: another relay again.
        connection.selectedPathIdentity = "relay one"
        let afterRead = try XCTUnwrap(harness.transport.pathMark)
        XCTAssertNotEqual(afterRead, outAndBack)
        // The report of that change, when it comes, is no second change.
        connection.reportOtherPaths()
        XCTAssertEqual(harness.transport.pathMark, afterRead)
        // Back by a read: it stays a change for every mark taken before.
        connection.selectedPathIdentity = "relay two"
        let back = try XCTUnwrap(harness.transport.pathMark)
        for earlier in [first, afterReport, outAndBack, afterRead] {
            XCTAssertNotEqual(back, earlier)
        }

        // No path selected: no mark, and the one after it is a new one.
        connection.selectedPath = nil
        connection.selectedPathIdentity = nil
        XCTAssertNil(harness.transport.pathMark)
        connection.selectedPath = .relay
        connection.selectedPathIdentity = "relay two"
        XCTAssertNotEqual(try XCTUnwrap(harness.transport.pathMark), back)

        // The connection is given up: nothing to mark.
        harness.transport.deselect()
        XCTAssertNil(harness.transport.pathMark)
    }

    /// The same at a measurement's sampling, fenced on the transport's mark
    /// as `measurementLink` fences it: reports about other paths while a
    /// leg runs keep every sample; another path selected ends the leg with
    /// what was timed before it.
    func testALegKeepsItsSamplesThroughReportsAboutOtherPathsAndEndsWhenAnotherIsSelected() async throws {
        let (harness, connection) = try await relayedPath()
        let transport = harness.transport
        let sample = DirectMeasure.Sample(elapsed: .milliseconds(20), healthy: true, counts: true)
        let far = { ContinuousClock.now.advanced(by: .seconds(30)) }

        let steady = try XCTUnwrap(transport.pathMark)
        let kept = await DirectMeasure.sample(
            limits: .standard,
            end: far(),
            unchanged: { _ in await MainActor.run { transport.pathMark == steady } }
        ) { _ in
            // Another path opened and closed during every request.
            connection.reportOtherPaths()
            connection.reportOtherPaths()
            return sample
        }
        XCTAssertEqual(kept.times.count, DirectMeasure.samples)
        XCTAssertNil(kept.error)

        let before = try XCTUnwrap(transport.pathMark)
        XCTAssertEqual(before, steady)
        let ended = await DirectMeasure.sample(
            limits: .standard,
            end: far(),
            unchanged: { _ in await MainActor.run { transport.pathMark == before } }
        ) { index in
            if index == 3 {
                // During the third timed request: another relay.
                connection.movePath(to: .relay, identity: "relay two")
            }
            return sample
        }
        XCTAssertEqual(ended.times.count, 2)
        XCTAssertEqual(ended.error, .failed)

        harness.transport.deselect()
    }

    /// Only the class of the far end's address leaves the binding: local
    /// (private, shared address space, unique-local, link-local) or not. Zero round trip is
    /// "not measured".
    func testASelectedPathIsClassedByItsRemoteAddressAndNeverKeepsIt() {
        let local = [
            "10.0.0.7:4433", "172.16.1.2:1", "172.31.255.255:1", "192.168.1.20:52000", "169.254.3.4:1",
            "[fd7a:115c:a1e0::1]:4433", "[fc00::1]:1", "[fe80::1%en0]:4433", "[FE80::abcd]:1",
            "[::ffff:192.168.1.2]:1", "100.64.0.1:1", "100.127.255.1:41641",
        ]
        for address in local {
            XCTAssertTrue(DirectSelectedPath.isLocalNetwork(socketAddress: address), address)
        }
        let global = [
            "203.0.113.5:4433", "172.32.0.1:1", "172.15.0.1:1", "100.63.255.1:1", "100.128.0.1:1", "8.8.8.8:53",
            "[2001:db8::1]:4433", "[::1]:1", "[::ffff:8.8.8.8]:1", "[fec0::1]:1", "not-an-address",
        ]
        for address in global {
            XCTAssertFalse(DirectSelectedPath.isLocalNetwork(socketAddress: address), address)
        }

        XCTAssertEqual(
            DirectSelectedPath(isRelay: false, isIp: true, remoteAddress: "[fe80::1]:4433", rttMs: 3),
            DirectSelectedPath(kind: .ipv6, local: true, rttMs: 3)
        )
        XCTAssertEqual(
            DirectSelectedPath(isRelay: false, isIp: true, remoteAddress: "203.0.113.5:4433", rttMs: 0),
            DirectSelectedPath(kind: .ipv4, local: false, rttMs: nil)
        )
        XCTAssertEqual(
            DirectSelectedPath(isRelay: true, isIp: false, remoteAddress: "https://relay.example/", rttMs: 140),
            DirectSelectedPath(kind: .relay, local: nil, rttMs: 140)
        )
        XCTAssertNil(DirectSelectedPath(isRelay: false, isIp: false, remoteAddress: "custom", rttMs: 5))
    }

    func testAChangeOfTheConnectionsPathIsReportedOnceAndOnlyWhileThePathIsLive() async throws {
        let harness = Harness()
        let endpoint = harness.engine.prepare()
        let connection = harness.makeConnection(answers: [DirectIrohFakeConnection.established])
        connection.selectedPath = .relay
        endpoint.connection.resolve(connection)
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(
                generation: "boot:1", client: harness.engine.key.endpointID, addresses: [], relay: Self.relayA
            )
        )
        harness.select()
        try await eventually("the path comes up") { harness.reports == [.usable(generation: "boot:1")] }

        // A stream opened while the connection still runs through the relay.
        let early = try await harness.transport.openStream(for: harness.tag("boot:1"))

        // The relayed connection reaches a direct address: reported once.
        connection.movePath(to: .ipv4)
        connection.movePath(to: .ipv4)
        try await eventually("the upgrade is reported") {
            harness.reports == [.usable(generation: "boot:1"), .pathChanged(.ipv4)]
        }
        XCTAssertEqual(harness.transport.pathDiagnostics.current, .ipv4)
        // The change is a migration inside the one connection: nothing was
        // closed, the stream opened before it still carries bytes, and new
        // streams open on the same connection.
        XCTAssertEqual(harness.log.count(of: "connection.close"), 0)
        try await early.write(Data("still".utf8))
        _ = try await harness.transport.openStream(for: harness.tag("boot:1"))
        XCTAssertEqual(harness.engine.bound.count, 1)
        connection.movePath(to: .relay)
        try await eventually("the way back is reported") {
            harness.reports.last == .pathChanged(.relay)
        }
        XCTAssertEqual(harness.reports.count, 3)

        // Once the selection ends the watch is stopped, and a late change of
        // the old connection is reported to nobody.
        harness.transport.deselect()
        try await eventually("the watch is stopped") { harness.log.count(of: "watch.stop") == 1 }
        connection.movePath(to: .ipv6)
        try await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(harness.reports.count, 3)
    }

    func testAnAnswerWithoutAKnownRelayBindsNoRelayAtAll() async throws {
        let harness = Harness()
        _ = try await harness.bringUp(
            generation: "boot:1",
            relay: ["policy": "public-trial-v2", "url": "https://aps1-1.relay.n0.iroh.link./"]
        )
        XCTAssertEqual(harness.engine.relayMaps, [[]])
        XCTAssertNil(harness.engine.bound[0].dials[0].relayURL)
    }

    func testAChangedRelayRetiresThePathAndALateDialForTheOldOneInstallsNothing() async throws {
        let harness = Harness()
        // Relay A and addresses, under boot:1.
        _ = try await harness.bringUp(generation: "boot:1", relay: Self.relayA)
        let old = try await harness.transport.openStream(for: harness.tag("boot:1"))

        // Relay B, same key and addresses, under the next generation: the
        // path is retired, then dialled again through B.
        let second = harness.engine.prepare()
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(generation: "boot:2", client: harness.engine.key.endpointID, relay: Self.relayB)
        )
        XCTAssertTrue(harness.scheduler.fire(240))
        try await eventually("relay B is dialled") { second.dials.count == 1 }
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1"), .lost])
        XCTAssertEqual(second.dials[0].relayURL, "https://use1-1.relay.n0.iroh.link./")
        await assertCancelled { try await old.write(Data("x".utf8)) }

        // The relay is withdrawn (B to null) while B's dial is still under
        // way: B's dial is given up, and what it produces late is closed.
        let third = harness.engine.prepare()
        third.connection.resolve(harness.makeConnection(answers: [DirectIrohFakeConnection.established]))
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(generation: "boot:3", client: harness.engine.key.endpointID)
        )
        XCTAssertTrue(harness.scheduler.fire(240))
        try await eventually("the path without a relay comes up") {
            harness.reports == [.usable(generation: "boot:1"), .lost, .usable(generation: "boot:3")]
        }
        second.connection.resolve(harness.makeConnection(answers: [DirectIrohFakeConnection.established]))
        try await eventually("B's late connection is closed") {
            harness.log.count(of: "connection.close") == 2
        }
        XCTAssertEqual(
            harness.engine.relayMaps,
            [DirectTrialRelay.publicTrialRelayURLs, DirectTrialRelay.publicTrialRelayURLs, []]
        )
        XCTAssertNil(third.dials[0].relayURL)
        await assertStale { _ = try await harness.transport.openStream(for: harness.tag("boot:2", path: 9)) }
        _ = try await harness.transport.openStream(for: harness.tag("boot:3", path: 9))
    }

    func testARelayDialHasItsOwnLongerBoundAndADirectDialKeepsItsOwn() async throws {
        XCTAssertEqual(DirectIrohTiming().dialSeconds, 1.5)
        XCTAssertEqual(DirectIrohTiming().relayDialSeconds, 5)

        var timing = Harness.patientTiming
        timing.dialSeconds = 0.05
        let harness = Harness(timing: timing)
        let endpoint = harness.engine.prepare()
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(
                generation: "boot:1", client: harness.engine.key.endpointID, addresses: [], relay: Self.relayA
            )
        )
        harness.select()
        try await eventually("the instance is dialled through the relay") { endpoint.dials.count == 1 }
        // Well past the direct bound: the relay dial is still waiting.
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(harness.log.count(of: "endpoint.close"), 0)
        endpoint.connection.resolve(harness.makeConnection(answers: [DirectIrohFakeConnection.established]))
        try await eventually("the relayed path is reported") {
            harness.reports == [.usable(generation: "boot:1")]
        }
    }

    func testARelayChangeUnderTheSameGenerationIsNotARenewal() async throws {
        let harness = Harness()
        _ = try await harness.bringUp(generation: "boot:1", relay: Self.relayA)
        // A policy-only or URL-only change the instance should have given a
        // new generation: the App still treats it as a new offer.
        let next = harness.engine.prepare()
        next.connection.resolve(harness.makeConnection(answers: [DirectIrohFakeConnection.established]))
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(generation: "boot:1", client: harness.engine.key.endpointID, relay: Self.relayB)
        )
        XCTAssertTrue(harness.scheduler.fire(240))
        try await eventually("the path is retired and dialled again") {
            harness.reports == [.usable(generation: "boot:1"), .lost, .usable(generation: "boot:1")]
        }
        XCTAssertEqual(harness.log.count(of: "bind"), 2)
        XCTAssertEqual(harness.log.count(of: "connection.close"), 1)
    }

    func testARelayOnlyAnswerThatIsWithdrawnEndsThePathAndThePublicOriginStays() async throws {
        let harness = Harness()
        _ = try await harness.bringUp(generation: "boot:1", addresses: [], relay: Self.relayA)
        // The instance has neither a relay nor an address any more.
        harness.rendezvous.answer(DirectIrohFixtures.unavailable(retryAfterSeconds: 60))
        XCTAssertTrue(harness.scheduler.fire(240))
        try await eventually("the withdrawal is reported") {
            harness.reports == [.usable(generation: "boot:1"), .lost]
        }
        XCTAssertEqual(harness.scheduler.pending, [60])
        await assertStale { _ = try await harness.transport.openStream(for: harness.tag("boot:1", path: 9)) }
    }

    func testAnAnswerForAnotherInstanceNeitherBringsAPathUpNorTakesOneDown() async throws {
        let harness = Harness()
        _ = try await harness.bringUp(generation: "boot:1")
        let relay = try await harness.transport.openStream(for: harness.tag("boot:1"))

        // "No direct path", said for another instance and for another host.
        harness.rendezvous.answer(DirectIrohFixtures.unavailable(instanceID: "inst_2"))
        XCTAssertTrue(harness.scheduler.fire(240))
        try await eventually("the refresh counts as failed") {
            harness.rendezvous.requests.count == 2 && harness.scheduler.pending.contains(15)
        }
        harness.rendezvous.answer(DirectIrohFixtures.unavailable(hostname: "bob-app.avibe.bot"))
        XCTAssertTrue(harness.scheduler.fire(15))
        try await eventually("the refresh counts as failed") {
            harness.rendezvous.requests.count == 3 && harness.scheduler.pending.contains(15)
        }

        // The selected instance's path is untouched, and its deadline too.
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1")])
        XCTAssertEqual(harness.log.count(of: "connection.close"), 0)
        XCTAssertEqual(harness.scheduler.pending.filter { $0 == 300 }, [300])
        try await relay.write(Data("x".utf8))
        _ = try await harness.transport.openStream(for: harness.tag("boot:1"))
    }

    // MARK: The answer's lifetime

    func testAnAnswerIsNotUsedPastItsLifetimeAndOnlyANewAnswerRenewsIt() async throws {
        let harness = Harness()
        // What each stream reads first: the dial's probe, a relay, and the
        // probe after the renewal.
        let connection = try await harness.bringUp(
            generation: "boot:1",
            answers: [
                DirectIrohFakeConnection.established,
                DirectIrohFakeConnection.established,
                DirectIrohFakeConnection.established,
            ]
        )
        let relay = try await harness.transport.openStream(for: harness.tag("boot:1", path: 2))

        // Every refresh fails. A failed refresh extends nothing.
        harness.rendezvous.fail(DirectRendezvousFetchError.failed)
        XCTAssertTrue(harness.scheduler.fire(240))
        try await eventually("the refresh is tried again") {
            harness.rendezvous.requests.count == 2 && harness.scheduler.pending.contains(15)
        }
        harness.rendezvous.fail(DirectRendezvousFetchError.refused(status: 401))
        XCTAssertTrue(harness.scheduler.fire(15))
        try await eventually("the refresh is tried again") {
            harness.rendezvous.requests.count == 3 && harness.scheduler.pending.contains(15)
        }
        XCTAssertEqual(harness.scheduler.pending.filter { $0 == 300 }, [300])
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1")])

        // The answer runs out: new connections leave the path at once.
        harness.clock.now += 300
        XCTAssertTrue(harness.scheduler.fire(300))
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1"), .verifying])
        await assertStale { _ = try await harness.transport.openStream(for: harness.tag("boot:1", path: 3)) }

        // Nothing is probed under it: not for a failed candidate, and not
        // for a network change, which asks the rendezvous again instead.
        harness.transport.pathDidFail(for: harness.tag("boot:1", path: 3))
        harness.network.change()
        harness.rendezvous.fail(DirectRendezvousFetchError.failed)
        XCTAssertTrue(harness.scheduler.fire(0.5))
        try await eventually("the rendezvous is asked again") {
            harness.rendezvous.requests.count == 4 && harness.scheduler.pending.contains(15)
        }
        XCTAssertEqual(connection.streams.count, 2)
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1"), .verifying])
        // What the path already carries is not closed on the phone's timing.
        try await relay.write(Data("x".utf8))
        XCTAssertEqual(harness.log.count(of: "connection.close"), 0)

        // A newly accepted answer of the same generation renews it. The
        // path is probed, and only then usable again, on the same connection.
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(generation: "boot:1", client: harness.engine.key.endpointID)
        )
        XCTAssertTrue(harness.scheduler.fire(15))
        try await eventually("the path is usable again") {
            harness.reports == [
                .usable(generation: "boot:1"),
                .verifying,
                .usable(generation: "boot:1"),
            ]
        }
        XCTAssertEqual(connection.streams.count, 3)
        XCTAssertEqual(harness.log.count(of: "bind"), 1)
        XCTAssertTrue(harness.scheduler.pending.contains(240))
        XCTAssertEqual(harness.scheduler.pending.filter { $0 == 300 }, [300])
        _ = try await harness.transport.openStream(for: harness.tag("boot:1", path: 3))
        try await relay.write(Data("y".utf8))

        // Run out again, and this time the instance ends the connection.
        harness.clock.now += 300
        XCTAssertTrue(harness.scheduler.fire(300))
        XCTAssertEqual(harness.reports.count, 4)
        connection.drop()
        try await eventually("the loss is reported") {
            harness.reports.count == 5 && harness.reports.last == .lost
        }
        await assertCancelled { try await relay.write(Data("z".utf8)) }
    }

    func testAnAnswerCountsFromWhenItWasAskedForAndOneThatArrivesTooLateIsNotAccepted() async throws {
        let harness = Harness()
        let endpoint = harness.engine.prepare()
        endpoint.connection.resolve(
            harness.makeConnection(answers: [DirectIrohFakeConnection.established])
        )
        harness.select()
        try await eventually("the rendezvous is asked") { harness.rendezvous.held.count == 1 }

        // The answer arrives 100 seconds after it was asked for. The
        // instance issued it at some point in between, so its five minutes
        // are counted from the request: 200 seconds are left, and the
        // refresh is due in 140.
        harness.clock.now += 100
        harness.rendezvous.held[0].resolve(
            DirectIrohFixtures.ready(generation: "boot:1", client: harness.engine.key.endpointID)
        )
        try await eventually("the path comes up") {
            harness.reports == [.usable(generation: "boot:1")]
        }
        XCTAssertEqual(harness.scheduler.pending.sorted(), [30, 140, 200])

        // A refresh whose answer arrives after its own span is no answer.
        // By then the first answer has run out too, although its timer has
        // not fired: the path stops being usable, and the late answer does
        // not renew it, so nothing is probed.
        XCTAssertTrue(harness.scheduler.fire(140))
        try await eventually("the refresh is asked") { harness.rendezvous.held.count == 2 }
        harness.clock.now += 300
        harness.rendezvous.held[1].resolve(
            DirectIrohFixtures.ready(generation: "boot:1", client: harness.engine.key.endpointID)
        )
        try await eventually("the late answer is not accepted") {
            harness.scheduler.pending.contains(15)
        }
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1"), .verifying])
        XCTAssertEqual(harness.log.count(of: "open"), 1)
        await assertStale { _ = try await harness.transport.openStream(for: harness.tag("boot:1")) }

        // The same before any path: an answer that arrives too late is
        // never dialled.
        let second = Harness()
        second.select()
        try await eventually("the rendezvous is asked") { second.rendezvous.held.count == 1 }
        second.clock.now += 300
        second.rendezvous.held[0].resolve(
            DirectIrohFixtures.ready(generation: "boot:1", client: second.engine.key.endpointID)
        )
        try await eventually("the late answer is not accepted") {
            second.scheduler.pending == [0.25]
        }
        XCTAssertEqual(second.log.all, ["key"])
    }

    func testAnAnswerThatRunsOutWhileItIsDialledIsGivenUp() async throws {
        let harness = Harness()
        let endpoint = harness.engine.prepare()
        harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
        harness.select()
        try await eventually("the instance is dialled") { endpoint.dials.count == 1 }

        harness.clock.now += 300
        XCTAssertTrue(harness.scheduler.fire(300))
        try await eventually("the dial is closed") {
            harness.log.count(of: "endpoint.close") == 1
        }
        XCTAssertEqual(harness.scheduler.pending, [0.25])
        XCTAssertEqual(harness.reports, [])
    }

    // MARK: The lease is the clock's, not its timer's

    func testALeaseTheClockHasPassedIsNotUsedAlthoughItsTimerHasNotFired() async throws {
        let harness = Harness()
        let connection = try await harness.bringUp(generation: "boot:1")
        let relay = try await harness.transport.openStream(for: harness.tag("boot:1", path: 2))

        // A timer that fires before the deadline changes nothing.
        XCTAssertTrue(harness.scheduler.fire(300))
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1")])
        XCTAssertEqual(harness.scheduler.pending.filter { $0 == 300 }, [300])
        _ = try await harness.transport.openStream(for: harness.tag("boot:1", path: 2))

        // The clock passes the deadline. No timer fires.
        harness.clock.now += 300
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1")])

        // The next thing asked of the path finds the lease run out: no
        // stream, no probe for a failed candidate, none for a network
        // change, and `verifying` is reported at that moment.
        await assertStale { _ = try await harness.transport.openStream(for: harness.tag("boot:1", path: 3)) }
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1"), .verifying])
        harness.transport.pathDidFail(for: harness.tag("boot:1", path: 3))
        harness.network.change()
        harness.rendezvous.fail(DirectRendezvousFetchError.failed)
        let asked = harness.rendezvous.requests.count
        XCTAssertTrue(harness.scheduler.fire(0.5))
        try await eventually("the rendezvous is asked instead") {
            harness.rendezvous.requests.count == asked + 1 && harness.scheduler.pending.contains(15)
        }
        XCTAssertEqual(connection.streams.count, 3)
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1"), .verifying])

        // What the path already carries stays, and the timer that fires
        // at last finds nothing left to do.
        try await relay.write(Data("x".utf8))
        XCTAssertEqual(harness.log.count(of: "connection.close"), 0)
        XCTAssertTrue(harness.scheduler.fire(300))
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1"), .verifying])
        XCTAssertEqual(harness.scheduler.pending.filter { $0 == 300 }, [])
    }

    func testNoStageOfADialStartsAfterItsLeaseRanOut() async throws {
        // In each case the clock passes the deadline while one stage is in
        // the binding, and the lease's timer does not fire. The stage ends
        // well, and the next one must not start.

        // The bind ends late: no connect.
        do {
            let harness = Harness()
            let gate = harness.engine.holdBinds()
            harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
            harness.select()
            try await eventually("the bind reaches the binding") { harness.log.count(of: "bind") == 1 }

            harness.clock.now += 300
            gate.resolve(())
            try await eventually("the endpoint is closed") {
                harness.log.count(of: "endpoint.close") == 1 && harness.scheduler.pending == [0.25]
            }
            try await Task.sleep(nanoseconds: 50_000_000)
            XCTAssertEqual(harness.log.count(of: "connect"), 0)
            XCTAssertEqual(harness.log.count(of: "open"), 0)
            XCTAssertEqual(harness.reports, [])
        }
        // The connect ends late: no probe is opened.
        do {
            let harness = Harness()
            let endpoint = harness.engine.prepare()
            harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
            harness.select()
            try await eventually("the instance is dialled") { endpoint.dials.count == 1 }

            harness.clock.now += 300
            endpoint.connection.resolve(
                harness.makeConnection(answers: [DirectIrohFakeConnection.established])
            )
            try await eventually("the late path is closed") {
                harness.log.count(of: "connection.close") == 1
                    && harness.log.count(of: "endpoint.close") == 1
                    && harness.scheduler.pending == [0.25]
            }
            try await Task.sleep(nanoseconds: 50_000_000)
            XCTAssertEqual(harness.log.count(of: "open"), 0)
            XCTAssertEqual(harness.log.count(of: "write"), 0)
            XCTAssertEqual(harness.reports, [])
        }
        // The probe's stream opens late: no CONNECT is written on it.
        do {
            let harness = Harness()
            let endpoint = harness.engine.prepare()
            let connection = harness.makeConnection(answers: [DirectIrohFakeConnection.established])
            let gate = connection.holdNextOpen()
            endpoint.connection.resolve(connection)
            harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
            harness.select()
            try await eventually("the probe's open reaches the binding") {
                harness.log.count(of: "open") == 1
            }

            harness.clock.now += 300
            gate.resolve(())
            try await eventually("the late stream and the path are closed") {
                harness.log.count(of: "reset") == 1
                    && harness.log.count(of: "connection.close") == 1
                    && harness.log.count(of: "endpoint.close") == 1
                    && harness.scheduler.pending == [0.25]
            }
            try await Task.sleep(nanoseconds: 50_000_000)
            XCTAssertEqual(harness.log.count(of: "write"), 0)
            XCTAssertEqual(harness.log.count(of: "read"), 0)
            XCTAssertEqual(harness.reports, [])
        }
    }

    func testAStageThatWasSetGoingStartsNothingOnceItsDialHasEnded() async throws {
        // In each case the bind's task is under way and has not reached
        // the binding: it is held where it asks whether it may start. The
        // dial ends meanwhile. The lease is still good for minutes, so only
        // the dial's ending keeps the bind from starting.
        var impatient = Harness.patientTiming
        impatient.bindSeconds = 0.05

        // The dial's own bound ends it, and then the instance withdraws.
        do {
            let harness = Harness(timing: impatient)
            let hold = harness.clock.holdNextBackgroundRead()
            harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
            harness.select()
            try await hold.reached.wait()
            try await eventually("the dial gives up and sets its retry") {
                harness.scheduler.pending == [0.25]
            }
            harness.rendezvous.answer(DirectIrohFixtures.unavailable(retryAfterSeconds: 90))
            XCTAssertTrue(harness.scheduler.fire(0.25))
            try await eventually("the instance's wait is kept") { harness.scheduler.pending == [90] }

            hold.release()
            try await Task.sleep(nanoseconds: 150_000_000)
            XCTAssertEqual(harness.log.all, ["key"])
            XCTAssertEqual(harness.reports, [])
            XCTAssertEqual(harness.scheduler.pending, [90])
        }
        // The dial's own bound ends it, and then the selection ends.
        do {
            let harness = Harness(timing: impatient)
            let hold = harness.clock.holdNextBackgroundRead()
            harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
            harness.select()
            try await hold.reached.wait()
            try await eventually("the dial gives up and sets its retry") {
                harness.scheduler.pending == [0.25]
            }
            harness.transport.deselect()

            hold.release()
            try await Task.sleep(nanoseconds: 150_000_000)
            XCTAssertEqual(harness.log.all, ["key"])
            XCTAssertEqual(harness.scheduler.pending, [])
        }
        // The dial is still waiting for its bind when the instance
        // withdraws, and when the App goes to the background.
        do {
            let harness = Harness()
            let hold = harness.clock.holdNextBackgroundRead()
            harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
            harness.select()
            try await hold.reached.wait()
            harness.rendezvous.answer(DirectIrohFixtures.unavailable(retryAfterSeconds: 90))
            XCTAssertTrue(harness.scheduler.fire(240))
            try await eventually("the instance's wait is kept") { harness.scheduler.pending == [90] }

            hold.release()
            try await Task.sleep(nanoseconds: 150_000_000)
            XCTAssertEqual(harness.log.all, ["key"])
            XCTAssertEqual(harness.reports, [])
        }
        do {
            let harness = Harness()
            let hold = harness.clock.holdNextBackgroundRead()
            harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
            harness.select()
            try await hold.reached.wait()
            harness.notifications.post(name: UIApplication.didEnterBackgroundNotification, object: nil)

            hold.release()
            try await Task.sleep(nanoseconds: 150_000_000)
            XCTAssertEqual(harness.log.all, ["key"])
            XCTAssertEqual(harness.scheduler.pending, [])
        }
    }

    func testARenewalWhileAProbeIsUnderWayReplacesTheProbeAndKeepsThePath() async throws {
        // A probe for a failed candidate.
        do {
            let harness = Harness()
            // What each stream reads first, in the order the streams open:
            // the dial's probe, a relay, and the replacement probe. The
            // first probe's stream opens last, and late.
            let connection = try await harness.bringUp(
                generation: "boot:1",
                answers: [
                    DirectIrohFakeConnection.established,
                    DirectIrohFakeConnection.established,
                    DirectIrohFakeConnection.established,
                ]
            )
            try await eventually("the dial's probe is abandoned") {
                harness.log.count(of: "reset") == 1
            }
            let relay = try await harness.transport.openStream(for: harness.tag("boot:1", path: 2))
            let gate = connection.holdNextOpen()
            harness.transport.pathDidFail(for: harness.tag("boot:1", path: 2))
            try await eventually("the probe's open reaches the binding") {
                harness.log.count(of: "open") == 3
            }

            // Inside the first lease, an answer of the same generation is
            // accepted. The probe under way is retired with the lease it
            // ran under, and the same question is asked under the new one.
            harness.clock.now += 100
            harness.rendezvous.answer(
                DirectIrohFixtures.ready(generation: "boot:1", client: harness.engine.key.endpointID)
            )
            XCTAssertTrue(harness.scheduler.fire(240))
            try await eventually("the replacement probe is answered") {
                harness.log.count(of: "open") == 4 && harness.scheduler.pending.contains(1)
            }

            // The clock passes the first lease's deadline, inside the
            // second. The first probe's stream opens at last: it is
            // abandoned, nothing more of that probe starts, and its end is
            // no verdict on the path.
            harness.clock.now += 250
            gate.resolve(())
            try await eventually("the late stream is abandoned") {
                harness.log.count(of: "reset") == 3
            }
            try await Task.sleep(nanoseconds: 100_000_000)
            XCTAssertEqual(harness.log.count(of: "write"), 2)
            XCTAssertEqual(harness.reports, [.usable(generation: "boot:1")])
            XCTAssertEqual(harness.log.count(of: "connection.close"), 0)
            try await relay.write(Data("x".utf8))
            _ = try await harness.transport.openStream(for: harness.tag("boot:1", path: 2))
        }
        // A probe after a network change.
        do {
            let harness = Harness()
            // The dial's probe, then the replacement probe.
            let connection = try await harness.bringUp(
                generation: "boot:1",
                answers: [
                    DirectIrohFakeConnection.established,
                    DirectIrohFakeConnection.established,
                ]
            )
            try await eventually("the dial's probe is abandoned") {
                harness.log.count(of: "reset") == 1
            }
            let gate = connection.holdNextOpen()
            harness.network.change()
            XCTAssertTrue(harness.scheduler.fire(0.5))
            try await eventually("the verification reaches the binding") {
                harness.log.count(of: "open") == 2
            }

            harness.clock.now += 100
            harness.rendezvous.answer(
                DirectIrohFixtures.ready(generation: "boot:1", client: harness.engine.key.endpointID)
            )
            XCTAssertTrue(harness.scheduler.fire(240))
            try await eventually("the replacement verification passes") {
                harness.reports == [
                    .usable(generation: "boot:1"),
                    .verifying,
                    .usable(generation: "boot:1"),
                ]
            }

            harness.clock.now += 250
            gate.resolve(())
            try await eventually("the late stream is abandoned") {
                harness.log.count(of: "reset") == 3
            }
            try await Task.sleep(nanoseconds: 100_000_000)
            XCTAssertEqual(harness.reports.count, 3)
            XCTAssertEqual(harness.log.count(of: "write"), 2)
            XCTAssertEqual(harness.log.count(of: "connection.close"), 0)
            XCTAssertEqual(connection.streams.count, 3)
        }
    }

    func testNoStageOfADialStartsAfterItWasWithdrawn() async throws {
        // The instance withdraws its endpoint while the bind is in the
        // binding. The bind ends well afterwards: nothing follows it.
        do {
            let harness = Harness()
            let gate = harness.engine.holdBinds()
            harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
            harness.select()
            try await eventually("the bind reaches the binding") { harness.log.count(of: "bind") == 1 }

            harness.rendezvous.answer(DirectIrohFixtures.unavailable(retryAfterSeconds: 90))
            XCTAssertTrue(harness.scheduler.fire(240))
            try await eventually("the instance's wait is kept") { harness.scheduler.pending == [90] }
            gate.resolve(())
            try await eventually("the late endpoint is closed") {
                harness.log.count(of: "endpoint.close") == 1
            }
            try await Task.sleep(nanoseconds: 50_000_000)
            XCTAssertEqual(harness.log.count(of: "connect"), 0)
            XCTAssertEqual(harness.reports, [])
            XCTAssertEqual(harness.scheduler.pending, [90])
        }
        // The selection ends while the bind is in the binding.
        do {
            let harness = Harness()
            let gate = harness.engine.holdBinds()
            harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
            harness.select()
            try await eventually("the bind reaches the binding") { harness.log.count(of: "bind") == 1 }

            harness.transport.deselect()
            gate.resolve(())
            try await eventually("the late endpoint is closed") {
                harness.log.count(of: "endpoint.close") == 1
            }
            try await Task.sleep(nanoseconds: 50_000_000)
            XCTAssertEqual(harness.log.count(of: "connect"), 0)
            XCTAssertEqual(harness.scheduler.pending, [])
        }
    }

    func testAProbeAndAnOpenThatWereDueStartNothingOnceTheLeaseRanOut() async throws {
        // A probe that was set going, and has not reached the binding when
        // the clock passes the deadline.
        do {
            let harness = Harness()
            let connection = try await harness.bringUp(generation: "boot:1")
            harness.network.change()
            // The timer starts the probe's task; the clock moves before
            // that task runs.
            XCTAssertTrue(harness.scheduler.fire(0.5))
            harness.clock.now += 300
            try await Task.sleep(nanoseconds: 100_000_000)
            XCTAssertEqual(connection.streams.count, 1)
            XCTAssertEqual(harness.log.count(of: "open"), 1)
            XCTAssertEqual(harness.reports, [.usable(generation: "boot:1"), .verifying])
            XCTAssertEqual(harness.log.count(of: "connection.close"), 0)
        }
        // A stream that was admitted on the main actor, and whose open has
        // not reached the binding when the clock passes the deadline: the
        // admission reads the clock once, and the clock moves right after.
        do {
            let harness = Harness()
            let connection = try await harness.bringUp(generation: "boot:1")
            let relay = try await harness.transport.openStream(for: harness.tag("boot:1"))
            XCTAssertEqual(connection.streams.count, 2)

            harness.clock.advance(by: 300, afterReads: 1)
            await assertStale { _ = try await harness.transport.openStream(for: harness.tag("boot:1")) }
            XCTAssertEqual(harness.clock.now, 1_300)
            XCTAssertEqual(connection.streams.count, 2)
            XCTAssertEqual(harness.log.count(of: "open"), 2)
            // The relay that was already there is untouched.
            try await relay.write(Data("x".utf8))
            XCTAssertEqual(harness.log.count(of: "connection.close"), 0)
        }
    }

    func testAProbeThatEndsAfterTheLeaseRanOutReportsNothing() async throws {
        let harness = Harness()
        // The dial's probe, then the verification's.
        let connection = try await harness.bringUp(
            generation: "boot:1",
            answers: [
                DirectIrohFakeConnection.established,
                DirectIrohFakeConnection.established,
            ]
        )
        try await eventually("the dial's probe is abandoned") {
            harness.log.count(of: "reset") == 1
        }
        let gate = connection.holdNextOpen()
        harness.network.change()
        XCTAssertTrue(harness.scheduler.fire(0.5))
        try await eventually("the verification reaches the binding") {
            harness.log.count(of: "open") == 2
        }

        // The clock passes the deadline while the probe is under way, and
        // the probe then passes.
        harness.clock.now += 300
        gate.resolve(())
        try await eventually("the probe has ended") { harness.log.count(of: "reset") == 2 }
        try await Task.sleep(nanoseconds: 100_000_000)
        // Its stream opened late, so no CONNECT was written on it: the
        // only one is the dial's.
        XCTAssertEqual(harness.log.count(of: "write"), 1)
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1"), .verifying])
        await assertStale { _ = try await harness.transport.openStream(for: harness.tag("boot:1", path: 9)) }
        XCTAssertEqual(harness.log.count(of: "connection.close"), 0)
    }

    func testAStreamThatOpensAfterTheLeaseRanOutIsAbandoned() async throws {
        let harness = Harness()
        let connection = try await harness.bringUp(generation: "boot:1")
        try await eventually("the probe's stream is abandoned") {
            harness.log.count(of: "reset") == 1
        }
        let gate = connection.holdNextOpen()
        let opening = Task<DirectIrohByteStream, Error> {
            try await harness.transport.openStream(for: harness.tag("boot:1"))
        }
        try await eventually("the open reaches the binding") { harness.log.count(of: "open") == 2 }

        harness.clock.now += 300
        gate.resolve(())
        let outcome = await directIrohOutcome(of: opening)
        XCTAssertEqual(Self.failure(of: outcome) as? DirectIrohError, .stale)
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1"), .verifying])
        try await eventually("the late stream is abandoned") {
            harness.log.count(of: "reset") == 2
        }
    }

    func testAnAnswerAfterTheClockPassedTheLeaseRenewsOnlyThroughAProbe() async throws {
        // A fresh answer of the same generation: its timer never fired.
        let harness = Harness()
        let connection = try await harness.bringUp(
            generation: "boot:1",
            answers: [
                DirectIrohFakeConnection.established,
                DirectIrohFakeConnection.established,
            ]
        )
        harness.clock.now += 300
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(generation: "boot:1", client: harness.engine.key.endpointID)
        )
        XCTAssertTrue(harness.scheduler.fire(240))
        try await eventually("the path is probed and usable again") {
            harness.reports == [
                .usable(generation: "boot:1"),
                .verifying,
                .usable(generation: "boot:1"),
            ]
        }
        XCTAssertEqual(connection.streams.count, 2)
        XCTAssertEqual(harness.log.count(of: "bind"), 1)
        XCTAssertEqual(harness.rendezvous.requests.count, 2)

        // A refresh that fails after the clock passed the lease.
        let failing = Harness()
        _ = try await failing.bringUp(generation: "boot:1")
        failing.clock.now += 300
        failing.rendezvous.fail(DirectRendezvousFetchError.failed)
        XCTAssertTrue(failing.scheduler.fire(240))
        try await eventually("the refresh is tried again") {
            failing.rendezvous.requests.count == 2 && failing.scheduler.pending.contains(15)
        }
        XCTAssertEqual(failing.reports, [.usable(generation: "boot:1"), .verifying])
        XCTAssertEqual(failing.log.count(of: "open"), 1)
    }

    func testAnAnswerAcceptedDuringADialReplacesItAndTheOldDialIsClosed() async throws {
        let harness = Harness()
        let first = harness.engine.prepare()
        harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
        harness.select()
        try await eventually("the instance is dialled") { first.dials.count == 1 }

        // The refresh comes round while the first dial is still under way.
        let second = harness.engine.prepare()
        second.connection.resolve(
            harness.makeConnection(answers: [DirectIrohFakeConnection.established])
        )
        harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
        XCTAssertTrue(harness.scheduler.fire(240))
        try await eventually("the new answer's path comes up") {
            harness.reports == [.usable(generation: "boot:1")]
        }
        try await eventually("the first dial's endpoint is closed") {
            harness.log.count(of: "endpoint.close") == 1
        }

        // What the first dial still produces is closed, not used.
        first.connection.resolve(
            harness.makeConnection(answers: [DirectIrohFakeConnection.established])
        )
        try await eventually("the first dial's connection is closed") {
            harness.log.count(of: "connection.close") == 1
        }
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1")])
        XCTAssertEqual(harness.log.count(of: "bind"), 2)
        // The first dial never borrowed the second answer: it opened no
        // probe of its own. The one open is the second dial's.
        XCTAssertEqual(harness.log.count(of: "open"), 1)
        XCTAssertEqual(harness.scheduler.pending.sorted(), [30, 240, 300])
        _ = try await harness.transport.openStream(for: harness.tag("boot:1"))
    }

    func testAnUnavailableAnswerDuringADialEndsTheDial() async throws {
        let harness = Harness()
        let endpoint = harness.engine.prepare()
        harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
        harness.select()
        try await eventually("the instance is dialled") { endpoint.dials.count == 1 }

        // The refresh comes round while the dial is still under way, and
        // the instance has withdrawn its endpoint.
        harness.rendezvous.answer(DirectIrohFixtures.unavailable(retryAfterSeconds: 90))
        XCTAssertTrue(harness.scheduler.fire(240))
        try await eventually("the dial is closed and the instance's wait is kept") {
            harness.log.count(of: "endpoint.close") == 1 && harness.scheduler.pending == [90]
        }

        // What the dial still produces is closed, never reported.
        endpoint.connection.resolve(
            harness.makeConnection(answers: [DirectIrohFakeConnection.established])
        )
        try await eventually("the late connection is closed") {
            harness.log.count(of: "connection.close") == 1
        }
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(harness.reports, [])
        XCTAssertEqual(harness.scheduler.pending, [90])
    }

    func testARefreshThatFailsDuringADialLeavesTheDialAndItsLease() async throws {
        let harness = Harness()
        let endpoint = harness.engine.prepare()
        harness.rendezvous.answer(DirectIrohFixtures.ready(client: harness.engine.key.endpointID))
        harness.select()
        try await eventually("the instance is dialled") { endpoint.dials.count == 1 }

        harness.rendezvous.fail(DirectRendezvousFetchError.failed)
        XCTAssertTrue(harness.scheduler.fire(240))
        try await eventually("the refresh is tried again soon") {
            harness.rendezvous.requests.count == 2 && harness.scheduler.pending.sorted() == [15, 300]
        }
        // The same for an answer that is not accepted: one for another
        // instance.
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(client: harness.engine.key.endpointID, instanceID: "inst_2")
        )
        XCTAssertTrue(harness.scheduler.fire(15))
        try await eventually("the refresh is tried again soon") {
            harness.rendezvous.requests.count == 3 && harness.scheduler.pending.sorted() == [15, 300]
        }
        XCTAssertEqual(harness.log.count(of: "endpoint.close"), 0)
        XCTAssertEqual(harness.log.count(of: "bind"), 1)

        endpoint.connection.resolve(
            harness.makeConnection(answers: [DirectIrohFakeConnection.established])
        )
        try await eventually("the dial's path comes up") {
            harness.reports == [.usable(generation: "boot:1")]
        }
        XCTAssertEqual(harness.scheduler.pending.sorted(), [15, 30, 300])
    }

    // MARK: Streams

    func testAStreamOpensOnlyUnderCurrentTags() async throws {
        let harness = Harness()
        let connection = try await harness.bringUp(generation: "boot:1")

        let stream = try await harness.transport.openStream(for: harness.tag("boot:1", path: 3))
        XCTAssertEqual(connection.streams.count, 2)
        try await stream.write(Data("abc".utf8))
        XCTAssertEqual(connection.streams[1].send.written, Data("abc".utf8))

        // Another selection, another generation.
        await assertStale {
            _ = try await harness.transport.openStream(
                for: DirectIrohStreamTag(
                    selection: DirectIrohFixtures.selection(2),
                    rendezvousGeneration: "boot:1",
                    pathGeneration: 3
                )
            )
        }
        await assertStale { _ = try await harness.transport.openStream(for: harness.tag("boot:0", path: 3)) }
        XCTAssertEqual(connection.streams.count, 2)

        // After the selection ends, nothing opens.
        harness.transport.deselect()
        await assertStale { _ = try await harness.transport.openStream(for: harness.tag("boot:1", path: 3)) }
        await assertCancelled { try await stream.write(Data("x".utf8)) }
        XCTAssertEqual(connection.streams.count, 2)
    }

    func testAStreamThatOpensAfterItsPathEndedIsAbandonedNotReturned() async throws {
        let harness = Harness()
        let connection = try await harness.bringUp(generation: "boot:1")
        let gate = connection.holdNextOpen()
        // The probe's own stream is abandoned first.
        try await eventually("the probe's stream is abandoned") {
            harness.log.count(of: "reset") == 1
        }
        let resets = 1

        let opening = Task<DirectIrohByteStream, Error> {
            try await harness.transport.openStream(for: harness.tag("boot:1"))
        }
        try await eventually("the open reaches the binding") { harness.log.count(of: "open") == 2 }

        harness.transport.deselect()
        gate.resolve(())
        let outcome = await directIrohOutcome(of: opening)
        XCTAssertEqual(Self.failure(of: outcome) as? DirectIrohError, .stale)
        try await eventually("the late stream is abandoned") {
            harness.log.count(of: "reset") == resets + 1
        }
    }

    func testAStreamThatOpensAcrossANetworkChangeOfItsPathIsAbandoned() async throws {
        let harness = Harness()
        // What each stream reads first, in the order the streams open: the
        // dial's probe, a relay, and the verification's probe. The held
        // stream opens last.
        let connection = try await harness.bringUp(
            generation: "boot:1",
            answers: [
                DirectIrohFakeConnection.established,
                DirectIrohFakeConnection.established,
                DirectIrohFakeConnection.established,
            ]
        )
        try await eventually("the probe's stream is abandoned") {
            harness.log.count(of: "reset") == 1
        }
        let relay = try await harness.transport.openStream(for: harness.tag("boot:1", path: 6))
        let gate = connection.holdNextOpen()
        let opening = Task<DirectIrohByteStream, Error> {
            try await harness.transport.openStream(for: harness.tag("boot:1", path: 7))
        }
        try await eventually("the open reaches the binding") { harness.log.count(of: "open") == 3 }

        // The network changes and the same path verifies again, all while
        // the binding is still opening the stream.
        harness.network.change()
        XCTAssertTrue(harness.scheduler.fire(0.5))
        try await eventually("the path is usable again") {
            harness.reports == [
                .usable(generation: "boot:1"),
                .verifying,
                .usable(generation: "boot:1"),
            ]
        }

        // The stream was asked under a tag from before the change: it is
        // abandoned although its path is usable again.
        gate.resolve(())
        let outcome = await directIrohOutcome(of: opening)
        XCTAssertEqual(Self.failure(of: outcome) as? DirectIrohError, .stale)
        try await eventually("the late stream is abandoned") {
            harness.log.count(of: "reset") == 3
        }
        XCTAssertEqual(connection.streams.count, 4)
        // The relay that was already there is untouched, and a tag from
        // after the change opens.
        try await relay.write(Data("x".utf8))
        XCTAssertEqual(harness.log.count(of: "connection.close"), 0)
        _ = try await harness.transport.openStream(for: harness.tag("boot:1", path: 8))
    }

    func testAStreamThatOpensAcrossTheRenewalOfItsAnswerIsAbandoned() async throws {
        let harness = Harness()
        // The dial's probe, a relay, and the probe after the renewal.
        let connection = try await harness.bringUp(
            generation: "boot:1",
            answers: [
                DirectIrohFakeConnection.established,
                DirectIrohFakeConnection.established,
                DirectIrohFakeConnection.established,
            ]
        )
        try await eventually("the probe's stream is abandoned") {
            harness.log.count(of: "reset") == 1
        }
        let relay = try await harness.transport.openStream(for: harness.tag("boot:1", path: 6))
        let gate = connection.holdNextOpen()
        let opening = Task<DirectIrohByteStream, Error> {
            try await harness.transport.openStream(for: harness.tag("boot:1", path: 7))
        }
        try await eventually("the open reaches the binding") { harness.log.count(of: "open") == 3 }

        // The answer runs out and is renewed on the same connection, all
        // while the binding is still opening the stream.
        harness.clock.now += 300
        XCTAssertTrue(harness.scheduler.fire(300))
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1"), .verifying])
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(generation: "boot:1", client: harness.engine.key.endpointID)
        )
        XCTAssertTrue(harness.scheduler.fire(240))
        try await eventually("the path is usable again") {
            harness.reports == [
                .usable(generation: "boot:1"),
                .verifying,
                .usable(generation: "boot:1"),
            ]
        }
        XCTAssertEqual(harness.log.count(of: "bind"), 1)

        gate.resolve(())
        let outcome = await directIrohOutcome(of: opening)
        XCTAssertEqual(Self.failure(of: outcome) as? DirectIrohError, .stale)
        try await eventually("the late stream is abandoned") {
            harness.log.count(of: "reset") == 3
        }
        XCTAssertEqual(connection.streams.count, 4)
        try await relay.write(Data("x".utf8))
        XCTAssertEqual(harness.log.count(of: "connection.close"), 0)
        _ = try await harness.transport.openStream(for: harness.tag("boot:1", path: 8))
    }

    func testACancelledOpenEndsAtOnceAndItsLateStreamIsAbandoned() async throws {
        let harness = Harness()
        let connection = try await harness.bringUp(generation: "boot:1")
        let gate = connection.holdNextOpen()
        // The probe's own stream is abandoned first.
        try await eventually("the probe's stream is abandoned") {
            harness.log.count(of: "reset") == 1
        }
        let resets = 1

        let opening = Task<DirectIrohByteStream, Error> {
            try await harness.transport.openStream(for: harness.tag("boot:1"))
        }
        try await eventually("the open reaches the binding") { harness.log.count(of: "open") == 2 }

        // The binding grants no stream; the caller's bound passes.
        opening.cancel()
        let outcome = await directIrohOutcome(of: opening)
        XCTAssertTrue(Self.failure(of: outcome) is CancellationError)

        gate.resolve(())
        try await eventually("the late stream is abandoned") {
            harness.log.count(of: "reset") == resets + 1
        }
        // The path is untouched.
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1")])
        XCTAssertEqual(harness.log.count(of: "connection.close"), 0)
    }

    func testPathGenerationsAreComparedWithinOneSelectionOnly() async throws {
        let harness = Harness()
        // The dial's probe, a relay, and the verification's probe.
        _ = try await harness.bringUp(
            generation: "boot:1",
            answers: [
                DirectIrohFakeConnection.established,
                DirectIrohFakeConnection.established,
                DirectIrohFakeConnection.established,
            ]
        )
        _ = try await harness.transport.openStream(for: harness.tag("boot:1", path: 50))
        harness.network.change()
        XCTAssertTrue(harness.scheduler.fire(0.5))
        try await eventually("the path is usable again") { harness.reports.count == 3 }
        // Within the selection, a tag from before the change is stale.
        await assertStale { _ = try await harness.transport.openStream(for: harness.tag("boot:1", path: 50)) }
        let old = harness.tag("boot:9", path: 99)

        // Another runtime opens. Nothing is assumed about how its tags are
        // numbered: a low number opens, and a tag of the selection before
        // does not, whatever its number.
        let endpoint = harness.engine.prepare()
        endpoint.connection.resolve(
            harness.makeConnection(answers: [DirectIrohFakeConnection.established])
        )
        harness.rendezvous.answer(
            DirectIrohFixtures.ready(generation: "boot:9", client: harness.engine.key.endpointID)
        )
        harness.select(DirectIrohFixtures.selection(2))
        try await eventually("the new selection's path comes up") {
            harness.reports.last == .usable(generation: "boot:9") && harness.reports.count == 4
        }
        _ = try await harness.transport.openStream(for: harness.tag("boot:9", path: 0))
        await assertStale { _ = try await harness.transport.openStream(for: old) }
    }

    // MARK: Losses are reported first

    /// Every loss of a live path is reported before any of its streams or
    /// its connection is closed: whoever hears the report closes and counts
    /// the relays the path carried in that turn, and a stream closed first
    /// would look to it as one that ended on its own.
    func testALossIsReportedBeforeItsStreamsAndItsConnectionAreClosed() async throws {
        enum Loss: CaseIterable {
            case background
            case unavailable
            case replacement
            case connectionClosed
        }
        for loss in Loss.allCases {
            let harness = Harness()
            let connection = try await harness.bringUp(generation: "boot:1")
            let stream = try await harness.transport.openStream(for: harness.tag("boot:1"))
            switch loss {
            case .background:
                harness.notifications.post(
                    name: UIApplication.didEnterBackgroundNotification,
                    object: nil
                )
            case .unavailable:
                harness.rendezvous.answer(DirectIrohFixtures.unavailable(retryAfterSeconds: 60))
                XCTAssertTrue(harness.scheduler.fire(240), "\(loss)")
            case .replacement:
                harness.engine.prepare().connection.resolve(
                    harness.makeConnection(answers: [DirectIrohFakeConnection.established])
                )
                harness.rendezvous.answer(
                    DirectIrohFixtures.ready(generation: "boot:2", client: harness.engine.key.endpointID)
                )
                XCTAssertTrue(harness.scheduler.fire(240), "\(loss)")
            case .connectionClosed:
                connection.drop()
            }
            try await eventually("the loss is reported: \(loss)") {
                harness.reports.count >= 2
            }
            // The background says so: the App handles it as its own.
            XCTAssertEqual(
                harness.reports[1],
                loss == .background ? .lostToBackground : .lost,
                "\(loss)"
            )
            // Nothing of the path was closed when the loss was reported.
            XCTAssertEqual(
                harness.logAtReports[1].filter { $0 == "connection.close" },
                [],
                "\(loss)"
            )
            // Then all of it was.
            XCTAssertEqual(harness.log.count(of: "connection.close"), 1, "\(loss)")
            await assertCancelled { try await stream.write(Data("x".utf8)) }
        }
    }

    /// Whatever the report of a loss leads to, in its own turn, owns what
    /// it started: the transport closes the lost path and does nothing
    /// more. A return to the foreground keeps its watch and its request,
    /// another selection keeps its own, and an answer for the old selection
    /// schedules nothing.
    func testWhatALossReportLeadsToIsNotUndoneByTheLoss() async throws {
        enum Reentry: CaseIterable {
            case foregroundFromBackground
            case foregroundAndAnotherSelectionFromBackground
            case anotherSelectionFromAnUnavailableAnswer
        }
        for reentry in Reentry.allCases {
            let harness = Harness()
            _ = try await harness.bringUp(generation: "boot:1")
            let asked = harness.rendezvous.requests.count
            harness.onReport = { [unowned harness] report in
                guard report == .lost || report == .lostToBackground else {
                    return
                }
                harness.onReport = nil
                switch reentry {
                case .foregroundFromBackground:
                    harness.notifications.post(
                        name: UIApplication.willEnterForegroundNotification,
                        object: nil
                    )
                case .foregroundAndAnotherSelectionFromBackground:
                    harness.notifications.post(
                        name: UIApplication.willEnterForegroundNotification,
                        object: nil
                    )
                    harness.select(DirectIrohFixtures.selection(2))
                case .anotherSelectionFromAnUnavailableAnswer:
                    harness.select(DirectIrohFixtures.selection(2))
                }
            }
            switch reentry {
            case .foregroundFromBackground, .foregroundAndAnotherSelectionFromBackground:
                harness.notifications.post(
                    name: UIApplication.didEnterBackgroundNotification,
                    object: nil
                )
            case .anotherSelectionFromAnUnavailableAnswer:
                harness.rendezvous.answer(DirectIrohFixtures.unavailable(retryAfterSeconds: 60))
                XCTAssertTrue(harness.scheduler.fire(240), "\(reentry)")
            }
            try await eventually("the request after the report is made: \(reentry)") {
                harness.rendezvous.requests.count == asked + (reentry == .anotherSelectionFromAnUnavailableAnswer ? 2 : 1)
            }
            // The lost path is closed, once.
            XCTAssertEqual(harness.log.count(of: "connection.close"), 1, "\(reentry)")
            // The work the report started is still there.
            XCTAssertTrue(harness.network.isWatching, "\(reentry)")
            XCTAssertEqual(
                harness.rendezvous.requests.last?.selection,
                reentry == .foregroundFromBackground
                    ? DirectIrohFixtures.selection(1)
                    : DirectIrohFixtures.selection(2),
                "\(reentry)"
            )
            XCTAssertEqual(harness.rendezvous.held.count, 1, "\(reentry)")
            // The old answer's "ask again in 60 seconds" was not scheduled.
            XCTAssertFalse(harness.scheduler.pending.contains(60), "\(reentry)")
        }
    }

    // MARK: The background

    func testNothingLivesInTheBackground() async throws {
        let harness = Harness()
        _ = try await harness.bringUp(generation: "boot:1")
        let stream = try await harness.transport.openStream(for: harness.tag("boot:1"))

        harness.notifications.post(name: UIApplication.didEnterBackgroundNotification, object: nil)
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1"), .lostToBackground])
        XCTAssertEqual(harness.log.count(of: "connection.close"), 1)
        try await eventually("the endpoint is closed") {
            harness.log.count(of: "endpoint.close") == 1
        }
        XCTAssertFalse(harness.network.isWatching)
        XCTAssertEqual(harness.scheduler.pending, [])
        await assertCancelled { try await stream.write(Data("x".utf8)) }

        // A selection made in the background waits for the foreground.
        let asked = harness.rendezvous.requests.count
        harness.select(DirectIrohFixtures.selection(2))
        harness.network.change()
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(harness.rendezvous.requests.count, asked)
        XCTAssertEqual(harness.scheduler.pending, [])

        harness.rendezvous.answer(DirectIrohFixtures.unavailable(retryAfterSeconds: 60))
        harness.notifications.post(name: UIApplication.willEnterForegroundNotification, object: nil)
        try await eventually("the rendezvous is asked on the return") {
            harness.rendezvous.requests.count == asked + 1 && harness.scheduler.pending == [60]
        }
        XCTAssertEqual(harness.rendezvous.requests.last?.selection, DirectIrohFixtures.selection(2))
        XCTAssertTrue(harness.network.isWatching)
        XCTAssertEqual(harness.log.count(of: "bind"), 1)
    }

    func testATransportCreatedInTheBackgroundDoesNothingUntilTheForeground() async throws {
        let harness = Harness(isInBackground: true)
        harness.select()
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(harness.rendezvous.requests, [])
        XCTAssertEqual(harness.log.all, [])
        XCTAssertFalse(harness.network.isWatching)

        harness.rendezvous.answer(DirectIrohFixtures.unavailable(retryAfterSeconds: 60))
        harness.notifications.post(name: UIApplication.willEnterForegroundNotification, object: nil)
        try await eventually("the rendezvous is asked") { harness.scheduler.pending == [60] }
        XCTAssertEqual(harness.log.all, ["key"])
    }

    // MARK: Helpers

    @MainActor
    private final class Harness {
        /// Bounds no test should reach: a slow simulator must not turn a
        /// stand-in that answers at once into a timeout.
        static let patientTiming: DirectIrohTiming = {
            var timing = DirectIrohTiming()
            timing.fetchSeconds = 120
            timing.bindSeconds = 120
            timing.dialSeconds = 120
            timing.relayDialSeconds = 120
            timing.probeSeconds = 120
            return timing
        }()

        let log = DirectIrohEventLog()
        let engine: DirectIrohFakeEngine
        let scheduler = DirectIrohManualScheduler()
        let network = DirectIrohFakeNetwork()
        let rendezvous = DirectIrohFakeRendezvous()
        let notifications = NotificationCenter()
        let clock = DirectIrohManualClock()
        let transport: DirectIrohTransport
        private(set) var reports: [DirectIrohReport] = []
        /// What the stand-ins had been asked to do when each report came.
        private(set) var logAtReports: [[String]] = []
        /// What the one hearing the reports does, in the report's own turn.
        var onReport: ((DirectIrohReport) -> Void)?
        private(set) var selection = DirectIrohFixtures.selection(1)

        init(timing: DirectIrohTiming = Harness.patientTiming, isInBackground: Bool = false) {
            let engine = DirectIrohFakeEngine(log: log)
            let rendezvous = rendezvous
            let clock = clock
            self.engine = engine
            transport = DirectIrohTransport(
                engine: engine,
                fetchRendezvous: { selection, clientEndpointID in
                    try await rendezvous.fetch(selection, clientEndpointID)
                },
                network: network,
                scheduler: scheduler,
                timing: timing,
                notificationCenter: notifications,
                isInBackground: isInBackground,
                uptime: { clock.read() }
            )
        }

        func select(_ selection: DirectIrohSelection = DirectIrohFixtures.selection(1)) {
            self.selection = selection
            transport.select(selection) { [weak self] report in
                guard let self else {
                    return
                }
                self.reports.append(report)
                self.logAtReports.append(self.log.all)
                self.onReport?(report)
            }
        }

        func tag(_ generation: String, path: Int = 1) -> DirectIrohStreamTag {
            DirectIrohStreamTag(
                selection: selection,
                rendezvousGeneration: generation,
                pathGeneration: path
            )
        }

        /// A connection to the instance the fixtures name.
        func makeConnection(answers: [Data]) -> DirectIrohFakeConnection {
            DirectIrohFakeConnection(
                log: log,
                remote: DirectIrohFixtures.instanceEndpoint,
                answers: answers
            )
        }

        /// Brings a path up: selects (or fires the timer of `firing`
        /// seconds that asks again), answers the rendezvous, and lets the
        /// dial and the probe succeed.
        func bringUp(
            generation: String,
            answers: [Data] = [DirectIrohFakeConnection.established],
            addresses: [[String: String]]? = nil,
            relay: [String: String]? = nil,
            firing: TimeInterval? = nil,
            file: StaticString = #filePath,
            line: UInt = #line
        ) async throws -> DirectIrohFakeConnection {
            let endpoint = engine.prepare()
            let connection = makeConnection(answers: answers)
            endpoint.connection.resolve(connection)
            rendezvous.answer(
                addresses.map {
                    DirectIrohFixtures.ready(
                        generation: generation, client: engine.key.endpointID, addresses: $0, relay: relay
                    )
                } ?? DirectIrohFixtures.ready(generation: generation, client: engine.key.endpointID, relay: relay)
            )
            let reported = reports.count
            if let firing {
                XCTAssertTrue(scheduler.fire(firing), file: file, line: line)
            } else {
                select()
            }
            try await directIrohEventually("the path comes up", file: file, line: line) {
                self.reports.count == reported + 1
            }
            XCTAssertEqual(reports.last, .usable(generation: generation), file: file, line: line)
            return connection
        }
    }

    private static func failure<Value>(of outcome: Result<Value, Error>?) -> Error? {
        guard case .failure(let error)? = outcome else {
            return nil
        }
        return error
    }

    private func assertStale(
        file: StaticString = #filePath,
        line: UInt = #line,
        _ operation: () async throws -> Void
    ) async {
        await assertThrows(.stale, file: file, line: line, operation)
    }

    private func assertCancelled(
        file: StaticString = #filePath,
        line: UInt = #line,
        _ operation: () async throws -> Void
    ) async {
        await assertThrows(.cancelled, file: file, line: line, operation)
    }

    private func assertThrows(
        _ expected: DirectIrohError,
        file: StaticString,
        line: UInt,
        _ operation: () async throws -> Void
    ) async {
        do {
            try await operation()
            XCTFail("Nothing was thrown.", file: file, line: line)
        } catch {
            XCTAssertEqual(error as? DirectIrohError, expected, file: file, line: line)
        }
    }

    private func eventually(
        _ description: String,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ condition: @MainActor () -> Bool
    ) async throws {
        try await directIrohEventually(description, file: file, line: line, condition)
    }
}
