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
        content.body = String(localized: VoiceCaptureCopy.failureLine(state))
        content.threadIdentifier = thread
        return content
    }

    /// Needs notification permission (asked when the user turns on
    /// notifications); without it the system drops the notice, and the
    /// island or the alert carries the line instead.
    private static let logger = Logger(subsystem: "bot.avibe.ios", category: "voice-capture")

    static func post(_ state: VoiceCaptureAttributes.ContentState) {
        let request = UNNotificationRequest(identifier: identifier, content: content(for: state), trigger: nil)
        UNUserNotificationCenter.current().add(request) { error in
            if let error { logger.error("failure notice not posted: \(error, privacy: .public)") }
        }
    }
}
