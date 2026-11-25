import Foundation
import OSLog
import UserNotifications

/// The notification a failed "Talk to Main Bot" start leaves in
/// Notification Center, so the line (step and system code) outlasts the
/// island. One identifier: a repeat replaces it rather than adding another.
enum VoiceCaptureFailureNotification {
    static let thread = "voice-capture"
    static let identifier = "voice-capture-failure"

    static func content(for state: VoiceCaptureAttributes.ContentState) -> UNMutableNotificationContent {
        let content = UNMutableNotificationContent()
        content.title = String(localized: VoiceCaptureCopy.title(state))
        if VoiceCaptureCopy.showsReasonLine(state) {
            content.body = String(localized: VoiceCaptureCopy.failureLine(state))
        }
        content.threadIdentifier = thread
        return content
    }

    /// Needs notification permission (asked when the user turns on
    /// notifications); without it the system drops the notice, and the
    /// island or the alert carries the line instead.
    private static let logger = Logger(subsystem: "bot.avibe.ios", category: "voice-capture")

    /// The notice that a take is still not sent: 没发出去 ·
    /// 这条语音没能发出去，打开 Avibe 可以重试。
    static let unsentIdentifier = "voice-capture-unsent"

    static func unsentContent() -> UNMutableNotificationContent {
        let content = UNMutableNotificationContent()
        content.title = String(localized: VoiceCaptureCopy.text("voiceCapture.phase.failed"))
        content.body = String(localized: VoiceCaptureCopy.text("voiceCapture.unsent.notice"))
        content.threadIdentifier = thread
        return content
    }

    /// What the notices need of the notification center. `system` is the
    /// App's own; each call is made on `NotificationCenterQueue`.
    struct Center: Sendable {
        var add: @Sendable (UNNotificationRequest, _ failure: String) -> Void
        /// Removes these identifiers, pending and shown.
        var remove: @Sendable ([String]) -> Void

        static let system = Center(
            add: { request, failure in
                UNUserNotificationCenter.current().add(request) { error in
                    if let error { VoiceCaptureFailureNotification.logger.error("\(failure, privacy: .public): \(error, privacy: .public)") }
                }
            },
            remove: { identifiers in
                let center = UNUserNotificationCenter.current()
                center.removePendingNotificationRequests(withIdentifiers: identifiers)
                center.removeDeliveredNotifications(withIdentifiers: identifiers)
            }
        )
    }

    /// Schedules the unsent notice for `date` (replacing one already
    /// scheduled), so the system shows it whether or not the App is running
    /// then; with nil removes it, pending or shown. Tapping it opens Avibe,
    /// which then offers Retry. Off the main thread, in call order: a removal
    /// never overtakes the schedule it clears.
    static func scheduleUnsent(
        at date: Date?,
        queue: NotificationCenterQueue = .system,
        center: Center = .system
    ) {
        guard let date else {
            queue.run { center.remove([unsentIdentifier]) }
            return
        }
        // The delay is the one asked for now, not when the queue reaches it.
        let request = UNNotificationRequest(
            identifier: unsentIdentifier,
            content: unsentContent(),
            trigger: UNTimeIntervalNotificationTrigger(timeInterval: max(date.timeIntervalSinceNow, 1), repeats: false)
        )
        queue.run { center.add(request, "unsent notice not scheduled") }
    }

    static func post(
        _ state: VoiceCaptureAttributes.ContentState,
        queue: NotificationCenterQueue = .system,
        center: Center = .system
    ) {
        let request = UNNotificationRequest(identifier: identifier, content: content(for: state), trigger: nil)
        queue.run { center.add(request, "failure notice not posted") }
    }
}
