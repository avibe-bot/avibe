import AVFoundation
import Foundation

/// How a take's audio session is set up.
enum VoiceCaptureSessionCandidate: Int, CaseIterable, Hashable {
    /// In front: `.record`, mode `.default`, no options (non-mixable).
    case exclusiveRecord = 1
    /// In the background: `.playAndRecord`, mode `.default`, `.mixWithOthers`
    /// and `.duckOthers`, as the first and only activation. Two projects
    /// record from the background on an iPhone 17 this way, each press a
    /// process the press launched (legato spikes/ios-keyboard/FINDINGS.md;
    /// oliv ios/docs/TESTING.md); a non-mixable session there is refused
    /// with cannotInterruptOthers.
    case mixableBackground = 2
}

/// The audio session a take records in, in the two steps a failure is
/// reported by.
protocol VoiceCaptureAudioSession {
    /// Category, mode and options (设置音频会话).
    func configure(for candidate: VoiceCaptureSessionCandidate) throws
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

/// Whether our audio session is active, for the press's diagnostics.
enum VoiceCaptureAudioState {
    nonisolated(unsafe) static var active = false
}

struct SystemAudioSession: VoiceCaptureAudioSession {
    func configure(for candidate: VoiceCaptureSessionCandidate) throws {
        let session = AVAudioSession.sharedInstance()
        switch candidate {
        case .exclusiveRecord:
            try session.setCategory(.record, mode: .default, options: [])
        case .mixableBackground:
            try session.setCategory(.playAndRecord, mode: .default, options: [.mixWithOthers, .duckOthers])
        }
    }

    func activate() throws {
        try AVAudioSession.sharedInstance().setActive(true)
        VoiceCaptureAudioState.active = true
    }

    func deactivate() {
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        VoiceCaptureAudioState.active = false
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
    static func discardAll(in base: URL? = nil) {
        guard let directory = try? takesDirectory(in: base, create: false) else { return }
        try? FileManager.default.removeItem(at: directory)
    }

    /// Where takes live: `Caches/VoiceCapture` (tests pass their own base).
    static func takesDirectory(in base: URL? = nil, create: Bool) throws -> URL {
        let root = try base ?? FileManager.default.url(
            for: .cachesDirectory, in: .userDomainMask, appropriateFor: nil, create: create
        )
        return root.appending(path: "VoiceCapture", directoryHint: .isDirectory)
    }

    static func takeURL(id: UUID, in base: URL? = nil) throws -> URL {
        let directory = try takesDirectory(in: base, create: true)
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true
        )
        // A take stays on the phone only until it is sent or discarded; it
        // is never backed up (Caches is not, and this says so explicitly).
        // Its protection stays the default class (complete until the first
        // unlock): a take may be written while the phone locks mid-take, and
        // `completeUnlessOpen` could not be verified for the recorder's
        // writes after a lock (the Simulator enforces no data protection).
        var directoryURL = directory
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try? directoryURL.setResourceValues(values)
        return directory.appending(path: "\(id.uuidString.lowercased()).m4a")
    }

    /// Whether the user already allowed the microphone. A background run
    /// cannot ask; the App asks when it comes to the front.
    static var hasPermission: Bool {
        AVAudioApplication.shared.recordPermission == .granted
    }

    /// Whether this take activated the audio session. The session is active
    /// from `prepare` (around the island, by the try) while the take records
    /// (the background `audio` mode keeps the App running for that, and
    /// nothing else); every way a take or a refused try ends goes through
    /// `stop()`, which deactivates it.
    private var sessionActive = false

    func start() throws {
        guard permission() else {
            throw Failure.permission
        }
        // The controller prepares a candidate's session first (before the
        // Live Activity); recording never sets one up on its own.
        guard sessionActive else {
            throw StartFailure(step: .sessionActivate, underlying: Failure.start)
        }
        do {
            let recorder = try makeEngine(url)
            guard recorder.record() else { throw Failure.start }
            self.recorder = recorder
        } catch {
            deactivateSession()
            throw StartFailure(step: .recorder, underlying: error)
        }
    }

    /// Sets up and activates the session for one candidate, recording
    /// nothing yet. The controller does this before or after the island, by the
    /// try.
    func prepare(_ candidate: VoiceCaptureSessionCandidate) throws {
        guard permission() else {
            throw Failure.permission
        }
        deactivateSession()
        var step = VoiceCaptureAttributes.Step.sessionCategory
        do {
            try session.configure(for: candidate)
            step = .sessionActivate
            try session.activate()
            sessionActive = true
        } catch {
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
