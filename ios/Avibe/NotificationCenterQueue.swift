import Foundation
import UserNotifications

/// Where the App calls the notification center: one serial queue, off the
/// main thread.
///
/// `UNUserNotificationCenter` reaches the system's notification service over
/// XPC, and some of its calls (removing notifications among them) wait for
/// that service before they return. While the service is not ready, such a
/// call on the main thread holds the whole App: on the CI simulator one held
/// it for 120 s, until SpringBoard's notification service had loaded, and
/// every WebView's callbacks waited behind it. The center is thread-safe, so
/// every call except setting the delegate and the authorization prompt goes
/// through here.
///
/// Calls run in the order they were submitted: a removal submitted after an
/// add never overtakes it.
final class NotificationCenterQueue: @unchecked Sendable {
    /// The App's one queue for the system's center.
    static let system = NotificationCenterQueue(label: "bot.avibe.ios.notification-center")

    private let queue: DispatchQueue

    init(label: String) {
        queue = DispatchQueue(label: label, qos: .utility)
    }

    /// Runs `work` after every call submitted before it, off the main thread.
    /// `@Sendable`, so a closure written on the main actor is not taken to be
    /// isolated to it.
    func run(_ work: @escaping @Sendable () -> Void) {
        queue.async(execute: work)
    }

    /// What `work` answers through its completion, after every call
    /// submitted before it.
    func value<T>(_ work: @escaping @Sendable (@escaping @Sendable (T) -> Void) -> Void) async -> T {
        await withCheckedContinuation { continuation in
            queue.async {
                work { continuation.resume(returning: $0) }
            }
        }
    }
}
