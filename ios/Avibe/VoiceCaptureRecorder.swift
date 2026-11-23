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

/// AVAudioRecorder's own readiness check, read when its `record()` says
/// no (it gives no reason).
protocol VoiceCapturePreparable: AnyObject {
    func prepareToRecord() -> Bool
}

extension AVAudioRecorder: VoiceCaptureRecordingEngine, VoiceCapturePreparable {}

/// A recorder that says why it did not start: the second recorder a
/// background take tries when AVAudioRecorder's `record()` says no.
protocol VoiceCaptureStartingEngine: VoiceCaptureRecordingEngine {
    func begin() throws
    /// How it records, for the report (the engine: `vp=1`).
    var detail: String { get }
    /// Called when it lost its input and could not go on recording.
    var onInputLost: (() -> Void)? { get set }
}

/// Whether our audio session is active, for the press's diagnostics.
enum VoiceCaptureAudioState {
    nonisolated(unsafe) static var active = false
}

struct SystemAudioSession: VoiceCaptureAudioSession {
    /// What the shared session says now, for a recorder that would not
    /// record: input available, the input port, how many inputs, its
    /// channels, the hardware rate, category, mode and options read back,
    /// and whether other audio plays (and should silence ours).
    static func reading() -> String {
        let session = AVAudioSession.sharedInstance()
        let port = session.currentRoute.inputs.first?.portType.rawValue ?? "-"
        let category = session.category.rawValue.replacingOccurrences(of: "AVAudioSessionCategory", with: "")
        let mode = session.mode.rawValue.replacingOccurrences(of: "AVAudioSessionMode", with: "")
        return "in=\(session.isInputAvailable ? 1 : 0) port=\(port) ins=\(session.availableInputs?.count ?? 0) "
            + "ch=\(session.inputNumberOfChannels) rate=\(Int(session.sampleRate)) cat=\(category) mode=\(mode) "
            + "opts=\(session.categoryOptions.rawValue) other=\(session.isOtherAudioPlaying ? 1 : 0) "
            + "hint=\(session.secondaryAudioShouldBeSilencedHint ? 1 : 0)"
    }

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
    private let makeFallback: ((URL) throws -> VoiceCaptureStartingEngine)?
    private let readSession: () -> String
    /// The candidate the session was last prepared for.
    private var candidate: VoiceCaptureSessionCandidate?

    /// Which recorder took the take, and what the session said when the
    /// first one would not record, for `capture_attempt`: `rec[recorder]`,
    /// `rec[engine <facts>]`, or `rec[<facts> engine=<code>]`.
    private(set) var report: String?
    /// Called when the second recorder lost its input and stopped.
    var onInputLost: (() -> Void)?

    init(
        url: URL,
        session: VoiceCaptureAudioSession = SystemAudioSession(),
        permission: @escaping () -> Bool = { VoiceCaptureRecorder.hasPermission },
        makeEngine: @escaping (URL) throws -> VoiceCaptureRecordingEngine = VoiceCaptureRecorder.makeAACRecorder,
        makeFallback: ((URL) throws -> VoiceCaptureStartingEngine)? = { VoiceCaptureEngineRecorder(url: $0) },
        readSession: @escaping () -> String = { SystemAudioSession.reading() }
    ) {
        self.url = url
        self.session = session
        self.permission = permission
        self.makeEngine = makeEngine
        self.makeFallback = makeFallback
        self.readSession = readSession
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
        report = nil
        do {
            let recorder = try makeEngine(url)
            if recorder.record() {
                self.recorder = recorder
                report = "rec[recorder]"
            } else {
                self.recorder = try startSecondRecorder(after: recorder)
            }
            watchFirstAudio(self.recorder!)
        } catch {
            recorder = nil
            deactivateSession()
            throw StartFailure(step: .recorder, underlying: error)
        }
    }

    /// AVAudioRecorder's `record()` said no, with no reason (39.1: in the
    /// background while navigation spoke, after the mixable session had
    /// activated). What the session says goes into the report; in the
    /// background an AVAudioEngine input on the same session tries next,
    /// with no second activation, and names its error if it fails too.
    private func startSecondRecorder(after refused: VoiceCaptureRecordingEngine) throws -> VoiceCaptureRecordingEngine {
        let prepared = (refused as? VoiceCapturePreparable)?.prepareToRecord()
        refused.stop()
        let facts = "recorder=0 prep=\(prepared.map { $0 ? "1" : "0" } ?? "-") \(readSession())"
        guard candidate == .mixableBackground, let makeFallback else {
            report = "rec[\(facts)]"
            throw Failure.start
        }
        try? FileManager.default.removeItem(at: url)
        var detail = ""
        do {
            let engine = try makeFallback(url)
            defer { detail = engine.detail }
            try engine.begin()
            engine.onInputLost = { [weak self] in self?.onInputLost?() }
            report = "rec[engine \(engine.detail) \(facts)]"
            return engine
        } catch {
            report = "rec[\(facts) engine=\(Self.code(error))\(detail.isEmpty ? "" : " " + detail)]"
            throw error
        }
    }

    /// An error's code, with its four characters when it is an OSStatus
    /// spelled that way (`561145187(!rec)`).
    static func code(_ error: Error) -> String {
        let code = (error as NSError).code
        let bytes = withUnsafeBytes(of: UInt32(truncatingIfNeeded: code).bigEndian) { Array($0) }
        guard code > 0x2000_0000, bytes.allSatisfy({ (0x20...0x7E).contains($0) }) else { return String(code) }
        return "\(code)(\(String(decoding: bytes, as: UTF8.self)))"
    }

    /// Sets up and activates the session for one candidate, recording
    /// nothing yet. The controller does this before or after the island, by the
    /// try.
    func prepare(_ candidate: VoiceCaptureSessionCandidate) throws {
        guard permission() else {
            throw Failure.permission
        }
        deactivateSession()
        self.candidate = candidate
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

    /// When the recorder first had audio, for the press's timing; until
    /// then, whether the watch gave up (`firstAudioLate`).
    private(set) var firstAudioAt: Date?
    private(set) var firstAudioLate = false

    /// Watches for the recorder's first audio: every 5 ms for the first
    /// second, then every 100 ms until the take stops.
    func watchFirstAudio(_ recorder: VoiceCaptureRecordingEngine, poll: Duration = .milliseconds(5)) {
        firstAudioAt = nil
        firstAudioLate = false
        Task { @MainActor [weak self, weak recorder] in
            var polls = 0
            while true {
                guard let self, let recorder, recorder.isRecording else {
                    self?.firstAudioLate = self?.firstAudioAt == nil
                    return
                }
                if recorder.currentTime > 0 {
                    self.firstAudioAt = Date()
                    return
                }
                polls += 1
                try? await Task.sleep(for: polls < 200 ? poll : poll * 20)
            }
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

/// The second recorder: the session's input through AVAudioEngine, written
/// as the same 16 kHz mono AAC `.m4a` AVAudioRecorder writes. Unlike
/// `record()`, its start throws the system's reason.
///
/// It first asks for voice processing (echo cancellation), so that sound
/// from the phone's speaker stays out of the take as far as the system can
/// manage; whether that covers another App's audio is only known on a
/// device, and `detail` says which input recorded.
final class VoiceCaptureEngineRecorder: VoiceCaptureStartingEngine, @unchecked Sendable {
    private let url: URL
    private var engine: AVAudioEngine?
    /// Writes in order, off the audio thread.
    private let writer = DispatchQueue(label: "bot.avibe.voice.engine-recorder")
    private let lock = NSLock()
    private var frames: AVAudioFramePosition = 0
    private var running = false
    private var file: AVAudioFile?
    /// `vp=1`: voice processing is on; `vp=0(<code>)`: it would not start,
    /// and the plain input records.
    private(set) var detail = ""
    var onInputLost: (() -> Void)?
    private var observer: NSObjectProtocol?

    init(url: URL) {
        self.url = url
    }

    var currentTime: TimeInterval {
        lock.withLock { Double(frames) / NativeHoldPCMConverter.outputFormat.sampleRate }
    }

    var isRecording: Bool {
        lock.withLock { running }
    }

    func record() -> Bool {
        (try? begin()) != nil
    }

    func begin() throws {
        do {
            try start(voiceProcessing: true)
            detail = "vp=1"
        } catch {
            detail = "vp=0(\(VoiceCaptureRecorder.code(error)))"
            try start(voiceProcessing: false)
        }
    }

    private func start(voiceProcessing: Bool) throws {
        let engine = AVAudioEngine()
        let input = engine.inputNode
        if voiceProcessing {
            try input.setVoiceProcessingEnabled(true)
        }
        let format = input.outputFormat(forBus: 0)
        guard let converter = NativeHoldPCMConverter(input: format) else {
            throw NSError(domain: "VoiceCaptureEngine", code: Int(format.sampleRate))
        }
        try? FileManager.default.removeItem(at: url)
        let file = try AVAudioFile(
            forWriting: url,
            settings: [
                AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
                AVSampleRateKey: 16_000,
                AVNumberOfChannelsKey: 1,
                AVEncoderBitRateKey: 32_000,
            ],
            commonFormat: .pcmFormatInt16,
            interleaved: true
        )
        writer.sync { self.file = file }
        input.installTap(onBus: 0, bufferSize: 1024, format: format, block: tap(converter))
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            writer.sync { self.file = nil }
            throw error
        }
        self.engine = engine
        lock.withLock { running = true }
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main
        ) { [weak self] _ in
            self?.inputChanged()
        }
    }

    /// The input changed under the take (a headset came or went, another
    /// App changed the route): the engine stopped itself. Recording goes on
    /// with the new input, into the same file; if it cannot, the take ends
    /// here with what it has, and says so.
    private func inputChanged() {
        guard lock.withLock({ running }), let engine else { return }
        let input = engine.inputNode
        input.removeTap(onBus: 0)
        let format = input.outputFormat(forBus: 0)
        if let converter = NativeHoldPCMConverter(input: format) {
            input.installTap(onBus: 0, bufferSize: 1024, format: format, block: tap(converter))
            engine.prepare()
            if (try? engine.start()) != nil { return }
            input.removeTap(onBus: 0)
        }
        stop()
        onInputLost?()
    }

    private func tap(_ converter: NativeHoldPCMConverter) -> AVAudioNodeTapBlock {
        { [weak self] buffer, _ in
            guard let self, let converted = converter.convertBuffer(buffer) else { return }
            self.writer.async {
                guard let file = self.file, (try? file.write(from: converted)) != nil else { return }
                self.lock.withLock { self.frames += AVAudioFramePosition(converted.frameLength) }
            }
        }
    }

    func stop() {
        if let observer { NotificationCenter.default.removeObserver(observer) }
        observer = nil
        guard lock.withLock({ running }), let engine else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        self.engine = nil
        lock.withLock { running = false }
        // Everything heard is written, then the file is closed.
        writer.sync { self.file = nil }
    }
}
