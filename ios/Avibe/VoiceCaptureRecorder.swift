import AVFoundation
import Foundation

/// Records one take to an AAC file (`.m4a`, which the instance's
/// transcription accepts) with the App's own microphone permission.
final class VoiceCaptureRecorder {
    enum Failure: Error {
        case permission
        case start
    }

    let url: URL
    private var recorder: AVAudioRecorder?

    init(url: URL) {
        self.url = url
    }

    static func takeURL(id: UUID) throws -> URL {
        let directory = try FileManager.default.url(
            for: .cachesDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        ).appending(path: "VoiceCapture", directoryHint: .isDirectory)
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true
        )
        return directory.appending(path: "\(id.uuidString.lowercased()).m4a")
    }

    /// Whether the user already allowed the microphone. A background run
    /// cannot ask; the App asks when it comes to the front.
    static var hasPermission: Bool {
        AVAudioApplication.shared.recordPermission == .granted
    }

    func start() throws {
        guard Self.hasPermission else {
            throw Failure.permission
        }
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.record, mode: .default, options: [])
        try session.setActive(true)
        let settings: [String: Any] = [
            AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
            AVSampleRateKey: 16_000,
            AVNumberOfChannelsKey: 1,
            AVEncoderBitRateKey: 32_000,
            AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
        ]
        let recorder = try AVAudioRecorder(url: url, settings: settings)
        guard recorder.record() else {
            try? session.setActive(false, options: .notifyOthersOnDeactivation)
            throw Failure.start
        }
        self.recorder = recorder
    }

    /// Stops the take and returns its length in seconds.
    @discardableResult
    func stop() -> TimeInterval {
        guard let recorder else { return 0 }
        let duration = recorder.currentTime
        recorder.stop()
        self.recorder = nil
        try? AVAudioSession.sharedInstance().setActive(
            false,
            options: .notifyOthersOnDeactivation
        )
        return duration
    }

    var isRecording: Bool {
        recorder?.isRecording == true
    }

    func discard() {
        stop()
        try? FileManager.default.removeItem(at: url)
    }
}
