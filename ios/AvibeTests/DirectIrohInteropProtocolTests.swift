import Foundation
import IrohLib
import XCTest
@testable import Avibe

/// Contracts owned by the App consumer, not native interop evidence. These
/// catch control corruption/false negative credit that the Python host tests
/// cannot exercise in the Swift implementation.
final class DirectIrohInteropProtocolTests: XCTestCase {
    func testSetupBarrierRequiresExactCaseSchemaAndEnvelope() throws {
        let good = try InteropWire.decode(Data("{\"type\":\"setup_done\",\"schema\":2,\"name\":\"refused_connect\"}\n".utf8))
        XCTAssertNoThrow(try InteropWire.expect(good, type: "setup_done", name: "refused_connect"))
        XCTAssertThrowsError(try InteropWire.expect(good, type: "setup_done", name: "admitted"))
        XCTAssertThrowsError(try InteropWire.expect(InteropWire.message("done"), type: "setup_done", name: "refused_connect"))
        var extra = good
        extra["streams"] = .integer(1)
        XCTAssertThrowsError(try InteropWire.expect(extra, type: "setup_done", name: "refused_connect"))
        XCTAssertThrowsError(try InteropWire.decode(Data("{\"type\":\"setup_done\",\"schema\":1,\"name\":\"refused_connect\"}\n".utf8)))
    }

    func testControlRejectsDuplicateEscapedNestedKeysTruncationAndOversize() throws {
        let invalid = [
            "{\"type\":\"done\",\"schema\":2,\"schema\":2}\n",
            "{\"type\":\"done\",\"schema\":2,\"x\":{\"name\":1,\"na\\u006de\":2}}\n",
            "{\"type\":\"done\",\"schema\":2}",
            "{\"type\":\"invented\",\"schema\":2}\n",
            "{\"type\":\"done\",\"schema\":2,\"x\":NaN}\n",
            "{\"type\":\"done\",\"schema\":2}{}\n",
        ]
        for line in invalid { XCTAssertThrowsError(try InteropWire.decode(Data(line.utf8)), line) }
        XCTAssertThrowsError(try InteropWire.decode(Data((String(repeating: " ", count: 4096) + "\n").utf8)))
        let escaped = try InteropWire.decode(Data("{\"type\":\"done\",\"schema\":2,\"detail\":\"braces {} and \\\"quotes\\\"\"}\n".utf8))
        XCTAssertEqual(escaped["detail"], .string("braces {} and \"quotes\""))
    }

    func testResultObjectIsIdenticalInControlAndArtifact() throws {
        let record: [String: InteropJSON] = [
            "name": .string("admitted"), "outcome": .string("passed"), "detail": .null,
            "observed": .object(["control_tcp": .bool(true), "payload": .object(["sent_bytes": .integer(24)])]),
        ]
        var wire = record
        wire["schema"] = .integer(2)
        wire["type"] = .string("result")
        var bytes = try InteropJSON.object(wire).encoded()
        bytes.append(10)
        var decoded = try InteropWire.decode(bytes)
        decoded.removeValue(forKey: "type")
        decoded.removeValue(forKey: "schema")
        XCTAssertEqual(decoded, record)
        XCTAssertEqual(try InteropJSON.object(decoded).encoded(), try InteropJSON.object(record).encoded())
        XCTAssertEqual(InteropWire.payload, Data([97,118,105,98,101,45,100,105,114,101,99,116,45,105,110,116,101,114,111,112,45,118,50,10]))
        XCTAssertEqual(InteropWire.names, ["admitted", "not_admitted", "wrong_identity", "refused_connect", "admission_ends", "sidecar_stops"])
    }

    func testLocalNativeTimeoutOrCloseNeverCountsAsPeerRejection() {
        for text in ["Connect { source: TimedOut }", "Connecting: connection timed out", "ConnectionError::LocallyClosed",
                     "Connection was rejected locally", "no address available", "internal consistency error"] {
            XCTAssertNil(InteropEngine.rejection(kind: .connect, debug: text))
        }
        XCTAssertEqual(InteropEngine.rejection(kind: .connect, debug: "Connecting { source: HandshakeFailure }"), "handshake")
        XCTAssertEqual(InteropEngine.rejection(kind: .connection, debug: "ApplicationClosed"), "closed_by_peer")
        XCTAssertNil(InteropEngine.rejection(kind: .timeout, debug: "ConnectionClosed"))
        XCTAssertNil(InteropEngine.rejection(kind: .invalidInput, debug: "TransportError"))
    }

    @MainActor
    func testOfferKeepsStringGenerationAndValidatesActualTransportAddresses() throws {
        let client = DirectEndpointID(bytes: Data(repeating: 1, count: 32))!
        let server = DirectEndpointID(bytes: Data(repeating: 2, count: 32))!
        var fields = InteropWire.message("case", name: "admitted")
        fields.merge([
            "sidecar_endpoint_id": .string(server.hex), "addresses": .array([.string("192.168.1.3:1234")]),
            "admissible_addresses": .array([.string("192.168.1.3:1234")]), "alpn": .string("avibe-direct/1"),
            "hostname": .string("interop.invalid"), "instance_id": .string("interop-fixture"),
            "generation": .string("interop-v2-1"), "lifetime_seconds": .integer(300),
        ]) { _, new in new }
        let offer = try DirectIrohInteropTests.Offer(fields, name: "admitted", tier: "transport", client: client)
        let response = try DirectRendezvous.answer(from: offer.rendezvous(client: client),
                                                 expecting: .init(instanceID: "interop-fixture", hostname: "interop.invalid", clientEndpointID: client))
        guard case .ready(let admitted) = response else { return XCTFail("expected real-validator ready offer") }
        XCTAssertEqual(admitted.generation, "interop-v2-1")
        XCTAssertEqual(admitted.addresses.map(\.socketAddress), ["192.168.1.3:1234"])
        fields["generation"] = .integer(1)
        XCTAssertThrowsError(try DirectIrohInteropTests.Offer(fields, name: "admitted", tier: "transport", client: client))
        fields["generation"] = .string("interop-v2-1")
        for address in ["127.0.0.1:1234", "8.8.8.8:1234", "example.org:1234"] {
            fields["addresses"] = .array([.string(address)])
            fields["admissible_addresses"] = .array([.string(address)])
            XCTAssertThrowsError(try DirectIrohInteropTests.Offer(fields, name: "admitted", tier: "transport", client: client))
        }
    }

    func testOnlyFirstEmptyTransportOfferMaySkip() {
        XCTAssertNoThrow(try InteropWire.requireEmptyTransportSkip(name: "admitted", completedCases: 0))
        XCTAssertThrowsError(try InteropWire.requireEmptyTransportSkip(name: "admitted", completedCases: 1))
        for name in InteropWire.names.dropFirst() {
            XCTAssertThrowsError(try InteropWire.requireEmptyTransportSkip(name: name, completedCases: 0))
            XCTAssertThrowsError(try InteropWire.requireEmptyTransportSkip(name: name, completedCases: 1))
        }
    }

    func testControlCannotDialNonLoopbackOrImplicitPort() {
        for address in ["example.org:80", "192.168.1.1:80", "127.0.0.1", "127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1:80:90"] {
            XCTAssertThrowsError(try InteropControl(address: address))
        }
    }
}
