import Foundation

/// Probation before a direct path is adopted
/// (docs/plans/remote-direct-probation.md).
///
/// A link that comes up is used only after it has stayed up for a wait, and
/// the wait grows when links die early: 0, 30 s, 600 s. It keeps a page from
/// going back and forth between a direct path that keeps dying and the
/// tunnel. Going to the tunnel is not delayed; only the way back is.
///
/// This is the rule alone: no timer, no transport, no storage. The
/// controller tells it what happened and when, runs the timer for a wait,
/// and stores `stored` when it changes. The Desktop core has the same rule
/// in Rust; both run `tests/fixtures/direct_probation_cases.json`.

/// Why a link ended. Only `lost` can raise the level.
enum DirectLinkEnd: String, CaseIterable, Sendable {
    /// The path died under it: the transport's idle time-out, a reset, a
    /// failed probe on probation.
    case lost
    /// The App went to the background and closed it.
    case background
    /// The user changed the connection mode, or left the instance.
    case choice
    /// A newer rendezvous answer replaced it.
    case superseded
    /// The instance closed the connection, or our lease on it ran out.
    case instanceEnded = "instance_ended"
    /// The loss came from the probe that a change of network started.
    case networkChange = "network_change"
}

/// Why the level last rose. `none` whenever the level is 0.
enum DirectProbationRose: String, Codable, Sendable {
    case none
    /// A link died within ten minutes of being adopted.
    case adopted
    /// A link died while it was waiting.
    case probation
}

/// One account and instance's level: what survives a relaunch.
struct DirectProbationRecord: Equatable, Codable, Sendable {
    /// An index into `DirectProbationGuard.levelsSeconds`.
    var level = 0
    var rose = DirectProbationRose.none
    /// The wall time of the last early death, in milliseconds. The level
    /// expires `expiryMs` after it.
    var lastEarlyWallMs: Int64?

    private enum CodingKeys: String, CodingKey {
        case level, rose
        case lastEarlyWallMs = "last_early_wall_ms"
    }
}

struct DirectProbationGuard: Sendable {
    static let levelsSeconds = [0, 30, 600]
    /// A link lost sooner than this after its adoption died early.
    static let earlyMs: Int64 = 600_000
    /// An adopted link that has been up this long clears the level.
    static let heldMs: Int64 = 600_000
    /// The level is cleared this long after the last early death.
    static let expiryMs: Int64 = 1_800_000

    private struct Link: Sendable {
        let id: Int
        let upAt: Int64
        let waitMs: Int64
        var adoptedAt: Int64?
        /// The foreground stretch it came up in; none in the background.
        let stretch: Int?
    }

    /// The levels, by account and instance. The controller stores it.
    private(set) var stored: [String: DirectProbationRecord]
    private var links: [String: Link] = [:]
    private var isForeground = false
    private var stretch = 0

    /// A new process: the stored levels, no link, in the background until
    /// told otherwise.
    init(stored: [String: DirectProbationRecord]) {
        self.stored = stored.filter { Self.levelsSeconds.indices.contains($0.value.level) }
    }

    mutating func foreground() {
        isForeground = true
        stretch += 1
    }

    mutating func background() {
        isForeground = false
    }

    /// A link came up. The answer is how long it must stay up before it is
    /// used, in milliseconds, fixed here: 0 when `strict` (the user ruled
    /// the tunnel out, so there is nothing to wait on), otherwise the
    /// level's time. `now` is monotonic; `wall` is used for the expiry.
    mutating func linkUp(_ key: String, link: Int, now: Int64, wall: Int64, strict: Bool) -> Int64 {
        let record = settle(key, now: now, wall: wall)
        let waitMs = strict ? 0 : Int64(Self.levelsSeconds[record.level]) * 1000
        links[key] = Link(
            id: link,
            upAt: now,
            waitMs: waitMs,
            adoptedAt: nil,
            stretch: isForeground ? stretch : nil
        )
        return waitMs
    }

    /// The controller's check, when its timer fires (at once for a wait of
    /// 0). True once: for the current link, in the foreground, in the
    /// stretch it came up in, when its wait is over. Adoption is never
    /// inferred from time alone: a link whose check has not run is not
    /// adopted.
    mutating func adopt(_ key: String, link: Int, now: Int64) -> Bool {
        guard var current = links[key], current.id == link, current.adoptedAt == nil,
              isForeground, current.stretch == stretch,
              now - current.upAt >= current.waitMs
        else {
            return false
        }
        current.adoptedAt = now
        links[key] = current
        return true
    }

    /// A link ended. One that is not the current link changes nothing (an
    /// older link's late close). A `lost` raises the level when the link
    /// was waiting, or was adopted less than `earlyMs` ago, and only for a
    /// link that came up in the present foreground stretch, in the
    /// foreground.
    mutating func linkDown(_ key: String, link: Int, cause: DirectLinkEnd, now: Int64, wall: Int64) {
        guard let current = links[key], current.id == link else {
            return
        }
        var record = settle(key, now: now, wall: wall)
        links[key] = nil
        guard cause == .lost, isForeground, current.stretch == stretch else {
            return
        }
        if let adoptedAt = current.adoptedAt, now - adoptedAt >= Self.earlyMs {
            // It held: `settle` has cleared the level.
            return
        }
        record.level = min(record.level + 1, Self.levelsSeconds.count - 1)
        record.rose = current.adoptedAt == nil ? .probation : .adopted
        record.lastEarlyWallMs = wall
        stored[key] = record
    }

    /// The level now, in seconds: 0, 30 or 600.
    mutating func levelSeconds(_ key: String, now: Int64, wall: Int64) -> Int {
        Self.levelsSeconds[settle(key, now: now, wall: wall).level]
    }

    /// Why the level last rose, as of the last call that read the clocks.
    func rose(_ key: String) -> DirectProbationRose {
        stored[key]?.rose ?? .none
    }

    /// The current link is up and adopted.
    func isAdopted(_ key: String) -> Bool {
        links[key]?.adoptedAt != nil
    }

    /// What time alone decides: the expiry, then "held".
    private mutating func settle(_ key: String, now: Int64, wall: Int64) -> DirectProbationRecord {
        var record = stored[key] ?? DirectProbationRecord()
        if record.level > 0, let last = record.lastEarlyWallMs {
            if wall < last {
                // The wall clock went back past the death: the 30 minutes
                // count from here, with no credit for what came before.
                record.lastEarlyWallMs = wall
            } else if wall - last >= Self.expiryMs {
                record = DirectProbationRecord()
            }
        }
        if let adoptedAt = links[key]?.adoptedAt, now - adoptedAt >= Self.heldMs {
            record = DirectProbationRecord()
        }
        if record == DirectProbationRecord() {
            stored[key] = nil
        } else {
            stored[key] = record
        }
        return record
    }
}

/// Where the levels are kept between launches.
protocol DirectProbationStoring: AnyObject {
    func load() -> [String: DirectProbationRecord]
    func save(_ levels: [String: DirectProbationRecord])
}

/// The levels in the App's own preferences, keyed as the path choices are:
/// by account and instance. Nothing of it leaves the device but the level
/// and why it rose (`DirectPathUse`, `path_guard`).
final class DirectProbationDefaultsStore: DirectProbationStoring {
    static let key = "avibe.direct.probation"

    private let defaults: UserDefaults

    init(defaults: UserDefaults) {
        self.defaults = defaults
    }

    func load() -> [String: DirectProbationRecord] {
        guard
            let data = defaults.data(forKey: Self.key),
            let levels = try? JSONDecoder().decode([String: DirectProbationRecord].self, from: data)
        else {
            return [:]
        }
        return levels
    }

    func save(_ levels: [String: DirectProbationRecord]) {
        if levels.isEmpty {
            defaults.removeObject(forKey: Self.key)
        } else if let data = try? JSONEncoder().encode(levels) {
            defaults.set(data, forKey: Self.key)
        }
    }

    /// The key of one account's instance.
    static func entry(accountID: String, host: String) -> String {
        "\(accountID) \(host)"
    }
}

/// The clocks a wait runs on. `now` is monotonic, for the wait itself and
/// for "held" and "early"; `wall` is used for the level's expiry only,
/// which is all that has to survive a relaunch.
struct DirectProbationClock: Sendable {
    /// Milliseconds on a clock that never goes back.
    var now: @Sendable () -> Int64
    /// Milliseconds since 1970.
    var wall: @Sendable () -> Int64
    /// Returns after `milliseconds`, or throws when its task is cancelled.
    var sleep: @Sendable (_ milliseconds: Int64) async throws -> Void

    static let system = DirectProbationClock(
        now: { Int64(DispatchTime.now().uptimeNanoseconds / 1_000_000) },
        wall: { Int64(Date().timeIntervalSince1970 * 1000) },
        sleep: { try await Task.sleep(for: .milliseconds($0)) }
    )
}
