import CoreFoundation
import Foundation
import UserNotifications

/// What the App icon badge needs from the notification center: whether the
/// user lets this app badge its icon, and setting the count.
protocol AppBadgeCentering: AnyObject {
    func badgeAllowed() async -> Bool
    func setBadgeCount(_ count: Int) async throws
}

/// The system's notification center, called on `NotificationCenterQueue`
/// (never on the main thread).
final class SystemAppBadgeCenter: AppBadgeCentering {
    private let queue: NotificationCenterQueue

    init(queue: NotificationCenterQueue = .system) {
        self.queue = queue
    }

    func badgeAllowed() async -> Bool {
        await queue.value { answer in
            UNUserNotificationCenter.current().getNotificationSettings { settings in
                switch settings.authorizationStatus {
                case .authorized, .provisional, .ephemeral:
                    answer(settings.badgeSetting == .enabled)
                case .denied, .notDetermined:
                    answer(false)
                @unknown default:
                    answer(false)
                }
            }
        }
    }

    func setBadgeCount(_ count: Int) async throws {
        let failure: Error? = await queue.value { answer in
            UNUserNotificationCenter.current().setBadgeCount(count) { error in
                answer(error)
            }
        }
        if let failure {
            throw failure
        }
    }
}

/// The count `badge.set({count})` asks for (`docs/plans/ios-app.md` section 4).
enum NativeBadgeCount {
    /// The largest count the badge shows; a larger one shows this.
    static let maximum = Int(Int32.max)

    /// The count `params` asks for: exactly one `count` number. A negative
    /// count clears the badge, a fraction is rounded down, and a count above
    /// `maximum` shows `maximum`. Any other value, or another parameter, is
    /// nil.
    static func parse(_ params: [String: Any]) -> Int? {
        guard
            params.count == 1,
            let number = params["count"] as? NSNumber,
            CFGetTypeID(number) != CFBooleanGetTypeID()
        else {
            return nil
        }
        let value = number.doubleValue
        guard !value.isNaN else {
            return nil
        }
        if value <= 0 {
            return 0
        }
        if value >= Double(maximum) {
            return maximum
        }
        return Int(value.rounded(.down))
    }
}

/// The App icon badge. Requests are applied one at a time, in order, and only
/// the latest waiting one is applied: a burst of Inbox changes ends on the
/// badge the last one asked for. Nothing is set while the user does not allow
/// the App's badges.
@MainActor
final class NativeBadge {
    private let center: AppBadgeCentering
    private var waiting: Int?
    private var applying: Task<Void, Never>?

    init(center: AppBadgeCentering = SystemAppBadgeCenter()) {
        self.center = center
    }

    func set(_ count: Int) {
        waiting = min(max(count, 0), NativeBadgeCount.maximum)
        guard applying == nil else {
            return
        }
        applying = Task { [weak self] in
            await self?.applyWaiting()
        }
    }

    func clear() {
        set(0)
    }

    private func applyWaiting() async {
        while let count = waiting {
            waiting = nil
            guard await center.badgeAllowed() else {
                continue
            }
            try? await center.setBadgeCount(count)
        }
        applying = nil
    }
}
