#if DEBUG
import ActivityKit
import Foundation

/// Test seam, DEBUG builds only (compiled out of Release): with
/// `AVIBE_FIXTURE_TURN_ACTIVITY` in the launch environment the App starts one
/// fixture Turn card, so a UI test can check how the system draws it
/// (TurnLiveActivityLayoutUITests). The App's model stays dormant
/// (`LaunchServices`), so nothing else starts or ends a card.
///
/// The value is JSON: `phase` (a `TurnActivityAttributes.Phase`), `elapsed`
/// (seconds since the Turn started), `title`, `untitled` (a session without
/// a title), `stale` (last confirmed long enough ago that the card turns
/// stale a few seconds after it starts). `end` only ends the cards a fixture
/// left.
///
/// The same for the Action Button's voice card: `AVIBE_FIXTURE_VOICE_ACTIVITY`
/// with `{"elapsed": seconds}` starts one listening take's card (its timer
/// running), for VoiceLiveActivityLayoutUITests; `end` ends it.
enum TurnActivityFixtureLaunch {
    static let key = "AVIBE_FIXTURE_TURN_ACTIVITY"
    static var requested: String? { ProcessInfo.processInfo.environment[key] }
    static let voiceKey = "AVIBE_FIXTURE_VOICE_ACTIVITY"
    static var voiceRequested: String? { ProcessInfo.processInfo.environment[voiceKey] }
    /// Either card asked for: the App's model stays dormant.
    static var anyRequested: Bool { requested != nil || voiceRequested != nil }

    struct VoiceSpec: Decodable {
        var elapsed: Int?
    }

    @MainActor static func runVoice() async {
        guard let requested = voiceRequested else { return }
        for card in Activity<VoiceCaptureAttributes>.activities {
            await card.end(nil, dismissalPolicy: .immediate)
        }
        guard requested != "end" else { return }
        guard let spec = try? JSONDecoder().decode(VoiceSpec.self, from: Data(requested.utf8)) else {
            NSLog("[voice-fixture] unreadable spec")
            return
        }
        let state = VoiceCaptureAttributes.ContentState(
            phase: .listening, startedAt: Date().addingTimeInterval(-Double(spec.elapsed ?? 54))
        )
        do {
            let card = try Activity.request(
                attributes: VoiceCaptureAttributes(botName: "奶酪包"),
                content: ActivityContent(state: state, staleDate: nil),
                pushType: nil
            )
            NSLog("[voice-fixture] started %@", card.id)
        } catch {
            NSLog("[voice-fixture] request failed: %@", String(describing: error))
        }
    }

    struct Spec: Decodable {
        var phase: TurnActivityAttributes.Phase?
        var elapsed: Int?
        var title: String?
        var untitled: Bool?
        var stale: Bool?
    }

    @MainActor static func run() async {
        guard let requested else { return }
        for card in Activity<TurnActivityAttributes>.activities {
            await card.end(nil, dismissalPolicy: .immediate)
        }
        guard requested != "end" else { return }
        guard let spec = try? JSONDecoder().decode(Spec.self, from: Data(requested.utf8)) else {
            NSLog("[turn-fixture] unreadable spec")
            return
        }
        let now = Int(Date().timeIntervalSince1970)
        let phase = spec.phase ?? .working
        // A stale date in the past is refused: the card turns stale in 5 s.
        let confirmed = spec.stale == true ? now - 2400 + 5 : now
        let started = min(now - (spec.elapsed ?? 75), confirmed)
        let state = TurnActivityAttributes.ContentState(
            phase: phase, startedAt: started, endedAt: phase.isRunning ? nil : confirmed, confirmedAt: confirmed
        )
        let attributes = TurnActivityAttributes(
            agentName: "奶酪包",
            sessionTitle: spec.untitled == true ? nil : (spec.title ?? "修复登录页的验证码"),
            accountID: "fixture", instanceOrigin: "https://fixture.invalid",
            sessionID: "fixture", turnID: "fixture"
        )
        do {
            let card = try Activity.request(
                attributes: attributes,
                content: ActivityContent(state: state, staleDate: state.staleDate),
                pushType: nil
            )
            NSLog("[turn-fixture] started %@", card.id)
        } catch {
            NSLog("[turn-fixture] request failed: %@", String(describing: error))
        }
    }
}
#endif
