import ActivityKit
import SwiftUI
import UIKit
import WidgetKit

struct TurnLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: TurnActivityAttributes.self) { context in
            TurnActivityCard(attributes: context.attributes, state: context.state, isStale: context.isStale)
                .padding(16)
                .activityBackgroundTint(nil)
                .widgetURL(URL(string: "avibe-app://live-activity/\(context.activityID)"))
        } dynamicIsland: { context in
            let shown = TurnActivityShown(state: context.state, isStale: context.isStale)
            return DynamicIsland {
                DynamicIslandExpandedRegion(.bottom) {
                    TurnActivityCard(attributes: context.attributes, state: context.state, isStale: context.isStale)
                        .padding(.horizontal, 8).padding(.bottom, 4)
                }
            } compactLeading: {
                VoiceCaptureMark()
            } compactTrailing: {
                if shown.runsTimer && context.state.phase == .working {
                    TurnActivityClock(state: context.state)
                        .foregroundStyle(VoiceCaptureStyle.accent)
                } else {
                    TurnActivityGlyph(shown: shown)
                }
            } minimal: {
                TurnActivityGlyph(shown: shown)
            }
            .keylineTint(VoiceCaptureStyle.accent)
            .widgetURL(URL(string: "avibe-app://live-activity/\(context.activityID)"))
        }
    }
}

struct TurnActivityShown {
    let state: TurnActivityAttributes.ContentState
    let isStale: Bool
    init(state: TurnActivityAttributes.ContentState, isStale: Bool) {
        self.state = state
        self.isStale = isStale && state.phase.isRunning
    }
    var runsTimer: Bool { state.phase.isRunning && !isStale }
    var statusKey: String { isStale ? "turn.stale" : "turn.\(state.phase.rawValue)" }
    var symbol: String {
        if isStale { return "clock" }
        switch state.phase {
        case .working: return "circle.fill"
        case .needsYou: return "hand.raised"
        case .done: return "checkmark"
        case .failed: return "xmark"
        case .stopped: return "stop.fill"
        }
    }
    var tint: Color {
        if isStale { return .secondary }
        switch state.phase {
        case .working, .done: return VoiceCaptureStyle.accent
        case .needsYou: return TurnActivityStyle.gold
        case .failed: return TurnActivityStyle.destructive
        case .stopped: return .secondary
        }
    }
    private func localized(_ key: String, locale: Locale) -> String {
        String(localized: LocalizedStringResource(String.LocalizationValue(key), table: "TurnActivity", locale: locale))
    }
    func detail(locale: Locale) -> String {
        if isStale {
            let time = Date(timeIntervalSince1970: Double(state.confirmedAt)).formatted(Date.FormatStyle(date: .omitted, time: .shortened).locale(locale))
            return String(format: localized("turn.stale.detail", locale: locale), time)
        }
        let duration = Duration.seconds(state.elapsed(at: Date())).formatted(.units(allowed: [.hours, .minutes, .seconds], width: .abbreviated, maximumUnitCount: 2).locale(locale))
        return String(format: localized("turn.\(state.phase.rawValue).detail", locale: locale), duration)
    }
}

struct TurnActivityGlyph: View {
    let shown: TurnActivityShown
    var body: some View {
        Image(systemName: shown.symbol)
            .font(.system(size: shown.state.phase == .working && !shown.isStale ? 9 : 13, weight: .semibold))
            .foregroundStyle(shown.tint)
            .accessibilityHidden(true)
    }
}

/// The running time, trailing in a slot as wide as its longest value
/// (`0:00:00`, in the same font). A timer's text has no bounded width of its
/// own: fixed at its ideal size it laid the whole card out infinitely wide and
/// the system drew a black card and island (11-26). Checked on the simulator
/// by TurnLiveActivityLayoutUITests.
struct TurnActivityClock: View {
    let state: TurnActivityAttributes.ContentState
    var body: some View {
        let start = Date(timeIntervalSince1970: Double(state.startedAt))
        Text(verbatim: "0:00:00").hidden()
            .overlay(alignment: .trailing) {
                Text(timerInterval: start...start.addingTimeInterval(28800), countsDown: false)
                    .multilineTextAlignment(.trailing).lineLimit(1)
            }
            .monospacedDigit()
    }
}

/// Shared card layout for Lock Screen and expanded island, following ios/live-activity frames.
struct TurnActivityCard: View {
    @Environment(\.locale) private var locale
    let attributes: TurnActivityAttributes
    let state: TurnActivityAttributes.ContentState
    let isStale: Bool
    var body: some View {
        let shown = TurnActivityShown(state: state, isStale: isStale)
        HStack(alignment: .top, spacing: 12) {
            Image("AvibeMark").resizable().scaledToFit().frame(width: 36, height: 36)
                .clipShape(Circle()).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(attributes.sessionTitle ?? attributes.agentName)
                    .font(.system(size: 15, weight: .semibold)).lineLimit(1).truncationMode(.tail)
                if attributes.sessionTitle != nil {
                    Text(attributes.agentName).font(.system(size: 13)).foregroundStyle(.secondary)
                        .lineLimit(1).truncationMode(.tail)
                }
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    // One width for every state's symbol, so the words start
                    // at the same place after a dot, a tick or a hand.
                    TurnActivityGlyph(shown: shown).frame(width: 16)
                    (Text(LocalizedStringKey(shown.statusKey), tableName: "TurnActivity")
                        .fontWeight(.semibold).foregroundColor(shown.tint)
                     + Text(" · " + shown.detail(locale: locale)).foregroundColor(.secondary))
                        .font(.system(size: 13)).fixedSize(horizontal: false, vertical: true)
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
            if shown.runsTimer {
                TurnActivityClock(state: state).font(.system(size: 20, weight: .semibold))
            }
        }
        .accessibilityElement(children: .combine)
    }
}


/// Native equivalents of the reviewed gold-ink and destructive-ink tokens.
private enum TurnActivityStyle {
    static let gold = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 1, green: 200.0 / 255, blue: 87.0 / 255, alpha: 1)
            : UIColor(red: 217.0 / 255, green: 119.0 / 255, blue: 6.0 / 255, alpha: 1)
    })
    static let destructive = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 1, green: 107.0 / 255, blue: 107.0 / 255, alpha: 1)
            : UIColor(red: 225.0 / 255, green: 29.0 / 255, blue: 72.0 / 255, alpha: 1)
    })
}
