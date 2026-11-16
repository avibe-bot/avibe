import ActivityKit
import AppIntents
import SwiftUI
import WidgetKit

@main
struct AvibeLiveActivityBundle: WidgetBundle {
    var body: some Widget {
        VoiceCaptureLiveActivity()
    }
}

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
                    VoiceCaptureGlyph(phase: context.state.phase)
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
                }
            } compactLeading: {
                VoiceCaptureGlyph(phase: context.state.phase)
            } compactTrailing: {
                VoiceCaptureClock(state: context.state)
                    .monospacedDigit()
                    .frame(maxWidth: 44)
            } minimal: {
                VoiceCaptureGlyph(phase: context.state.phase)
            }
            .keylineTint(.orange)
        }
    }
}

enum VoiceCaptureCopy {
    static func title(_ state: VoiceCaptureAttributes.ContentState) -> LocalizedStringResource {
        switch state.phase {
        case .listening: return "voiceCapture.phase.listening"
        case .sending: return "voiceCapture.phase.sending"
        case .sent: return "voiceCapture.phase.sent"
        case .failed: return "voiceCapture.phase.failed"
        case .needsApp: return "voiceCapture.phase.needsApp"
        }
    }

    static func reason(_ reason: VoiceCaptureAttributes.Reason) -> LocalizedStringResource {
        switch reason {
        case .offline: return "voiceCapture.reason.offline"
        case .nothingHeard: return "voiceCapture.reason.nothingHeard"
        case .transcriptionFailed: return "voiceCapture.reason.transcriptionFailed"
        case .sendFailed: return "voiceCapture.reason.sendFailed"
        case .signIn: return "voiceCapture.reason.signIn"
        case .microphone: return "voiceCapture.reason.microphone"
        case .consent: return "voiceCapture.reason.consent"
        case .updateAvibe: return "voiceCapture.reason.updateAvibe"
        case .voiceUnavailable: return "voiceCapture.reason.voiceUnavailable"
        case .liveActivities: return "voiceCapture.reason.liveActivities"
        case .interrupted: return "voiceCapture.reason.interrupted"
        }
    }
}

struct VoiceCaptureGlyph: View {
    let phase: VoiceCaptureAttributes.Phase

    var body: some View {
        switch phase {
        case .listening:
            Image(systemName: "waveform")
                .symbolEffect(.variableColor.iterative)
                .foregroundStyle(.orange)
        case .sending:
            Image(systemName: "arrow.up.circle")
                .foregroundStyle(.orange)
        case .sent:
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
        case .failed:
            Image(systemName: "exclamationmark.circle.fill")
                .foregroundStyle(.red)
        case .needsApp:
            Image(systemName: "arrow.up.forward.app")
                .foregroundStyle(.orange)
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

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let heard = state.heard, state.phase == .sent {
                Text(heard)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            } else if let reason = state.reason {
                Text(VoiceCaptureCopy.reason(reason))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            switch state.phase {
            case .listening:
                HStack {
                    Button(intent: VoiceCaptureCancelIntent()) {
                        Text("voiceCapture.action.cancel").frame(maxWidth: .infinity)
                    }
                    .tint(.gray)
                    Button(intent: VoiceCaptureSendIntent()) {
                        Text("voiceCapture.action.send").frame(maxWidth: .infinity)
                    }
                    .tint(.orange)
                }
            case .failed where state.reason.map(VoiceCaptureDetail.retryable) == true:
                HStack {
                    Button(intent: VoiceCaptureCancelIntent()) {
                        Text("voiceCapture.action.discard").frame(maxWidth: .infinity)
                    }
                    .tint(.gray)
                    Button(intent: VoiceCaptureRetryIntent()) {
                        Text("voiceCapture.action.retry").frame(maxWidth: .infinity)
                    }
                    .tint(.orange)
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
                VoiceCaptureGlyph(phase: state.phase)
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
