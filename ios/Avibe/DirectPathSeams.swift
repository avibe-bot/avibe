import Foundation

// The App's side of two features of the direct path controller, built
// before the controller's side lands: 「仅直连和中继」 (direct only) and
// 「线路测速」 (`direct.measure()`). The controller conforms to these
// protocols; until it does, the App offers neither (no capability, and the
// bridge refuses both). The calls are the controller's contract, as the
// direct lane settled them on 2026-11-24 and 11-25.

/// 「仅直连和中继」 for one instance host.
@MainActor
protocol DirectOnlyControlling: AnyObject {
    /// Sets the mode for the host, from this device's stored choice: before
    /// the runtime's route is prepared, at selection and restore, and again
    /// at every change of the choice. Idempotent. Enabling or disabling it
    /// closes the host's connections and rewrites its route; the App reloads
    /// the instance's view after it.
    func setDirectOnly(_ enabled: Bool, forInstanceURL url: URL)
    /// The host loaded without a serving route to the listener in this
    /// process: enabling recorded the choice but neither links nor cuts
    /// over until the next launch.
    func directOnlyNeedsRelaunch(forInstanceURL url: URL) -> Bool
    /// One per foreground drop that ended the strict state and entered
    /// re-establishment; cumulative for the controller's lifetime.
    func strictFailureCount(forInstanceURL url: URL) -> UInt64
    /// The direct path (direct or relay) is up for the host now.
    func isDirectOnlyLinked(forInstanceURL url: URL) -> Bool
    /// Rendezvous the instance answered that ended without a link;
    /// cumulative for the controller's lifetime.
    func unlinkedRendezvousCount(forInstanceURL url: URL) -> UInt64
    /// The shell is about to load the instance in a WebView on the account
    /// store: called synchronously at the decision, never from a gate read.
    func noteInstanceLoad(forInstanceURL url: URL)
    /// The same for the Action Button's hidden view, whose load must
    /// arrive: where the host's route could not carry it now, the route is
    /// opened and the choice waits for the next launch.
    func noteVoiceLoad(forInstanceURL url: URL)
}

/// 「线路测速」: the controller measures both paths itself.
@MainActor
protocol DirectPathMeasuring: AnyObject {
    /// Bounded by the controller at about 30 s.
    func measurePaths(forInstanceURL url: URL) async -> DirectMeasureResult
}

/// What `measurePaths` answers.
enum DirectMeasureResult: Equatable, Sendable {
    case measured(DirectMeasurement)
    /// Another measurement is running.
    case busy
}

/// One measurement of both paths, as `direct.measure()` answers it
/// (docs: the protocol of 2026-11-25). Numbers and labels only: no address,
/// port, identifier or certificate detail.
struct DirectMeasurement: Equatable, Sendable {
    /// When it finished.
    let measuredAt: Date
    /// The direct or relay path in use now; `nil` when there is none.
    let current: DirectMeasureLeg?
    /// The public origin through Cloudflare; `nil` only if it could not be
    /// attempted at all.
    let tunnel: DirectMeasureLeg?

    static let basis = "warm-https-health"
    static let samples = 5

    var jsonObject: [String: Any] {
        [
            "measuredAt": Int((measuredAt.timeIntervalSince1970 * 1_000).rounded()),
            "basis": Self.basis,
            "samples": Self.samples,
            "current": current?.jsonObject ?? NSNull(),
            "tunnel": tunnel?.jsonObject ?? NSNull(),
        ]
    }
}

struct DirectMeasureLeg: Equatable, Sendable {
    enum Kind: String, Equatable, Sendable {
        case relay, ipv4, ipv6, tunnel
    }

    enum Scope: String, Equatable, Sendable {
        /// The same network.
        case local
        /// Punched across networks.
        case global
    }

    enum Failure: String, Equatable, Sendable {
        case failed, timeout
    }

    let kind: Kind
    /// For `ipv4` and `ipv6` only.
    let scope: Scope?
    /// For `relay` only, when known.
    let relayRegion: String?
    /// Timed samples that completed.
    let ok: Int
    let medianMs: Int?
    let maxMs: Int?
    /// The first non-success's kind, when `ok` is below the samples.
    let error: Failure?

    var jsonObject: [String: Any] {
        [
            "kind": kind.rawValue,
            "scope": scope?.rawValue ?? NSNull(),
            "relayRegion": relayRegion ?? NSNull(),
            "ok": ok,
            "medianMs": medianMs ?? NSNull(),
            "maxMs": maxMs ?? NSNull(),
            "error": error?.rawValue ?? NSNull(),
        ]
    }
}

/// What the App answers `direct.measure()` with.
enum DirectMeasureReply: Equatable, Sendable {
    case measured(DirectMeasurement)
    /// A measurement is already running (the App's or the controller's).
    case busy
    /// The controller did not answer within the App's bound.
    case timedOut
}

/// Runs `direct.measure()` for the App: one at a time, and bounded just
/// above the controller's own 30 s, so a controller that never answers
/// cannot leave the page waiting.
@MainActor
final class DirectMeasureRunner {
    nonisolated static let bound: Duration = .seconds(35)

    private let measuring: DirectPathMeasuring
    private let bound: Duration
    private var running = false

    init(measuring: DirectPathMeasuring, bound: Duration = DirectMeasureRunner.bound) {
        self.measuring = measuring
        self.bound = bound
    }

    func measure(forInstanceURL url: URL) async -> DirectMeasureReply {
        guard !running else {
            return .busy
        }
        running = true
        defer { running = false }
        let answer = DirectMeasureAnswer()
        let measuring = measuring
        let work = Task { @MainActor in
            let result = await measuring.measurePaths(forInstanceURL: url)
            answer.settle(result)
        }
        let deadline = Task { @MainActor [bound] in
            try? await Task.sleep(for: bound)
            answer.settle(nil)
        }
        let result = await answer.value()
        work.cancel()
        deadline.cancel()
        switch result {
        case .measured(let measurement)?:
            return .measured(measurement)
        case .busy?:
            return .busy
        case nil:
            return .timedOut
        }
    }
}

/// The first of the measurement and the bound; the other is ignored.
@MainActor
private final class DirectMeasureAnswer {
    private var settled = false
    private var result: DirectMeasureResult?
    private var waiter: CheckedContinuation<DirectMeasureResult?, Never>?

    func settle(_ result: DirectMeasureResult?) {
        guard !settled else {
            return
        }
        settled = true
        self.result = result
        waiter?.resume(returning: result)
        waiter = nil
    }

    func value() async -> DirectMeasureResult? {
        if settled {
            return result
        }
        return await withCheckedContinuation { waiter = $0 }
    }
}

/// When 「仅直连和中继」 goes back to automatic by itself, for one instance in
/// one App process: the rules the direct lane and the Devices lead set on
/// 2026-11-24, the same on Desktop. Only on evidence that this phone reached
/// the instance (or, for the listener rule, that its own listener could not
/// serve while the network was there): never on a listing, a network status
/// or a timer alone.
///
/// - **a1** In one bootstrap, the instance answered two rendezvous that
///   ended without a link.
/// - **a2** In one bootstrap, a fresh page of the instance (not the cached
///   one) answered, and no link came within 20 s of it.
/// - **b** Three drops (`strictFailureCount` increments) within 10 minutes
///   arm the revert; it is made at the next evidence that the instance
///   answers in a bootstrap (an unlinked rendezvous, a fresh page, or the
///   link). A phone that drops three times for want of a network keeps its
///   choice until the instance answers again; the revert is made then.
/// - **c** The listener could not serve in the foreground for 60 s from
///   its first failed attempt that counts. Not counted: a launch's or a
///   return's first attempt, the background, a failure that only means
///   there is no network (`waiting`), and anything before a successful
///   attempt. The caller tracks that instant (`listenerDownSince`).
///
/// A bootstrap begins at an open, at a return to the foreground, and at
/// each drop. Nothing is reloaded when the link arrives: the controller
/// closes the host's tunnel connections and tells the page, which opens its
/// connections again on the link (`DirectPathController.cutover`).
struct DirectOnlyRevertRules: Equatable {
    static let unlinkedRendezvousLimit: UInt64 = 2
    static let pageWithoutLink: Duration = .seconds(20)
    static let dropLimit = 3
    static let dropWindow: Duration = .seconds(10 * 60)
    static let listenerDown: Duration = .seconds(60)

    /// What the controller and the listener say at one moment.
    struct Observation: Equatable {
        var now: ContinuousClock.Instant
        var linked: Bool
        var strictFailures: UInt64
        var unlinkedRendezvous: UInt64
        var listenerDownSince: ContinuousClock.Instant?
    }

    enum Decision: Equatable {
        /// Keep the choice; look again by `nextCheck` if nothing else
        /// happens first.
        case keep(nextCheck: ContinuousClock.Instant?)
        /// Back to automatic, recording when and from what (a1, a2, c):
        /// the page says so under the choice.
        case revert
        /// Back to automatic with nothing recorded (b): the page shows
        /// automatic and no line.
        case revertWithoutNotice
    }

    private var unlinkedBase: UInt64?
    private var freshPageAt: ContinuousClock.Instant?
    private var lastStrictFailures: UInt64?
    private var drops: [ContinuousClock.Instant] = []
    private var armed = false
    /// The link came up in this bootstrap: it succeeded, and a1 and a2 no
    /// longer apply to it. A later loss is the controller's to count (a
    /// drop, which begins a new bootstrap).
    private var establishedInBootstrap = false

    /// A new bootstrap: an open or a return to the foreground. (A drop
    /// begins one by itself, seen in `observe`.) Counts are the
    /// controller's at this moment.
    mutating func beginBootstrap(unlinkedRendezvous: UInt64, linked: Bool) {
        unlinkedBase = unlinkedRendezvous
        freshPageAt = nil
        establishedInBootstrap = linked
    }

    /// A fresh response of the instance's page reached this phone. The
    /// first in a bootstrap starts rule a2's 20 s.
    mutating func noteFreshPage(at now: ContinuousClock.Instant) {
        if freshPageAt == nil {
            freshPageAt = now
        }
    }

    /// The view left the instance's page for a sign-in page: the link
    /// cannot come while it is away (its rendezvous is asked from the
    /// instance's page), so a2's 20 s do not run. They start again at the
    /// next fresh page.
    mutating func noteLeftInstancePage() {
        freshPageAt = nil
    }

    mutating func observe(_ seen: Observation) -> Decision {
        // A drop: counted, and a new bootstrap from here.
        if let last = lastStrictFailures, seen.strictFailures > last {
            let count = min(seen.strictFailures - last, UInt64(Self.dropLimit))
            drops.append(contentsOf: repeatElement(seen.now, count: Int(count)))
            beginBootstrap(unlinkedRendezvous: seen.unlinkedRendezvous, linked: seen.linked)
        }
        lastStrictFailures = seen.strictFailures
        drops.removeAll { $0.duration(to: seen.now) > Self.dropWindow }
        if drops.count >= Self.dropLimit {
            armed = true
        }
        if unlinkedBase == nil {
            unlinkedBase = seen.unlinkedRendezvous
        }
        let base = unlinkedBase ?? seen.unlinkedRendezvous
        let unlinked = seen.unlinkedRendezvous > base ? seen.unlinkedRendezvous - base : 0

        // c
        if let since = seen.listenerDownSince,
           since.duration(to: seen.now) >= Self.listenerDown
        {
            return .revert
        }
        // b: armed, and the instance answered in this bootstrap.
        if armed, unlinked > 0 || freshPageAt != nil || seen.linked {
            return .revertWithoutNotice
        }
        if seen.linked {
            establishedInBootstrap = true
        }
        if !seen.linked, !establishedInBootstrap {
            // a1
            if unlinked >= Self.unlinkedRendezvousLimit {
                return .revert
            }
            // a2
            if let page = freshPageAt, page.duration(to: seen.now) >= Self.pageWithoutLink {
                return .revert
            }
        }
        var next: ContinuousClock.Instant?
        if !seen.linked, !establishedInBootstrap, let page = freshPageAt {
            next = page.advanced(by: Self.pageWithoutLink)
        }
        if let since = seen.listenerDownSince {
            let due = since.advanced(by: Self.listenerDown)
            next = next.map { min($0, due) } ?? due
        }
        return .keep(nextCheck: next)
    }
}

/// Rule c's clock: from the listener's first failed attempt that counts,
/// with the exclusions applied here (the controller's recovery reports
/// every failure). Not counted: the first attempt of a recovery that began
/// at a return or at the launch (a launch whose listener never serves
/// counts from its second failed attempt, so a direct-only phone is never
/// held for good), as opposed to one that began with a failure while
/// serving in front; and a failure that only means there is no network
/// (`waiting`), which also starts the count over. The background is
/// excluded by the caller, which resets it there.
struct DirectOnlyListenerClock: Equatable {
    private var episode: ContinuousClock.Instant?
    private var firstFailureAt: ContinuousClock.Instant?
    private(set) var downSince: ContinuousClock.Instant?

    /// `recovery` is the controller's while the listener does not serve
    /// (`since`, and the last failure's instant and whether it was
    /// `waiting`); nil while it serves.
    mutating func observe(
        recovery: (since: ContinuousClock.Instant, failureAt: ContinuousClock.Instant?, waiting: Bool)?
    ) -> ContinuousClock.Instant? {
        guard let recovery else {
            reset()
            return nil
        }
        if episode != recovery.since {
            episode = recovery.since
            firstFailureAt = nil
            downSince = nil
        }
        guard let failureAt = recovery.failureAt else {
            return downSince
        }
        if recovery.waiting {
            // No network: nothing counts, and the count starts over.
            firstFailureAt = nil
            downSince = nil
            return nil
        }
        if firstFailureAt == nil {
            firstFailureAt = failureAt
        }
        // A recovery that began with this failure (in front, while serving)
        // counts from it; one that began at a return counts from its
        // second failed attempt.
        let opensEpisode = failureAt == recovery.since
        if opensEpisode || failureAt > (firstFailureAt ?? failureAt) {
            downSince = downSince ?? failureAt
        }
        return downSince
    }

    /// The background, or the listener serving again.
    mutating func reset() {
        episode = nil
        firstFailureAt = nil
        downSince = nil
    }
}

/// Called by the instance's view at each load of the instance it starts
/// (`DirectOnlyControlling.noteInstanceLoad`).
typealias DirectInstanceLoadNote = @MainActor () -> Void

/// The Action Button's hidden WebView (`VoiceCaptureInstanceClient`) tells
/// the direct path of each load of the instance it makes, as the page's view
/// does (`DirectOnlyControlling.noteInstanceLoad`), with its take's WebKit
/// store. Set by the App's model; `nil` without 「仅直连和中继」 in the path
/// controller. Called synchronously at the decision, before the load.
@MainActor
enum VoiceCaptureInstanceLoads {
    static var note: ((WebOrigin, UUID) -> Void)?
}
