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

    /// Whether `host` is the parent domain of one of `routed`: that host
    /// without its first label, as `avibe.bot` is of an instance at
    /// `name.avibe.bot`. Exactly that: no grandparent, and never a single
    /// label (a top-level domain).
    ///
    /// WebKit sends a route's proxy not only the hosts `matchDomains` lists
    /// and their subdomains but also their parent domain (measured
    /// 2026-11-28, iOS 26.5 simulator, branch `experiment/webkit-rc6-gate`,
    /// runs 46145770889 and 46147194369: a sibling host never came, the
    /// parent always did, and `excludedDomains` with the parent took the
    /// routed host off the route with it). Refusing the parent was harmless
    /// only while the route allowed fail-over. Under 「仅直连和中继」 it does
    /// not, and the page's requests to avibe.bot (voice) failed.
    ///
    /// Being a parent is spelling, not ownership: the parent of a routed
    /// host can be anyone's. The controller carries one only when it is
    /// also the control plane's own host (`DirectPathController.plan`).
    static func isParentDomain(_ host: String, ofAny routed: some Sequence<String>) -> Bool {
        host.contains(".") && routed.contains { name in
            name.firstIndex(of: ".").map { name[name.index(after: $0)...] == host } ?? false
        }
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

/// Why a usable path was lost, as far as the transport can tell. The raw
/// value is the word the App reports (`DirectPathUse.RetiredRule`).
///
/// A decision of our own, or a close the instance sent, is not a death of
/// the path: `linkEnd` says which of these raise the probation level
/// (docs/plans/remote-direct-probation.md section 2).
enum DirectLossCause: String, Equatable, Sendable {
    /// The connection timed out idle: nothing came from the instance.
    case idle
    /// The connection ended some other way with no close from the instance
    /// (a reset, or a cause this build does not know).
    case connectionEnded = "connection_ended"
    /// A probe got no answer on a connection still open: after a direct
    /// candidate failed, after a renewed answer, or on probation.
    case probeTimeout = "timeout"
    /// The instance closed the connection, saying the admission ended.
    case revocation
    /// The instance closed the connection for another reason of its own
    /// (it is stopping, or the connection carried nothing for too long).
    case closedByPeer = "closed_by_peer"
    /// The newest rendezvous answer offers no direct path.
    case notReady = "not_ready"
    /// A newer rendezvous answer replaced the endpoint.
    case superseded
    /// The probe that a change of network started failed.
    case networkChange = "network_change"
    /// This App closed the connection itself.
    case withdrawn = "other"

    /// The cause a connection's own close text gives. Both forms the
    /// binding may hand over are read: the error's display text ("timed
    /// out", "closed by peer: admission ended (code 1)", "closed") and its
    /// debug form ("TimedOut", "ApplicationClosed(…)", "LocallyClosed").
    /// Only a typed application close counts as the instance's own.
    init(closeText text: String) {
        if text.hasPrefix("closed by peer") || text.contains("ApplicationClosed") {
            self = text.contains("admission") ? .revocation : .closedByPeer
        } else if text == "timed out" || text.contains("TimedOut") {
            self = .idle
        } else if text == "closed" || text.contains("LocallyClosed") {
            self = .withdrawn
        } else {
            self = .connectionEnded
        }
    }

    /// What the probation rule is told.
    var linkEnd: DirectLinkEnd {
        switch self {
        case .idle, .connectionEnded, .probeTimeout:
            return .lost
        case .revocation, .closedByPeer, .notReady, .withdrawn:
            return .instanceEnded
        case .superseded:
            return .superseded
        case .networkChange:
            return .networkChange
        }
    }
}

enum DirectPathReport: Equatable, Sendable {
    /// A validated answer from the selected instance ended without an admitted link.
    case answeredWithoutLink
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
    case lost(DirectLossCause)
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

/// The path the App's direct connection has selected, for display: its
/// kind, whether the far end is on a local network, and its round-trip time.
/// A class and a number, never an address.
struct DirectSelectedPath: Equatable, Sendable {
    let kind: DirectPathKind
    /// For a path to an IPv4 or IPv6 address: whether that address is
    /// private, unique-local, or link-local (`true`), or anything else
    /// (`false`). Nil through the relay.
    let local: Bool?
    /// The path's round-trip time in milliseconds; nil until measured.
    let rttMs: Int?
    /// Through the relay only: the region of the relay this path runs
    /// through, when it is one of the known public ones
    /// (`relayRegion(ofRelayURL:)`). Nil for any other relay, and for a
    /// path to an address. A label, never the relay's address.
    let relayRegion: String?

    /// From one of the connection's paths, as the binding reports it. Nil
    /// for a path that is neither the relay nor a numeric address.
    init?(isRelay: Bool, isIp: Bool, remoteAddress: String, rttMs: UInt64) {
        if isRelay {
            kind = .relay
            local = nil
            // For a relay path the binding's remote address is the relay's
            // URL: the one this path is selected on, not one configured.
            relayRegion = Self.relayRegion(ofRelayURL: remoteAddress)
        } else if isIp, let family = DirectPathKind.family(ofSocketAddress: remoteAddress) {
            kind = family
            local = Self.isLocalNetwork(socketAddress: remoteAddress)
            relayRegion = nil
        } else {
            return nil
        }
        // Zero is what the binding reports before a measurement.
        self.rttMs = rttMs > 0 ? Int(min(rttMs, UInt64(Int.max))) : nil
    }

    init(kind: DirectPathKind, local: Bool?, rttMs: Int?, relayRegion: String? = nil) {
        self.kind = kind
        self.local = local
        self.rttMs = rttMs
        self.relayRegion = kind == .relay ? relayRegion : nil
    }

    /// The region label of one of the four public relays, by its URL, as
    /// the desktop app names them (`desktop/src-tauri/src/direct/measure.rs`).
    /// Nil for anything else: a region is never guessed from a name that is
    /// not exactly one of the relays a dial may use
    /// (`DirectTrialRelay.publicTrialRelayURLs`).
    static func relayRegion(ofRelayURL url: String) -> String? {
        relayEndpoint(url).flatMap { relayRegions[$0] }
    }

    /// A relay's URL as one comparable name: its host in lower case without
    /// the root dot. Nil unless the URL is that relay's own root: `https`,
    /// the default port (written or not), no path beyond `/`, and no user,
    /// query or fragment. Another port or path on the same host is another
    /// service, not that relay.
    private static func relayEndpoint(_ url: String) -> String? {
        guard
            let components = URLComponents(string: url),
            components.scheme?.lowercased() == "https",
            components.user == nil,
            components.password == nil,
            components.query == nil,
            components.fragment == nil,
            components.port == nil || components.port == 443,
            components.path.isEmpty || components.path == "/",
            var host = components.host?.lowercased(),
            !host.isEmpty
        else {
            return nil
        }
        if host.hasSuffix(".") {
            host.removeLast()
        }
        return host
    }

    /// Each relay a dial may use, by `relayEndpoint`, with its region: the
    /// first label of its name up to the hyphen (`euc1-1.…` is `euc1`).
    private static let relayRegions: [String: String] = {
        var regions: [String: String] = [:]
        for url in DirectTrialRelay.publicTrialRelayURLs {
            guard
                let endpoint = relayEndpoint(url),
                let region = endpoint.split(separator: "-").first
            else {
                continue
            }
            regions[endpoint] = String(region)
        }
        return regions
    }()

    /// Whether a numeric socket address (`a.b.c.d:port` or `[x:...:x]:port`)
    /// is on a local network: IPv4 10/8, 172.16/12, 192.168/16, 169.254/16,
    /// and 100.64/10 (shared address space: Tailscale, or a carrier's NAT);
    /// IPv6 fc00::/7 (unique local), fe80::/10 (link-local), or an
    /// IPv4-mapped address of a local IPv4 one.
    static func isLocalNetwork(socketAddress address: String) -> Bool {
        if address.hasPrefix("[") {
            let inner = address.dropFirst().prefix { $0 != "]" }
            let host = inner.prefix { $0 != "%" }.lowercased()
            if host.hasPrefix("::ffff:") {
                return isLocalIPv4(String(host.dropFirst("::ffff:".count)))
            }
            guard
                let first = host.split(separator: ":", omittingEmptySubsequences: false).first,
                !first.isEmpty,
                let hextet = UInt16(first, radix: 16)
            else {
                return false
            }
            return hextet & 0xFE00 == 0xFC00 || hextet & 0xFFC0 == 0xFE80
        }
        return isLocalIPv4(address.split(separator: ":").first.map(String.init) ?? "")
    }

    private static func isLocalIPv4(_ host: String) -> Bool {
        let octets = host.split(separator: ".").compactMap { UInt8($0) }
        guard octets.count == 4 else {
            return false
        }
        switch (octets[0], octets[1]) {
        case (10, _), (192, 168), (169, 254):
            return true
        case (172, let second):
            return (16...31).contains(second)
        case (100, let second):
            return (64...127).contains(second)
        default:
            return false
        }
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
        /// A sticky direct attempt that did not answer within its bound.
        /// No failure of the path is claimed for it.
        case timedOut
        /// Started, and cancelled when the other won.
        case cancelled

        var jsonValue: Any {
            switch self {
            case .none: return "none"
            case .ready(let ms): return ms
            case .failed: return "failed"
            case .timedOut: return "timeout"
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
    enum Cause: String, Equatable, Sendable {
        /// The transport reported the path lost. The one cause that says a
        /// request in flight was probably cut.
        case lost
        /// A newer rendezvous answer replaced the endpoint the relays ran on.
        case generationReplaced = "generation_replaced"
        /// The App went to the background.
        case background
    }

    let cause: Cause
    let relays: Int
    /// Milliseconds since each relay's last byte, shortest first, at most
    /// `quietLimit` of them.
    let quietMs: [Int]
    /// The kind of path the App's connection ran on before the loss.
    let path: DirectPathKind?

    static let quietLimit = 8

    var jsonObject: [String: Any] {
        [
            "cause": cause.rawValue,
            "relays": relays,
            "last_byte_ms": quietMs,
            "path": path.map { $0.rawValue as Any } ?? NSNull(),
        ]
    }
}

/// How often the transport checked its path again, and why, since the
/// instance was selected. For diagnosis: counts only.
struct DirectPathCounts: Equatable, Sendable {
    /// Times a usable path was suspended to be checked again (`verifying`).
    var verifications = 0
    /// Probes started after a network path update.
    var probesAfterPathUpdate = 0
    /// Probes started after a direct candidate failed.
    var probesAfterCandidateFailure = 0
    /// Probes started after an answer that had run out was renewed.
    var probesAfterRenewal = 0
    /// Connections to the instance attempted. One that connects sends one
    /// probe of its own, not counted above.
    var dials = 0
    /// Updates of the system's network path that were acted on.
    var pathUpdates = 0
    /// Updates of the system's network path that changed nothing the
    /// transport depends on, and were ignored.
    var pathUpdatesIgnored = 0

    static let zero = DirectPathCounts()

    var jsonObject: [String: Any] {
        [
            "verifications": verifications,
            "probes": [
                "path_update": probesAfterPathUpdate,
                "candidate_failed": probesAfterCandidateFailure,
                "renewal": probesAfterRenewal,
            ],
            "dials": dials,
            "path_updates": pathUpdates,
            "path_updates_ignored": pathUpdatesIgnored,
        ]
    }
}

/// What the transport can say about its path, for diagnosis.
struct DirectPathDiagnostics: Equatable, Sendable {
    /// The path the live connection runs on now, if one is usable.
    var current: DirectPathKind?
    /// The last few attempts, oldest first.
    var attempts: [DirectDialAttempt] = []
    /// The selected path's class and round-trip time, read with `current`.
    var selected: DirectSelectedPath? = nil

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

    /// The path reported usable is waiting on probation and carries
    /// nothing: ask it once whether it still answers. A failure is reported
    /// as `lost`; a pass reports nothing.
    @MainActor
    func probeWaitingPath()

    /// Opens one stream over the path reported usable for `tag`. Throws when
    /// that path is gone, and promptly when its task is cancelled.
    func openStream(for tag: DirectStreamTag) async throws -> any DirectByteStream

    /// The path the live connection runs on, and the last attempts.
    @MainActor
    var pathDiagnostics: DirectPathDiagnostics { get }

    /// How often the path was checked again, and why.
    @MainActor
    var pathCounts: DirectPathCounts { get }

    /// The path the usable connection runs on now, read now, with its
    /// mark: for a measurement. Nil with no usable connection, once the
    /// answer it was admitted under has run out (by the clock at this
    /// read, not by a timer having run), with none of its paths selected or
    /// one that has no kind, or when the transport cannot watch its paths.
    @MainActor
    var markedPath: DirectMarkedPath? { get }
}

/// A mark of the path a transport's connection runs on, for a measurement
/// (`DirectMeasure.swift`). Two equal marks mean: the same connection, and
/// the same path selected at every observation from the first to the
/// second. An observation is a read of the connection's selected path made
/// then: when the mark is read, and whenever the watch of the connection's
/// paths reports. A report after which the selected path is the one it was
/// changes nothing: the connection opens and closes other paths all the
/// time while it looks for a better one. Another path of the same kind
/// (another relay, another address) is another path. Counts only: which
/// path it is, by its address or its relay, stays in the transport.
///
/// Not seen: a path that changed and changed back between two
/// observations. The binding's watch reports once for changes that follow
/// each other closely, so the read a report causes may find the path there
/// was before.
struct DirectPathMark: Equatable, Sendable {
    /// Which of the transport's connections.
    let connection: Int
    /// How often an observation found another path selected than the one
    /// before it.
    let moves: Int
}

/// A path with its mark, both from one observation: the labels are the
/// marked path's own, whatever the controller has been told so far.
struct DirectMarkedPath: Equatable, Sendable {
    let mark: DirectPathMark
    let path: DirectSelectedPath
}

extension DirectTransporting {
    @MainActor
    var markedPath: DirectMarkedPath? {
        nil
    }

    @MainActor
    var pathCounts: DirectPathCounts {
        .zero
    }

    @MainActor
    var pathDiagnostics: DirectPathDiagnostics {
        .empty
    }
}
