import ActivityKit
import AppIntents
import AVFoundation
import SwiftUI
import UIKit

#if VARIANT_A
let probeVariant = "A"
#elseif VARIANT_B
let probeVariant = "B"
#else
let probeVariant = "C"
#endif

@main
struct ProbeApp: App {
    init() {
        ProbeLog.record("launch variant=\(probeVariant) state=\(ProbeRun.appState())")
        AVAudioApplication.requestRecordPermission { granted in
            ProbeLog.record("microphone permission \(granted)")
        }
    }

    var body: some Scene {
        WindowGroup {
            Text("Probe \(probeVariant)")
        }
    }
}

/// A: AudioRecordingIntent only. B: the same, the App has the audio
/// background mode. C: also a LiveActivityIntent.
#if VARIANT_C
struct ProbeIntent: AudioRecordingIntent, LiveActivityIntent {
    static let title: LocalizedStringResource = "Record C"
    @available(iOS 26.0, *)
    static var supportedModes: IntentModes { [.background] }

    @MainActor
    func perform() async throws -> some IntentResult {
        await ProbeRun.run()
        return .result()
    }
}
#else
struct ProbeIntent: AudioRecordingIntent {
    #if VARIANT_A
    static let title: LocalizedStringResource = "Record A"
    #else
    static let title: LocalizedStringResource = "Record B"
    #endif
    @available(iOS 26.0, *)
    static var supportedModes: IntentModes { [.background] }

    @MainActor
    func perform() async throws -> some IntentResult {
        await ProbeRun.run()
        return .result()
    }
}
#endif

struct ProbeShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        #if VARIANT_A
        AppShortcut(
            intent: ProbeIntent(), phrases: ["Record in \(.applicationName)"],
            shortTitle: "Record A", systemImageName: "mic"
        )
        #elseif VARIANT_B
        AppShortcut(
            intent: ProbeIntent(), phrases: ["Record in \(.applicationName)"],
            shortTitle: "Record B", systemImageName: "mic"
        )
        #else
        AppShortcut(
            intent: ProbeIntent(), phrases: ["Record in \(.applicationName)"],
            shortTitle: "Record C", systemImageName: "mic"
        )
        #endif
    }
}

@MainActor
enum ProbeRun {
    static func appState() -> String {
        switch UIApplication.shared.applicationState {
        case .active: "active"
        case .inactive: "inactive"
        case .background: "background"
        @unknown default: "unknown"
        }
    }

    /// One press: the Live Activity, then a `.record` session, else a
    /// mixable `.playAndRecord`, then three seconds of recording.
    static func run() async {
        let session = AVAudioSession.sharedInstance()
        let route = session.currentRoute
        ProbeLog.record(
            "press variant=\(probeVariant) state=\(appState()) otherAudio=\(session.isOtherAudioPlaying) "
                + "silenceHint=\(session.secondaryAudioShouldBeSilencedHint) "
                + "in=\(route.inputs.map(\.portType.rawValue)) out=\(route.outputs.map(\.portType.rawValue)) "
                + "permission=\(AVAudioApplication.shared.recordPermission.rawValue)"
        )
        var activity: Activity<ProbeAttributes>?
        do {
            activity = try Activity.request(
                attributes: ProbeAttributes(app: "Probe \(probeVariant)"),
                content: ActivityContent(state: .init(label: "Recording"), staleDate: nil)
            )
            ProbeLog.record("liveActivity ok")
        } catch {
            ProbeLog.record("liveActivity failed \(ProbeLog.describe(error))")
        }
        var active = false
        do {
            try session.setCategory(.record, mode: .default, options: [])
            try session.setActive(true)
            active = true
            ProbeLog.record("session record ok")
        } catch {
            ProbeLog.record("session record failed \(ProbeLog.describe(error))")
        }
        if !active {
            do {
                try session.setCategory(.playAndRecord, mode: .default, options: [.mixWithOthers, .duckOthers])
                try session.setActive(true)
                active = true
                ProbeLog.record("session playAndRecord+mix ok")
            } catch {
                ProbeLog.record("session playAndRecord+mix failed \(ProbeLog.describe(error))")
            }
        }
        if active {
            let url = FileManager.default.temporaryDirectory.appending(path: "take-\(UUID().uuidString).m4a")
            do {
                let recorder = try AVAudioRecorder(url: url, settings: [
                    AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
                    AVSampleRateKey: 16_000,
                    AVNumberOfChannelsKey: 1,
                ])
                let started = recorder.record()
                ProbeLog.record("record() \(started)")
                try? await Task.sleep(for: .seconds(3))
                ProbeLog.record("after 3s isRecording=\(recorder.isRecording) time=\(String(format: "%.2f", recorder.currentTime))")
                recorder.stop()
            } catch {
                ProbeLog.record("recorder failed \(ProbeLog.describe(error))")
            }
            try? session.setActive(false, options: .notifyOthersOnDeactivation)
        }
        if let activity {
            await activity.end(nil, dismissalPolicy: .immediate)
        }
        ProbeLog.record("done")
    }
}
