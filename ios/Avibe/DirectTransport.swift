import Foundation

// The seam between the direct transport (iroh, the rendezvous, probing) and
// the local proxy that carries WebKit's connections. `AvibeApp` registers
// the binding-backed transport; it remains inactive until a runtime is ready.
// Design: docs/plans/remote-direct-ios.md.

/// An instance authority the local proxy may serve: an exact host, on port
/// 443.
struct DirectAuthority: Equatable, Sendable {
    let host: String

    /// `nil` unless `instanceURL` is an HTTPS origin on the default port
    /// whose host is a DNS name. The instance's ingress serves nothing else,
    /// and the host is written into a request head, so it may hold only
    /// host-name characters.
    init?(instanceURL: URL) {
        guard
            let origin = WebOrigin(url: instanceURL),
            origin.port == nil
        else {
            return nil
        }
        self.init(host: origin.host)
    }

    /// `nil` unless `host` is a lowercase DNS name (never an IP literal).
    init?(host: String) {
        guard Self.isDNSName(host) else {
            return nil
        }
        self.host = host
    }

    /// The CONNECT request target, and the `Host` the instance requires.
    var connectTarget: String {
        "\(host):443"
    }

    private static func isDNSName(_ host: String) -> Bool {
        let labels = host.split(separator: ".", omittingEmptySubsequences: false)
        guard
            host.utf8.count <= 253,
            let last = labels.last,
            labels.allSatisfy({ label in
                !label.isEmpty
                    && label.utf8.allSatisfy { byte in
                        (UInt8(ascii: "a")...UInt8(ascii: "z")).contains(byte)
                            || (UInt8(ascii: "0")...UInt8(ascii: "9")).contains(byte)
                            || byte == UInt8(ascii: "-")
                    }
            })
        else {
            return false
        }
        // A numeric last label is how an IPv4 literal reads.
        return !last.utf8.allSatisfy { (UInt8(ascii: "0")...UInt8(ascii: "9")).contains($0) }
    }
}

/// What a transport is asked to reach: one opened runtime's instance.
struct DirectTarget: Equatable, Sendable {
    /// Issued by `DirectPathController`. Advances each time a runtime opens
    /// and each time the App returns from the background, so two targets for
    /// the same instance are never equal.
    let selection: Int
    /// The account, the instance, and the lease `MobileSessionCoordinator`
    /// issued for them.
    let runtime: MobileRuntimeContext
    /// The account data store the runtime opened on.
    let storeIdentifier: UUID
    let authority: DirectAuthority
}

/// The tags one direct stream is opened under. A stream is used only while
/// every one of them is still current.
struct DirectStreamTag: Equatable, Sendable {
    let target: DirectTarget
    /// The `generation` of the rendezvous answer the path was found with.
    let rendezvousGeneration: String
    /// Issued by `DirectPathController`. Advances at every event of the
    /// path-generation table (docs/plans/remote-direct-ios.md section 7).
    let pathGeneration: Int
}

enum DirectPathReport: Equatable, Sendable {
    /// A path to the target's instance is usable. `generation` is the
    /// rendezvous answer's.
    case usable(generation: String)
    /// The network changed and the transport is validating the path again.
    /// New connections prefer the public origin meanwhile; nothing is closed.
    case verifying
    /// The path is gone, or its validation failed. The App then closes the
    /// relays the path carried and counts them as one closure per host. A
    /// stream that failed on its own before this report is not counted: its
    /// page already saw that connection fail, and no event closed it.
    case lost
    /// The App went to the background: the transport stopped, and the path
    /// it reported usable is gone. The App handles it as its own background
    /// (`remote-direct-ios.md` section 9), whichever of the two heard the
    /// background first, so its closure waits for the return.
    case lostToBackground
    /// The usable path's connection now runs on another kind of path:
    /// through the relay, or straight to an IPv4 or IPv6 address. Nothing
    /// is closed; the page is told, and new connections may stay on a direct
    /// path (section 6, stickiness).
    case pathChanged(DirectPathKind?)
}

/// One byte stream to the instance, as the strict ingress sees it
/// (docs/plans/remote-direct-ingress.md section 3).
protocol DirectByteStream: AnyObject, Sendable {
    /// The next bytes from the instance, or `nil` once it has finished
    /// sending.
    func read(maxLength: Int) async throws -> Data?
    /// Returns once the transport has accepted `data`. Not returning is the
    /// backpressure.
    func write(_ data: Data) async throws
    /// This side has finished sending. Reads continue.
    func closeWrite() async throws
    /// Ends both directions at once. A `read` or `write` in progress throws.
    func cancel()
}

/// The kind of path a direct connection runs on: through a relay, or
/// straight to an IPv4 or IPv6 address. Families only, never an address.
enum DirectPathKind: String, Equatable, Sendable {
    case relay
    case ipv4
    case ipv6

    /// The family of a numeric socket address (`a.b.c.d:port` or
    /// `[x:...:x]:port`), or nil for anything else.
    static func family(ofSocketAddress address: String) -> DirectPathKind? {
        if address.hasPrefix("[") {
            return .ipv6
        }
        let host = address.split(separator: ":").first.map(String.init) ?? ""
        let octets = host.split(separator: ".")
        return octets.count == 4 && octets.allSatisfy({ UInt8($0) != nil }) ? .ipv4 : nil
    }
}

/// How many candidate addresses of each family. Counts only.
struct DirectAddressFamilies: Equatable, Sendable {
    var ipv4 = 0
    var ipv6 = 0

    init(ipv4: Int = 0, ipv6: Int = 0) {
        self.ipv4 = ipv4
        self.ipv6 = ipv6
    }

    init(socketAddresses: [String]) {
        for address in socketAddresses {
            switch DirectPathKind.family(ofSocketAddress: address) {
            case .ipv4: ipv4 += 1
            case .ipv6: ipv6 += 1
            default: break
            }
        }
    }

    var jsonObject: [String: Any] {
        ["ipv4": ipv4, "ipv6": ipv6]
    }
}

/// One connection attempt to the instance, for diagnosis: what the
/// instance's answer offered, what this device could offer, and which path
/// won. No address, key, or identity.
struct DirectDialAttempt: Equatable, Sendable {
    /// The instance's candidate addresses, by family.
    let offered: DirectAddressFamilies
    /// Whether the answer named a relay.
    let offeredRelay: Bool
    /// This device's own candidate addresses when the attempt ended, if
    /// the endpoint was bound.
    let local: DirectAddressFamilies?
    /// The path the connection ran on when it was found usable; nil if the
    /// attempt failed.
    let path: DirectPathKind?

    var jsonObject: [String: Any] {
        var object: [String: Any] = [
            "offered": offered.jsonObject,
            "offered_relay": offeredRelay,
            "result": path?.rawValue ?? "failed",
        ]
        if let local {
            object["local"] = local.jsonObject
        }
        return object
    }
}

/// One CONNECT's race between the direct path and the public origin, for
/// diagnosis: how long each took from the CONNECT's plan to being ready, or
/// what became of it, which won, and the App's path then. Timings and
/// families only.
struct DirectRaceRecord: Equatable, Sendable {
    enum Leg: Equatable, Sendable {
        /// Not part of this race: no direct candidate, or the public origin
        /// was never dialled.
        case none
        /// Ready after this many milliseconds.
        case ready(Int)
        case failed
        /// Started, and cancelled when the other won.
        case cancelled

        var jsonValue: Any {
            switch self {
            case .none: return "none"
            case .ready(let ms): return ms
            case .failed: return "failed"
            case .cancelled: return "cancelled"
            }
        }
    }

    let direct: Leg
    let publicOrigin: Leg
    /// `direct`, `public`, or nil when neither won.
    let winner: DirectRelayBackend?
    let path: DirectPathKind?
    /// The plan went straight to the direct path (section 6, stickiness).
    let sticky: Bool

    var jsonObject: [String: Any] {
        [
            "direct": direct.jsonValue,
            "public": publicOrigin.jsonValue,
            "winner": winner.map { $0 == .direct ? "direct" : "public" } ?? "none",
            "path": path.map { $0.rawValue as Any } ?? NSNull(),
            "sticky": sticky,
        ]
    }
}

/// Committed direct relays the App closed because their path was lost (or
/// replaced, or the App went to the background), for diagnosis: how many,
/// and for each how long it had carried no byte when it was closed. A short
/// time means bytes were moving, so a request was probably cut; the listener
/// cannot see requests themselves. Counts and durations only.
struct DirectLossRecord: Equatable, Sendable {
    let relays: Int
    /// Milliseconds since each relay's last byte, shortest first, at most
    /// `quietLimit` of them.
    let quietMs: [Int]
    /// The kind of path the App's connection ran on before the loss.
    let path: DirectPathKind?

    static let quietLimit = 8

    var jsonObject: [String: Any] {
        [
            "relays": relays,
            "last_byte_ms": quietMs,
            "path": path.map { $0.rawValue as Any } ?? NSNull(),
        ]
    }
}

/// What the transport can say about its path, for diagnosis.
struct DirectPathDiagnostics: Equatable, Sendable {
    /// The path the live connection runs on now, if one is usable.
    var current: DirectPathKind?
    /// The last few attempts, oldest first.
    var attempts: [DirectDialAttempt] = []

    static let empty = DirectPathDiagnostics(current: nil, attempts: [])
    /// How many attempts are kept.
    static let attemptLimit = 5
}

/// "Open a byte stream to the selected instance."
protocol DirectTransporting: AnyObject, Sendable {
    /// A runtime opened for `target`. The transport stops any work for the
    /// previous target and tells `report` whenever this target's path
    /// becomes usable or is lost. A report that arrives after the next
    /// `select` or `deselect` is dropped by the controller, so the transport
    /// need not suppress it.
    @MainActor
    func select(
        _ target: DirectTarget,
        report: @escaping @MainActor (DirectPathReport) -> Void
    )

    /// No instance is selected: stop probing and close every stream.
    @MainActor
    func deselect()

    /// A direct candidate opened under `tag` failed before anything was
    /// forwarded: the stream did not open, the instance refused the CONNECT,
    /// or it did not answer in time. That connection went to the public
    /// origin. The transport decides when to probe again.
    @MainActor
    func pathDidFail(for tag: DirectStreamTag)

    /// Opens one stream over the path reported usable for `tag`. Throws when
    /// that path is gone, and promptly when its task is cancelled.
    func openStream(for tag: DirectStreamTag) async throws -> any DirectByteStream

    /// The path the live connection runs on, and the last attempts.
    @MainActor
    var pathDiagnostics: DirectPathDiagnostics { get }
}

extension DirectTransporting {
    @MainActor
    var pathDiagnostics: DirectPathDiagnostics {
        .empty
    }
}
