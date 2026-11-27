import Foundation

/// One connection a page opened through the App's listener, as the
/// listener settled it: which leg carried it, how the plan chose, and what
/// became of the direct leg. Made from the listener's own record of the
/// connection (`DirectRaceRecord`) and the plan it ran under.
struct DirectPathUseConnection: Equatable, Sendable {
    enum Carrier: Equatable, Sendable {
        case direct
        case tunnel
        /// Neither leg got ready: the connection was refused.
        case none
    }

    /// Why the legs were, or were not, raced.
    enum Plan: Equatable, Sendable {
        /// Both legs were started and the first one ready carried it.
        case raced
        /// No usable direct path: the tunnel alone.
        case noDirect
        /// The direct leg was tried alone first (stickiness).
        case sticky
        /// Only the direct path and the relay were allowed.
        case strict
        /// The user's choice, or no permission: the tunnel alone.
        case tunnelOnly
    }

    var carrier: Carrier
    var plan: Plan
    /// How long the leg that carried it took to get ready.
    var readyMs: Int?
    var directFailed = false
    var directTimedOut = false

    /// `carried` is what the listener settled the connection on: the
    /// backend its relay was granted, or nil when it gave the connection
    /// up. The race's own winner is not that: a race can be won and its
    /// connection still planned again. `strict` and `tunnelOnly` are the
    /// plan's own, read when the plan was made: the record does not carry
    /// them.
    init(record: DirectRaceRecord, carried: DirectRelayBackend?, strict: Bool, tunnelOnly: Bool) {
        switch carried {
        case .direct?:
            carrier = .direct
            if case .ready(let ms) = record.direct {
                readyMs = ms
            }
        case .publicOrigin?:
            carrier = .tunnel
            if case .ready(let ms) = record.publicOrigin {
                readyMs = ms
            }
        case nil:
            carrier = .none
        }
        if strict {
            plan = .strict
        } else if record.sticky {
            plan = .sticky
        } else if record.direct == .none {
            plan = tunnelOnly ? .tunnelOnly : .noDirect
        } else {
            plan = .raced
        }
        directFailed = record.direct == .failed
        directTimedOut = record.direct == .timedOut
    }

    init(carrier: Carrier, plan: Plan, readyMs: Int? = nil, directFailed: Bool = false, directTimedOut: Bool = false) {
        self.carrier = carrier
        self.plan = plan
        self.readyMs = readyMs
        self.directFailed = directFailed
        self.directTimedOut = directTimedOut
    }
}

/// The App's account of which leg carried its pages' connections, per
/// minute and per direct path present in it, for the instance's log
/// (`path_use` on the rendezvous request, docs/plans/remote-direct-rendezvous.md
/// section 2). Off the LAN the direct links came up and the pages hardly
/// used them, and nothing recorded why: this does.
///
/// The unit is a connection the page opened through the App's listener,
/// not a request. A page that stays on a connection it made before the
/// route existed never reaches the listener and is not counted here; the
/// instance's own count of requests by arrival path covers that.
///
/// What is sent is fixed words, counts and milliseconds only: no address,
/// host, identifier or wall time. A minute is reported once it is over, on
/// the next request its own instance answers, and then forgotten. With no
/// answered request the minutes wait, the oldest dropped past `kept`.
///
/// Counts are kept apart by the account's data store and the instance's
/// host (`Scope`), in memory only, and a report carries only the counts of
/// the instance it goes to. An account change drops them all.
///
/// Beside the minutes it keeps the last hand-overs the page asked for, one
/// record each (`Handover`, `handovers` on the same request): a minute says
/// how many were asked and refused, a record says what one of them closed
/// and kept, how long it took, and what kind of document refused it.
final class DirectPathUse: @unchecked Sendable {
    static let shared = DirectPathUse()

    /// The direct path present when a connection's plan was made, in the
    /// words the instance accepts.
    enum Path: String, Equatable, Sendable {
        case none
        case lan
        case punched
        case relay

        /// An address path is `lan` when its address is private,
        /// unique-local or link-local, and `punched` otherwise. One whose
        /// class the App has not learnt yet is sent as `punched`.
        init(kind: DirectPathKind?, local: Bool?) {
            switch kind {
            case nil: self = .none
            case .relay?: self = .relay
            case .ipv4?, .ipv6?: self = local == true ? .lan : .punched
            }
        }
    }

    /// Why a hand-over the page asked for was refused. The App knows two
    /// rules; the page's own (a relay path, spacing, its caps) never reach it.
    enum HandoverRule: String, CaseIterable, Sendable {
        case notUsable = "not_usable"
        case otherDocuments = "other_documents"
    }

    /// Why committed direct connections were closed for a path that was gone.
    enum RetiredRule: String, CaseIterable, Sendable {
        /// The transport reported the path lost.
        case pathMonitor = "path_monitor"
        /// A newer rendezvous answer replaced the endpoint they ran on.
        case superseded
        /// The App went to the background.
        case background
    }

    /// Whose connections a count is of: the account's data store and the
    /// instance's host. Never sent; it only decides which request may
    /// carry the count.
    struct Scope: Hashable, Sendable {
        let store: UUID
        let host: String
    }

    /// One finished minute of one path, ready to send.
    struct Report: Equatable, Sendable {
        let scope: Scope
        let minute: Int
        let path: Path
        /// The entry as the instance requires it: exactly its 22 keys.
        let json: String
    }

    /// One hand-over the page asked for, as the App answered it, ready to
    /// send.
    struct Handover: Sendable {
        let id: Int
        let scope: Scope
        /// The second it was asked, on this object's clock: the second the
        /// App answered it, less the whole seconds that took.
        let asked: Int
        /// From the ask to the App's answer, in milliseconds.
        let ms: Int
        /// Nil when the App took it: it then closed `closed` quiet
        /// connections and kept `kept` busy ones, and either may be zero.
        /// A hand-over that was taken is not thereby one that moved the
        /// page.
        let refused: HandoverRule?
        /// What refused it, when another document did; `sole` otherwise.
        let other: DirectOtherDocument
        let closed: Int
        let kept: Int
        let path: Path

        /// The entry as the instance requires it: exactly its seven keys,
        /// written by hand in a fixed order. `age` is seconds from the ask
        /// to now. The instance accepts `other` other than `none` with
        /// `other_documents`, and only with it.
        func json(now: Int) -> String {
            func bounded(_ value: Int, _ most: Int = Int(Int32.max)) -> Int {
                min(max(value, 0), most)
            }
            let age = bounded(now - asked, DirectPathUse.maxHandoverAgeSeconds)
            let blocker: DirectOtherDocument
            if refused == .otherDocuments {
                blocker = other == .sole ? .unknown : other
            } else {
                blocker = .sole
            }
            return "{\"age_s\":\(age),\"ms\":\(bounded(ms))"
                + ",\"refused\":\"\(refused?.rawValue ?? "none")\",\"other\":\"\(blocker.rawValue)\""
                + ",\"closed\":\(bounded(closed)),\"kept\":\(bounded(kept)),\"path\":\"\(path.rawValue)\"}"
        }
    }

    /// The hand-overs of one instance waiting for an answered request, with
    /// the entries to send for them.
    struct HandoverReport: Sendable {
        let ids: [Int]
        /// Oldest first, at most `handoverLimit`.
        let json: [String]
    }

    /// The most entries one request carries; the instance ignores a longer
    /// list whole.
    static let reportLimit = 10
    /// The most hand-overs one request carries, and the most kept for one
    /// instance while none is answered: the oldest is dropped for a new one.
    static let handoverLimit = 8
    /// The most hand-overs kept in all, over every instance.
    static let handoversKept = 32
    /// A hand-over older than this is dropped unsent: the instance accepts
    /// no greater age.
    static let maxHandoverAgeSeconds = 86400
    /// The most minute-and-path entries kept while none is answered.
    static let kept = 30
    /// A minute older than this is dropped unsent: the instance accepts no
    /// greater age.
    static let maxAgeMinutes = 1440
    /// The most times to ready kept for a minute's median.
    static let sampleLimit = 32

    private struct Key: Hashable {
        let scope: Scope
        let minute: Int
        let path: Path

        /// Oldest first; within a minute a fixed order.
        static func earlier(_ one: Key, _ other: Key) -> Bool {
            (one.minute, one.scope.host, one.path.rawValue) < (other.minute, other.scope.host, other.path.rawValue)
        }
    }

    private struct Entry {
        var direct = 0
        var tunnel = 0
        var raced = 0
        var directWon = 0
        var tunnelWon = 0
        var noDirect = 0
        var sticky = 0
        var strict = 0
        var tunnelOnly = 0
        var directFailed = 0
        var directTimedOut = 0
        var handoverAsked = 0
        var handoverDone = 0
        var handoverRefused = 0
        var retired = 0
        var directMs: [Int] = []
        var tunnelMs: [Int] = []
        var handoverRules: [HandoverRule: Int] = [:]
        var retiredRules: [RetiredRule: Int] = [:]
    }

    private let lock = NSLock()
    private let seconds: @Sendable () -> Int
    /// This App's build (`CFBundleVersion`, for example "57.1"), sent as each
    /// entry's `app`; nil when it is unknown or not what the instance
    /// accepts (1–32 printable ASCII characters).
    let app: String?
    private var entries: [Key: Entry] = [:]
    /// In the order they were answered.
    private var handovers: [Handover] = []
    private var nextHandoverID = 0

    /// `seconds`: whole seconds on a clock that never goes back and keeps
    /// counting while the device sleeps.
    init(
        seconds: @escaping @Sendable () -> Int = DirectPathUse.continuousSeconds(),
        app: String? = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String
    ) {
        self.seconds = seconds
        self.app = app.flatMap(Self.acceptedBuild)
    }

    /// The build as the instance accepts it, or nil.
    static func acceptedBuild(_ build: String) -> String? {
        let bytes = Array(build.utf8)
        guard (1...32).contains(bytes.count), bytes.allSatisfy({ (0x20...0x7e).contains($0) }) else {
            return nil
        }
        return build
    }

    static func continuousSeconds() -> @Sendable () -> Int {
        let clock = ContinuousClock()
        let start = clock.now
        return { Int(start.duration(to: clock.now).components.seconds) }
    }

    func connection(_ connection: DirectPathUseConnection, scope: Scope, path: Path) {
        update(scope, path) { entry in
            switch connection.carrier {
            case .direct:
                entry.direct += 1
                Self.sample(&entry.directMs, connection.readyMs)
            case .tunnel:
                entry.tunnel += 1
                Self.sample(&entry.tunnelMs, connection.readyMs)
            case .none:
                break
            }
            switch connection.plan {
            case .raced:
                entry.raced += 1
                if connection.carrier == .direct {
                    entry.directWon += 1
                } else if connection.carrier == .tunnel {
                    entry.tunnelWon += 1
                }
            case .noDirect: entry.noDirect += 1
            case .sticky: entry.sticky += 1
            case .strict: entry.strict += 1
            case .tunnelOnly: entry.tunnelOnly += 1
            }
            if connection.directFailed {
                entry.directFailed += 1
            }
            if connection.directTimedOut {
                entry.directTimedOut += 1
            }
        }
    }

    /// A hand-over the page asked for, and how the App answered it:
    /// `refused` nil when it was done.
    func handover(refused: HandoverRule?, scope: Scope, path: Path) {
        update(scope, path) { entry in
            entry.handoverAsked += 1
            if let refused {
                entry.handoverRefused += 1
                entry.handoverRules[refused, default: 0] += 1
            } else {
                entry.handoverDone += 1
            }
        }
    }

    /// The App answered a hand-over the page asked for: `outcome` is its
    /// answer to the page, `other` what refused it when another document
    /// did, `took` the time from the ask, and `path` the direct path present
    /// when it was asked. `handover(refused:scope:path:)` counted the ask.
    func handoverAnswered(
        _ outcome: DirectHandoverOutcome,
        refusedBy other: DirectOtherDocument,
        took: Duration,
        scope: Scope,
        path: Path
    ) {
        let refused: HandoverRule?
        var closed = 0
        var kept = 0
        switch outcome {
        case .done(let closedCount, let keptCount):
            refused = nil
            closed = closedCount
            kept = keptCount
        case .skipped(.notUsable):
            refused = .notUsable
        case .skipped(.otherDocuments):
            refused = .otherDocuments
        }
        // Within the instance's bound (0 ..< 2^31).
        let (wholeSeconds, attoseconds) = took.components
        let ms = min(
            max(0, Int(clamping: wholeSeconds) &* 1_000 &+ Int(attoseconds / 1_000_000_000_000_000)),
            Int(Int32.max)
        )
        lock.withLock {
            let now = seconds()
            nextHandoverID += 1
            handovers.append(Handover(
                id: nextHandoverID, scope: scope, asked: now - ms / 1000, ms: ms,
                refused: refused, other: other, closed: closed, kept: kept, path: path
            ))
            dropHandoversLocked(now: now)
        }
    }

    /// The hand-overs of `scope` no answered request has carried yet, the
    /// oldest ask first (to the second; of two asked in one second, the one
    /// answered first). Asking changes nothing: a request that fails
    /// reports them again with the next.
    func pendingHandovers(for scope: Scope) -> HandoverReport {
        lock.withLock {
            let now = seconds()
            dropHandoversLocked(now: now)
            let mine = handovers.filter { $0.scope == scope }.suffix(Self.handoverLimit)
                .sorted { ($0.asked, $0.id) < ($1.asked, $1.id) }
            return HandoverReport(ids: mine.map(\.id), json: mine.map { $0.json(now: now) })
        }
    }

    /// The instance answered a request that carried these: they are done.
    func answered(_ report: HandoverReport) {
        lock.withLock {
            handovers.removeAll { report.ids.contains($0.id) }
        }
    }

    func retired(_ rule: RetiredRule, scope: Scope, path: Path) {
        update(scope, path) { entry in
            entry.retired += 1
            entry.retiredRules[rule, default: 0] += 1
        }
    }

    /// The minutes of `scope` that are over and not yet answered, oldest
    /// first, at most `reportLimit`. Asking changes nothing: a request that
    /// fails reports them again with the next.
    func pending(for scope: Scope) -> [Report] {
        lock.withLock {
            let now = seconds() / 60
            dropLocked(now: now)
            return entries.keys
                .filter { $0.scope == scope && $0.minute < now }
                .sorted(by: Key.earlier)
                .prefix(Self.reportLimit)
                .compactMap { key in
                    entries[key].map {
                        Report(scope: key.scope, minute: key.minute, path: key.path, json: Self.json($0, key, now: now, app: app))
                    }
                }
        }
    }

    /// The instance answered a request that carried these: they are done.
    func answered(_ reports: [Report]) {
        lock.withLock {
            for report in reports {
                entries.removeValue(forKey: Key(scope: report.scope, minute: report.minute, path: report.path))
            }
        }
    }

    /// The account changes: nothing counted so far is ever sent.
    func removeAll() {
        lock.withLock {
            entries.removeAll()
            handovers.removeAll()
        }
    }

    private func update(_ scope: Scope, _ path: Path, _ change: (inout Entry) -> Void) {
        lock.withLock {
            let now = seconds() / 60
            change(&entries[Key(scope: scope, minute: now, path: path), default: Entry()])
            dropLocked(now: now)
        }
    }

    /// Minutes too old to send, then the oldest past `kept`.
    private func dropLocked(now: Int) {
        for key in entries.keys where now - key.minute > Self.maxAgeMinutes {
            entries.removeValue(forKey: key)
        }
        let excess = entries.count - Self.kept
        guard excess > 0 else {
            return
        }
        for key in entries.keys.sorted(by: Key.earlier).prefix(excess) {
            entries.removeValue(forKey: key)
        }
    }

    /// Hand-overs too old to send, then the oldest of an instance past
    /// `handoverLimit`, then the oldest of all past `handoversKept`.
    private func dropHandoversLocked(now: Int) {
        handovers.removeAll { now - $0.asked > Self.maxHandoverAgeSeconds }
        var seen: [Scope: Int] = [:]
        var keep = Set<Int>()
        for handover in handovers.reversed() where seen[handover.scope, default: 0] < Self.handoverLimit {
            seen[handover.scope, default: 0] += 1
            keep.insert(handover.id)
        }
        handovers.removeAll { !keep.contains($0.id) }
        if handovers.count > Self.handoversKept {
            handovers.removeFirst(handovers.count - Self.handoversKept)
        }
    }

    private static func sample(_ samples: inout [Int], _ ms: Int?) {
        if let ms, samples.count < sampleLimit {
            samples.append(ms)
        }
    }

    /// The middle value, the lower of two; 0 for none.
    static func median(_ samples: [Int]) -> Int {
        guard !samples.isEmpty else {
            return 0
        }
        return samples.sorted()[(samples.count - 1) / 2]
    }

    /// The rule counted most; of equals, the first in the type's order.
    private static func most<Rule: CaseIterable & Hashable & RawRepresentable>(
        _ tally: [Rule: Int]
    ) -> String where Rule.RawValue == String {
        var best: (rule: Rule, count: Int)?
        for rule in Rule.allCases {
            if let count = tally[rule], count > (best?.count ?? 0) {
                best = (rule, count)
            }
        }
        return best?.rule.rawValue ?? "none"
    }

    /// Written by hand, in a fixed order: four fixed words and integers
    /// within the instance's bounds, then `app` when the build is known.
    private static func json(_ entry: Entry, _ key: Key, now: Int, app: String?) -> String {
        func bounded(_ value: Int) -> Int {
            min(max(value, 0), Int(Int32.max))
        }
        let numbers: [(String, Int)] = [
            ("age_min", min(max(now - key.minute, 0), maxAgeMinutes)),
            ("direct", entry.direct), ("tunnel", entry.tunnel),
            ("raced", entry.raced), ("direct_won", entry.directWon), ("tunnel_won", entry.tunnelWon),
            ("no_direct", entry.noDirect), ("sticky", entry.sticky), ("strict", entry.strict),
            ("tunnel_only", entry.tunnelOnly),
            ("direct_failed", entry.directFailed), ("direct_timed_out", entry.directTimedOut),
            ("direct_ms", median(entry.directMs)), ("tunnel_ms", median(entry.tunnelMs)),
            ("handover_asked", entry.handoverAsked), ("handover_done", entry.handoverDone),
            ("handover_refused", entry.handoverRefused), ("retired", entry.retired),
        ]
        let words = "\"client\":\"ios\",\"path\":\"\(key.path.rawValue)\""
            + ",\"handover_rule\":\"\(most(entry.handoverRules))\""
            + ",\"retired_rule\":\"\(most(entry.retiredRules))\""
        // Printable ASCII only (`acceptedBuild`): a quote and a backslash
        // are the two characters JSON escapes among them.
        let build = app.map {
            ",\"app\":\"" + $0.replacingOccurrences(of: "\\", with: "\\\\")
                .replacingOccurrences(of: "\"", with: "\\\"") + "\""
        } ?? ""
        return "{" + words + numbers.map { ",\"\($0.0)\":\(bounded($0.1))" }.joined() + build + "}"
    }
}
