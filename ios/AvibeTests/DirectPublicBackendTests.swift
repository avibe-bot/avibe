import XCTest
@testable import Avibe

/// The public backend: which resolved addresses it may dial, and that it
/// dials exactly those, never after it was cancelled. A stand-in resolver and
/// a dial spy replace the system, so nothing here touches the network.
final class DirectPublicBackendTests: XCTestCase {
    private let authority = DirectAuthority(host: "instance.example")!

    // MARK: The address policy

    func testOnlyPublicUnicastIPv4AddressesAreAllowed() {
        let allowed: [[UInt8]] = [
            [104, 16, 132, 229],
            [1, 1, 1, 1],
            [100, 63, 255, 255],
            [100, 128, 0, 1],
            [172, 15, 0, 1],
            [172, 32, 0, 1],
            [192, 169, 0, 1],
            [198, 20, 0, 1],
            [223, 255, 255, 255],
        ]
        let refused: [[UInt8]] = [
            [0, 0, 0, 0],
            [10, 0, 0, 1],
            [127, 0, 0, 1],
            [100, 64, 0, 1],
            [100, 127, 255, 255],
            [169, 254, 1, 1],
            [172, 16, 0, 1],
            [172, 31, 255, 255],
            [192, 168, 1, 20],
            [192, 0, 0, 1],
            [192, 0, 2, 1],
            [198, 18, 0, 1],
            [198, 19, 255, 255],
            [198, 51, 100, 1],
            [203, 0, 113, 1],
            [224, 0, 0, 1],
            [240, 0, 0, 1],
            [255, 255, 255, 255],
        ]
        for bytes in allowed {
            XCTAssertTrue(DirectPublicAddressPolicy.allowsIPv4(bytes), "\(bytes)")
        }
        for bytes in refused {
            XCTAssertFalse(DirectPublicAddressPolicy.allowsIPv4(bytes), "\(bytes)")
        }
        XCTAssertFalse(DirectPublicAddressPolicy.allowsIPv4([1, 1, 1]))
    }

    func testOnlyGlobalUnicastIPv6AddressesAreAllowedAndCarriedIPv4IsJudgedAsIPv4() {
        func v6(_ text: String) -> [UInt8] {
            var address = in6_addr()
            XCTAssertEqual(inet_pton(AF_INET6, text, &address), 1, text)
            return withUnsafeBytes(of: &address) { Array($0) }
        }
        let allowed = [
            "2606:4700::6810:84e5",
            "2a00:1450:4001::1",
            "::ffff:104.16.132.229",
            "64:ff9b::104.16.132.229",
        ]
        let refused = [
            "::",
            "::1",
            "fc00::1",
            "fd12:3456::1",
            "fe80::1",
            "ff02::1",
            "2001::1",
            "2001:db8::1",
            "2002:c0a8:0114::1",
            "::ffff:192.168.1.20",
            "::ffff:127.0.0.1",
            "64:ff9b::10.0.0.1",
            "64:ff9b::100.64.0.1",
            "4000::1",
        ]
        for text in allowed {
            XCTAssertTrue(DirectPublicAddressPolicy.allowsIPv6(v6(text)), text)
        }
        for text in refused {
            XCTAssertFalse(DirectPublicAddressPolicy.allowsIPv6(v6(text)), text)
        }
        XCTAssertFalse(DirectPublicAddressPolicy.allowsIPv6(Array(repeating: 0x20, count: 15)))
    }

    // MARK: Opening

    func testARefusedAddressIsNeverDialledAndAnAllowedOneIsDialledExactlyOnPort443() async throws {
        let refused = DirectResolvedAddress(ipv4: [192, 168, 1, 20])!
        let loopback = DirectResolvedAddress(ipv6: Array(repeating: 0, count: 15) + [1])!
        let allowed = DirectResolvedAddress(ipv4: [104, 16, 132, 229])!
        let dialer = DialSpy()
        let backend = DirectPublicBackend(
            resolver: StandInResolver(.answer([refused, loopback, allowed])),
            dialer: dialer
        )

        let stream = try await backend.open(authority)

        XCTAssertEqual(dialer.dials, [Dial(address: allowed, port: 443)])
        XCTAssertTrue(stream === dialer.streams.first)
    }

    func testAHostWithOnlyRefusedAddressesIsNeverDialled() async throws {
        let dialer = DialSpy()
        let backend = DirectPublicBackend(
            resolver: StandInResolver(.answer([
                DirectResolvedAddress(ipv4: [10, 0, 0, 5])!,
                DirectResolvedAddress(ipv4: [127, 0, 0, 1])!,
            ])),
            dialer: dialer
        )

        do {
            _ = try await backend.open(authority)
            XCTFail("A host with no public address was opened.")
        } catch {
            XCTAssertEqual(error as? DirectPublicError, .noPublicAddress)
        }
        XCTAssertEqual(dialer.dials, [])
    }

    func testAFailedDialMovesOnToTheNextAllowedAddress() async throws {
        let first = DirectResolvedAddress(ipv4: [104, 16, 132, 229])!
        let second = DirectResolvedAddress(ipv4: [104, 16, 133, 229])!
        let dialer = DialSpy(failing: [first])
        let backend = DirectPublicBackend(
            resolver: StandInResolver(.answer([first, second])),
            dialer: dialer
        )

        _ = try await backend.open(authority)

        XCTAssertEqual(
            dialer.dials,
            [Dial(address: first, port: 443), Dial(address: second, port: 443)]
        )
    }

    func testAnAnswerThatArrivesAfterCancellationIsNeverDialled() async throws {
        let resolver = StandInResolver(.gate)
        let dialer = DialSpy()
        let backend = DirectPublicBackend(resolver: resolver, dialer: dialer)

        let opening = Task {
            try await backend.open(self.authority)
        }
        try await eventually { resolver.isWaiting }
        opening.cancel()
        do {
            _ = try await opening.value
            XCTFail("A cancelled open returned a stream.")
        } catch {}
        // The lookup answers late, with an allowed address.
        resolver.openGate(with: [DirectResolvedAddress(ipv4: [104, 16, 132, 229])!])
        try await Task.sleep(for: .milliseconds(200))

        XCTAssertEqual(dialer.dials, [])
    }

    func testAStreamThatConnectsAfterCancellationIsClosedAndNotReturned() async throws {
        let dialer = DialSpy(ignoresCancellationFor: .milliseconds(300))
        let backend = DirectPublicBackend(
            resolver: StandInResolver(.answer([DirectResolvedAddress(ipv4: [104, 16, 132, 229])!])),
            dialer: dialer
        )

        let opening = Task {
            try await backend.open(self.authority)
        }
        try await eventually { dialer.dials.count == 1 }
        opening.cancel()
        do {
            _ = try await opening.value
            XCTFail("A cancelled open returned a stream.")
        } catch {}

        try await eventually { dialer.streams.count == 1 }
        try await eventually { dialer.streams[0].isCancelled }
    }

    func testTheWholeOpenIsBounded() async throws {
        let backend = DirectPublicBackend(
            resolver: StandInResolver(.gate),
            dialer: DialSpy(),
            connectSeconds: 0.2
        )

        do {
            _ = try await backend.open(authority)
            XCTFail("An open that never resolved returned.")
        } catch {
            XCTAssertEqual(error as? DirectConnectError, .timedOut)
        }
    }
}

private struct Dial: Equatable {
    let address: DirectResolvedAddress
    let port: UInt16
}

/// Answers at once, or waits for the test to open its gate. Either way it
/// returns promptly when its task is cancelled, as the protocol requires.
private final class StandInResolver: DirectHostResolving, @unchecked Sendable {
    enum Mode {
        case answer([DirectResolvedAddress])
        case gate
    }

    private let lock = NSLock()
    private let mode: Mode
    private var waiting: [DirectOnceAnswer<[DirectResolvedAddress]>] = []

    init(_ mode: Mode) {
        self.mode = mode
    }

    var isWaiting: Bool {
        lock.withLock { !waiting.isEmpty }
    }

    func openGate(with addresses: [DirectResolvedAddress]) {
        let answers = lock.withLock { waiting }
        for answer in answers {
            answer.resolve(with: .success(addresses))
        }
    }

    func resolve(_ host: String) async throws -> [DirectResolvedAddress] {
        switch mode {
        case .answer(let addresses):
            return addresses
        case .gate:
            let answer = DirectOnceAnswer<[DirectResolvedAddress]>()
            lock.withLock {
                waiting.append(answer)
            }
            return try await withTaskCancellationHandler {
                try await withCheckedThrowingContinuation { continuation in
                    answer.wait(with: continuation)
                }
            } onCancel: {
                answer.resolve(with: .failure(CancellationError()))
            }
        }
    }
}

/// Records every dial and hands back stand-in streams.
private final class DialSpy: DirectPublicDialing, @unchecked Sendable {
    struct Refused: Error {}

    private let lock = NSLock()
    private let failing: [DirectResolvedAddress]
    private let ignoresCancellationFor: Duration?
    private var recorded: [Dial] = []
    private var made: [StandInDirectStream] = []

    init(
        failing: [DirectResolvedAddress] = [],
        ignoresCancellationFor: Duration? = nil
    ) {
        self.failing = failing
        self.ignoresCancellationFor = ignoresCancellationFor
    }

    var dials: [Dial] {
        lock.withLock { recorded }
    }

    var streams: [StandInDirectStream] {
        lock.withLock { made }
    }

    func dial(_ address: DirectResolvedAddress, port: UInt16) async throws -> any DirectByteStream {
        lock.withLock {
            recorded.append(Dial(address: address, port: port))
        }
        if failing.contains(address) {
            throw Refused()
        }
        if let ignoresCancellationFor {
            // A connection that completes although its task was cancelled.
            let clock = ContinuousClock()
            let deadline = clock.now.advanced(by: ignoresCancellationFor)
            while clock.now < deadline {
                usleep(10_000)
            }
        }
        let stream = StandInDirectStream()
        lock.withLock {
            made.append(stream)
        }
        return stream
    }
}
