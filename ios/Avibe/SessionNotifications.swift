import Foundation

/// The thread the instance gives a Session's notifications, `session:<id>`.
/// The notification service extension carries the tag into
/// `threadIdentifier`, and the instance origin and account into `userInfo`;
/// a notification it could not decrypt has none of these, so nothing here
/// can match it.
enum SessionNotificationThread {
    static let prefix = "session:"
}

/// Removes this account's notifications for one instance's Sessions from
/// Notification Center once nothing there is unread.
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
        Task { [center] in
            let delivered = await center.deliveredNotifications()
                .filter { entry in
                    guard let deliveredAt = entry.deliveredAt else {
                        return false
                    }
                    return deliveredAt <= readAt
                        && entry.threadIdentifier.hasPrefix(SessionNotificationThread.prefix)
                        && entry.userInfo["instance_origin"] as? String == instanceOrigin
                        && entry.userInfo["account_id"] as? String == accountID
                }
                .map(\.identifier)
            center.removeDeliveredNotifications(withIdentifiers: delivered)
        }
    }
}
