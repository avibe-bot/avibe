import Foundation
import Network

/// This device's choice of path to one instance, per account and instance
/// (the instance's "Devices › Connection" page, "this device"): automatic
/// (the direct path when it can, otherwise the Cloudflare tunnel), or the
/// Cloudflare tunnel only. Kept on this device only.
struct DirectPathPreference: Equatable, Codable, Sendable {
    enum Mode: String, Equatable, Codable, Sendable {
        case automatic
        case tunnel

        /// A mode the page names. Unknown names are refused, so a later
        /// mode can be added without changing the shape.
        init?(name: String) {
            self.init(rawValue: name)
        }
    }

    /// The App set the choice back to automatic itself.
    struct Reversion: Equatable, Codable, Sendable {
        enum Reason: String, Equatable, Codable, Sendable {
            /// The instance could not be reached on the tunnel alone.
            case unreachable
        }

        let at: Date
        let reason: Reason
    }

    var mode: Mode = .automatic
    var reverted: Reversion? = nil

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
                ] as Any
            } ?? NSNull(),
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
