import ActivityKit
import AVFoundation
import XCTest
@testable import Avibe

/// DRAFT-ONLY CI PROBE (not for merge): drives the real voice capture
/// controller in the App process against the stand-in instance on the CI
/// host. Proves the hidden WKWebView's authenticated requests (unknown 2)
/// and what the Simulator does with the recorder and the Live Activity.
@MainActor
final class VoiceCaptureProbeTests: XCTestCase {
    func testControllerRecordsAndSendsThroughTheHiddenWebView() async throws {
        let server = URL(string: "http://127.0.0.1:8765")!
        let cookies = await VoiceCaptureProbeSetup.setUp(origin: server)
        print("PROBE UNIT: store cookies after login \(cookies)")
        print("PROBE UNIT: mic permission \(AVAudioApplication.shared.recordPermission.rawValue) activities enabled \(ActivityAuthorizationInfo().areActivitiesEnabled)")

        let first = await VoiceCaptureController.shared.toggle()
        print("PROBE UNIT: first toggle -> \(first)")
        try await Task.sleep(for: .seconds(4))
        print("PROBE UNIT: live activities \(Activity<VoiceCaptureAttributes>.activities.map { "\($0.activityState) \($0.content.state.phase)" })")
        let second = await VoiceCaptureController.shared.toggle()
        print("PROBE UNIT: second toggle -> \(second)")
        try await Task.sleep(for: .seconds(2))
        print("PROBE UNIT: live activities after \(Activity<VoiceCaptureAttributes>.activities.map { "\($0.activityState) \($0.content.state.phase) \(String(describing: $0.content.state.reason))" })")

        // Unknown 2 alone, independent of the microphone: the client's
        // requests through the hidden WebView on the signed-in store.
        guard let target = VoiceCaptureTarget.current() else {
            return XCTFail("no probe target")
        }
        let client = VoiceCaptureInstanceClient(target: target)
        do {
            let status = try await client.status()
            print("PROBE UNIT: client status \(status)")
            let text = try await client.transcribe(audio: Data(repeating: 1, count: 2048), name: "voice.m4a", mime: "audio/mp4")
            print("PROBE UNIT: client transcribe \(text)")
            try await client.sendToMainBot(text)
            print("PROBE UNIT: client sent")
        } catch {
            print("PROBE UNIT: client failed \(error)")
        }
        client.close()
    }
}
