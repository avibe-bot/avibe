import ActivityKit
import AppIntents
import Foundation

/// The Live Activity a background voice take shows in the Dynamic Island and
/// on the Lock Screen while it records and is sent to the main Bot.
///
/// This file is compiled into both the App and the `AvibeLiveActivity`
/// extension. The extension only draws; every action runs in the App's
/// process (`LiveActivityIntent`), so the intents below call the App's
/// controller only when compiled into the App.
struct VoiceCaptureAttributes: ActivityAttributes {
    enum Phase: String, Codable, Hashable {
        /// The microphone is open.
        case listening
        /// Recording stopped; the words are being transcribed and sent.
        case sending
        /// The main Bot received the message.
        case sent
        /// Transcribing or sending failed; the take is kept for Retry.
        case failed
        /// Avibe has to open before this can work (sign-in, microphone
        /// permission, voice-input consent, an older Avibe on the computer).
        case needsApp
    }

    /// Why a take failed or needs the App. The extension maps each to copy.
    enum Reason: String, Codable, Hashable {
        case offline
        case nothingHeard
        case transcriptionFailed
        case sendFailed
        case signIn
        case microphone
        case consent
        case updateAvibe
        case voiceUnavailable
        case liveActivities
        /// The system would not start the Live Activity, so nothing was
        /// recorded.
        case activityFailed
        case interrupted
    }

    struct ContentState: Codable, Hashable {
        var phase: Phase
        /// When the microphone opened; the timer counts from here.
        var startedAt: Date
        /// When the microphone closed, so the timer stops there.
        var endedAt: Date?
        var reason: Reason?
        /// The start of what was heard, shown once sent. Bounded so the
        /// activity stays under ActivityKit's 4 KB payload.
        var heard: String?

        static let heardLimit = 160
    }

    /// The main Bot's display name, when the App knows it.
    var botName: String?
}

/// The island's Send button: stop recording and send what was heard.
struct VoiceCaptureSendIntent: LiveActivityIntent {
    static let title: LocalizedStringResource = "Send"
    static let isDiscoverable = false

    func perform() async throws -> some IntentResult {
        #if !AVIBE_LIVE_ACTIVITY_EXTENSION
        await VoiceCaptureController.shared.finish(send: true)
        #endif
        return .result()
    }
}

/// The island's Cancel button: stop recording and discard the take.
struct VoiceCaptureCancelIntent: LiveActivityIntent {
    static let title: LocalizedStringResource = "Cancel"
    static let isDiscoverable = false

    func perform() async throws -> some IntentResult {
        #if !AVIBE_LIVE_ACTIVITY_EXTENSION
        await VoiceCaptureController.shared.finish(send: false)
        #endif
        return .result()
    }
}

/// Retry on a failed take: send the kept recording again.
struct VoiceCaptureRetryIntent: LiveActivityIntent {
    static let title: LocalizedStringResource = "Retry"
    static let isDiscoverable = false

    func perform() async throws -> some IntentResult {
        #if !AVIBE_LIVE_ACTIVITY_EXTENSION
        await VoiceCaptureController.shared.retry()
        #endif
        return .result()
    }
}
