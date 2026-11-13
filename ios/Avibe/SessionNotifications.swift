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
/// Notification Center (and any still pending) once they are read in the App.
@MainActor
final class SessionNotificationCleaner {
    private let center: PushNotificationCentering

    init(center: PushNotificationCentering) {
        self.center = center
    }

    /// The Session `sessionID` was opened on `instanceOrigin`.
    func clearSession(
        _ sessionID: String,
        instanceOrigin: String,
        accountID: String
    ) {
        let thread = SessionNotificationThread.identifier(forSession: sessionID)
        clear(instanceOrigin: instanceOrigin, accountID: accountID) {
            $0 == thread
        }
    }

    /// Nothing on `instanceOrigin` is unread any more. Only Session threads
    /// are cleared: a workspace notice such as `harness:failure` is not part
    /// of the Inbox's unread total, so a zero says nothing about it.
    func clearAllSessions(instanceOrigin: String, accountID: String) {
        clear(instanceOrigin: instanceOrigin, accountID: accountID) {
            $0.hasPrefix(SessionNotificationThread.prefix)
        }
    }

    private func clear(
        instanceOrigin: String,
        accountID: String,
        thread matches: @escaping (String) -> Bool
    ) {
        let selected = { (entry: PushNotificationCenterEntry) -> Bool in
            matches(entry.threadIdentifier)
                && entry.userInfo["instance_origin"] as? String == instanceOrigin
                && entry.userInfo["account_id"] as? String == accountID
        }
        Task { [center] in
            let delivered = await center.deliveredNotifications()
                .filter(selected)
                .map(\.identifier)
            let pending = await center.pendingNotificationRequests()
                .filter(selected)
                .map(\.identifier)
            if !delivered.isEmpty {
                center.removeDeliveredNotifications(withIdentifiers: delivered)
            }
            if !pending.isEmpty {
                center.removePendingNotificationRequests(withIdentifiers: pending)
            }
        }
    }
}
