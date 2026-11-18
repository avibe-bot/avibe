import AVFoundation
import Foundation

/// The audio session a take records in, in the two steps a failure is
/// reported by.
protocol VoiceCaptureAudioSession {
    /// Category, mode and options (设置音频会话).
    func configureForRecording() throws
    /// `setActive(true)` (激活音频会话).
    func activate() throws
    func deactivate()
}

/// What records the take to its file.
protocol VoiceCaptureRecordingEngine: AnyObject {
    var currentTime: TimeInterval { get }
    var isRecording: Bool { get }
    func record() -> Bool
    func stop()
}

extension AVAudioRecorder: VoiceCaptureRecordingEngine {}

struct SystemAudioSession: VoiceCaptureAudioSession {
    func configureForRecording() throws {
        try AVAudioSession.sharedInstance().setCategory(.record, mode: .default, options: [])
    }

    func activate() throws {
        try AVAudioSession.sharedInstance().setActive(true)
    }

    func deactivate() {
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
}

/// Records one take to an AAC file (`.m4a`, which the instance's
/// transcription accepts) with the App's own microphone permission.
final class VoiceCaptureRecorder {
    enum Failure: Error {
        case permission
        case start
    }

    /// Where starting stopped, and what the system answered there.
    struct StartFailure: Error {
        let step: VoiceCaptureAttributes.Step
        let underlying: Error
    }

    let url: URL
    private var recorder: VoiceCaptureRecordingEngine?
    private let session: VoiceCaptureAudioSession
    private let permission: () -> Bool
    private let makeEngine: (URL) throws -> VoiceCaptureRecordingEngine

    init(
        url: URL,
        session: VoiceCaptureAudioSession = SystemAudioSession(),
        permission: @escaping () -> Bool = { VoiceCaptureRecorder.hasPermission },
        makeEngine: @escaping (URL) throws -> VoiceCaptureRecordingEngine = VoiceCaptureRecorder.makeAACRecorder
    ) {
        self.url = url
        self.session = session
        self.permission = permission
        self.makeEngine = makeEngine
    }

    static func makeAACRecorder(url: URL) throws -> VoiceCaptureRecordingEngine {
        try AVAudioRecorder(url: url, settings: [
            AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
            AVSampleRateKey: 16_000,
            AVNumberOfChannelsKey: 1,
            AVEncoderBitRateKey: 32_000,
            AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
        ])
    }

    /// Deletes every take on disk; only called while none is recording.
    static func discardAll() {
        guard let directory = try? FileManager.default.url(
            for: .cachesDirectory, in: .userDomainMask, appropriateFor: nil, create: false
        ).appending(path: "VoiceCapture", directoryHint: .isDirectory) else { return }
        try? FileManager.default.removeItem(at: directory)
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

    /// Whether this take activated the audio session. The session is
    /// active only while a take records (the background `audio` mode keeps
    /// the App running for that, and nothing else), and every way a take
    /// ends goes through `stop()`, which deactivates it.
    private var sessionActive = false

    func start() throws {
        guard permission() else {
            throw Failure.permission
        }
        var step = VoiceCaptureAttributes.Step.sessionCategory
        do {
            try session.configureForRecording()
            step = .sessionActivate
            try session.activate()
            sessionActive = true
            step = .recorder
            let recorder = try makeEngine(url)
            guard recorder.record() else { throw Failure.start }
            self.recorder = recorder
        } catch {
            deactivateSession()
            throw StartFailure(step: step, underlying: error)
        }
    }

    /// Stops the take, releases the audio session, and returns the take's
    /// length in seconds. Safe to call more than once.
    @discardableResult
    func stop() -> TimeInterval {
        let duration = recorder?.currentTime ?? 0
        recorder?.stop()
        recorder = nil
        deactivateSession()
        return duration
    }

    private func deactivateSession() {
        guard sessionActive else { return }
        sessionActive = false
        session.deactivate()
    }

    var isRecording: Bool {
        recorder?.isRecording == true
    }

    func discard() {
        stop()
        try? FileManager.default.removeItem(at: url)
    }
}
