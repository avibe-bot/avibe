import ActivityKit
import Foundation

/// Shared with AvibeLiveActivity. Push times are Unix seconds, never Date's
/// reference-date encoding. Names are immutable, phone-local attributes.
struct TurnActivityAttributes: ActivityAttributes {
    enum Phase: String, Codable, Hashable {
        case working, needsYou, done, failed, stopped
        var isRunning: Bool { self == .working || self == .needsYou }
    }

    struct ContentState: Codable, Hashable {
        var phase: Phase
        var startedAt: Int
        var endedAt: Int?
        var confirmedAt: Int

        var staleDate: Date? {
            phase.isRunning ? Date(timeIntervalSince1970: Double(confirmedAt + 2400)) : nil
        }
        var dismissalDate: Date {
            Date(timeIntervalSince1970: Double((endedAt ?? confirmedAt) + 3600))
        }
        func elapsed(at date: Date) -> TimeInterval {
            max(0, Double(endedAt ?? Int(date.timeIntervalSince1970)) - Double(startedAt))
        }
    }

    var agentName: String
    var sessionTitle: String?
    var accountID: String
    var instanceOrigin: String
    var sessionID: String
    var turnID: String

    static func displayName(_ text: String, limit: Int) -> String {
        let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return value.count > limit ? String(value.prefix(limit - 1)) + "…" : value
    }

    static func displayTitle(_ title: String?, agentName: String) -> String? {
        let value = title?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return value.isEmpty || value == agentName ? nil : displayName(value, limit: 60)
    }

    static func activityID(from url: URL) -> String? {
        guard url.scheme == "avibe-app", url.host == "live-activity",
              url.user == nil, url.password == nil, url.port == nil,
              url.query == nil, url.fragment == nil else { return nil }
        let id = String(url.path.dropFirst())
        guard (1...64).contains(id.count), id.utf8.allSatisfy({
            (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45
        }) else { return nil }
        return id
    }
}
