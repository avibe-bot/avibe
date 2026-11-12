import XCTest
@testable import Avibe

/// What the App accepts from a rendezvous answer
/// (docs/plans/remote-direct-rendezvous.md section 5).
final class DirectRendezvousTests: XCTestCase {
    private let client = DirectEndpointID(bytes: Data(repeating: 0x11, count: 32))!
    private let instance = DirectEndpointID(bytes: Data(repeating: 0x22, count: 32))!

    private var expectation: DirectRendezvousExpectation {
        DirectRendezvousExpectation(
            instanceID: "inst_1",
            hostname: "alice-app.avibe.bot",
            clientEndpointID: client
        )
    }

    // MARK: Endpoint IDs

    func testAnEndpointIDIsExactlySixtyFourLowercaseHexCharacters() {
        let hex = String(repeating: "0a", count: 32)
        let parsed = DirectEndpointID(hex: hex)
        XCTAssertEqual(parsed?.bytes, Data(repeating: 0x0A, count: 32))
        XCTAssertEqual(parsed?.hex, hex)
        XCTAssertEqual(client.hex, String(repeating: "11", count: 32))

        XCTAssertNil(DirectEndpointID(hex: String(repeating: "0A", count: 32)))
        XCTAssertNil(DirectEndpointID(hex: String(repeating: "0a", count: 31)))
        XCTAssertNil(DirectEndpointID(hex: String(repeating: "0a", count: 33)))
        XCTAssertNil(DirectEndpointID(hex: String(repeating: "0g", count: 32)))
        XCTAssertNil(DirectEndpointID(hex: ""))
        XCTAssertNil(DirectEndpointID(bytes: Data(repeating: 1, count: 31)))
        XCTAssertNil(DirectEndpointID(bytes: Data(repeating: 1, count: 33)))
    }

    // MARK: Addresses

    func testOnlyPrivateIPv4UniqueLocalAndGlobalIPv6AddressesAreAdmitted() {
        let admitted: [(kind: String, address: String, dialled: String)] = [
            ("lan", "10.0.0.1:1", "10.0.0.1:1"),
            ("lan", "172.16.0.1:51234", "172.16.0.1:51234"),
            ("lan", "172.31.255.254:51234", "172.31.255.254:51234"),
            ("lan", "192.168.1.20:65535", "192.168.1.20:65535"),
            ("lan", "[fd12::1]:51234", "[fd12:0:0:0:0:0:0:1]:51234"),
            ("lan", "[fc00::1]:51234", "[fc00:0:0:0:0:0:0:1]:51234"),
            ("ipv6", "[240e:b86:551:7700::1]:51234", "[240e:b86:551:7700:0:0:0:1]:51234"),
            ("ipv6", "[2A00:1450:4001:81B::200E]:443", "[2a00:1450:4001:81b:0:0:0:200e]:443"),
            ("ipv6", "[3ffe::1]:9", "[3ffe:0:0:0:0:0:0:1]:9"),
        ]
        for row in admitted {
            let candidate = DirectCandidateAddress.admit(kind: row.kind, address: row.address)
            XCTAssertEqual(candidate?.socketAddress, row.dialled, row.address)
            XCTAssertEqual(candidate?.kind.rawValue, row.kind, row.address)
        }

        let refused: [(kind: String, address: String)] = [
            // Public, shared, loopback, link-local, and unspecified IPv4.
            ("lan", "8.8.8.8:53"),
            ("lan", "100.64.0.1:51234"),
            ("lan", "127.0.0.1:51234"),
            ("lan", "169.254.1.1:51234"),
            ("lan", "0.0.0.0:51234"),
            ("lan", "172.15.0.1:51234"),
            ("lan", "172.32.0.1:51234"),
            ("lan", "192.169.0.1:51234"),
            ("lan", "224.0.0.251:5353"),
            ("lan", "255.255.255.255:51234"),
            // IPv6 that no path may use.
            ("lan", "[::1]:51234"),
            ("lan", "[::]:51234"),
            ("lan", "[fe80::1]:51234"),
            ("ipv6", "[fe80::1]:51234"),
            ("ipv6", "[ff02::1]:51234"),
            ("ipv6", "[fec0::1]:51234"),
            // An IPv4 address in an IPv6 form.
            ("lan", "[::ffff:192.168.1.20]:51234"),
            ("ipv6", "[::ffff:8.8.8.8]:51234"),
            ("ipv6", "[64:ff9b::808:808]:51234"),
            ("ipv6", "[2002:808:808::1]:51234"),
            // Documentation and protocol assignments.
            ("ipv6", "[2001:db8::1]:51234"),
            ("ipv6", "[3fff:0abc::1]:51234"),
            ("ipv6", "[2001::1]:51234"),
            ("ipv6", "[2001:10::1]:51234"),
            // A class that does not match its kind.
            ("ipv6", "192.168.1.20:51234"),
            ("ipv6", "[fd12::1]:51234"),
            ("lan", "[240e:b86:551:7700::1]:51234"),
            // Kinds this version does not know.
            ("relay", "192.168.1.20:51234"),
            ("LAN", "192.168.1.20:51234"),
            ("", "192.168.1.20:51234"),
            // Not a numeric socket address.
            ("lan", "192.168.1.20"),
            ("lan", "192.168.1.20:"),
            ("lan", "192.168.1.20:0"),
            ("lan", "192.168.1.20:65536"),
            ("lan", "192.168.1.20:051234"),
            ("lan", "192.168.1.20:-1"),
            ("lan", "192.168.1.20: 80"),
            ("lan", "192.168.01.20:51234"),
            ("lan", "192.168.1:51234"),
            ("lan", "192.168.1.20.5:51234"),
            ("lan", "192.168.1.256:51234"),
            ("lan", "0xC0.168.1.20:51234"),
            ("lan", "nas.local:51234"),
            ("lan", "localhost:51234"),
            ("lan", "fd12::1:51234"),
            ("lan", "[fd12::1]"),
            ("lan", "[fd12::1]51234"),
            ("lan", "[fd12::1%en0]:51234"),
            ("lan", "[fd12::1]:51234 "),
            ("lan", " 192.168.1.20:51234"),
            ("lan", "[]:51234"),
            ("lan", "[fd12:::1]:51234"),
            ("lan", ""),
            ("lan", String(repeating: "1", count: 200)),
        ]
        for row in refused {
            XCTAssertNil(
                DirectCandidateAddress.admit(kind: row.kind, address: row.address),
                "\(row.kind) \(row.address)"
            )
        }
    }

    // MARK: Answers

    func testAReadyAnswerForTheSelectedInstanceIsAccepted() throws {
        let answer = try DirectRendezvous.answer(
            from: ready(),
            expecting: expectation
        )
        XCTAssertEqual(
            answer,
            .ready(
                DirectRendezvousOffer(
                    generation: "5f1c9a0b2d3e4f60:3",
                    endpointID: instance,
                    addresses: [
                        DirectCandidateAddress.admit(kind: "lan", address: "192.168.1.20:51234")!,
                        DirectCandidateAddress.admit(
                            kind: "ipv6",
                            address: "[240e:b86:551:7700::1]:51234"
                        )!,
                    ],
                    lifetimeSeconds: 300
                )
            )
        )
        XCTAssertEqual(
            DirectRendezvous.requestBody(clientEndpointID: client),
            "{\"version\":1,\"client_endpoint_id\":\"\(client.hex)\"}"
        )
    }

    func testAnAnswerThatDoesNotMatchTheSelectionIsRejected() {
        let lanAddress = [["kind": "lan", "address": "192.168.1.20:51234"]]

        assertRejected(.unsupportedVersion) { $0["version"] = 2 }
        assertRejected(.malformed) { $0["version"] = "1" }
        assertRejected(.malformed) { $0["version"] = true }
        assertRejected(.malformed) { $0["status"] = "maybe" }
        assertRejected(.wrongInstance) { $0["instance_id"] = "inst_2" }
        assertRejected(.malformed) { $0["instance_id"] = nil }
        assertRejected(.wrongHostname) { $0["hostname"] = "bob-app.avibe.bot" }
        assertRejected(.wrongHostname) { $0["hostname"] = "ALICE-app.avibe.bot" }
        assertRejected(.wrongProtocol) { $0["alpn"] = "avibe-direct/2" }
        assertRejected(.malformed) { $0["alpn"] = nil }
        assertRejected(.invalidGeneration) { $0["generation"] = "" }
        assertRejected(.invalidGeneration) {
            $0["generation"] = String(repeating: "g", count: 129)
        }
        assertRejected(.malformed) { $0["generation"] = 3 }

        assertRejected(.wrongClient) {
            $0["client"] = self.clientObject(id: String(repeating: "33", count: 32))
        }
        assertRejected(.malformed) {
            $0["client"] = ["endpoint_id": self.client.hex]
        }
        assertRejected(.malformed) { $0["client"] = nil }

        assertRejected(.invalidEndpoint) {
            $0["endpoint"] = self.endpointObject(id: "not-hex", addresses: lanAddress)
        }
        // The instance cannot be this App's own endpoint.
        assertRejected(.invalidEndpoint) {
            $0["endpoint"] = self.endpointObject(id: self.client.hex, addresses: lanAddress)
        }
        assertRejected(.malformed) {
            $0["endpoint"] = ["id": self.instance.hex]
        }
        assertRejected(.noAdmittedAddress) {
            $0["endpoint"] = self.endpointObject(id: self.instance.hex, addresses: [])
        }
        assertRejected(.noAdmittedAddress) {
            $0["endpoint"] = self.endpointObject(
                id: self.instance.hex,
                addresses: [
                    ["kind": "lan", "address": "8.8.8.8:53"],
                    ["kind": "lan", "address": "127.0.0.1:1"],
                ]
            )
        }

        assertRejected(.invalidLifetime) { $0["expires_at"] = 1_800_000_000 }
        assertRejected(.invalidLifetime) { $0["expires_at"] = 1_799_999_999 }
        assertRejected(.invalidLifetime) { $0["issued_at"] = -1 }
        assertRejected(.malformed) { $0["issued_at"] = 1.5 }
        assertRejected(.invalidLifetime) {
            $0["client"] = self.clientObject(id: self.client.hex, admittedUntil: 1_800_000_000)
        }

        for body in ["", "[]", "null", "{", "\"ready\"", "{\"version\":1}"] {
            XCTAssertThrowsError(
                try DirectRendezvous.answer(from: Data(body.utf8), expecting: expectation),
                body
            ) { error in
                XCTAssertEqual(error as? DirectRendezvousRejection, .malformed, body)
            }
        }
        // Larger than any answer the contract allows.
        var padded = readyObject()
        padded["padding"] = String(repeating: "a", count: DirectRendezvous.maxAnswerBytes)
        XCTAssertThrowsError(
            try DirectRendezvous.answer(from: data(padded), expecting: expectation)
        ) { error in
            XCTAssertEqual(error as? DirectRendezvousRejection, .malformed)
        }
    }

    func testTheLifetimeIsTheAnswersOwnSpanAndAtMostFiveMinutes() throws {
        func lifetime(_ change: (inout [String: Any]) -> Void) throws -> Int {
            var object = readyObject()
            change(&object)
            guard
                case .ready(let offer) = try DirectRendezvous.answer(
                    from: data(object),
                    expecting: expectation
                )
            else {
                XCTFail("not ready")
                return 0
            }
            return offer.lifetimeSeconds
        }

        XCTAssertEqual(try lifetime { _ in }, 300)
        // The phone's clock plays no part: an answer issued long ago by the
        // instance's clock still lasts its own span from receipt.
        XCTAssertEqual(
            try lifetime {
                $0["issued_at"] = 1_000
                $0["expires_at"] = 1_120
                $0["client"] = self.clientObject(id: self.client.hex, admittedUntil: 1_300)
            },
            120
        )
        XCTAssertEqual(
            try lifetime {
                $0["issued_at"] = 4_000_000_000
                $0["expires_at"] = 4_000_000_300
                $0["client"] = self.clientObject(id: self.client.hex, admittedUntil: 4_000_000_300)
            },
            300
        )
        // The admission can end before the answer does.
        XCTAssertEqual(
            try lifetime {
                $0["client"] = self.clientObject(
                    id: self.client.hex,
                    admittedUntil: 1_800_000_060
                )
            },
            60
        )
        // A longer span is cut to the contract's.
        XCTAssertEqual(
            try lifetime {
                $0["expires_at"] = 1_800_086_400
                $0["client"] = self.clientObject(
                    id: self.client.hex,
                    admittedUntil: 1_800_086_400
                )
            },
            300
        )
    }

    func testAtMostEightCheckedAddressesAreKeptAndTheRestOfTheListIsSkipped() throws {
        var object = readyObject()
        var listed: [Any] = [
            // Entries this version does not know are skipped, not errors.
            "192.168.1.1:1",
            7,
            ["kind": "relay", "address": "https://relay.example"],
            ["kind": "lan"],
            ["address": "192.168.1.2:1"],
            ["kind": "lan", "address": "8.8.8.8:53"],
            // The same socket address twice, in two spellings.
            ["kind": "lan", "address": "[fd00::1]:9"],
            ["kind": "lan", "address": "[FD00:0:0:0:0:0:0:1]:9"],
        ]
        for host in 1...12 {
            listed.append(["kind": "lan", "address": "10.0.0.\(host):51234"])
        }
        let listedEndpoint: [String: Any] = [
            "id": instance.hex,
            "addresses": listed,
            "relay": ["url": "https://relay.example", "capability": "x"],
        ]
        object["endpoint"] = listedEndpoint
        object["unknown"] = ["nested": true]

        guard
            case .ready(let offer) = try DirectRendezvous.answer(
                from: data(object),
                expecting: expectation
            )
        else {
            return XCTFail("not ready")
        }
        XCTAssertEqual(
            offer.addresses.map(\.socketAddress),
            ["[fd00:0:0:0:0:0:0:1]:9"] + (1...7).map { "10.0.0.\($0):51234" }
        )

        // Entries beyond the scanned part of the list are never looked at.
        var long = readyObject()
        var longList = Array(
            repeating: ["kind": "lan", "address": "8.8.8.8:53"],
            count: DirectRendezvous.maxListedAddresses
        )
        longList.append(["kind": "lan", "address": "192.168.1.20:51234"])
        long["endpoint"] = endpointObject(id: instance.hex, addresses: longList)
        XCTAssertThrowsError(
            try DirectRendezvous.answer(from: data(long), expecting: expectation)
        ) { error in
            XCTAssertEqual(error as? DirectRendezvousRejection, .noAdmittedAddress)
        }
    }

    func testAnUnavailableAnswerSaysWhenToAskAgain() throws {
        func retry(_ seconds: Any?) throws -> DirectRendezvousAnswer {
            var object: [String: Any] = [
                "version": 1,
                "status": "unavailable",
                "reason": "something_new",
                "instance_id": "inst_1",
                "hostname": "alice-app.avibe.bot",
            ]
            object["retry_after_seconds"] = seconds
            return try DirectRendezvous.answer(from: data(object), expecting: expectation)
        }

        XCTAssertEqual(try retry(60), .unavailable(retryAfterSeconds: 60))
        XCTAssertEqual(try retry(nil), .unavailable(retryAfterSeconds: 60))
        XCTAssertEqual(try retry(0), .unavailable(retryAfterSeconds: 30))
        XCTAssertEqual(try retry(-5), .unavailable(retryAfterSeconds: 30))
        XCTAssertEqual(try retry(86_400), .unavailable(retryAfterSeconds: 900))
    }

    // MARK: Fixtures

    private func assertRejected(
        _ rejection: DirectRendezvousRejection,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ change: (inout [String: Any]) -> Void
    ) {
        var object = readyObject()
        change(&object)
        XCTAssertThrowsError(
            try DirectRendezvous.answer(from: data(object), expecting: expectation),
            file: file,
            line: line
        ) { error in
            XCTAssertEqual(
                error as? DirectRendezvousRejection,
                rejection,
                file: file,
                line: line
            )
        }
    }

    private func clientObject(
        id: String,
        admittedUntil: Int = 1_800_000_300
    ) -> [String: Any] {
        ["endpoint_id": id, "admitted_until": admittedUntil]
    }

    private func endpointObject(
        id: String,
        addresses: [[String: String]]
    ) -> [String: Any] {
        ["id": id, "addresses": addresses, "relay": NSNull()]
    }

    private func readyObject() -> [String: Any] {
        [
            "version": 1,
            "status": "ready",
            "instance_id": "inst_1",
            "hostname": "alice-app.avibe.bot",
            "generation": "5f1c9a0b2d3e4f60:3",
            "issued_at": 1_800_000_000,
            "expires_at": 1_800_000_300,
            "alpn": "avibe-direct/1",
            "endpoint": endpointObject(
                id: instance.hex,
                addresses: [
                    ["kind": "lan", "address": "192.168.1.20:51234"],
                    ["kind": "ipv6", "address": "[240e:b86:551:7700::1]:51234"],
                ]
            ),
            "client": clientObject(id: client.hex),
        ]
    }

    private func ready() -> Data {
        data(readyObject())
    }

    private func data(_ object: [String: Any]) -> Data {
        // swiftlint:disable:next force_try
        try! JSONSerialization.data(withJSONObject: object)
    }
}
