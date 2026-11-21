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
        /// Setting the session's category, in ms.
        var categoryMs = 0
    }

    /// One of the page's hold surfaces, in the WebView's own points.
    struct Surface: Equatable {
        let id: String
        let rect: CGRect
    }

    /// What preparing a hold took (`NativeHoldCaptureAudio.arm`), in ms.
    struct ArmTimings: Equatable {
        var categoryMs: Int
        var activateMs: Int
        /// Making the engine and its tap, and `prepare()`.
        var prepareMs: Int
    }

    /// What starting a hold's input took: `engine.start()`, and the
    /// preparing it had to do first when nothing was prepared.
    struct BeginTimings: Equatable {
        var armed: ArmTimings?
        var startMs: Int
    }

    /// One press's timings, in ms from its touch-down unless a duration.
    struct HoldTimings: Equatable {
        /// `early`: started at touch-down (the default). `quiet`: prepared
        /// at touch-down, started at recognition. `late`: all of it at
        /// recognition (other audio playing, or a Bluetooth route).
        var variant = "early"
        /// The hold threshold this press was timed with, in ms.
        var holdMs = 200
        /// Why nothing was prepared at touch-down (`other`, `bluetooth`).
        var why: String?
        var route = ""
        var categoryMs: Int?
        var activateMs: Int?
        var prepareMs: Int?
        /// When the prepared engine was ready.
        var armedAt: Int?
        var recognisedAt: Int?
        var hapticAt: Int?
        /// `engine.start()`'s own duration.
        var startMs: Int?
        /// When the engine had started: the first captured audio.
        var liveAt: Int?
        var firstBufferAt: Int?
        var firstBufferBytes: Int?
        var adoptedAt: Int?
        var releasedAt: Int?
        /// How it ended: `tap`, `moved`, `rejected`, `unadopted`, `stopped`,
        /// a failed step, or the system's reason.
        var end: String?

        /// The cue to the first captured audio (negative: audio first).
        var cueToLive: Int? {
            guard let hapticAt, let liveAt else { return nil }
            return liveAt - hapticAt
        }

        /// Setting up the session and the input, start excluded.
        private var setUpMs: Int? {
            guard let categoryMs, let activateMs, let prepareMs else { return nil }
            return categoryMs + activateMs + prepareMs
        }

        /// Touch-down to the first captured audio had the input started at
        /// touch-down: measured when it did (`early`), else from this
        /// press's own set-up and start times.
        var liveEarly: Int? {
            if variant == "early" { return liveAt }
            guard let setUpMs, let startMs else { return nil }
            return setUpMs + startMs
        }

        /// The same had it been prepared at touch-down and started at
        /// recognition: measured for `quiet`, else computed.
        var liveQuiet: Int? {
            if variant == "quiet" { return liveAt }
            guard let setUpMs, let startMs else { return nil }
            return max(holdMs, setUpMs) + startMs
        }

        var fields: [String: Any] {
            var fields: [String: Any] = ["variant": variant, "route": route]
            let numbers: [(String, Int?)] = [
                ("categoryMs", categoryMs), ("activateMs", activateMs), ("prepareMs", prepareMs),
                ("armedAt", armedAt), ("recognisedAt", recognisedAt), ("hapticAt", hapticAt),
                ("startMs", startMs), ("liveAt", liveAt), ("cueToLive", cueToLive),
                ("liveEarly", liveEarly), ("liveQuiet", liveQuiet), ("holdMs", holdMs),
                ("firstBufferAt", firstBufferAt), ("firstBufferBytes", firstBufferBytes),
                ("adoptedAt", adoptedAt), ("releasedAt", releasedAt),
            ]
            for (name, value) in numbers { if let value { fields[name] = value } }
            if let why { fields["why"] = why }
            if let end { fields["end"] = end }
            return fields
        }

        /// One short line for the phone's own record (`NativeHoldTimings`).
        var line: String {
            func part(_ name: String, _ value: Int?) -> String { value.map { " \(name)=\($0)" } ?? "" }
            return "hold \(variant)\(why.map { ":" + $0 } ?? "") \(route)"
                + part("cat", categoryMs) + part("act", activateMs) + part("prep", prepareMs)
                + part("armed", armedAt) + part("rec", recognisedAt) + part("hap", hapticAt)
                + part("start", startMs) + part("live", liveAt) + part("gap", cueToLive)
                + part("e", liveEarly) + part("q", liveQuiet)
                + part("buf", firstBufferAt) + part("bytes", firstBufferBytes)
                + part("adopt", adoptedAt) + part("up", releasedAt)
                + (end.map { " end=" + $0 } ?? "")
        }
    }

    /// A press the App itself follows, from its touch-down until its take
    /// has ended (or it turned out not to be a hold).
    @MainActor
    final class Hold {
        let take: String
        let surface: String
        weak var webView: WKWebView?
        let origin: CGPoint
        let downAt: Int
        var recognised = false
        /// The input was asked to start (at touch-down for `early`).
        var begun = false
        /// The input was prepared at touch-down (`quiet`).
        var prepared = false
        /// A recognised hold's finger is in the cancel position.
        var inCancel = false
        var timings = HoldTimings()
        var cancelTimer: (() -> Void)?

        init(take: String, surface: String, webView: WKWebView?, origin: CGPoint, downAt: Int) {
            self.take = take
            self.surface = surface
            self.webView = webView
            self.origin = origin
            self.downAt = downAt
        }
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
        /// The App's own hold whose take this is; nil for one the page started.
        var hold: Hold?
        /// False while a hold's take waits for its page: its audio is kept
        /// here, in memory, and nothing is delivered.
        var adopted = true

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
            guard adopted else {
                pending.append(data)
                return
            }
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

        /// The page took the take: what was recorded so far goes first, in
        /// one event, then the rest as it comes.
        func adopt() {
            adopted = true
            // Nothing recorded yet: the first audio still goes at once.
            deliveredFirst = !pending.isEmpty
            flush()
        }
    }

    private let audio: NativeHoldCaptureAudio
    private var running: Take?
    /// Stopped takes whose input may still have audio on its way here.
    private var closing: [String: Take] = [:]
    private let afterQueued: (@escaping () -> Void) -> Void
    private var backgroundObserver: NSObjectProtocol?
    private var inactiveObserver: NSObjectProtocol?

    private let isInBackground: () -> Bool

    // The App's own hold recognition (`voice.holdSurfaces`).
    private var surfaces: [Surface] = []
    private weak var surfacesWebView: WKWebView?
    /// The WebViews whose touches reach `touchDown` (their recogniser).
    private let watched = NSHashTable<WKWebView>.weakObjects()
    /// The page's hold threshold, as it named it with its surfaces.
    private var holdSeconds = NativeHoldCapture.defaultHoldSeconds
    /// The press being followed, until its take is adopted or it ends.
    private var hold: Hold?
    private let after: @MainActor (TimeInterval, @escaping @MainActor () -> Void) -> (() -> Void)
    private let uptimeMs: () -> Int
    private let haptic: NativeHoldHaptic
    private let startsAtTouchDown: () -> Bool
    private let noteTimings: (String) -> Void
    private let noteTap: (_ ms: Int, _ moved: Bool) -> Void
    private let sendHoldEvent: @MainActor (WKWebView?, [String: Any]) -> Void
    private let sendCaptureEvent: @MainActor (WKWebView?, String, Event) -> Void

    /// How long a press is held before it is a hold, and how far it may
    /// move first: the page's own numbers for a finger (`HOLD_GESTURE`).
    static let defaultHoldSeconds: TimeInterval = 0.2
    static let slop: CGFloat = 10
    /// A recognised hold slides up this far to arm its cancel, and comes
    /// back within this to disarm it: the page's own lines (`HOLD_GESTURE`).
    static let cancelDistance: CGFloat = 80
    static let uncancelDistance: CGFloat = 60
    /// How long a recognised hold's take waits for its page.
    static let adoptionWait: TimeInterval = 3
    /// The most audio kept for a page that has not taken its take: 15 s.
    static let unadoptedBytes = 15 * 16_000 * 2

    /// `afterQueued` runs its work once what the input already sent to the
    /// main queue has arrived (by default, the main queue's next turn);
    /// `isInBackground` says whether the App is in the background.
    init(
        audio: NativeHoldCaptureAudio,
        afterQueued: @escaping (@escaping () -> Void) -> Void = { DispatchQueue.main.async(execute: $0) },
        isInBackground: @escaping () -> Bool = { UIApplication.shared.applicationState == .background },
        after: (@MainActor (TimeInterval, @escaping @MainActor () -> Void) -> (() -> Void))? = nil,
        uptimeMs: @escaping () -> Int = { Int(DispatchTime.now().uptimeNanoseconds / 1_000_000) },
        haptic: NativeHoldHaptic? = nil,
        startsAtTouchDown: @escaping () -> Bool = { !NativeHoldQuietSwitch.isOn },
        noteTimings: @escaping (String) -> Void = { NativeHoldTimings.add($0) },
        noteTap: @escaping (_ ms: Int, _ moved: Bool) -> Void = { NativeHoldTaps.note(ms: $0, moved: $1) },
        sendHoldEvent: (@MainActor (WKWebView?, [String: Any]) -> Void)? = nil,
        sendCaptureEvent: (@MainActor (WKWebView?, String, Event) -> Void)? = nil
    ) {
        self.audio = audio
        self.afterQueued = afterQueued
        self.isInBackground = isInBackground
        self.after = after ?? { delay, work in NativeHoldCapture.mainTimer(delay, work) }
        self.uptimeMs = uptimeMs
        self.haptic = haptic ?? .system()
        self.startsAtTouchDown = startsAtTouchDown
        self.noteTimings = noteTimings
        self.noteTap = noteTap
        self.sendHoldEvent = sendHoldEvent ?? { webView, detail in NativeHoldCapture.dispatchHoldEvent(webView, detail) }
        self.sendCaptureEvent = sendCaptureEvent ?? { webView, take, event in
            webView?.evaluateJavaScript(NativeHoldCapture.eventScript(take: take, event: event))
        }
        // A hold is a finger on the screen: it never records in the
        // background, whatever the page managed to say before it hid.
        backgroundObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.didEnterBackgroundNotification,
            object: nil,
            // Posted on the main thread; handled as it is posted.
            queue: nil
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self else { return }
                if let hold = self.hold, !hold.begun { self.cancelHold(hold, why: "background") }
                self.end(reason: "background")
            }
        }
        inactiveObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.willResignActiveNotification, object: nil, queue: nil
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.appWillResignActive() }
        }
    }

    /// App Lock, Control Center, a call: the App is no longer in front. A
    /// press that is not yet its page's take ends, and what it recorded is
    /// dropped. A take its page has is the page's to end, as today.
    func appWillResignActive() {
        if let take = running, take.hold != nil, !take.adopted {
            discard(take, why: "inactive")
        } else if let hold, !hold.begun {
            cancelHold(hold, why: "inactive")
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
        guard running == nil, hold == nil, !VoiceCaptureAudioState.active else { return }
        audio.prepare()
    }

    /// The page's `holdMs` argument: a whole number of ms from 100 to 1000.
    static func holdMs(from value: Any?) -> Int? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue == number.doubleValue.rounded(), (100...1000).contains(number.intValue)
        else { return nil }
        return number.intValue
    }

    /// The page's `surfaces` argument: at most eight, each an id and a
    /// rectangle of finite, positive size. Nil when it is not that.
    static func surfaces(from value: Any?) -> [Surface]? {
        guard let items = value as? [[String: Any]], items.count <= 8 else { return nil }
        var surfaces: [Surface] = []
        for item in items {
            func number(_ name: String) -> CGFloat? {
                guard let value = item[name] as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID(),
                      value.doubleValue.isFinite, abs(value.doubleValue) <= 100_000
                else { return nil }
                return CGFloat(value.doubleValue)
            }
            guard item.count == 5, let id = isValidTake(item["id"]),
                  let x = number("x"), let y = number("y"),
                  let width = number("width"), let height = number("height"),
                  width > 0, height > 0
            else { return nil }
            surfaces.append(Surface(id: id, rect: CGRect(x: x, y: y, width: width, height: height)))
        }
        return surfaces
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
        // A press of the App's own that never became this page's take.
        if let hold, !hold.begun { cancelHold(hold, why: "replaced") }
        // What that press (or the take just replaced) asked of the input
        // has finished before this start sets the session.
        audio.settle()
        running = take
        do {
            let other = audio.otherAudioPlaying
            let timings = try audio.start(onPCM: receiver(for: take), onEnd: ender(for: take))
            // The page of an older Avibe drops these: kept on the phone too.
            noteTimings(
                "hold page \(audio.route) cat=\(timings.categoryMs) act=\(timings.activateMs)"
                    + " eng=\(timings.engineMs) other=\(other ? 1 : 0)"
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

    private func receiver(for take: Take) -> @MainActor (Data) -> Void {
        { [weak self, weak take] data in
            guard let self, let take else { return }
            if self.running === take {
                guard take.pageIsPresent else {
                    self.end(reason: "gone")
                    return
                }
                if let hold = take.hold, hold.timings.firstBufferAt == nil {
                    hold.timings.firstBufferAt = self.since(hold)
                    hold.timings.firstBufferBytes = data.count
                }
                take.receive(data)
                if !take.adopted, take.pending.count > Self.unadoptedBytes {
                    self.discard(take, why: "unadopted")
                }
            } else if self.closing[take.id] === take {
                take.pending.append(data)
            }
        }
    }

    private func ender(for take: Take) -> @MainActor (String) -> Void {
        { [weak self, weak take] reason in
            guard let self, let take, self.running === take else { return }
            self.end(reason: reason)
        }
    }

    // MARK: A hold the App recognises itself

    /// `webView`'s touches reach this capture (its recogniser is attached).
    func watch(_ webView: WKWebView) {
        watched.add(webView)
    }

    /// The page's hold surfaces where a hold may record now (none: the App
    /// follows no press), and its hold threshold. One WebView's list at a
    /// time: a new one replaces the last, and only its own WebView can
    /// take it back. False for a WebView whose touches the App does not see
    /// (a popup): its page goes on recognising its holds itself.
    @discardableResult
    func setSurfaces(_ surfaces: [Surface], holdMs: Int? = nil, for webView: WKWebView) -> Bool {
        if surfaces.isEmpty {
            guard surfacesWebView === webView else { return true }
        } else {
            guard watched.contains(webView) else { return false }
        }
        self.surfaces = surfaces
        surfacesWebView = surfaces.isEmpty ? nil : webView
        if let holdMs { holdSeconds = TimeInterval(holdMs) / 1000 }
        // A press still waiting to be a hold, on a surface that is no
        // longer one: it is not followed any more.
        if let hold, !hold.recognised,
           !(hold.webView === webView && surfaces.contains { $0.id == hold.surface && $0.rect.contains(hold.origin) }) {
            cancelHold(hold, why: "withdrawn")
        }
        return true
    }

    var followsPress: Bool { hold != nil }

    /// A finger came down in `webView` at `point` (its own points). On one
    /// of the page's surfaces the App prepares the microphone, without
    /// starting it, and times the hold itself.
    func touchDown(at point: CGPoint, in webView: WKWebView) {
        guard hold == nil, running == nil, surfacesWebView === webView,
              let surface = surfaces.first(where: { $0.rect.contains(point) }),
              !VoiceCaptureAudioState.active, !isInBackground(), audio.permissionGranted
        else { return }
        let hold = Hold(
            take: "n" + String(UUID().uuidString.lowercased().replacingOccurrences(of: "-", with: "").prefix(16)),
            surface: surface.id, webView: webView, origin: point, downAt: uptimeMs()
        )
        self.hold = hold
        hold.timings.route = audio.route
        hold.timings.holdMs = Int((holdSeconds * 1000).rounded())
        hold.timings.variant = "quiet"
        haptic.prepare()
        if audio.otherAudioPlaying {
            // Activating would pause it: a touch that is not a hold must not.
            hold.timings.variant = "late"
            hold.timings.why = "other"
        } else if audio.routeIsBluetooth {
            // Activating can switch a headset to its call mode.
            hold.timings.variant = "late"
            hold.timings.why = "bluetooth"
        } else if startsAtTouchDown() {
            hold.timings.variant = "early"
            begin(hold)
        } else {
            hold.prepared = true
            audio.arm { [weak self, weak hold] result in
                guard let self, let hold else { return }
                switch result {
                case .success(let armed):
                    hold.timings.categoryMs = armed.categoryMs
                    hold.timings.activateMs = armed.activateMs
                    hold.timings.prepareMs = armed.prepareMs
                    hold.timings.armedAt = self.since(hold)
                case .failure:
                    // The start at recognition says what failed.
                    break
                }
            }
        }
        hold.cancelTimer = after(holdSeconds) { [weak self, weak hold] in
            guard let self, let hold, self.hold === hold, !hold.recognised else { return }
            self.recognise(hold)
        }
    }

    func touchMoved(to point: CGPoint, in webView: WKWebView) {
        guard let hold, hold.webView === webView else { return }
        guard hold.recognised else {
            if hypot(point.x - hold.origin.x, point.y - hold.origin.y) > Self.slop {
                cancelHold(hold, why: "moved")
            }
            return
        }
        // Sliding up arms the cancel and coming back disarms it, each felt
        // once per crossing: the two lines are 20 pt apart, so a finger
        // resting at either does not repeat it.
        guard let take = running, take.hold === hold else { return }
        let up = hold.origin.y - point.y
        if !hold.inCancel, up > Self.cancelDistance {
            hold.inCancel = true
            haptic.intoCancel()
        } else if hold.inCancel, up <= Self.uncancelDistance {
            hold.inCancel = false
            haptic.back()
        }
    }

    /// The finger went up (or the system took the touch). Before the hold
    /// was recognised it was a tap: whatever was prepared is taken down and
    /// nothing was recorded. After, the page ends the take as it does today.
    func touchEnded(in webView: WKWebView) {
        guard let hold, hold.webView === webView else { return }
        if hold.recognised {
            hold.timings.releasedAt = since(hold)
        } else {
            cancelHold(hold, why: "tap")
        }
    }

    private func since(_ hold: Hold) -> Int {
        max(0, uptimeMs() - hold.downAt)
    }

    /// The press is a hold. The cue comes now, whatever the microphone is
    /// doing: the haptic here, the preview in the page.
    private func recognise(_ hold: Hold) {
        hold.recognised = true
        hold.timings.recognisedAt = since(hold)
        haptic.fire()
        hold.timings.hapticAt = since(hold)
        haptic.prepareSlide()
        if !hold.begun { begin(hold) }
        sendHoldEvent(hold.webView, ["phase": "recognised", "take": hold.take, "surface": hold.surface])
        hold.cancelTimer = after(Self.adoptionWait) { [weak self, weak hold] in
            guard let self, let hold, let take = self.running, take.hold === hold, !take.adopted else { return }
            self.discard(take, why: "unadopted")
        }
    }

    /// Starts the input for `hold`; its audio waits in memory for the page.
    private func begin(_ hold: Hold) {
        hold.begun = true
        let webView = hold.webView
        let id = hold.take
        let send = sendCaptureEvent
        let take = Take(id: id, webView: webView, toPage: true) { [weak webView] event in
            send(webView, id, event)
        }
        take.hold = hold
        take.adopted = false
        running = take
        audio.begin(onPCM: receiver(for: take), onEnd: ender(for: take)) { [weak self, weak take, weak hold] result in
            guard let self, let take, let hold else { return }
            switch result {
            case .success(let begun):
                if let armed = begun.armed {
                    hold.timings.categoryMs = armed.categoryMs
                    hold.timings.activateMs = armed.activateMs
                    hold.timings.prepareMs = armed.prepareMs
                }
                hold.timings.startMs = begun.startMs
                hold.timings.liveAt = self.since(hold)
            case .failure(let error):
                guard self.running === take else { return }
                let step: String
                switch error {
                case .permission: step = "permission"
                case .busy: step = "busy"
                case .failed(let failed): step = failed
                }
                // An adopted take's page hears the end; another's is dropped.
                if take.adopted { self.end(reason: step) } else { self.discard(take, why: step) }
            }
        }
    }

    /// The page takes a recognised hold's take: what was recorded since the
    /// input started goes to it first, then the rest. The timings so far
    /// are its answer.
    func adopt(take id: String, webView: WKWebView) -> Result<[String: Any], StartError> {
        guard let take = running, take.id == id, let hold = take.hold, hold.recognised,
              !take.adopted, take.webView === webView
        else { return .failure(.failed("gone")) }
        hold.cancelTimer?()
        hold.cancelTimer = nil
        hold.timings.adoptedAt = since(hold)
        take.adopt()
        return .success(hold.timings.fields)
    }

    /// The page does not see this press as a hold of its own (a surface
    /// that moved): the take ends, and what it recorded is dropped.
    func reject(take id: String, webView: WKWebView) {
        guard let take = running, take.id == id, take.hold != nil, !take.adopted, take.webView === webView
        else { return }
        discard(take, why: "rejected")
    }

    /// A press that is not a hold: nothing was recorded for it, or (started
    /// at touch-down) what was is dropped here.
    private func cancelHold(_ hold: Hold, why: String) {
        hold.cancelTimer?()
        hold.cancelTimer = nil
        if let take = running, take.hold === hold {
            discard(take, why: why)
            return
        }
        self.hold = nil
        if hold.prepared { audio.disarm() }
        hold.timings.end = why
        note(hold)
    }

    /// Keeps a finished press's timings. A tap or a scroll is only counted,
    /// by how long it lasted: they are many, and would push the holds out.
    private func note(_ hold: Hold) {
        switch hold.timings.end {
        case "tap": noteTap(since(hold), false)
        case "moved": noteTap(since(hold), true)
        default: noteTimings(hold.timings.line)
        }
    }

    /// Ends a hold's take that no page took: its audio never leaves memory.
    private func discard(_ take: Take, why: String) {
        take.detached = true
        take.pending = Data()
        end(reason: why)
    }

    /// Timers on the main queue; the returned closure cancels.
    static func mainTimer(_ delay: TimeInterval, _ work: @escaping @MainActor () -> Void) -> (() -> Void) {
        let item = DispatchWorkItem { MainActor.assumeIsolated { work() } }
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: item)
        return { item.cancel() }
    }

    static func dispatchHoldEvent(_ webView: WKWebView?, _ detail: [String: Any]) {
        let json = (try? JSONSerialization.data(withJSONObject: detail, options: [.sortedKeys]))
            .flatMap { String(data: $0, encoding: .utf8) } ?? "{}"
        webView?.evaluateJavaScript(
            "window.dispatchEvent(new CustomEvent(\"avibe:native:voice.hold\", { detail: \(json) }));"
        )
    }

    /// Stops `take` if it is the one running; its page hears the rest of its
    /// audio, then `ended`, once.
    func stop(take stopping: String) {
        guard running?.id == stopping else { return }
        end(reason: nil)
    }

    /// Stops whatever runs for `webView` (it is going away).
    func stop(webView gone: WKWebView) {
        setSurfaces([], for: gone)
        if let hold, hold.webView === gone, !hold.begun { cancelHold(hold, why: "gone") }
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
        if let hold, !hold.begun { cancelHold(hold, why: "action_button") }
        end(reason: "action_button")
        // The session is the Action Button's from here: nothing a press
        // asked of the input is still on its way.
        audio.settle()
    }

    private func end(reason: String?) {
        guard let take = running else { return }
        running = nil
        // The input stops here; what it had already sent is still queued
        // for the main actor, and belongs to this take.
        audio.stop()
        if take.hold != nil, !take.adopted {
            // No page took it: what it recorded never leaves memory.
            take.detached = true
            take.pending = Data()
        }
        if let hold = take.hold {
            hold.cancelTimer?()
            hold.cancelTimer = nil
            if self.hold === hold { self.hold = nil }
            hold.timings.end = reason ?? "stopped"
            note(hold)
            if take.adopted, !take.detached {
                // For the page's own trace of this take.
                sendHoldEvent(take.webView, ["phase": "timings", "take": take.id, "timings": hold.timings.fields])
            }
        }
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

/// The cue's haptic: made ready at touch-down, fired at recognition.
struct NativeHoldHaptic {
    var prepare: @MainActor () -> Void
    var fire: @MainActor () -> Void
    /// The two slide haptics, made ready at recognition: into the cancel
    /// position, and back onto the composer.
    var prepareSlide: @MainActor () -> Void
    var intoCancel: @MainActor () -> Void
    var back: @MainActor () -> Void

    @MainActor
    static func system() -> NativeHoldHaptic {
        // The page's own cue for a hold (`impactFeedback('medium')`); rigid
        // and soft differ from it and from the release's light.
        let start = UIImpactFeedbackGenerator(style: .medium)
        let cancel = UIImpactFeedbackGenerator(style: .rigid)
        let back = UIImpactFeedbackGenerator(style: .soft)
        return NativeHoldHaptic(
            prepare: { start.prepare() },
            fire: { start.impactOccurred() },
            prepareSlide: {
                cancel.prepare()
                back.prepare()
            },
            intoCancel: {
                cancel.impactOccurred()
                back.prepare()
            },
            back: {
                back.impactOccurred()
                cancel.prepare()
            }
        )
    }
}

/// The hidden switch for the variant that only prepares the microphone at
/// touch-down and starts it at recognition
/// (`avibe-diagnostics://voice/hold-quiet?on=1`): off unless a TestFlight or
/// development build was told otherwise. Off, the input starts at
/// touch-down (owner decision, 2026-11-21).
enum NativeHoldQuietSwitch {
    static let key = "avibe.voiceHold.startsAtRecognition"

    static var isOn: Bool { isOn(in: .standard, internalBuild: isInternalBuild) }

    static func isOn(in defaults: UserDefaults, internalBuild: Bool) -> Bool {
        internalBuild && defaults.bool(forKey: key)
    }

    /// Handles the switch's URL; returns whether it was one.
    @discardableResult
    static func handle(_ url: URL, allowed: Bool = isInternalBuild, defaults: UserDefaults = .standard) -> Bool {
        guard allowed, url.scheme == "avibe-diagnostics", url.host == "voice", url.path == "/hold-quiet",
              let on = URLComponents(url: url, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "on" })?.value
        else { return false }
        defaults.set(on == "1", forKey: key)
        return true
    }

    static var isInternalBuild: Bool {
        #if DEBUG
        return true
        #else
        return Bundle.main.appStoreReceiptURL?.lastPathComponent == "sandboxReceipt"
        #endif
    }
}

/// The last holds' timings, kept on the phone until a message carries them
/// (the Action Button's `capture_earlier`): numbers and states only, no
/// audio and no words.
enum NativeHoldTimings {
    static let key = "avibe.voiceHold.timings"
    static let maximumLines = 12
    static let maximumLength = 220

    static func adding(_ line: String, to lines: [String]) -> [String] {
        Array((lines + [String(line.prefix(maximumLength))]).suffix(maximumLines))
    }

    /// The kept holds, then the taps' counts (`NativeHoldTaps`) if any.
    static var lines: [String] {
        (UserDefaults.standard.stringArray(forKey: key) ?? []) + [NativeHoldTaps.line()].compactMap { $0 }
    }

    static func add(_ line: String) {
        let kept = UserDefaults.standard.stringArray(forKey: key) ?? []
        UserDefaults.standard.set(adding(line, to: kept), forKey: key)
    }

    /// `lines` without the ones in `sent` (each once).
    static func removing(_ sent: [String], from lines: [String]) -> [String] {
        var left = sent
        return lines.filter { line in
            guard let index = left.firstIndex(of: line) else { return true }
            left.remove(at: index)
            return false
        }
    }

    static func drop(_ sent: [String]) {
        let kept = UserDefaults.standard.stringArray(forKey: key) ?? []
        UserDefaults.standard.set(removing(sent, from: kept), forKey: key)
        sent.forEach { NativeHoldTaps.drop(line: $0) }
    }
}

/// How long the touches on a hold surface that were not holds lasted: counts
/// per 25 ms, and those that moved away counted apart. Counts only, to judge
/// the hold threshold; sent with the holds' timings and then taken off.
enum NativeHoldTaps {
    static let key = "avibe.voiceHold.taps"
    static let bucketMs = 25
    static let moved = "moved"
    private static let prefix = "hold taps"

    /// The bucket a touch of `ms` falls in: its lower edge, capped at 975.
    static func bucket(ms: Int) -> String {
        String(min(max(0, ms), 975) / bucketMs * bucketMs)
    }

    static func adding(ms: Int, moved: Bool, to counts: [String: Int]) -> [String: Int] {
        var counts = counts
        counts[moved ? Self.moved : bucket(ms: ms), default: 0] += 1
        return counts
    }

    /// `hold taps 50:3 75:12 100:4 moved=2`, or nil with nothing counted.
    static func line(for counts: [String: Int]) -> String? {
        let buckets = counts.filter { $0.key != moved && $0.value > 0 }
            .sorted { (Int($0.key) ?? 0) < (Int($1.key) ?? 0) }
            .map { " \($0.key):\($0.value)" }
            .joined()
        let movedCount = counts[moved] ?? 0
        guard !buckets.isEmpty || movedCount > 0 else { return nil }
        return prefix + buckets + (movedCount > 0 ? " \(moved)=\(movedCount)" : "")
    }

    /// The counts in a line `line(for:)` made (nil for any other line).
    static func counts(in line: String) -> [String: Int]? {
        guard line.hasPrefix(prefix) else { return nil }
        var counts: [String: Int] = [:]
        for part in line.dropFirst(prefix.count).split(separator: " ") {
            let pair = part.split(whereSeparator: { $0 == ":" || $0 == "=" })
            guard pair.count == 2, let value = Int(pair[1]) else { return nil }
            counts[String(pair[0])] = value
        }
        return counts
    }

    /// `counts` less what `sent` carried (touches counted since stay).
    static func removing(_ sent: [String: Int], from counts: [String: Int]) -> [String: Int] {
        var counts = counts
        for (name, value) in sent {
            let left = (counts[name] ?? 0) - value
            counts[name] = left > 0 ? left : nil
        }
        return counts
    }

    private static var stored: [String: Int] {
        UserDefaults.standard.dictionary(forKey: key) as? [String: Int] ?? [:]
    }

    static func note(ms: Int, moved: Bool) {
        UserDefaults.standard.set(adding(ms: ms, moved: moved, to: stored), forKey: key)
    }

    static func line() -> String? {
        line(for: stored)
    }

    static func drop(line: String) {
        guard let sent = counts(in: line) else { return }
        UserDefaults.standard.set(removing(sent, from: stored), forKey: key)
    }
}

/// The session and input a hold's take records with.
@MainActor
protocol NativeHoldCaptureAudio: AnyObject {
    var permissionGranted: Bool { get }
    /// Whether another app's audio is playing (`isOtherAudioPlaying`).
    var otherAudioPlaying: Bool { get }
    /// The current output's port type (`Speaker`, `BluetoothA2DPOutput`…).
    var route: String { get }
    var routeIsBluetooth: Bool { get }
    /// Sets up and activates the session and makes and prepares the input,
    /// without starting it: no audio is captured. `completion` on the main actor.
    func arm(completion: @escaping @MainActor (Result<NativeHoldCapture.ArmTimings, NativeHoldCapture.StartError>) -> Void)
    /// Starts the prepared input (preparing it first when nothing is).
    func begin(
        onPCM: @escaping @MainActor (Data) -> Void,
        onEnd: @escaping @MainActor (String) -> Void,
        completion: @escaping @MainActor (Result<NativeHoldCapture.BeginTimings, NativeHoldCapture.StartError>) -> Void
    )
    /// Takes down what `arm` prepared; nothing was captured.
    func disarm()
    /// Returns once everything asked of the input so far (a preparing, a
    /// start, a taking down) has finished: the session is free for its next
    /// owner, and nothing will set or deactivate it afterwards.
    func settle()
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

    /// The input buffer as 16 kHz mono Int16 (nil when nothing came out).
    func convertBuffer(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
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
        guard status != .error, error == nil, output.frameLength > 0 else {
            return nil
        }
        return output
    }

    func convert(_ buffer: AVAudioPCMBuffer) -> Data? {
        guard let output = convertBuffer(buffer), let samples = output.int16ChannelData else {
            return nil
        }
        return Data(bytes: samples[0], count: Int(output.frameLength) * MemoryLayout<Int16>.size)
    }
}

/// The input of a hold the App recognises itself: made, started and taken
/// down on one serial queue, so activating the session never holds up the
/// touch that asked for it.
private final class NativeHoldHeldInput: @unchecked Sendable {
    private var engine: AVAudioEngine?
    private var observers: [NSObjectProtocol] = []
    private var armed: NativeHoldCapture.ArmTimings?
    private let lock = NSLock()
    private var onPCM: ((Data) -> Void)?
    /// Counts the takedowns asked for: work asked before one is not done
    /// once that takedown has been asked (a press that ended before its
    /// preparing even began, so quick taps do not queue up work).
    private var takedowns = 0

    /// A ticket for work asked now; `isCurrent` once it runs.
    func ticket() -> Int {
        lock.lock()
        defer { lock.unlock() }
        return takedowns
    }

    func askTakedown() {
        lock.lock()
        takedowns += 1
        lock.unlock()
    }

    private func isCurrent(_ ticket: Int) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return ticket == takedowns
    }

    func arm(_ ticket: Int) -> Result<NativeHoldCapture.ArmTimings, NativeHoldCapture.StartError> {
        guard isCurrent(ticket) else { return .failure(.failed("cancelled")) }
        return arm()
    }

    private func arm() -> Result<NativeHoldCapture.ArmTimings, NativeHoldCapture.StartError> {
        if engine != nil, let armed { return .success(armed) }
        let session = AVAudioSession.sharedInstance()
        let categoryStart = DispatchTime.now()
        do {
            try SystemNativeHoldCaptureAudio.configure()
        } catch {
            return .failure(.failed("category:\((error as NSError).code)"))
        }
        let categoryMs = SystemNativeHoldCaptureAudio.ms(since: categoryStart)
        let activateStart = DispatchTime.now()
        do {
            try session.setActive(true)
        } catch {
            return .failure(.failed("activate:\((error as NSError).code)"))
        }
        let activateMs = SystemNativeHoldCaptureAudio.ms(since: activateStart)
        let prepareStart = DispatchTime.now()
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard let converter = NativeHoldPCMConverter(input: format) else {
            try? session.setActive(false, options: .notifyOthersOnDeactivation)
            return .failure(.failed("format:\(Int(format.sampleRate))"))
        }
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            guard let self, let data = converter.convert(buffer) else { return }
            self.lock.lock()
            let onPCM = self.onPCM
            self.lock.unlock()
            onPCM?(data)
        }
        // Allocates what the start needs; the hardware starts with `start()`.
        engine.prepare()
        self.engine = engine
        let timings = NativeHoldCapture.ArmTimings(
            categoryMs: categoryMs, activateMs: activateMs,
            prepareMs: SystemNativeHoldCaptureAudio.ms(since: prepareStart)
        )
        armed = timings
        return .success(timings)
    }

    func begin(
        _ ticket: Int,
        onPCM: @escaping (Data) -> Void,
        onEnd: @escaping (String) -> Void
    ) -> Result<NativeHoldCapture.BeginTimings, NativeHoldCapture.StartError> {
        guard isCurrent(ticket) else { return .failure(.failed("cancelled")) }
        var armedNow: NativeHoldCapture.ArmTimings?
        if engine == nil {
            switch arm() {
            case .success(let timings): armedNow = timings
            case .failure(let error): return .failure(error)
            }
        }
        guard let engine else { return .failure(.failed("engine:0")) }
        lock.lock()
        self.onPCM = onPCM
        lock.unlock()
        let start = DispatchTime.now()
        do {
            try engine.start()
        } catch {
            takeDown(deactivate: true)
            return .failure(.failed("engine:\((error as NSError).code)"))
        }
        let startMs = SystemNativeHoldCaptureAudio.ms(since: start)
        let center = NotificationCenter.default
        observers = [
            center.addObserver(
                forName: AVAudioSession.interruptionNotification,
                object: AVAudioSession.sharedInstance(),
                queue: nil
            ) { note in
                let type = (note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt)
                    .flatMap(AVAudioSession.InterruptionType.init(rawValue:))
                guard type == .began else { return }
                onEnd("interrupted")
            },
            center.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { _ in
                onEnd("route")
            },
        ]
        return .success(.init(armed: armedNow, startMs: startMs))
    }

    func takeDown(deactivate: Bool) {
        observers.forEach(NotificationCenter.default.removeObserver)
        observers = []
        lock.lock()
        onPCM = nil
        lock.unlock()
        armed = nil
        guard let engine else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        self.engine = nil
        guard deactivate else { return }
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
}

@MainActor
final class SystemNativeHoldCaptureAudio: NativeHoldCaptureAudio {
    private var engine: AVAudioEngine?
    private var observers: [NSObjectProtocol] = []
    private let queue = DispatchQueue(label: "bot.avibe.ios.hold-capture", qos: .userInteractive)
    private let held = NativeHoldHeldInput()
    /// Whether `held` may hold an input (asked for on `queue`, not yet taken down).
    private var heldInUse = false

    var permissionGranted: Bool {
        AVAudioApplication.shared.recordPermission == .granted
    }

    var otherAudioPlaying: Bool {
        AVAudioSession.sharedInstance().isOtherAudioPlaying
    }

    var route: String {
        AVAudioSession.sharedInstance().currentRoute.outputs.first?.portType.rawValue ?? "none"
    }

    var routeIsBluetooth: Bool {
        let bluetooth: [AVAudioSession.Port] = [.bluetoothA2DP, .bluetoothHFP, .bluetoothLE]
        return AVAudioSession.sharedInstance().currentRoute.outputs.contains { bluetooth.contains($0.portType) }
    }

    func arm(completion: @escaping @MainActor (Result<NativeHoldCapture.ArmTimings, NativeHoldCapture.StartError>) -> Void) {
        heldInUse = true
        let ticket = held.ticket()
        queue.async { [held] in
            let result = held.arm(ticket)
            DispatchQueue.main.async { MainActor.assumeIsolated { completion(result) } }
        }
    }

    func begin(
        onPCM: @escaping @MainActor (Data) -> Void,
        onEnd: @escaping @MainActor (String) -> Void,
        completion: @escaping @MainActor (Result<NativeHoldCapture.BeginTimings, NativeHoldCapture.StartError>) -> Void
    ) {
        heldInUse = true
        let ticket = held.ticket()
        queue.async { [held] in
            let result = held.begin(
                ticket,
                onPCM: { data in DispatchQueue.main.async { MainActor.assumeIsolated { onPCM(data) } } },
                onEnd: { reason in DispatchQueue.main.async { MainActor.assumeIsolated { onEnd(reason) } } }
            )
            DispatchQueue.main.async { MainActor.assumeIsolated { completion(result) } }
        }
    }

    func disarm() {
        takeDownHeld()
    }

    func settle() {
        // Nothing on this queue ever waits for the main thread.
        queue.sync {}
    }

    private func takeDownHeld() {
        guard heldInUse else { return }
        heldInUse = false
        held.askTakedown()
        // An Action Button take that has the session now keeps it.
        let deactivate = !VoiceCaptureAudioState.active
        queue.async { [held] in held.takeDown(deactivate: deactivate) }
    }

    nonisolated static func configure() throws {
        // As WebKit's own capture: speech in, the page's sounds still from
        // the speaker; nothing else's audio is interrupted until a take.
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(
            .playAndRecord,
            mode: .default,
            options: [.defaultToSpeaker, .allowBluetooth]
        )
        // The cue is a haptic felt while the microphone runs: the system
        // mutes haptics during recording unless the session allows them
        // (set after the category, which resets it).
        try? session.setAllowHapticsAndSystemSoundsDuringRecording(true)
    }

    func prepare() {
        try? Self.configure()
    }

    func start(
        onPCM: @escaping @MainActor (Data) -> Void,
        onEnd: @escaping @MainActor (String) -> Void
    ) throws -> NativeHoldCapture.Timings {
        let session = AVAudioSession.sharedInstance()
        let categoryStart = DispatchTime.now()
        do {
            // Every time: an Action Button take or WebKit may have left the
            // session mixable, ducking, or without the speaker and headsets.
            try Self.configure()
        } catch {
            throw NativeHoldCapture.StartError.failed("category:\((error as NSError).code)")
        }
        let categoryMs = Self.ms(since: categoryStart)
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
        return .init(activateMs: activateMs, engineMs: Self.ms(since: engineStart), categoryMs: categoryMs)
    }

    func stop() {
        takeDownHeld()
        guard engine != nil || !observers.isEmpty else { return }
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

    nonisolated static func ms(since start: DispatchTime) -> Int {
        Int((DispatchTime.now().uptimeNanoseconds - start.uptimeNanoseconds) / 1_000_000)
    }
}
