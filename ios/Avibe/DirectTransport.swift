import Foundation

// The seam between the direct transport (iroh, the rendezvous, probing: not
// in the App yet) and the parts that route WebKit onto it. Nothing in the App
// implements `DirectTransporting`, so nothing below it ever activates.
// Design: docs/plans/remote-direct-ios.md.

/// The one authority a direct route serves: the selected instance's exact
/// host, on port 443.
struct DirectAuthority: Equatable, Sendable {
    let host: String

    /// `nil` unless `instanceURL` is an HTTPS origin on the default port
    /// whose host is a DNS name. The instance's ingress serves nothing else,
    /// and the host is written into a request head, so it may hold only
    /// host-name characters.
    init?(instanceURL: URL) {
        guard
            let origin = WebOrigin(url: instanceURL),
            origin.port == nil,
            Self.isDNSName(origin.host)
        else {
            return nil
        }
        host = origin.host
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

/// The tags one route carries. A route is installed only while every one of
/// them is still current.
struct DirectRouteTag: Equatable, Sendable {
    let target: DirectTarget
    /// The `generation` of the rendezvous answer the path was found with.
    let rendezvousGeneration: String
}

enum DirectPathReport: Equatable, Sendable {
    /// A path to the target's instance is usable. `generation` is the
    /// rendezvous answer's.
    case usable(generation: String)
    /// The path is gone, or was never found.
    case lost
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

    /// The App is on the tunnel although this transport reported the path
    /// for `tag` usable: a stream did not open, the instance refused the
    /// CONNECT, the listener failed, or the route could not be installed.
    /// The transport decides when to probe again. Until it reports the path
    /// usable once more, the App stays on the tunnel.
    @MainActor
    func pathDidFail(for tag: DirectRouteTag)

    /// Opens one stream over the path reported usable for `tag`. Throws when
    /// that path is gone, and promptly when its task is cancelled.
    func openStream(for tag: DirectRouteTag) async throws -> any DirectByteStream
}
