import SwiftUI
import UIKit

/// Says why "Talk to Main Bot" brought Avibe to the front instead of
/// listening, in the words the island would have used.
@MainActor
enum VoiceCaptureAlert {
    /// The alert's text: for a Live Activity that would not start, the
    /// failure line with its step and code (the notification's line, which
    /// may not show if notifications are off). None where the title says it
    /// all (`confirmPending`).
    static func message(
        _ reason: VoiceCaptureAttributes.Reason,
        failure: VoiceCaptureAttributes.ContentState?
    ) -> String? {
        guard VoiceCaptureCopy.showsLine(for: reason) else { return nil }
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

    /// How long ago a take was spoken, in minutes under an hour and whole
    /// hours from there (a take is kept a day at most), never less than a
    /// minute. Chinese as Product writes it, with a space between the
    /// number and the unit (25 分钟前, 3 小时前); otherwise the system's
    /// relative wording (25 minutes ago, 3 hours ago).
    static func age(
        of spoken: Date,
        now: Date = Date(),
        language: String = Bundle.main.preferredLocalizations.first ?? "en"
    ) -> String {
        let minutes = max(1, Int(now.timeIntervalSince(spoken) / 60))
        if language.hasPrefix("zh") {
            return minutes < 60 ? "\(minutes) 分钟前" : "\(minutes / 60) 小时前"
        }
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = Locale(identifier: language)
        formatter.unitsStyle = .full
        formatter.dateTimeStyle = .numeric
        return formatter.localizedString(
            fromTimeInterval: -Double(minutes < 60 ? minutes * 60 : minutes / 60 * 3600)
        )
    }

    /// What the unsent alert says: 有一条 3 小时前的语音没能发出去。 or, for
    /// several, 有 3 条语音没能发出去，最早的是 3 小时前。
    static func unsentMessage(count: Int, oldest: Date, now: Date = Date()) -> String {
        let age = age(of: oldest, now: now)
        return count == 1
            ? String(localized: LocalizedStringResource(
                String.LocalizationValue("voiceCapture.unsent.one \(age)"), table: "VoiceCapture"
            ))
            : String(localized: LocalizedStringResource(
                String.LocalizationValue("voiceCapture.unsent.many \(count) \(age)"), table: "VoiceCapture"
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
    static func presentUnsent(
        count: Int, oldest: Date, answer: @escaping (Bool?) -> Void, attempt: Int = 0
    ) {
        guard let presenter = topViewController() else {
            guard attempt < 20 else {
                // Never shown: it is asked again the next time Avibe comes
                // to the front.
                answer(nil)
                return
            }
            Task { @MainActor in
                try? await Task.sleep(for: .milliseconds(250))
                presentUnsent(count: count, oldest: oldest, answer: answer, attempt: attempt + 1)
            }
            return
        }
        let alert = UIAlertController(
            title: String(localized: VoiceCaptureCopy.text("voiceCapture.phase.failed")),
            message: unsentMessage(count: count, oldest: oldest),
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(
            title: String(localized: VoiceCaptureCopy.text("voiceCapture.action.discard")),
            style: .destructive
        ) { _ in answer(false) })
        let retry = UIAlertAction(title: unsentRetryTitle(count: count), style: .default) { _ in answer(true) }
        alert.addAction(retry)
        alert.preferredAction = retry
        unsentAlert = alert
        presenter.present(alert, animated: true)
    }

    /// The unsent alert, while it is on screen. UIKit may drop an alert
    /// without running an action (its presenter went away), so what is
    /// asked is whether it is still presented, not whether it was answered.
    private static weak var unsentAlert: UIAlertController?

    static var unsentIsShown: Bool {
        unsentAlert?.presentingViewController != nil
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
