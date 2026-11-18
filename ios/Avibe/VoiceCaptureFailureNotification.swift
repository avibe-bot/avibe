import Foundation
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
        content.body = String(localized: VoiceCaptureCopy.reasonLine(
            state.reason ?? .recordingFailed, errorCode: state.errorCode, step: state.failedStep
        ))
        content.threadIdentifier = thread
        return content
    }

    static func post(_ state: VoiceCaptureAttributes.ContentState) {
        let request = UNNotificationRequest(identifier: identifier, content: content(for: state), trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }
}
