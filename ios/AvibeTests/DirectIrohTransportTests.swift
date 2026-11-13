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

        harness.network.change()
        harness.network.change()
        // Reported at once and once; probed when the network is quiet.
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1"), .verifying])
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
            XCTAssertEqual(harness.reports[1], .lost, "\(loss)")
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

    // MARK: The background

    func testNothingLivesInTheBackground() async throws {
        let harness = Harness()
        _ = try await harness.bringUp(generation: "boot:1")
        let stream = try await harness.transport.openStream(for: harness.tag("boot:1"))

        harness.notifications.post(name: UIApplication.didEnterBackgroundNotification, object: nil)
        XCTAssertEqual(harness.reports, [.usable(generation: "boot:1"), .lost])
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
            firing: TimeInterval? = nil,
            file: StaticString = #filePath,
            line: UInt = #line
        ) async throws -> DirectIrohFakeConnection {
            let endpoint = engine.prepare()
            let connection = makeConnection(answers: answers)
            endpoint.connection.resolve(connection)
            rendezvous.answer(
                DirectIrohFixtures.ready(generation: generation, client: engine.key.endpointID)
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
