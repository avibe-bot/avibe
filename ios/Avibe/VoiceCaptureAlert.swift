import SwiftUI
import UIKit

/// Says why "Talk to Main Bot" brought Avibe to the front instead of
/// listening, in the words the island would have used.
@MainActor
enum VoiceCaptureAlert {
    /// The alert's text: for a Live Activity that would not start, the
    /// failure line with its step and code (the notification's line, which
    /// may not show if notifications are off).
    static func message(
        _ reason: VoiceCaptureAttributes.Reason,
        failure: VoiceCaptureAttributes.ContentState?
    ) -> String {
        if reason == .activityFailed || reason == .recordingFailed, let failure, failure.failedStep != nil {
            return String(localized: VoiceCaptureCopy.failureLine(failure))
        }
        return String(localized: VoiceCaptureCopy.reason(reason))
    }

    static func present(
        _ reason: VoiceCaptureAttributes.Reason,
        failure: VoiceCaptureAttributes.ContentState? = nil,
        attempt: Int = 0
    ) {
        guard let presenter = topViewController() else {
            // The scene is still coming up after `continueInForeground`.
            guard attempt < 20 else { return }
            Task { @MainActor in
                try? await Task.sleep(for: .milliseconds(250))
                present(reason, failure: failure, attempt: attempt + 1)
            }
            return
        }
        let alert = UIAlertController(
            title: String(localized: VoiceCaptureCopy.text(
                reason == .recordingFailed ? "voiceCapture.phase.failed" : "voiceCapture.phase.needsApp"
            )),
            message: message(reason, failure: failure),
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(
            title: String(localized: VoiceCaptureCopy.text("voiceCapture.action.ok")),
            style: .default
        ))
        presenter.present(alert, animated: true)
    }

    /// What the unsent alert says: 有一条语音没能发出去。 or, for several,
    /// 有 3 条语音没能发出去。
    static func unsentMessage(count: Int) -> String {
        count == 1
            ? String(localized: VoiceCaptureCopy.text("voiceCapture.unsent.one"))
            : String(localized: LocalizedStringResource(
                String.LocalizationValue("voiceCapture.unsent.many \(count)"), table: "VoiceCapture"
            ))
    }

    /// The Retry button: 重试, or for several takes 全部重试.
    static func unsentRetryTitle(count: Int) -> String {
        String(localized: VoiceCaptureCopy.text(
            count == 1 ? "voiceCapture.action.retry" : "voiceCapture.action.retryAll"
        ))
    }

    /// One alert that takes are not sent (没发出去), with Retry (the
    /// default) and Discard, which deletes them and asks nothing more.
    /// `answer` gets `true` for Retry, `false` for Discard, and nil when
    /// the alert could not be shown.
    static func presentUnsent(count: Int, answer: @escaping (Bool?) -> Void, attempt: Int = 0) {
        guard let presenter = topViewController() else {
            guard attempt < 20 else {
                // Never shown: it is asked again the next time Avibe comes
                // to the front.
                answer(nil)
                return
            }
            Task { @MainActor in
                try? await Task.sleep(for: .milliseconds(250))
                presentUnsent(count: count, answer: answer, attempt: attempt + 1)
            }
            return
        }
        let alert = UIAlertController(
            title: String(localized: VoiceCaptureCopy.text("voiceCapture.phase.failed")),
            message: unsentMessage(count: count),
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(
            title: String(localized: VoiceCaptureCopy.text("voiceCapture.action.discard")),
            style: .destructive
        ) { _ in answer(false) })
        let retry = UIAlertAction(title: unsentRetryTitle(count: count), style: .default) { _ in answer(true) }
        alert.addAction(retry)
        alert.preferredAction = retry
        presenter.present(alert, animated: true)
    }

    private static func topViewController() -> UIViewController? {
        let window = UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .filter { $0.activationState == .foregroundActive }
            .flatMap(\.windows)
            .first { $0.isKeyWindow }
        var top = window?.rootViewController
        while let presented = top?.presentedViewController {
            top = presented
        }
        return top
    }
}
