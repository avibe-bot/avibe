import ActivityKit
import UIKit
import AppIntents
import SwiftUI
import WidgetKit

/// The background voice take in the Dynamic Island and on the Lock Screen.
/// Prototype layout; the reviewed frames replace it.
struct VoiceCaptureLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: VoiceCaptureAttributes.self) { context in
            VoiceCaptureLockScreenView(state: context.state)
                .padding(16)
                .activityBackgroundTint(nil)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    VoiceCaptureGlyph(state: context.state)
                        .font(.title2)
                        .padding(.leading, 4)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    VoiceCaptureClock(state: context.state)
                        .font(.title3.monospacedDigit())
                        .padding(.trailing, 4)
                }
                DynamicIslandExpandedRegion(.center) {
                    Text(VoiceCaptureCopy.title(context.state))
                        .font(.headline)
                        .lineLimit(1)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VoiceCaptureDetail(state: context.state)
                        .padding(.horizontal, VoiceCaptureDetail.islandInset)
                        .padding(.bottom, 4)
                }
            } compactLeading: {
                VoiceCaptureMark()
            } compactTrailing: {
                VoiceCaptureCompactTrailing(state: context.state)
            } minimal: {
                VoiceCaptureGlyph(state: context.state)
            }
            .keylineTint(VoiceCaptureStyle.accent)
        }
    }
}

/// The Live Activity's words, from the `VoiceCapture` table, which both
/// the App and the extension carry (the App renders the review frames).
enum VoiceCaptureCopy {
    static func text(_ key: String) -> LocalizedStringResource {
        LocalizedStringResource(String.LocalizationValue(key), table: "VoiceCapture")
    }

    static func title(_ state: VoiceCaptureAttributes.ContentState) -> LocalizedStringResource {
        if state.phase == .failed, state.reason == .updateAvibe {
            return text("voiceCapture.phase.updateRequired")
        }
        switch state.phase {
        case .listening: return text("voiceCapture.phase.listening")
        case .sending: return text("voiceCapture.phase.sending")
        case .sent: return text("voiceCapture.phase.sent")
        case .failed: return text("voiceCapture.phase.failed")
        case .needsApp: return text("voiceCapture.phase.needsApp")
        }
    }

    static func reason(_ reason: VoiceCaptureAttributes.Reason) -> LocalizedStringResource {
        switch reason {
        case .offline: return text("voiceCapture.reason.offline")
        case .nothingHeard: return text("voiceCapture.reason.nothingHeard")
        case .transcriptionFailed: return text("voiceCapture.reason.transcriptionFailed")
        case .sendFailed: return text("voiceCapture.reason.sendFailed")
        case .signIn: return text("voiceCapture.reason.signIn")
        case .microphone: return text("voiceCapture.reason.microphone")
        case .consent: return text("voiceCapture.reason.consent")
        case .updateAvibe: return text("voiceCapture.reason.updateAvibe")
        case .voiceUnavailable: return text("voiceCapture.reason.voiceUnavailable")
        case .liveActivities: return text("voiceCapture.reason.liveActivities")
        case .interrupted: return text("voiceCapture.reason.interrupted")
        }
    }
}

/// Avibe's colours: the mint accent (the Web UI's `--primary`, darker in
/// light mode) and red for errors only.
enum VoiceCaptureStyle {
    static let accent = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 0x5B / 255, green: 0xFF / 255, blue: 0xA0 / 255, alpha: 1)
            : UIColor(red: 0x10 / 255, green: 0xB9 / 255, blue: 0x81 / 255, alpha: 1)
    })
    /// Text on an accent-filled button.
    static let onAccent = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 0x08 / 255, green: 0x08 / 255, blue: 0x12 / 255, alpha: 1)
            : .white
    })
}

/// The Avibe mark, in the compact island's leading slot.
struct VoiceCaptureMark: View {
    var body: some View {
        Image("AvibeMark")
            .resizable()
            .scaledToFit()
            .frame(width: 22, height: 22)
            .clipShape(Circle())
    }
}

/// The compact island's trailing slot: the timer while it records or sends,
/// otherwise the outcome.
struct VoiceCaptureCompactTrailing: View {
    let state: VoiceCaptureAttributes.ContentState

    var body: some View {
        switch state.phase {
        case .listening, .sending:
            VoiceCaptureClock(state: state)
                .monospacedDigit()
                .foregroundStyle(VoiceCaptureStyle.accent)
                .frame(maxWidth: 44)
        default:
            VoiceCaptureGlyph(state: state)
        }
    }
}

struct VoiceCaptureGlyph: View {
    let state: VoiceCaptureAttributes.ContentState

    var body: some View {
        switch state.phase {
        case .listening:
            Image(systemName: "waveform")
                .symbolEffect(.variableColor.iterative)
                .foregroundStyle(VoiceCaptureStyle.accent)
        case .sending:
            Image(systemName: "arrow.up.circle")
                .foregroundStyle(VoiceCaptureStyle.accent)
        case .sent:
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(VoiceCaptureStyle.accent)
        case .failed where state.reason == .updateAvibe:
            Image(systemName: "arrow.up.forward.app")
                .foregroundStyle(VoiceCaptureStyle.accent)
        case .failed:
            Image(systemName: "exclamationmark.circle.fill")
                .foregroundStyle(.red)
        case .needsApp:
            Image(systemName: "arrow.up.forward.app")
                .foregroundStyle(VoiceCaptureStyle.accent)
        }
    }
}

struct VoiceCaptureClock: View {
    let state: VoiceCaptureAttributes.ContentState

    var body: some View {
        if state.phase == .listening {
            Text(timerInterval: state.startedAt...Date.distantFuture, countsDown: false)
        } else if let endedAt = state.endedAt, state.phase == .sending {
            Text(Duration.seconds(endedAt.timeIntervalSince(state.startedAt)),
                 format: .time(pattern: .minuteSecond))
        } else {
            EmptyView()
        }
    }
}

struct VoiceCaptureDetail: View {
    let state: VoiceCaptureAttributes.ContentState

    /// Keeps text and buttons clear of the expanded island's rounded ends.
    static let islandInset: CGFloat = 14

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let heard = state.heard, state.phase == .sent {
                Text(heard)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .truncationMode(.tail)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else if let reason = state.reason {
                Text(VoiceCaptureCopy.reason(reason))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            switch state.phase {
            case .listening:
                HStack(spacing: 10) {
                    Button(intent: VoiceCaptureCancelIntent()) {
                        Text(VoiceCaptureCopy.text("voiceCapture.action.cancel")).font(.subheadline.weight(.semibold)).frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    .tint(.gray)
                    Button(intent: VoiceCaptureSendIntent()) {
                        Text(VoiceCaptureCopy.text("voiceCapture.action.send")).font(.subheadline.weight(.semibold)).frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(VoiceCaptureStyle.accent)
                    .foregroundStyle(VoiceCaptureStyle.onAccent)
                }
            case .failed where state.reason.map(VoiceCaptureDetail.retryable) == true:
                HStack(spacing: 10) {
                    Button(intent: VoiceCaptureCancelIntent()) {
                        Text(VoiceCaptureCopy.text("voiceCapture.action.discard")).font(.subheadline.weight(.semibold)).frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    .tint(.gray)
                    Button(intent: VoiceCaptureRetryIntent()) {
                        Text(VoiceCaptureCopy.text("voiceCapture.action.retry")).font(.subheadline.weight(.semibold)).frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(VoiceCaptureStyle.accent)
                    .foregroundStyle(VoiceCaptureStyle.onAccent)
                }
            default:
                EmptyView()
            }
        }
    }

    static func retryable(_ reason: VoiceCaptureAttributes.Reason) -> Bool {
        switch reason {
        case .offline, .transcriptionFailed, .sendFailed: return true
        default: return false
        }
    }
}

struct VoiceCaptureLockScreenView: View {
    let state: VoiceCaptureAttributes.ContentState

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                VoiceCaptureGlyph(state: state)
                    .font(.title3)
                Text(VoiceCaptureCopy.title(state))
                    .font(.headline)
                Spacer()
                VoiceCaptureClock(state: state)
                    .font(.headline.monospacedDigit())
            }
            VoiceCaptureDetail(state: state)
        }
    }
}
