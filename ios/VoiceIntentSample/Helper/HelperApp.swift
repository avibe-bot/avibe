import ActivityKit
import AVFoundation
import SwiftUI

/// Navigation-like: a voice-prompt audio session playing in the background,
/// and a Live Activity of its own occupying the island.
@main
struct HelperApp: App {
    init() {
        Navigator.shared.start()
    }

    var body: some Scene {
        WindowGroup {
            Text("Nav Helper")
        }
    }
}

final class Navigator {
    static let shared = Navigator()
    private let engine = AVAudioEngine()
    private var phase = 0.0

    func start() {
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(
                .playback, mode: .voicePrompt, options: [.duckOthers, .interruptSpokenAudioAndMixWithOthers]
            )
            try session.setActive(true)
            let format = engine.outputNode.inputFormat(forBus: 0)
            let rate = format.sampleRate
            let source = AVAudioSourceNode { [unowned self] _, _, frameCount, buffers -> OSStatus in
                let list = UnsafeMutableAudioBufferListPointer(buffers)
                for frame in 0..<Int(frameCount) {
                    let value = Float(sin(self.phase) * 0.05)
                    self.phase += 2 * .pi * 440 / rate
                    for buffer in list {
                        buffer.mData?.assumingMemoryBound(to: Float.self)[frame] = value
                    }
                }
                return noErr
            }
            engine.attach(source)
            engine.connect(source, to: engine.mainMixerNode, format: AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1))
            try engine.start()
            ProbeLog.record("helper playing")
        } catch {
            ProbeLog.record("helper audio failed \(ProbeLog.describe(error))")
        }
        do {
            _ = try Activity.request(
                attributes: ProbeAttributes(app: "Nav"),
                content: ActivityContent(state: .init(label: "Navigating"), staleDate: nil)
            )
            ProbeLog.record("helper liveActivity ok")
        } catch {
            ProbeLog.record("helper liveActivity failed \(ProbeLog.describe(error))")
        }
    }
}
