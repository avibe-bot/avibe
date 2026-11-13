import CoreFoundation
import Foundation

/// The thread the instance gives a Session's notifications, `session:<id>`.
/// The notification service extension carries the tag into
/// `threadIdentifier`, and the instance origin and account into `userInfo`;
/// a notification it could not decrypt has none of these, so nothing here
/// can match it.
enum SessionNotificationThread {
    static let prefix = "session:"
}

/// The unread Sessions `notifications.reconcile({unreadSessions, asOf})` names
/// (`docs/plans/ios-app.md` section 4): every Session with unread messages in
/// one whole unread map the server returned, and a device time at or before
/// the moment the request for it went out.
struct NotificationReconcile: Equatable {
    static let maximumSessions = 1000
    static let maximumSessionIDLength = 200

    let unreadSessions: Set<String>
    let asOf: Date

    /// Exactly `unreadSessions`, at most `maximumSessions` distinct non-empty
    /// strings of at most `maximumSessionIDLength` characters, and `asOf`, a
    /// finite non-negative number of milliseconds since the Unix epoch. Any
    /// other value, or another parameter, is nil.
    static func parse(_ params: [String: Any]) -> NotificationReconcile? {
        guard
            params.count == 2,
            let ids = params["unreadSessions"] as? [Any],
            ids.count <= maximumSessions,
            let number = params["asOf"] as? NSNumber,
            CFGetTypeID(number) != CFBooleanGetTypeID()
        else {
            return nil
        }
        let milliseconds = number.doubleValue
        guard milliseconds.isFinite, milliseconds >= 0 else {
            return nil
        }
        var unread = Set<String>()
        for id in ids {
            guard
                let id = id as? String,
                !id.isEmpty,
                id.count <= maximumSessionIDLength,
                unread.insert(id).inserted
            else {
                return nil
            }
        }
        return NotificationReconcile(
            unreadSessions: unread,
            asOf: Date(timeIntervalSince1970: milliseconds / 1000)
        )
    }
}

/// Removes this account's notifications for one instance's Sessions from
/// Notification Center once they are read.
///
/// Only notifications delivered by the moment of the read are removed: one
/// that lands after it is news the read did not cover, and stays. A request
/// still pending has not been delivered, so it is never removed.
@MainActor
final class SessionNotificationCleaner {
    private let center: PushNotificationCentering

    init(center: PushNotificationCentering) {
        self.center = center
    }

    /// Nothing notified on `instanceOrigin` was unread at `readAt`. The badge
    /// is the loud, preference-filtered total, and pushes go out only for
    /// loud categories, so a zero covers every Session notification; it is
    /// still only a hint, so only Session threads go. A workspace notice such
    /// as `harness:failure` is not part of the Inbox's unread total, so a zero
    /// says nothing about it.
    func clearAllSessions(instanceOrigin: String, accountID: String, readAt: Date) {
        clear(instanceOrigin: instanceOrigin, accountID: accountID, deliveredBy: readAt) { _ in
            true
        }
    }

    /// The Web UI read a whole unread map whose request went out at or after
    /// `reconcile.asOf`: a Session it does not name as unread has read every
    /// notification delivered by then. One delivered later may describe a
    /// message the map did not see, so it stays. A future `asOf` counts as now.
    func reconcile(
        _ reconcile: NotificationReconcile,
        instanceOrigin: String,
        accountID: String
    ) {
        clear(
            instanceOrigin: instanceOrigin,
            accountID: accountID,
            deliveredBy: Date()
        ) { sessionID in
            !reconcile.unreadSessions.contains(sessionID)
        }
    }

    /// Removes the delivered `session:<id>` notifications of this instance and
    /// account, delivered by `cutoff`, whose `<id>` `isRead`.
    private func clear(
        instanceOrigin: String,
        accountID: String,
        deliveredBy cutoff: Date,
        isRead: @escaping (String) -> Bool
    ) {
        Task { [center] in
            let delivered = await center.deliveredNotifications()
                .filter { entry in
                    guard
                        let deliveredAt = entry.deliveredAt,
                        deliveredAt <= cutoff,
                        entry.threadIdentifier.hasPrefix(SessionNotificationThread.prefix),
                        entry.userInfo["instance_origin"] as? String == instanceOrigin,
                        entry.userInfo["account_id"] as? String == accountID
                    else {
                        return false
                    }
                    let sessionID = String(
                        entry.threadIdentifier.dropFirst(SessionNotificationThread.prefix.count)
                    )
                    return isRead(sessionID)
                }
                .map(\.identifier)
            center.removeDeliveredNotifications(withIdentifiers: delivered)
        }
    }
}
