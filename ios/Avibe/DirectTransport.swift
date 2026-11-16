import Foundation

// The seam between the direct transport (iroh, the rendezvous, probing: not
// in the App yet) and the local proxy that carries WebKit's connections.
// Nothing in the App implements `DirectTransporting` and `AvibeApp` registers
// none, so none of this runs: no listener, no route write, no `direct`
// capability, no transport work.
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

    /// A direct candidate opened under `tag` failed before anything was
    /// forwarded: the stream did not open, the instance refused the CONNECT,
    /// or it did not answer in time. That connection went to the public
    /// origin. The transport decides when to probe again.
    @MainActor
    func pathDidFail(for tag: DirectStreamTag)

    /// Opens one stream over the path reported usable for `tag`. Throws when
    /// that path is gone, and promptly when its task is cancelled.
    func openStream(for tag: DirectStreamTag) async throws -> any DirectByteStream
}
