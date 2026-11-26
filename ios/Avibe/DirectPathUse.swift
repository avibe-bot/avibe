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

    /// The most entries one request carries; the instance ignores a longer
    /// list whole.
    static let reportLimit = 10
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
    private var entries: [Key: Entry] = [:]

    /// `seconds`: whole seconds on a clock that never goes back and keeps
    /// counting while the device sleeps.
    init(seconds: @escaping @Sendable () -> Int = DirectPathUse.continuousSeconds()) {
        self.seconds = seconds
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
                        Report(scope: key.scope, minute: key.minute, path: key.path, json: Self.json($0, key, now: now))
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
    /// within the instance's bounds.
    private static func json(_ entry: Entry, _ key: Key, now: Int) -> String {
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
        return "{" + words + numbers.map { ",\"\($0.0)\":\(bounded($0.1))" }.joined() + "}"
    }
}
