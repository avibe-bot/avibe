import Foundation
import IrohLib
import XCTest
@testable import Avibe

/// Compiled by normal ios-shell. Real combined execution requires the host's
/// explicit fixture; a missing fixture is a visible XCTest skip, never a pass.
@MainActor
final class DirectIrohInteropTests: XCTestCase {
    func testEngine() async throws { try await runTier("engine") }
    func testTransport() async throws { try await runTier("transport") }

    private func runTier(_ tier: String) async throws {
        let fixture = try Fixture.load()
        let control = try InteropControl(address: fixture.control)
        var records: [InteropJSON] = []
        var status = "failed"
        do {
            try await control.connect()
            var hello = InteropWire.message("hello")
            hello["tier"] = .string(tier)
            try await control.send(hello, budget: InteropBudget(seconds: 5))
            for name in InteropWire.names {
                let key = try DirectIrohBinding().makeLaunchKey()
                var next = InteropWire.message("next")
                next["client_endpoint_id"] = .string(key.endpointID.hex)
                let offerBudget = InteropBudget(seconds: 20)
                try await control.send(next, budget: offerBudget)
                let fields = try await control.receive(budget: offerBudget)
                let offer = try Offer(fields, name: name, tier: tier, client: key.endpointID)
                if tier == "transport" && offer.addresses.isEmpty {
                    try InteropWire.requireEmptyTransportSkip(name: name, completedCases: records.count)
                    let skipped = record(name, outcome: "not_covered", observed: ["control_tcp": .bool(true)])
                    try await reconcile(skipped, control: control, budget: offerBudget)
                    let done = try await control.receive(budget: offerBudget)
                    try InteropWire.expect(done, type: "done")
                    status = "not_covered"
                    break
                }
                let resources = CaseResources(key: key)
                var observed: [String: InteropJSON] = ["control_tcp": .bool(true)]
                var caseError: Error?
                do {
                    if !offer.negative {
                        let setup = try await resources.setup(offer, seconds: fixture.setupSeconds)
                        observed["setup_attempts"] = .integer(setup.attempts)
                        observed["setup_probe"] = .object(["status": .integer(200), "streams": .integer(setup.streams)])
                        let barrier = InteropBudget(seconds: fixture.setupSeconds)
                        try await control.send(InteropWire.message("setup_done", name: name), budget: barrier)
                        let ack = try await control.receive(budget: barrier)
                        try InteropWire.expect(ack, type: "setup_done", name: name)
                    }
                    // No case endpoint/dial is made before the exact setup ACK.
                    let budget = InteropBudget(seconds: fixture.caseSeconds)
                    let facts = try await resources.exercise(offer, tier: tier, control: control, budget: budget)
                    observed.merge(facts) { _, new in new }
                } catch { caseError = error }
                do { try await resources.close() } catch { caseError = caseError ?? error }
                // Never write identifiers or raw foreign error messages into evidence.
                let result = record(name, outcome: caseError == nil ? "passed" : "failed", observed: observed,
                                    detail: caseError == nil ? nil : "App assertion, deadline, or cleanup failed")
                records.append(result)
                try await reconcile(result, control: control, budget: InteropBudget(seconds: 20))
                if let caseError { throw caseError }
            }
            if status != "not_covered" {
                let done = try await control.receive(budget: InteropBudget(seconds: 10))
                try InteropWire.expect(done, type: "done")
                try interopRequire(records.count == InteropWire.names.count, "incomplete case inventory")
                status = "passed"
            }
            try fixture.save(tier: tier, status: status, records: records)
            await control.close()
        } catch {
            await control.close()
            try fixture.save(tier: tier, status: "failed", records: records)
            throw InteropFault.invalid("App interop failed; inspect the identifier-free case artifact and host verdict")
        }
    }

    private func record(_ name: String, outcome: String, observed: [String: InteropJSON], detail: String? = nil) -> InteropJSON {
        .object(["name": .string(name), "outcome": .string(outcome), "observed": .object(observed),
                 "detail": detail.map(InteropJSON.string) ?? .null])
    }

    private func reconcile(_ record: InteropJSON, control: InteropControl, budget: InteropBudget) async throws {
        guard var fields = record.fields, let name = fields["name"]?.text else { throw InteropFault.invalid("invalid result") }
        // Preserve the exact case object in the file; only the wire gets these
        // two envelope keys. The host hashes the object independently.
        fields["type"] = .string("result")
        fields["schema"] = .integer(2)
        try await control.send(fields, budget: budget)
        let verdict = try await control.receive(budget: budget)
        try InteropWire.expect(verdict, type: "verdict", name: name)
        try interopRequire(Set(verdict.keys) == ["type", "schema", "name", "host", "detail"], "unexpected verdict fields")
        try interopRequire(verdict["host"] == .string("confirmed") && verdict["detail"] == .null, "host contradicted App evidence")
    }

    private struct Fixture {
        let control: String
        let directory: URL
        let caseSeconds: Int
        let setupSeconds: Int
        static func load() throws -> Self {
            let env = ProcessInfo.processInfo.environment
            guard let path = env["AVIBE_DIRECT_INTEROP_FIXTURE"] ?? env["TEST_RUNNER_AVIBE_DIRECT_INTEROP_FIXTURE"] else {
                throw XCTSkip("No hosted interop fixture: source compiled, real App/sidecar interoperability NOT RUN")
            }
            try interopRequire(path.hasPrefix("/"), "fixture path must be absolute")
            let url = URL(fileURLWithPath: path).standardizedFileURL
            let data = try Data(contentsOf: url)
            try interopRequire(data.count <= 4096, "oversized fixture")
            let object = try JSONDecoder().decode(InteropJSON.self, from: data)
            guard let fields = object.fields, fields["schema"] == .integer(2),
                  let control = fields["control"]?.text, let directory = fields["result_dir"]?.text,
                  fields["case_deadline_seconds"] == .integer(20), fields["setup_deadline_seconds"] == .integer(10) else {
                throw InteropFault.invalid("invalid fixture")
            }
            let result = URL(fileURLWithPath: directory).standardizedFileURL
            try interopRequire(directory.hasPrefix("/") && result == url.deletingLastPathComponent(), "result directory must be fixture-owned")
            return Self(control: control, directory: result, caseSeconds: 20, setupSeconds: 10)
        }
        func save(tier: String, status: String, records: [InteropJSON]) throws {
            let doc = InteropJSON.object(["schema": .integer(2), "tier": .string(tier), "status": .string(status), "cases": .array(records)])
            try doc.encoded().write(to: directory.appendingPathComponent("\(tier).json"), options: .atomic)
        }
    }

    struct Offer: Sendable {
        let name: String
        let endpoint: DirectEndpointID
        let addresses: [String]
        var negative: Bool { name == "not_admitted" || name == "wrong_identity" }
        init(_ fields: [String: InteropJSON], name: String, tier: String, client: DirectEndpointID) throws {
            try InteropWire.expect(fields, type: "case", name: name)
            guard let id = fields["sidecar_endpoint_id"]?.text, let endpoint = DirectEndpointID(hex: id), endpoint != client,
                  let values = fields["addresses"]?.list, let admitted = fields["admissible_addresses"]?.list else {
                throw InteropFault.invalid("invalid endpoint offer")
            }
            let addresses = values.compactMap(\.text)
            try interopRequire(addresses.count == values.count && addresses.count <= 32, "invalid address list")
            try interopRequire(fields["alpn"] == .string("avibe-direct/1") && fields["hostname"] == .string("interop.invalid")
                && fields["instance_id"] == .string("interop-fixture") && fields["generation"] == .string("interop-v2-1")
                && fields["lifetime_seconds"] == .integer(300), "changed offer contract")
            if tier == "transport" {
                try interopRequire(values == admitted && addresses.allSatisfy { DirectCandidateAddress.admit(kind: "lan", address: $0) != nil }, "inadmissible transport address")
            } else {
                try interopRequire(!addresses.isEmpty, "engine requires actual addresses")
            }
            self.name = name
            self.endpoint = endpoint
            self.addresses = addresses
        }
        func rendezvous(client: DirectEndpointID) throws -> Data {
            let now = Int(Date().timeIntervalSince1970)
            return try InteropJSON.object([
                "version": .integer(1), "status": .string("ready"), "instance_id": .string("interop-fixture"),
                "hostname": .string("interop.invalid"), "generation": .string("interop-v2-1"), "alpn": .string("avibe-direct/1"),
                "issued_at": .integer(now), "expires_at": .integer(now + 300),
                "client": .object(["endpoint_id": .string(client.hex), "admitted_until": .integer(now + 300)]),
                "endpoint": .object(["id": .string(endpoint.hex), "relay": .null,
                    "addresses": .array(addresses.map { .object(["kind": .string("lan"), "address": .string($0)]) })])
            ]).encoded()
        }
    }

    @MainActor
    private final class CaseResources {
        let key: DirectIrohLaunchKey
        let engine: InteropEngine
        var transport: DirectIrohTransport?
        var stream: DirectIrohByteStream?
        var reports: [DirectIrohReport] = []
        var payloadStreams = 0
        var connectionWatch: Task<Void, Never>?
        init(key: DirectIrohLaunchKey) { self.key = key; engine = InteropEngine(key: key) }

        func setup(_ offer: Offer, seconds: Int) async throws -> (attempts: Int, streams: Int) {
            let setupEngine = InteropEngine(key: key)
            let budget = InteropBudget(seconds: seconds)
            var attempts = 0
            do {
                while true {
                    attempts += 1
                    do {
                        let endpoint = try await budget.run { try await setupEngine.bind(secretKey: setupEngine.key.secret, relayURLs: []) }
                        let connection = try await budget.run { try await endpoint.connect(to: offer.endpoint, addresses: offer.addresses, relayURL: nil, alpn: Data("avibe-direct/1".utf8)) }
                        defer { connection.close() }
                        try interopRequire(connection.remoteEndpointID == offer.endpoint, "setup identity mismatch")
                        let lease = DirectIrohLeaseDeadline(deadline: ProcessInfo.processInfo.systemUptime + budget.remaining,
                                                          clock: { ProcessInfo.processInfo.systemUptime })
                        try await DirectIrohProbe.run(on: connection, host: "interop.invalid", within: min(2, budget.remaining), lease: lease)
                        break
                    } catch {
                        try interopRequire(budget.remaining > 0.2, "setup did not establish before deadline")
                        try await Task.sleep(for: .milliseconds(100))
                    }
                }
                try await setupEngine.close()
                let snapshot = setupEngine.snapshot
                try interopRequire(snapshot.probes.count == snapshot.streams && snapshot.probes.allSatisfy { $0 == 200 }, "unconfirmed setup stream")
                try interopRequire(budget.remaining > 0, "setup cleanup exceeded deadline")
                return (attempts, snapshot.streams)
            } catch {
                try await setupEngine.close()
                throw error
            }
        }

        func exercise(_ offer: Offer, tier: String, control: InteropControl, budget: InteropBudget) async throws -> [String: InteropJSON] {
            if tier == "transport" { try startTransport(offer) }
            else {
                let engine = engine
                let endpoint = try await budget.run { try await engine.bind(secretKey: engine.key.secret, relayURLs: []) }
                do {
                    let connection = try await budget.run { try await endpoint.connect(to: offer.endpoint, addresses: offer.addresses, relayURL: nil, alpn: Data("avibe-direct/1".utf8)) }
                    try interopRequire(connection.remoteEndpointID == offer.endpoint, "case identity mismatch")
                    connectionWatch = Task { await connection.closed() }
                    // Wrong identity must never establish. Admission is checked
                    // remotely after TLS, so not_admitted may establish and then
                    // close. Observe a native peer reason without sending bytes.
                    try interopRequire(offer.name != "wrong_identity", "wrong-identity handshake succeeded")
                    if offer.name == "not_admitted" {
                        let halves = try await budget.run { try await connection.openStream() }
                        defer { DirectIrohByteStream.abort(halves) }
                        _ = try await budget.run { try await halves.receive.read(sizeLimit: 1) }
                        throw InteropFault.invalid("admission rejection lacked a native peer failure")
                    }
                    let lease = DirectIrohLeaseDeadline(deadline: ProcessInfo.processInfo.systemUptime + budget.remaining,
                                                      clock: { ProcessInfo.processInfo.systemUptime })
                    do {
                        try await DirectIrohProbe.run(on: connection, host: "interop.invalid", within: min(5, budget.remaining), lease: lease)
                    } catch {
                        if offer.name != "refused_connect" || error as? DirectIrohError != .probeRefused { throw error }
                    }
                    if offer.name != "refused_connect" {
                        let halves = try await budget.run { try await connection.openStream() }
                        stream = DirectIrohByteStream(halves)
                    }
                } catch {
                    guard offer.negative, InteropEngine.rejection(error) != nil else { throw error }
                }
            }
            if offer.negative {
                try await wait(budget) { self.engine.snapshot.peerFailure != nil }
                let snapshot = engine.snapshot
                let usable = reports.contains { if case .usable = $0 { return true }; return false }
                return try InteropWire.negativeObservation(name: offer.name, failure: snapshot.peerFailure,
                                                          localStreams: snapshot.streams, successfulConnects: snapshot.successfulConnects,
                                                          payloadStreams: payloadStreams, usable: usable)
            }
            if offer.name == "refused_connect" {
                try await wait(budget) { self.engine.snapshot.probes == [403] }
                try interopRequire(reports.isEmpty && engine.snapshot.streams == 1 && stream == nil, "refusal reached usable/payload")
                return ["failure": .string("probe_refused"), "case_probe": .object(["status": .integer(403)])]
            }
            if tier == "transport" {
                try await wait(budget) { self.reports.contains(.usable(generation: "interop-v2-1")) }
                guard let transport else { throw InteropFault.invalid("transport missing") }
                let tag = DirectIrohStreamTag(selection: Self.selection, rendezvousGeneration: "interop-v2-1", pathGeneration: 1)
                stream = try await budget.run { try await transport.openStream(for: tag) }
            }
            guard let stream else { throw InteropFault.invalid("payload stream missing") }
            try interopRequire(engine.snapshot.probes == [200], "case probe not independently observed")
            payloadStreams += 1
            try await budget.run { try await stream.write(InteropWire.payload) }
            var echoed = Data()
            while echoed.count < InteropWire.payload.count {
                guard let part = try await budget.run({ try await stream.read(maxLength: InteropWire.payload.count) }), !part.isEmpty else {
                    throw InteropFault.invalid("payload EOF before complete echo")
                }
                echoed.append(part)
            }
            try interopRequire(echoed == InteropWire.payload && engine.snapshot.streams == 2, "payload mismatch or extra streams")
            var observed: [String: InteropJSON] = [
                "dial": .string("established"), "remote_identity": .string("matched"),
                "case_probe": .object(["status": .integer(200)]),
                "payload": .object(["sent_bytes": .integer(InteropWire.payload.count), "received_bytes": .integer(echoed.count)])
            ]
            if offer.name == "admitted" {
                try await budget.run { try await stream.closeWrite() }
                let end = try await budget.run { try await stream.read(maxLength: 1) }
                try interopRequire(end == nil, "expected payload FIN")
                observed["payload"] = .object(["sent_bytes": .integer(InteropWire.payload.count), "received_bytes": .integer(echoed.count),
                                                "sent_fin": .bool(true), "received_fin": .bool(true)])
            } else {
                let terminal = InteropTerminal()
                let read = Task { () -> Result<Data?, Error> in
                    let result = await Result<Data?, Error>(catchingAsync: { try await stream.read(maxLength: 1) })
                    terminal.finish()
                    return result
                }
                defer { read.cancel() }
                try interopRequire(!terminal.finished && engine.snapshot.connectionsClosed == 0 && !reports.contains(.lost), "stream already closed before armed")
                try await control.send(InteropWire.message("armed", name: offer.name), budget: budget)
                let triggered = try await control.receive(budget: budget)
                try InteropWire.expect(triggered, type: "triggered", name: offer.name)
                let result = try await budget.run {
                    while !terminal.finished { try await Task.sleep(for: .milliseconds(10)) }
                    return await read.value
                }
                switch result {
                case .success(let bytes): try interopRequire(bytes == nil, "closure delivered extra payload")
                case .failure(let error):
                    let lostByTransport = tier == "transport" && reports.contains(.lost) && error as? DirectIrohError == .cancelled
                    try interopRequire(lostByTransport || InteropEngine.rejection(error) != nil,
                                       "closure was a local failure, not peer/transport loss")
                }
                observed["armed_alive"] = .bool(true)
                observed["closed_after_armed"] = .bool(true)
            }
            return observed
        }

        private static let selection = DirectIrohSelection(number: 1, instanceID: "interop-fixture", host: "interop.invalid",
                                                          storeIdentifier: UUID())
        private func startTransport(_ offer: Offer) throws {
            let data = try offer.rendezvous(client: key.endpointID)
            // The real validator and production state machine consume this
            // test-owned answer. No production networking or reports are faked.
            _ = try DirectRendezvous.answer(from: data, expecting: .init(instanceID: "interop-fixture", hostname: "interop.invalid", clientEndpointID: key.endpointID))
            var timing = DirectIrohTiming.standard
            timing.dialSeconds = 5
            timing.probeSeconds = 5
            timing.retryLadder = [60] // no case retry; failure remains observable
            let transport = DirectIrohTransport(engine: engine, fetchRendezvous: { _, _ in data },
                                                network: DirectIrohFakeNetwork(), timing: timing,
                                                notificationCenter: NotificationCenter(), isInBackground: false)
            self.transport = transport
            transport.select(Self.selection) { [weak self] report in self?.reports.append(report) }
        }
        private func wait(_ budget: InteropBudget, until condition: () -> Bool) async throws {
            while !condition() {
                try interopRequire(budget.remaining > 0, "case observation deadline")
                try await Task.sleep(for: .milliseconds(10))
            }
        }
        func close() async throws {
            transport?.deselect()
            stream?.cancel()
            try await engine.close()
            if let connectionWatch {
                _ = try await InteropBudget(seconds: 2).run { await connectionWatch.value }
            }
        }
    }
}

private final class InteropTerminal: @unchecked Sendable {
    private let lock = NSLock()
    private var done = false
    var finished: Bool { lock.withLock { done } }
    func finish() { lock.withLock { done = true } }
}

private extension Result where Failure == Error {
    init(catchingAsync operation: () async throws -> Success) async {
        do { self = .success(try await operation()) }
        catch { self = .failure(error) }
    }
}
