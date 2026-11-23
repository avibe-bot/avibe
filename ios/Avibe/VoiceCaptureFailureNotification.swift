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

    /// Schedules the unsent notice for `date` (replacing one already
    /// scheduled), so the system shows it whether or not the App is running
    /// then; with nil removes it, pending or shown. Tapping it opens Avibe,
    /// which then offers Retry.
    static func scheduleUnsent(at date: Date?) {
        let center = UNUserNotificationCenter.current()
        guard let date else {
            center.removePendingNotificationRequests(withIdentifiers: [unsentIdentifier])
            center.removeDeliveredNotifications(withIdentifiers: [unsentIdentifier])
            return
        }
        let request = UNNotificationRequest(
            identifier: unsentIdentifier,
            content: unsentContent(),
            trigger: UNTimeIntervalNotificationTrigger(timeInterval: max(date.timeIntervalSinceNow, 1), repeats: false)
        )
        center.add(request) { error in
            if let error { logger.error("unsent notice not scheduled: \(error, privacy: .public)") }
        }
    }

    static func post(_ state: VoiceCaptureAttributes.ContentState) {
        let request = UNNotificationRequest(identifier: identifier, content: content(for: state), trigger: nil)
        UNUserNotificationCenter.current().add(request) { error in
            if let error { logger.error("failure notice not posted: \(error, privacy: .public)") }
        }
    }
}
