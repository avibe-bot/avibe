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
                    relay: nil,
                    lifetimeSeconds: 300
                )
            )
        )
        XCTAssertEqual(
            DirectRendezvous.requestBody(clientEndpointID: client, listenerFaults: []),
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

    // MARK: The public-relay owner trial

    func testEachPinnedRelayIsAcceptedUnderThePublicTrialPolicy() throws {
        XCTAssertEqual(DirectTrialRelay.publicTrialRelayURLs, [
            "https://use1-1.relay.n0.iroh.link./",
            "https://usw1-1.relay.n0.iroh.link./",
            "https://euc1-1.relay.n0.iroh.link./",
            "https://aps1-1.relay.n0.iroh.link./",
        ])
        for url in DirectTrialRelay.publicTrialRelayURLs {
            let offer = try readyOffer(relay: ["policy": "public-trial-v1", "url": url])
            XCTAssertEqual(offer.relay, DirectTrialRelay(policy: .publicTrialV1, url: url))
            XCTAssertEqual(offer.addresses.count, 2)
        }
    }

    func testAnUnknownPolicyOrAnotherURLIsNoRelayAndTheAddressesStay() throws {
        let relays: [Any] = [
            ["policy": "public-trial-v2", "url": "https://aps1-1.relay.n0.iroh.link./"],
            ["url": "https://aps1-1.relay.n0.iroh.link./"],
            ["policy": "public-trial-v1"],
            ["policy": "public-trial-v1", "url": "https://aps1-1.relay.n0.iroh.link/"],
            ["policy": "public-trial-v1", "url": "https://APS1-1.relay.n0.iroh.link./"],
            ["policy": "public-trial-v1", "url": "http://aps1-1.relay.n0.iroh.link./"],
            ["policy": "public-trial-v1", "url": "https://relay.example./"],
            ["policy": "public-trial-v1", "url": "https://aps1-1.relay.n0.iroh.link./x"],
            ["version": 1, "url": "https://relay.example", "expires_at": 1_800_000_300] as [String: Any],
            ["policy": 1, "url": ["x"]] as [String: Any],
            "public-trial-v1",
            7,
            NSNull(),
        ]
        for relay in relays {
            let offer = try readyOffer(relay: relay)
            XCTAssertNil(offer.relay, "\(relay)")
            XCTAssertEqual(offer.addresses.count, 2, "\(relay)")
        }
    }

    func testARelayAloneIsDialableAndNothingAtAllIsNot() throws {
        let relayOnly = try readyOffer(
            addresses: [],
            relay: ["policy": "public-trial-v1", "url": "https://aps1-1.relay.n0.iroh.link./"]
        )
        XCTAssertEqual(relayOnly.addresses, [])
        XCTAssertEqual(relayOnly.relay?.url, "https://aps1-1.relay.n0.iroh.link./")

        for relay in [NSNull(), ["policy": "unknown", "url": "https://aps1-1.relay.n0.iroh.link./"]] as [Any] {
            var object = readyObject()
            object["endpoint"] = ["id": instance.hex, "addresses": [] as [Any], "relay": relay] as [String: Any]
            XCTAssertThrowsError(try DirectRendezvous.answer(from: data(object), expecting: expectation)) {
                XCTAssertEqual($0 as? DirectRendezvousRejection, .noAdmittedAddress)
            }
        }
    }

    private func readyOffer(
        addresses: [[String: String]]? = nil,
        relay: Any
    ) throws -> DirectRendezvousOffer {
        var object = readyObject()
        guard var endpoint = object["endpoint"] as? [String: Any] else {
            throw DirectIrohTestError.scripted
        }
        if let addresses {
            endpoint["addresses"] = addresses
        }
        endpoint["relay"] = relay
        object["endpoint"] = endpoint
        guard case .ready(let offer) = try DirectRendezvous.answer(from: data(object), expecting: expectation) else {
            throw DirectIrohTestError.scripted
        }
        return offer
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

    func testAnUnavailableAnswerForAnotherInstanceIsNoAnswer() {
        func rejection(_ change: (inout [String: Any]) -> Void) -> DirectRendezvousRejection? {
            var object: [String: Any] = [
                "version": 1,
                "status": "unavailable",
                "reason": "not_running",
                "instance_id": "inst_1",
                "hostname": "alice-app.avibe.bot",
                "retry_after_seconds": 60,
            ]
            change(&object)
            do {
                _ = try DirectRendezvous.answer(from: data(object), expecting: expectation)
                return nil
            } catch {
                return error as? DirectRendezvousRejection
            }
        }

        XCTAssertNil(rejection { _ in })
        XCTAssertEqual(rejection { $0["instance_id"] = "inst_2" }, .wrongInstance)
        XCTAssertEqual(rejection { $0["instance_id"] = "" }, .wrongInstance)
        XCTAssertEqual(rejection { $0["instance_id"] = nil }, .malformed)
        XCTAssertEqual(rejection { $0["instance_id"] = 7 }, .malformed)
        XCTAssertEqual(rejection { $0["hostname"] = "bob-app.avibe.bot" }, .wrongHostname)
        XCTAssertEqual(rejection { $0["hostname"] = nil }, .malformed)
        XCTAssertEqual(rejection { $0["version"] = 2 }, .unsupportedVersion)
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

    // MARK: The listener's failures

    private func failure(
        _ step: DirectListenerFailure.Step,
        code: Int32? = nil,
        away: Int? = nil,
        at: ContinuousClock.Instant = .now
    ) -> DirectListenerFailure {
        DirectListenerFailure(step: step, code: code, backgroundSeconds: away, at: at)
    }

    /// A first bind confirmed after the first load is reported in the
    /// instance's own shape: the step `late`, and the bind's duration in
    /// milliseconds where a failure has its error number.
    func testTheRequestReportsALateFirstBindWithItsDuration() throws {
        XCTAssertEqual(
            DirectRendezvous.requestBody(clientEndpointID: client, listenerFaults: [failure(.late, code: 1_840)]),
            "{\"version\":1,\"client_endpoint_id\":\"\(client.hex)\""
                + ",\"listener_faults\":[{\"step\":\"late\",\"code\":1840,\"background_seconds\":0}]}"
        )
    }

    /// The App's slow store removals ride on the same request, as at most
    /// five non-negative integers the instance accepts (milliseconds), and
    /// no field when there are none.
    func testTheRequestReportsSlowStoreRemovalsWithinTheInstancesLimits() throws {
        let head = "{\"version\":1,\"client_endpoint_id\":\"\(client.hex)\""
        XCTAssertEqual(
            DirectRendezvous.requestBody(clientEndpointID: client, listenerFaults: [], slowStoreRemovalsMs: []),
            head + "}"
        )
        XCTAssertEqual(
            DirectRendezvous.requestBody(clientEndpointID: client, listenerFaults: [], slowStoreRemovalsMs: [1_200, 485_000]),
            head + ",\"slow_store_removals_ms\":[1200,485000]}"
        )
        XCTAssertEqual(
            DirectRendezvous.requestBody(
                clientEndpointID: client,
                listenerFaults: [failure(.deadline)],
                slowStoreRemovalsMs: [-5, 1, 2, 3, 4, Int.max]
            ),
            head + ",\"listener_faults\":[{\"step\":\"deadline\",\"code\":0,\"background_seconds\":0}]"
                + ",\"slow_store_removals_ms\":[1,2,3,4,\(Int32.max)]}"
        )
    }

    /// The account of which leg carried the pages' connections rides on the
    /// same request: at most ten entries, each as `DirectPathUse` wrote it,
    /// after the other reports; and no field when there are none.
    func testTheRequestReportsPathUseWithinTheInstancesLimit() throws {
        let head = "{\"version\":1,\"client_endpoint_id\":\"\(client.hex)\""
        let clock = DirectPathUseTestClock()
        let use = DirectPathUse(seconds: { clock.now })
        let scope = DirectPathUse.Scope(store: UUID(), host: "instance-one.example")
        XCTAssertEqual(
            DirectRendezvous.requestBody(clientEndpointID: client, listenerFaults: [], pathUse: use.pending(for: scope)),
            head + "}"
        )
        for _ in 0..<12 {
            use.connection(
                DirectPathUseConnection(carrier: .tunnel, plan: .raced, readyMs: 80), scope: scope, path: .relay
            )
            clock.now += 60
        }
        let reports = use.pending(for: scope)
        XCTAssertEqual(reports.count, 10)
        let body = DirectRendezvous.requestBody(
            clientEndpointID: client,
            listenerFaults: [failure(.deadline)],
            slowStoreRemovalsMs: [1_200],
            pathUse: reports + reports
        )
        XCTAssertEqual(
            body,
            head + ",\"listener_faults\":[{\"step\":\"deadline\",\"code\":0,\"background_seconds\":0}]"
                + ",\"slow_store_removals_ms\":[1200]"
                + ",\"path_use\":[" + reports.map(\.json).joined(separator: ",") + "]}"
        )
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(body.utf8)) as? [String: Any])
        let entries = try XCTUnwrap(object["path_use"] as? [[String: Any]])
        XCTAssertEqual(entries.count, 10)
        // Oldest first, and no wall time: only how long ago.
        XCTAssertEqual(entries.first?["age_min"] as? Int, 12)
        XCTAssertEqual(entries.last?["age_min"] as? Int, 3)
        XCTAssertEqual(entries.first?["tunnel_won"] as? Int, 1)
    }

    /// `listener_faults` is what the instance accepts and nothing more: at
    /// most five entries, the newest, each with exactly a step (one it
    /// knows), a code and the seconds away, all within its bounds; no time,
    /// address, port or identifier; and no field at all when there is
    /// nothing to report. (The instance ignores the whole list for one
    /// entry it does not accept: `vibe/direct_rendezvous.py`.)
    func testTheRequestReportsTheListenersLastFailuresWithinTheInstancesLimits() throws {
        let head = "{\"version\":1,\"client_endpoint_id\":\"\(client.hex)\""
        XCTAssertEqual(
            DirectRendezvous.requestBody(clientEndpointID: client, listenerFaults: []),
            head + "}"
        )
        XCTAssertEqual(
            DirectRendezvous.requestBody(
                clientEndpointID: client,
                listenerFaults: [
                    failure(.bind, code: EADDRINUSE, away: 1_860),
                    failure(.deadline),
                    failure(.socket, code: ECONNABORTED),
                ]
            ),
            head + ",\"listener_faults\":["
                + "{\"step\":\"bind\",\"code\":48,\"background_seconds\":1860},"
                // No number and no background before it: zeros, never a
                // missing key.
                + "{\"step\":\"deadline\",\"code\":0,\"background_seconds\":0},"
                + "{\"step\":\"socket\",\"code\":53,\"background_seconds\":0}]}"
        )

        // Every step is one the instance knows (it also accepts
        // `foreign`, which the App never sends).
        let steps: [DirectListenerFailure.Step] = [.bind, .waiting, .ownership, .deadline, .socket]
        XCTAssertEqual(
            steps.map(\.rawValue),
            ["bind", "waiting", "ownership", "deadline", "socket"]
        )

        // Seven failures: the last five. Seconds away outside what the
        // instance accepts are sent as its nearest bound.
        let many = (1...7).map { failure(.bind, code: Int32($0), away: $0 == 7 ? Int.max : -$0) }
        let body = DirectRendezvous.requestBody(clientEndpointID: client, listenerFaults: many)
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(body.utf8)) as? [String: Any]
        )
        XCTAssertEqual(Set(object.keys), ["version", "client_endpoint_id", "listener_faults"])
        let faults = try XCTUnwrap(object["listener_faults"] as? [[String: Any]])
        XCTAssertEqual(faults.count, 5)
        XCTAssertEqual(faults.map { $0["code"] as? Int }, [3, 4, 5, 6, 7])
        XCTAssertEqual(
            faults.map { $0["background_seconds"] as? Int },
            [0, 0, 0, 0, Int(Int32.max)]
        )
        for fault in faults {
            XCTAssertEqual(Set(fault.keys), ["step", "code", "background_seconds"])
            XCTAssertNotNil(fault["code"] as? Int)
            XCTAssertNotNil(fault["background_seconds"] as? Int)
        }
        // `direct.status()` says the same of one failure.
        XCTAssertEqual(
            failure(.bind, code: EADDRINUSE, away: 12).jsonObject as NSDictionary,
            ["step": "bind", "code": 48, "background_seconds": 12] as NSDictionary
        )
        XCTAssertEqual(
            failure(.ownership).jsonObject as NSDictionary,
            ["step": "ownership"] as NSDictionary
        )
    }

    /// Each failure reaches the instance once, with the first request that
    /// is answered after it: a request that fails reports it again, and an
    /// answered one never does.
    func testEachListenerFailureIsReportedOnceWithTheFirstAnsweredRequest() {
        var report = DirectListenerFaultReport()
        let start = ContinuousClock.now
        func at(_ seconds: Int) -> ContinuousClock.Instant {
            start.advanced(by: .seconds(seconds))
        }
        XCTAssertEqual(report.pending(from: []), [])

        let first = failure(.bind, code: EADDRINUSE, at: at(1))
        let second = failure(.deadline, at: at(2))
        XCTAssertEqual(report.pending(from: [first, second]), [first, second])
        // The request failed: nothing was answered, so both are still due.
        XCTAssertEqual(report.pending(from: [first, second]), [first, second])

        // A request that carried only the first is answered while the
        // second has happened: the second is still due.
        report.answered([first])
        XCTAssertEqual(report.pending(from: [first, second]), [second])
        report.answered([second])
        XCTAssertEqual(report.pending(from: [first, second]), [])
        // An answer to an older request arriving late takes nothing back.
        report.answered([first])
        XCTAssertEqual(report.pending(from: [first, second]), [])

        // A new failure is due again, alone; and never more than five.
        let third = failure(.socket, code: ECONNABORTED, at: at(3))
        XCTAssertEqual(report.pending(from: [first, second, third]), [third])
        let many = (10...16).map { failure(.bind, code: Int32($0), at: at($0)) }
        XCTAssertEqual(report.pending(from: many), Array(many.suffix(5)))
        report.answered([])
        XCTAssertEqual(report.pending(from: [third]), [third])
    }

    private func data(_ object: [String: Any]) -> Data {
        // swiftlint:disable:next force_try
        try! JSONSerialization.data(withJSONObject: object)
    }
}

/// The App's cold starts of a restored instance, as the rendezvous request
/// reports them (`startup`): exactly the four keys the instance accepts, one
/// entry per launch only when both times were measured, at most five.
@MainActor
final class ColdStartTimingsTests: XCTestCase {
    private let client = DirectEndpointID(bytes: Data(repeating: 0x11, count: 32))!

    func testAnEntryNeedsTheRestoredOpenTheBindAndAVisibleFrame() throws {
        let timings = ColdStartTimings()
        timings.launched()
        timings.firstBindConfirmed(took: .milliseconds(240), late: false)
        // A frame with no restored open (a launch from a tap, or the list)
        // is not measured.
        timings.firstVisible(.live, attemptID: 1)
        XCTAssertTrue(timings.pending.isEmpty)
        timings.openedRestoredInstance(attemptID: 1)
        XCTAssertTrue(timings.pending.isEmpty)
        timings.firstVisible(.cached, attemptID: 1)
        let entry = try XCTUnwrap(timings.pending.first)
        XCTAssertEqual(timings.pending.count, 1)
        XCTAssertEqual(entry.bindMs, 240)
        XCTAssertFalse(entry.late)
        XCTAssertEqual(entry.visible, .cached)
        XCTAssertGreaterThanOrEqual(entry.visibleMs, 0)
        // Later binds, frames or opens in the same process add nothing.
        timings.firstBindConfirmed(took: .seconds(3), late: true)
        timings.firstVisible(.live, attemptID: 1)
        timings.openedRestoredInstance(attemptID: 1)
        XCTAssertEqual(timings.pending, [entry])

        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(entry.json.utf8)) as? [String: Any])
        XCTAssertEqual(Set(object.keys), ["bind_ms", "visible_ms", "late", "visible"])
        XCTAssertEqual(object["bind_ms"] as? Int, 240)
        XCTAssertEqual(object["late"] as? Bool, false)
        XCTAssertEqual(object["visible"] as? String, "cached")
    }

    func testNoEntryWithoutABindOrAVisibleFrame() {
        let noBind = ColdStartTimings()
        noBind.launched()
        noBind.openedRestoredInstance(attemptID: 1)
        noBind.firstVisible(.live, attemptID: 1)
        XCTAssertTrue(noBind.pending.isEmpty)

        let nothingShown = ColdStartTimings()
        nothingShown.launched()
        nothingShown.openedRestoredInstance(attemptID: 1)
        nothingShown.firstBindConfirmed(took: .milliseconds(1_800), late: true)
        XCTAssertTrue(nothingShown.pending.isEmpty)

        // Another attempt's frame is not this launch's, and once the
        // restored attempt has ended without a frame nothing is recorded.
        let other = ColdStartTimings()
        other.launched()
        other.openedRestoredInstance(attemptID: 1)
        other.firstBindConfirmed(took: .milliseconds(300), late: false)
        other.firstVisible(.live, attemptID: 2)
        XCTAssertTrue(other.pending.isEmpty)
        other.openEnded(attemptID: 1)
        other.firstVisible(.live, attemptID: 1)
        XCTAssertTrue(other.pending.isEmpty)

        // An attempt that ends after its frame keeps its entry.
        let ended = ColdStartTimings()
        ended.launched()
        ended.openedRestoredInstance(attemptID: 1)
        ended.firstVisible(.cached, attemptID: 1)
        ended.openEnded(attemptID: 1)
        ended.firstBindConfirmed(took: .milliseconds(90), late: false)
        XCTAssertEqual(ended.pending.count, 1)

        // A frame before `launched` is not measured.
        let unlaunched = ColdStartTimings()
        unlaunched.openedRestoredInstance(attemptID: 1)
        unlaunched.firstBindConfirmed(took: .milliseconds(10), late: false)
        unlaunched.firstVisible(.live, attemptID: 1)
        XCTAssertTrue(unlaunched.pending.isEmpty)
    }

    func testTheRequestCarriesTheEntriesUntilAnswered() throws {
        let timings = ColdStartTimings()
        timings.launched()
        timings.openedRestoredInstance(attemptID: 1)
        timings.firstBindConfirmed(took: .milliseconds(2_400), late: true)
        timings.firstVisible(.live, attemptID: 1)
        let sent = timings.pending
        let head = "{\"version\":1,\"client_endpoint_id\":\"\(client.hex)\""
        XCTAssertEqual(
            DirectRendezvous.requestBody(clientEndpointID: client, listenerFaults: []),
            head + "}"
        )
        let body = DirectRendezvous.requestBody(
            clientEndpointID: client, listenerFaults: [], startup: sent.map(\.json)
        )
        XCTAssertTrue(body.hasPrefix(head + ",\"startup\":[{\"bind_ms\":2400,\"visible_ms\":"))
        XCTAssertTrue(body.hasSuffix(",\"late\":true,\"visible\":\"live\"}]}"))
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(body.utf8)) as? [String: Any])
        XCTAssertEqual((object["startup"] as? [[String: Any]])?.count, 1)
        // At most five, the newest.
        let six = (0..<6).map { "{\"bind_ms\":\($0),\"visible_ms\":0,\"late\":false,\"visible\":\"live\"}" }
        let capped = try XCTUnwrap(JSONSerialization.jsonObject(
            with: Data(DirectRendezvous.requestBody(clientEndpointID: client, listenerFaults: [], startup: six).utf8)
        ) as? [String: Any])
        let entries = try XCTUnwrap(capped["startup"] as? [[String: Any]])
        XCTAssertEqual(entries.map { $0["bind_ms"] as? Int }, [1, 2, 3, 4, 5])

        timings.answered(sent)
        XCTAssertTrue(timings.pending.isEmpty)
    }

    func testMillisecondsAreWholeAndNeverNegative() {
        XCTAssertEqual(ColdStartTimings.milliseconds(.milliseconds(1_234)), 1_234)
        XCTAssertEqual(ColdStartTimings.milliseconds(.microseconds(999)), 0)
        XCTAssertEqual(ColdStartTimings.milliseconds(.zero - .seconds(1)), 0)
        XCTAssertEqual(ColdStartTimings.milliseconds(.seconds(3_000_000)), Int(Int32.max))
    }
}
