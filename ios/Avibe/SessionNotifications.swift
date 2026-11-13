import Foundation

/// The delivered notifications a read in the App retires.
///
/// The instance tags a Session's notifications `session:<id>`, and the
/// notification service extension carries the tag into `threadIdentifier` and
/// the instance origin and account into `userInfo`. A notification it could
/// not decrypt has none of these, so nothing here can match it.
enum SessionNotificationThread {
    static let prefix = "session:"

    static func identifier(forSession sessionID: String) -> String {
        prefix + sessionID
    }

    /// The Session a page of the Web UI shows: `/chat/<id>`, its one path
    /// segment percent-decoded, whatever the query or fragment.
    static func sessionID(shownAt url: URL) -> String? {
        let segments = url.path.split(separator: "/", omittingEmptySubsequences: false)
        // "/chat/<id>" splits into "", "chat", "<id>".
        guard
            segments.count == 3,
            segments[0].isEmpty,
            segments[1] == "chat",
            !segments[2].isEmpty
        else {
            return nil
        }
        // `URL.path` is already percent-decoded.
        return String(segments[2])
    }
}

/// Removes this account's notifications for one instance's Sessions from
/// Notification Center once they are read in the App.
///
/// Only notifications delivered by the moment of the read are removed: one
/// that lands after it (a new result just after the Session was left, or
/// after the zero was computed) is news the read did not cover, and stays.
/// A request still pending has not been delivered, so it is never removed.
@MainActor
final class SessionNotificationCleaner {
    private let center: PushNotificationCentering

    init(center: PushNotificationCentering) {
        self.center = center
    }

    /// The Session `sessionID` was on screen on `instanceOrigin` at `readAt`.
    func clearSession(
        _ sessionID: String,
        instanceOrigin: String,
        accountID: String,
        readAt: Date
    ) {
        let thread = SessionNotificationThread.identifier(forSession: sessionID)
        clear(instanceOrigin: instanceOrigin, accountID: accountID, deliveredBy: readAt) {
            $0 == thread
        }
    }

    /// Nothing notified on `instanceOrigin` was unread at `readAt`. The badge
    /// is the loud, preference-filtered total, and pushes go out only for
    /// loud categories, so a zero covers every Session notification; it is
    /// still only a hint, so only Session threads go. A workspace notice such
    /// as `harness:failure` is not part of the Inbox's unread total, so a zero
    /// says nothing about it.
    func clearAllSessions(instanceOrigin: String, accountID: String, readAt: Date) {
        clear(instanceOrigin: instanceOrigin, accountID: accountID, deliveredBy: readAt) {
            $0.hasPrefix(SessionNotificationThread.prefix)
        }
    }

    private func clear(
        instanceOrigin: String,
        accountID: String,
        deliveredBy cutoff: Date,
        thread matches: @escaping (String) -> Bool
    ) {
        Task { [center] in
            let delivered = await center.deliveredNotifications()
                .filter { entry in
                    guard let deliveredAt = entry.deliveredAt else {
                        return false
                    }
                    return deliveredAt <= cutoff
                        && matches(entry.threadIdentifier)
                        && entry.userInfo["instance_origin"] as? String == instanceOrigin
                        && entry.userInfo["account_id"] as? String == accountID
                }
                .map(\.identifier)
            center.removeDeliveredNotifications(withIdentifiers: delivered)
        }
    }
}
