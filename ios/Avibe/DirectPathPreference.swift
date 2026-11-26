import Foundation
import Network

/// This device's choice of path to one instance, per account and instance
/// (the instance's "Devices › Connection" page, "this device"): automatic
/// (the direct path when it can, otherwise the Cloudflare tunnel), the
/// Cloudflare tunnel only, or the direct path and its relay only (the
/// tunnel only to establish it). Kept on this device only. A build that
/// does not know a mode reads its entry as automatic.
struct DirectPathPreference: Equatable, Codable, Sendable {
    enum Mode: String, Equatable, Codable, Sendable {
        case automatic
        case tunnel
        /// 「仅直连和中继」: once this instance's direct path has come up,
        /// new connections never go through the tunnel
        /// (`DirectOnlyControlling`).
        case direct

        /// A mode the page names. Unknown names are refused, so a later
        /// mode can be added without changing the shape.
        init?(name: String) {
            self.init(rawValue: name)
        }
    }

    /// The App set the choice back to automatic itself.
    struct Reversion: Equatable, Codable, Sendable {
        enum Reason: String, Equatable, Codable, Sendable {
            /// The instance could not be reached on the chosen path alone.
            case unreachable
        }

        let at: Date
        let reason: Reason
        /// The mode it was set back from. An entry stored before there
        /// were two such modes has none: it was the tunnel.
        let from: Mode

        init(at: Date, reason: Reason, from: Mode = .tunnel) {
            self.at = at
            self.reason = reason
            self.from = from
        }

        private enum CodingKeys: String, CodingKey {
            case at, reason, from
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            at = try container.decode(Date.self, forKey: .at)
            reason = try container.decode(Reason.self, forKey: .reason)
            from = try container.decodeIfPresent(Mode.self, forKey: .from) ?? .tunnel
        }
    }

    var mode: Mode = .automatic
    var reverted: Reversion? = nil
    /// The choice of 「仅直连和中继」 is recorded but takes effect only at
    /// the next launch: the instance loaded without its route to the App's
    /// listener in this process (`DirectOnlyControlling`). Not stored.
    var pendingRelaunch = false

    private enum CodingKeys: String, CodingKey {
        case mode, reverted
    }

    static let automatic = DirectPathPreference()

    /// What `direct.preference()` and `direct.setPreference()` answer, and
    /// what `direct.preferenceChanged` carries.
    var jsonObject: [String: Any] {
        [
            "mode": mode.rawValue,
            "reverted": reverted.map {
                [
                    "at": Int(($0.at.timeIntervalSince1970 * 1_000).rounded()),
                    "reason": $0.reason.rawValue,
                    "from": $0.from.rawValue,
                ] as Any
            } ?? NSNull(),
            "pendingRelaunch": pendingRelaunch,
        ]
    }

    /// The script that delivers `direct.preferenceChanged` to a document on
    /// `origin` (and only there: a navigation can commit before it runs).
    func eventScript(for origin: WebOrigin) -> String? {
        guard
            let data = try? JSONSerialization.data(
                withJSONObject: ["origin": origin.serialized, "detail": jsonObject],
                options: [.sortedKeys]
            ),
            let payload = String(data: data, encoding: .utf8)
        else {
            return nil
        }
        return """
        (() => {
          const event = \(payload);
          if (window.location.origin !== event.origin) return;
          window.dispatchEvent(new CustomEvent("avibe:native:direct.preferenceChanged", {
            detail: event.detail
          }));
        })();
        """
    }
}

/// The choices, in the App's own preferences, keyed by account and instance
/// origin. An entry this build cannot read (a later mode) reads as automatic.
final class DirectPathPreferenceStore {
    static let key = "avibe.direct.pathPreferences"

    private let defaults: UserDefaults

    init(defaults: UserDefaults) {
        self.defaults = defaults
    }

    func preference(accountID: String, origin: String) -> DirectPathPreference {
        guard
            let data = stored()[Self.entry(accountID: accountID, origin: origin)],
            let preference = try? JSONDecoder().decode(DirectPathPreference.self, from: data)
        else {
            return .automatic
        }
        return preference
    }

    func set(_ preference: DirectPathPreference, accountID: String, origin: String) {
        var entries = stored()
        let entry = Self.entry(accountID: accountID, origin: origin)
        var preference = preference
        preference.pendingRelaunch = false
        if preference == .automatic {
            entries.removeValue(forKey: entry)
        } else if let data = try? JSONEncoder().encode(preference) {
            entries[entry] = data
        }
        defaults.set(entries, forKey: Self.key)
    }

    private func stored() -> [String: Data] {
        defaults.dictionary(forKey: Self.key) as? [String: Data] ?? [:]
    }

    private static func entry(accountID: String, origin: String) -> String {
        "\(accountID) \(origin)"
    }
}

/// `direct.preference()` and `direct.setPreference()` for one runtime's
/// pages. Both answer for the instance of the asking page; `nil` when that
/// page is not the open runtime's.
struct DirectPreferenceProvider {
    let read: @MainActor (WebOrigin) -> DirectPathPreference?
    /// The mode, and the page's URL: a change reopens the instance there,
    /// after the answer.
    let write: @MainActor (WebOrigin, DirectPathPreference.Mode, URL) -> DirectPathPreference?
    /// Whether 「仅直连和中继」 can be chosen: only with a path controller
    /// that enforces it (`DirectOnlyControlling`). Otherwise the bridge
    /// refuses `direct`, as a mode it does not know.
    var acceptsDirectOnly = false
    /// `direct.measure()` for the instance (`DirectPathMeasuring`); `nil`
    /// when the path controller cannot measure.
    var measure: (@MainActor (WebOrigin) async -> DirectMeasureReply)? = nil
}

/// Whether the device has a usable network, as the system reports it.
@MainActor
protocol NetworkPathReading: AnyObject {
    var isSatisfied: Bool { get }
}

@MainActor
final class SystemNetworkPath: NetworkPathReading {
    private let monitor = NWPathMonitor()
    private(set) var isSatisfied = true

    init() {
        monitor.pathUpdateHandler = { [weak self] path in
            let satisfied = path.status == .satisfied
            Task { @MainActor in
                self?.isSatisfied = satisfied
            }
        }
        monitor.start(queue: DispatchQueue(label: "avibe.network-path"))
    }

    deinit {
        monitor.cancel()
    }
}
