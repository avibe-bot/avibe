import ActivityKit
import AVFoundation
import SwiftUI

/// Navigation-like: a voice-prompt audio session that keeps playing in the
/// background (a looping tone file), and a Live Activity of its own. Logs
/// every 2 s whether it is still playing.
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
    private var player: AVAudioPlayer?
    private var timer: Timer?

    func start() {
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(
                .playback, mode: .voicePrompt, options: [.duckOthers, .interruptSpokenAudioAndMixWithOthers]
            )
            try session.setActive(true)
            let url = try Self.toneFile()
            let player = try AVAudioPlayer(contentsOf: url)
            player.numberOfLoops = -1
            player.volume = 0.3
            player.play()
            self.player = player
            ProbeLog.record("helper playing \(player.isPlaying)")
        } catch {
            ProbeLog.record("helper audio failed \(ProbeLog.describe(error))")
        }
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            ProbeLog.record("helper tick playing=\(self?.player?.isPlaying ?? false) otherAudio=\(AVAudioSession.sharedInstance().isOtherAudioPlaying)")
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

    /// Two seconds of a 440 Hz tone as 16-bit mono WAV.
    static func toneFile() throws -> URL {
        let rate = 44_100, seconds = 2
        var samples = Data()
        for n in 0..<(rate * seconds) {
            var value = Int16(sin(2 * Double.pi * 440 * Double(n) / Double(rate)) * 8000).littleEndian
            samples.append(Data(bytes: &value, count: 2))
        }
        func le32(_ v: UInt32) -> Data { withUnsafeBytes(of: v.littleEndian) { Data($0) } }
        func le16(_ v: UInt16) -> Data { withUnsafeBytes(of: v.littleEndian) { Data($0) } }
        var wav = Data("RIFF".utf8) + le32(UInt32(36 + samples.count)) + Data("WAVEfmt ".utf8)
        wav += le32(16) + le16(1) + le16(1) + le32(UInt32(rate)) + le32(UInt32(rate * 2)) + le16(2) + le16(16)
        wav += Data("data".utf8) + le32(UInt32(samples.count)) + samples
        let url = FileManager.default.temporaryDirectory.appending(path: "tone.wav")
        try wav.write(to: url)
        return url
    }
}
