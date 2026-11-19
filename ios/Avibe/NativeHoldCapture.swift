import AVFoundation
import Foundation
import UIKit
import WebKit

/// The in-App hold-to-talk's microphone (`voice.capturePrepare`,
/// `voice.captureStart`, `voice.captureStop`): the page's own capture
/// (`getUserMedia` and Web Audio) opened about 0.5 s (p90 1 s) after a hold
/// on an iPhone, so the take's first words were lost or waited for. The App
/// records instead, and hands the page 16 kHz mono Int16 PCM as
/// `avibe:native:voice.capture` events; everything after capture (live
/// recognition, segments, consent, sending) stays the page's.
///
/// Prepare sets the session's category only: nothing is activated and no
/// input runs, so the system shows no microphone indicator until a hold
/// starts a take.
@MainActor
final class NativeHoldCapture {
    static let shared = NativeHoldCapture(audio: SystemNativeHoldCaptureAudio())

    /// What a running take reports to its page.
    enum Event: Equatable {
        case pcm(Data)
        /// The take is over and no more audio follows: stopped by the page,
        /// or ended by the system (`reason`).
        case ended(reason: String?)
    }

    enum StartError: Error, Equatable {
        /// The microphone is not allowed: the page asks through WebKit.
        case permission
        /// An Action Button take records, or another page's take does.
        case busy
        /// The session or the input would not start (the step and its code).
        case failed(String)
    }

    struct Timings: Equatable {
        /// `setActive(true)`, and the engine's start, in ms.
        var activateMs: Int
        var engineMs: Int
    }

    /// A take, from its start until its page has heard its end.
    @MainActor
    private final class Take {
        let id: String
        weak var webView: WKWebView?
        /// Whether the audio goes to `webView`'s page, which may go away.
        let toPage: Bool
        private let send: (Event) -> Void
        /// The document that started the take was replaced, or its process
        /// died: nothing more goes into that WebView.
        var detached = false
        var pending = Data()
        var deliveredFirst = false

        init(id: String, webView: WKWebView?, toPage: Bool, deliver: @escaping (Event) -> Void) {
            self.id = id
            self.webView = webView
            self.toPage = toPage
            self.send = deliver
        }

        var pageIsPresent: Bool { !toPage || webView != nil }

        func deliver(_ event: Event) {
            guard !(toPage && detached) else { return }
            send(event)
        }

        func receive(_ data: Data) {
            if !deliveredFirst {
                deliveredFirst = true
                deliver(.pcm(data))
                return
            }
            pending.append(data)
            if pending.count >= nativeHoldBatchBytes { flush() }
        }

        func flush() {
            guard !pending.isEmpty else { return }
            deliver(.pcm(pending))
            pending = Data()
        }
    }

    private let audio: NativeHoldCaptureAudio
    private var running: Take?
    /// Stopped takes whose input may still have audio on its way here.
    private var closing: [String: Take] = [:]
    private let afterQueued: (@escaping () -> Void) -> Void
    private var backgroundObserver: NSObjectProtocol?

    private let isInBackground: () -> Bool

    /// `afterQueued` runs its work once what the input already sent to the
    /// main queue has arrived (by default, the main queue's next turn);
    /// `isInBackground` says whether the App is in the background.
    init(
        audio: NativeHoldCaptureAudio,
        afterQueued: @escaping (@escaping () -> Void) -> Void = { DispatchQueue.main.async(execute: $0) },
        isInBackground: @escaping () -> Bool = { UIApplication.shared.applicationState == .background }
    ) {
        self.audio = audio
        self.afterQueued = afterQueued
        self.isInBackground = isInBackground
        // A hold is a finger on the screen: it never records in the
        // background, whatever the page managed to say before it hid.
        backgroundObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.didEnterBackgroundNotification,
            object: nil,
            // Posted on the main thread; handled as it is posted.
            queue: nil
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.end(reason: "background") }
        }
    }

    var currentTake: String? { running?.id }

    static func isValidTake(_ value: Any?) -> String? {
        guard let take = value as? String,
              (1...64).contains(take.count),
              take.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-" || $0 == "_") })
        else {
            return nil
        }
        return take
    }

    func prepare() {
        guard running == nil, !VoiceCaptureAudioState.active else { return }
        audio.prepare()
    }

    /// Starts `take` for `webView`; its audio goes to `deliver` (by default,
    /// the page's `avibe:native:voice.capture` events).
    func start(
        take newTake: String,
        webView: WKWebView?,
        deliver: ((Event) -> Void)? = nil
    ) -> Result<Timings, StartError> {
        // A hold is a finger on the screen: a page that hid before its
        // start arrived does not get the microphone in the background.
        guard !isInBackground() else { return .failure(.failed("background")) }
        guard audio.permissionGranted else { return .failure(.permission) }
        if let running {
            guard let webView, webView === running.webView else { return .failure(.busy) }
            // The same page's newer take replaces its last one, which the page
            // has given up on.
            end(reason: "replaced")
        }
        guard !VoiceCaptureAudioState.active, closing[newTake] == nil else { return .failure(.busy) }
        let take = Take(
            id: newTake,
            webView: webView,
            toPage: deliver == nil,
            deliver: deliver ?? { [weak webView] event in
                webView?.evaluateJavaScript(Self.eventScript(take: newTake, event: event))
            }
        )
        running = take
        do {
            let timings = try audio.start(
                onPCM: { [weak self, weak take] data in
                    guard let self, let take else { return }
                    if self.running === take {
                        guard take.pageIsPresent else {
                            self.end(reason: "gone")
                            return
                        }
                        take.receive(data)
                    } else if self.closing[take.id] === take {
                        take.pending.append(data)
                    }
                },
                onEnd: { [weak self, weak take] reason in
                    guard let self, let take, self.running === take else { return }
                    self.end(reason: reason)
                }
            )
            return .success(timings)
        } catch let error as StartError {
            running = nil
            return .failure(error)
        } catch {
            running = nil
            return .failure(.failed("start:\((error as NSError).code)"))
        }
    }

    /// Stops `take` if it is the one running; its page hears the rest of its
    /// audio, then `ended`, once.
    func stop(take stopping: String) {
        guard running?.id == stopping else { return }
        end(reason: nil)
    }

    /// Stops whatever runs for `webView` (it is going away).
    func stop(webView gone: WKWebView) {
        // Its document is not the one that asked: it hears nothing more,
        // a take still flushing included.
        for take in closing.values where take.webView === gone {
            take.detached = true
        }
        guard let running, running.webView === gone else { return }
        running.detached = true
        end(reason: "gone")
    }

    /// An Action Button take is about to set the session: a hold's take
    /// ends first (its page hears `action_button`).
    func yieldToActionButton() {
        end(reason: "action_button")
    }

    private func end(reason: String?) {
        guard let take = running else { return }
        running = nil
        // The input stops here; what it had already sent is still queued
        // for the main actor, and belongs to this take.
        audio.stop()
        closing[take.id] = take
        afterQueued { [weak self] in
            MainActor.assumeIsolated {
                self?.closing[take.id] = nil
                take.flush()
                take.deliver(.ended(reason: reason))
            }
        }
    }

    static func eventScript(take: String, event: Event) -> String {
        var detail: [String: Any] = ["take": take]
        switch event {
        case .pcm(let data):
            detail["pcm"] = data.base64EncodedString()
        case .ended(let reason):
            detail["ended"] = true
            if let reason { detail["reason"] = reason }
        }
        let json = (try? JSONSerialization.data(withJSONObject: detail, options: [.sortedKeys]))
            .flatMap { String(data: $0, encoding: .utf8) } ?? "{}"
        return "window.dispatchEvent(new CustomEvent(\"avibe:native:voice.capture\", { detail: \(json) }));"
    }
}

/// About 100 ms of 16 kHz Int16: how much audio each event after a take's
/// first carries (the first goes at once, so the page knows when the
/// microphone began).
let nativeHoldBatchBytes = 3_200

/// The session and input a hold's take records with.
@MainActor
protocol NativeHoldCaptureAudio: AnyObject {
    var permissionGranted: Bool { get }
    func prepare()
    /// Activates the session and starts the input. `onPCM` gets 16 kHz mono
    /// little-endian Int16 audio and `onEnd` the system's reason for ending
    /// the input, both on the main actor.
    func start(
        onPCM: @escaping @MainActor (Data) -> Void,
        onEnd: @escaping @MainActor (String) -> Void
    ) throws -> NativeHoldCapture.Timings
    func stop()
}

/// 16 kHz mono Int16 from whatever the input delivers.
final class NativeHoldPCMConverter {
    static let outputFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16,
        sampleRate: 16_000,
        channels: 1,
        interleaved: true
    )!

    private let converter: AVAudioConverter
    private let inputRate: Double

    init?(input: AVAudioFormat) {
        guard input.sampleRate > 0,
              let converter = AVAudioConverter(from: input, to: Self.outputFormat)
        else {
            return nil
        }
        self.converter = converter
        self.inputRate = input.sampleRate
    }

    func convert(_ buffer: AVAudioPCMBuffer) -> Data? {
        let capacity = AVAudioFrameCount(
            (Double(buffer.frameLength) * Self.outputFormat.sampleRate / inputRate).rounded(.up)
        ) + 32
        guard buffer.frameLength > 0,
              let output = AVAudioPCMBuffer(pcmFormat: Self.outputFormat, frameCapacity: capacity)
        else {
            return nil
        }
        var supplied = false
        var error: NSError?
        let status = converter.convert(to: output, error: &error) { _, inputStatus in
            if supplied {
                inputStatus.pointee = .noDataNow
                return nil
            }
            supplied = true
            inputStatus.pointee = .haveData
            return buffer
        }
        guard status != .error, error == nil, output.frameLength > 0,
              let samples = output.int16ChannelData
        else {
            return nil
        }
        return Data(bytes: samples[0], count: Int(output.frameLength) * MemoryLayout<Int16>.size)
    }
}

@MainActor
final class SystemNativeHoldCaptureAudio: NativeHoldCaptureAudio {
    private var engine: AVAudioEngine?
    private var observers: [NSObjectProtocol] = []

    var permissionGranted: Bool {
        AVAudioApplication.shared.recordPermission == .granted
    }

    private static func configure() throws {
        // As WebKit's own capture: speech in, the page's sounds still from
        // the speaker; nothing else's audio is interrupted until a take.
        try AVAudioSession.sharedInstance().setCategory(
            .playAndRecord,
            mode: .default,
            options: [.defaultToSpeaker, .allowBluetooth]
        )
    }

    func prepare() {
        try? Self.configure()
    }

    func start(
        onPCM: @escaping @MainActor (Data) -> Void,
        onEnd: @escaping @MainActor (String) -> Void
    ) throws -> NativeHoldCapture.Timings {
        let session = AVAudioSession.sharedInstance()
        do {
            // Every time: an Action Button take or WebKit may have left the
            // session mixable, ducking, or without the speaker and headsets.
            try Self.configure()
        } catch {
            throw NativeHoldCapture.StartError.failed("category:\((error as NSError).code)")
        }
        let activateStart = DispatchTime.now()
        do {
            try session.setActive(true)
        } catch {
            throw NativeHoldCapture.StartError.failed("activate:\((error as NSError).code)")
        }
        let activateMs = Self.ms(since: activateStart)

        let engineStart = DispatchTime.now()
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard let converter = NativeHoldPCMConverter(input: format) else {
            try? session.setActive(false, options: .notifyOthersOnDeactivation)
            throw NativeHoldCapture.StartError.failed("format:\(Int(format.sampleRate))")
        }
        input.installTap(
            onBus: 0,
            bufferSize: 1024,
            format: format,
            block: Self.tap(converter: converter, onPCM: onPCM)
        )
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            try? session.setActive(false, options: .notifyOthersOnDeactivation)
            throw NativeHoldCapture.StartError.failed("engine:\((error as NSError).code)")
        }
        self.engine = engine
        let center = NotificationCenter.default
        observers = [
            center.addObserver(
                forName: AVAudioSession.interruptionNotification,
                object: session,
                queue: .main
            ) { note in
                let type = (note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt)
                    .flatMap(AVAudioSession.InterruptionType.init(rawValue:))
                guard type == .began else { return }
                MainActor.assumeIsolated { onEnd("interrupted") }
            },
            center.addObserver(
                forName: .AVAudioEngineConfigurationChange,
                object: engine,
                queue: .main
            ) { _ in
                // The input changed under the take (a headset came or went):
                // it stopped; what was recorded goes on to be sent.
                MainActor.assumeIsolated { onEnd("route") }
            },
        ]
        return .init(activateMs: activateMs, engineMs: Self.ms(since: engineStart))
    }

    func stop() {
        observers.forEach(NotificationCenter.default.removeObserver)
        observers = []
        if let engine {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
        }
        engine = nil
        // An Action Button take that has the session now keeps it.
        guard !VoiceCaptureAudioState.active else { return }
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    /// Made outside the main actor: the block runs on the audio thread.
    private nonisolated static func tap(
        converter: NativeHoldPCMConverter,
        onPCM: @escaping @MainActor (Data) -> Void
    ) -> AVAudioNodeTapBlock {
        { buffer, _ in
            guard let data = converter.convert(buffer) else { return }
            DispatchQueue.main.async {
                MainActor.assumeIsolated { onPCM(data) }
            }
        }
    }

    private static func ms(since start: DispatchTime) -> Int {
        Int((DispatchTime.now().uptimeNanoseconds - start.uptimeNanoseconds) / 1_000_000)
    }
}
