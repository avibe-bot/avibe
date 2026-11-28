import AVFoundation
import Foundation
import UIKit
import UIKit.UIGestureRecognizerSubclass
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
        /// The page asked that a touch here open no microphone: the input
        /// is only prepared at touch-down and starts when the hold is
        /// recognised (`start: "recognition"`), whatever the App's default.
        var startsAtRecognition = false
        /// The earliest the input may start, in ms after touch-down
        /// (`startAfterMs`); nil: the App's default.
        var startAfterMs: Int?
        /// Nothing is prepared at touch-down either (`prepare: "start"`):
        /// the session is activated only when the input starts.
        var preparesAtStart = false
    }

    /// A touch on a surface that was not a hold, as it is counted.
    struct Tap: Equatable {
        /// How long it lasted, by the touch's own times where known.
        var ms: Int
        /// It moved away (a scroll); else it lifted.
        var moved: Bool
        /// The microphone was started for it.
        var opened = false
        /// For each threshold in `NativeHoldTaps.thresholds`: whether the
        /// finger was still down when the input could have started with
        /// it (prepared, and not before the threshold).
        var wouldOpen: [Int: Bool] = [:]
        /// Its touch was gone when it was judged: it lifted at a time not
        /// known, and `ms` is only the most it can have lasted (from the
        /// App's handling of its touch-down). `wouldOpen` is then empty.
        var lengthUnknown = false
        /// For such a tap, for each threshold: it surely ended before the
        /// input could have started with it.
        var surelyClosed: [Int: Bool] = [:]
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
        /// What was planned at touch-down. `early`: start before
        /// recognition, once prepared and not before `startAfterMs`, with the
        /// finger seen down (the default). `quiet`: prepared at touch-down,
        /// started at recognition. `late`: all of it at recognition (other
        /// audio playing, or a Bluetooth route). An `early` press whose
        /// start was overtaken by recognition (preparing slower than the
        /// threshold, a stalled main thread) has `beginAt` at or after
        /// `recognisedAt`.
        var variant = "early"
        /// The hold threshold this press was timed with, in ms.
        var holdMs = 200
        /// Why nothing was prepared at touch-down (`other`, `bluetooth`).
        var why: String?
        var route = ""
        /// The earliest the input might start after touch-down, in ms.
        var startAfterMs: Int?
        /// Nothing was prepared before the start (`prepare: "start"`).
        var bare = false
        /// How long the touch took to reach the App (its own time to the
        /// App's handling of it), in ms. Every other time here counts from
        /// that handling, not from the finger meeting the glass.
        var deliveryMs: Int?
        var categoryMs: Int?
        var activateMs: Int?
        var prepareMs: Int?
        /// When the prepared engine was ready.
        var armedAt: Int?
        /// When the input was asked to start: the finger had just been
        /// seen down.
        var beginAt: Int?
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
        /// How often the engine was started again after its configuration
        /// changed under the take, and the last such change.
        var restarts = 0
        var change: NativeHoldInputChange?

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
        /// touch-down with no wait: this press's own set-up and start times,
        /// worked out the same way for every variant. (`liveAt` is what this
        /// press measured, waits included.)
        var liveEarly: Int? {
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
                ("startAfterMs", startAfterMs), ("deliveryMs", deliveryMs),
                ("categoryMs", categoryMs), ("activateMs", activateMs), ("prepareMs", prepareMs),
                ("armedAt", armedAt), ("beginAt", beginAt), ("recognisedAt", recognisedAt), ("hapticAt", hapticAt),
                ("startMs", startMs), ("liveAt", liveAt), ("cueToLive", cueToLive),
                ("liveEarly", liveEarly), ("liveQuiet", liveQuiet), ("holdMs", holdMs),
                ("firstBufferAt", firstBufferAt), ("firstBufferBytes", firstBufferBytes),
                ("adoptedAt", adoptedAt), ("releasedAt", releasedAt),
            ]
            for (name, value) in numbers { if let value { fields[name] = value } }
            if bare { fields["bare"] = true }
            if let why { fields["why"] = why }
            if let end { fields["end"] = end }
            if restarts > 0 { fields["restarts"] = restarts }
            if let change { fields.merge(change.fields) { _, new in new } }
            return fields
        }

        /// One short line for the phone's own record (`NativeHoldTimings`).
        var line: String {
            func part(_ name: String, _ value: Int?) -> String { value.map { " \(name)=\($0)" } ?? "" }
            return "hold \(variant)\(why.map { ":" + $0 } ?? "") \(route)"
                + part("t", startAfterMs) + (bare ? " bare" : "") + part("dly", deliveryMs)
                + part("cat", categoryMs) + part("act", activateMs) + part("prep", prepareMs)
                + part("armed", armedAt) + part("begin", beginAt) + part("rec", recognisedAt) + part("hap", hapticAt)
                + part("start", startMs) + part("live", liveAt) + part("gap", cueToLive)
                + part("e", liveEarly) + part("q", liveQuiet)
                + part("buf", firstBufferAt) + part("bytes", firstBufferBytes)
                + part("adopt", adoptedAt) + part("up", releasedAt)
                + (restarts > 0 ? " rs=\(restarts)" : "") + (change.map { " cfg=" + $0.text } ?? "")
                + (end.map { " end=" + $0 } ?? "")
        }
    }

    /// What a touch says of itself when asked: when it lifted, by its own
    /// time (nil while the finger is down), and where it is in its WebView
    /// (nil when that is not known).
    struct TouchState {
        var liftedAt: TimeInterval?
        var point: CGPoint?
        /// The touch is gone: it has lifted, but when is not known
        /// (`liftedAt` is then the time of asking).
        var gone = false
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
        /// The touch's own time at touch-down (`UITouch.timestamp`).
        var downStamp: TimeInterval?
        /// Asks the touch itself where it is and whether it has lifted. On
        /// a surface whose page took the press for itself, UIKit tells the
        /// observer of neither.
        var touch: (@MainActor () -> TouchState)?
        /// How long the touch lasted by its own times, when that made it a tap.
        var tapMs: Int?
        /// A tap whose touch was gone when it was judged: its lift time is
        /// not known, only that it came before then.
        var lostTap = false
        var recognised = false
        /// The input was asked to start.
        var begun = false
        /// The input was asked to prepare at touch-down.
        var prepared = false
        /// That preparing has answered (it may have failed: the start says how).
        var armAnswered = false
        /// The earliest the input may start has passed.
        var startDue = false
        /// Ends the wait for that moment, and the look at the finger then.
        var cancelStart: (() -> Void)?
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
    /// Each WebView's `voice.holdSurfaces` calls, counted: a list whose
    /// consent answer comes back after a later call, or after its document
    /// went, is not applied.
    private let surfaceRequests = NSMapTable<WKWebView, NSNumber>.weakToStrongObjects()
    /// The page's hold threshold, as it named it with its surfaces.
    private var holdSeconds = NativeHoldCapture.defaultHoldSeconds
    /// The press being followed, until its take is adopted or it ends.
    private var hold: Hold?
    private let after: @MainActor (TimeInterval, @escaping @MainActor () -> Void) -> (() -> Void)
    private let onceTouchesAreHeard: @MainActor (@escaping @MainActor () -> Void) -> (() -> Void)
    private let uptimeMs: () -> Int
    private let haptic: NativeHoldHaptic
    private let startsAtTouchDown: () -> Bool
    private let noteTimings: (String) -> Void
    private let noteTap: (Tap) -> Void
    private let tapsTotal: () -> String?
    private let tapsLost: () -> String?
    private let sendHoldEvent: @MainActor (WKWebView?, [String: Any]) -> Void
    private let sendCaptureEvent: @MainActor (WKWebView?, String, Event) -> Void

    /// How long a press is held before it is a hold, and how far it may
    /// move first: the page's own numbers for a finger (`HOLD_GESTURE`).
    static let defaultHoldSeconds: TimeInterval = 0.2
    /// The earliest the input starts after touch-down when the page names
    /// no time for a surface: long enough that a quick tap has lifted.
    static let defaultStartAfterMs = 50
    static let slop: CGFloat = 10
    /// How long a recognised hold's take waits for its page.
    /// The microphone is open meanwhile, so no longer than a page needs:
    /// its answer is two turns of its main thread (the event, then the
    /// consent check), tens of ms. In 85 holds on a device the page's own
    /// 200 ms timer was never more than 153 ms late. When it runs out the
    /// take is dropped and its line ends `end=unadopted`.
    static let adoptionWait: TimeInterval = 1
    /// The most audio kept for a page that has not taken its take: 2 s,
    /// more than the longest threshold and the wait together can give.
    /// The input itself keeps to it (`NativeHoldSink`), on the thread its
    /// audio arrives on, so it holds while the main thread is stalled.
    nonisolated static let unadoptedBytes = 2 * 16_000 * 2

    /// `afterQueued` runs its work once what the input already sent to the
    /// main queue has arrived (by default, the main queue's next turn);
    /// `isInBackground` says whether the App is in the background;
    /// `onceTouchesAreHeard` runs its work once every touch event that has
    /// reached the App has been handled (`whenTouchesAreHeard`).
    init(
        audio: NativeHoldCaptureAudio,
        afterQueued: @escaping (@escaping () -> Void) -> Void = { DispatchQueue.main.async(execute: $0) },
        isInBackground: @escaping () -> Bool = { UIApplication.shared.applicationState == .background },
        after: (@MainActor (TimeInterval, @escaping @MainActor () -> Void) -> (() -> Void))? = nil,
        onceTouchesAreHeard: (@MainActor (@escaping @MainActor () -> Void) -> (() -> Void))? = nil,
        uptimeMs: @escaping () -> Int = { Int(DispatchTime.now().uptimeNanoseconds / 1_000_000) },
        haptic: NativeHoldHaptic? = nil,
        startsAtTouchDown: @escaping () -> Bool = { !NativeHoldQuietSwitch.isOn },
        noteTimings: @escaping (String) -> Void = { NativeHoldTimings.add($0) },
        noteTap: @escaping (Tap) -> Void = { NativeHoldTaps.note($0) },
        tapsTotal: @escaping () -> String? = { NativeHoldTaps.total() },
        tapsLost: @escaping () -> String? = { NativeHoldTaps.lostTotal() },
        sendHoldEvent: (@MainActor (WKWebView?, [String: Any]) -> Void)? = nil,
        sendCaptureEvent: (@MainActor (WKWebView?, String, Event) -> Void)? = nil
    ) {
        self.audio = audio
        self.afterQueued = afterQueued
        self.isInBackground = isInBackground
        self.after = after ?? { delay, work in NativeHoldCapture.mainTimer(delay, work) }
        self.onceTouchesAreHeard = onceTouchesAreHeard ?? { work in NativeHoldCapture.whenTouchesAreHeard(work) }
        self.uptimeMs = uptimeMs
        self.haptic = haptic ?? .system()
        self.startsAtTouchDown = startsAtTouchDown
        self.noteTimings = noteTimings
        self.noteTap = noteTap
        self.tapsTotal = tapsTotal
        self.tapsLost = tapsLost
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

    /// The hold thresholds the App takes from a page, in ms. The upper end
    /// is a promise, not a taste: the privacy policy says the audio of a
    /// touch that is not a hold is "that fraction of a second"
    /// (avibe-backend#397), and such a touch records for at most its
    /// threshold. Do not raise it without changing that sentence.
    static let holdMsRange = 100...500

    /// The page's `holdMs` argument: a whole number of ms in `holdMsRange`.
    /// Any other is refused, not rounded: the page and the App time a hold
    /// with one number.
    static func holdMs(from value: Any?) -> Int? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue == number.doubleValue.rounded(), holdMsRange.contains(number.intValue)
        else { return nil }
        return number.intValue
    }

    /// A surface's `startAfterMs`: a whole number of ms from 0 to the
    /// longest hold threshold. Any other is refused, not rounded.
    static func startAfterMs(from value: Any?) -> Int? {
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue == number.doubleValue.rounded(),
              (0...holdMsRange.upperBound).contains(number.intValue)
        else { return nil }
        return number.intValue
    }

    /// The page's `surfaces` argument: at most eight, each an id and a
    /// rectangle of finite, positive size, and optionally when its input
    /// may start: `start` (`touch`, the default, or `recognition`),
    /// `startAfterMs` (the earliest, in ms after touch-down; the App's
    /// default without it), and `prepare` (`touch`, the default, or
    /// `start`: nothing is prepared before the start). Nil when it is not
    /// that.
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
            let start = item["start"] as? String
            let prepare = item["prepare"] as? String
            let startAfterMs = Self.startAfterMs(from: item["startAfterMs"])
            let optional = ["start", "startAfterMs", "prepare"].filter { item[$0] != nil }.count
            guard item.count == 5 + optional,
                  item["start"] == nil || start == "touch" || start == "recognition",
                  item["startAfterMs"] == nil || startAfterMs != nil,
                  // At recognition is a time of its own: not both.
                  !(start == "recognition" && startAfterMs != nil),
                  item["prepare"] == nil || prepare == "touch" || prepare == "start",
                  let id = isValidTake(item["id"]),
                  let x = number("x"), let y = number("y"),
                  let width = number("width"), let height = number("height"),
                  width > 0, height > 0
            else { return nil }
            surfaces.append(Surface(
                id: id, rect: CGRect(x: x, y: y, width: width, height: height),
                startsAtRecognition: start == "recognition",
                startAfterMs: startAfterMs,
                preparesAtStart: prepare == "start"
            ))
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
            let timings = try audio.start(
                onPCM: receiver(for: take), onEnd: ender(for: take), onChange: changer(for: take)
            )
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
                // The input keeps this bound itself, where its audio arrives
                // (`NativeHoldSink`); here it holds of any input.
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

    /// The engine's configuration changed under `take`: a hold's timings
    /// keep it (the page's trace and the phone's line); a take the page
    /// started has only the phone's line.
    private func changer(for take: Take) -> @MainActor (NativeHoldInputChange) -> Void {
        { [weak self, weak take] change in
            guard let self, let take, self.running === take else { return }
            if let hold = take.hold {
                hold.timings.change = change
                if change.restarted { hold.timings.restarts += 1 }
            } else {
                self.noteTimings("hold page cfg=\(change.text)")
            }
        }
    }

    // MARK: A hold the App recognises itself

    /// `webView`'s touches reach this capture (its recogniser is attached).
    func watch(_ webView: WKWebView) {
        watched.add(webView)
    }

    /// A `voice.holdSurfaces` call from `webView` arrived: its number.
    func surfacesRequested(by webView: WKWebView) -> Int {
        let number = (surfaceRequests.object(forKey: webView)?.intValue ?? 0) + 1
        surfaceRequests.setObject(NSNumber(value: number), forKey: webView)
        return number
    }

    /// Whether `request` is still `webView`'s last call, in the document
    /// that made it: the page's calls take effect in the order it made them.
    func isLatestSurfacesRequest(_ request: Int, of webView: WKWebView) -> Bool {
        surfaceRequests.object(forKey: webView)?.intValue == request
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
        // The threshold is this list's: without one, the default.
        holdSeconds = holdMs.map { TimeInterval($0) / 1000 } ?? Self.defaultHoldSeconds
        // A press still waiting to be a hold, on a surface that is no
        // longer one: it is not followed any more.
        if let hold, !hold.recognised,
           !(hold.webView === webView && surfaces.contains { $0.id == hold.surface && $0.rect.contains(hold.origin) }) {
            // Most often the tap that focuses the composer: the page took the
            // press for itself, so the App was not told of the lift, and the
            // focused composer left the list before the App learnt of it.
            // Asked, the touch says whether it lifted: then it was a tap, and
            // is counted as one by its own length. A touch already gone (on a
            // phone, by the time the keyboard has come up) lifted at a time
            // not known, before now: still a tap, counted with that bound
            // instead of its length (`NativeHoldTaps`, `lost`).
            if let state = hold.touch?(), let lifted = state.liftedAt {
                if state.gone {
                    hold.lostTap = true
                } else if let down = hold.downStamp {
                    hold.tapMs = max(0, Int(((lifted - down) * 1000).rounded()))
                }
                cancelHold(hold, why: "tap")
            } else {
                cancelHold(hold, why: "withdrawn")
            }
        }
        return true
    }

    var followsPress: Bool { hold != nil }

    /// A finger came down in `webView` at `point` (its own points). On one
    /// of the page's surfaces the App prepares the microphone, without
    /// starting it, and times the hold itself.
    /// `touch` asks the touch itself where it is and whether it has lifted;
    /// `deliveredAfterMs` is how long the touch took to reach the App.
    func touchDown(
        at point: CGPoint, in webView: WKWebView, timestamp: TimeInterval? = nil,
        deliveredAfterMs: Int? = nil,
        touch: (@MainActor () -> TouchState)? = nil
    ) {
        guard hold == nil, running == nil, surfacesWebView === webView,
              let surface = surfaces.first(where: { $0.rect.contains(point) }),
              !VoiceCaptureAudioState.active, !isInBackground(), audio.permissionGranted
        else { return }
        let hold = Hold(
            take: "n" + String(UUID().uuidString.lowercased().replacingOccurrences(of: "-", with: "").prefix(16)),
            surface: surface.id, webView: webView, origin: point, downAt: uptimeMs()
        )
        self.hold = hold
        hold.downStamp = timestamp
        hold.touch = touch
        hold.timings.route = audio.route
        let holdMs = Int((holdSeconds * 1000).rounded())
        hold.timings.holdMs = holdMs
        hold.timings.deliveryMs = deliveredAfterMs
        // The earliest the input may start: at recognition when the page
        // (or the App's own switch) says so, else the surface's time or the
        // App's default, and never after recognition.
        let startAfterMs = !startsAtTouchDown() || surface.startsAtRecognition
            ? holdMs
            : min(surface.startAfterMs ?? Self.defaultStartAfterMs, holdMs)
        hold.timings.variant = startAfterMs < holdMs ? "early" : "quiet"
        haptic.prepare()
        if audio.otherAudioPlaying {
            // Activating would pause it: a touch that is not a hold must not.
            hold.timings.variant = "late"
            hold.timings.why = "other"
        } else if audio.routeIsBluetooth {
            // Activating can switch a headset to its call mode.
            hold.timings.variant = "late"
            hold.timings.why = "bluetooth"
        } else {
            hold.timings.startAfterMs = startAfterMs
            if surface.preparesAtStart {
                hold.timings.bare = true
            } else {
                hold.prepared = true
                audio.arm { [weak self, weak hold] result in
                    guard let self, let hold else { return }
                    hold.armAnswered = true
                    switch result {
                    case .success(let armed):
                        hold.timings.categoryMs = armed.categoryMs
                        hold.timings.activateMs = armed.activateMs
                        hold.timings.prepareMs = armed.prepareMs
                        hold.timings.armedAt = self.since(hold)
                    case .failure:
                        // The start says what failed.
                        break
                    }
                    self.startIfDue(hold)
                }
            }
            if startAfterMs < holdMs {
                let cancel = after(TimeInterval(startAfterMs) / 1000) { [weak self, weak hold] in
                    guard let self, let hold else { return }
                    hold.startDue = true
                    self.startIfDue(hold)
                }
                hold.cancelStart = cancel
            }
        }
        hold.cancelTimer = after(holdSeconds) { [weak self, weak hold] in
            guard let self, let hold, self.hold === hold, !hold.recognised else { return }
            // The threshold has passed by the clock, which does not say the
            // finger is still down: on a busy main thread this block can run
            // while the touch's end waits to be handled, and a tap would get
            // the cue and keep the microphone. The hold is recognised once
            // every touch event that has reached the App has been handled;
            // a lift among them ends the press first, as a tap.
            hold.cancelTimer = self.onceTouchesAreHeard { [weak self, weak hold] in
                guard let self, let hold, self.hold === hold, !hold.recognised else { return }
                // And the touch itself is asked. On a surface whose page took
                // the press for itself (it prevents the default on touchstart,
                // as the composer does), UIKit stops telling the observer:
                // no moves, and the lift only when it resets it, which a tap
                // that raises the keyboard delays past this moment.
                let touch = hold.touch?()
                if let touch, let lifted = touch.liftedAt {
                    // A touch already gone lifted at a time not known: counted with its bound.
                    if touch.gone { hold.lostTap = true }
                    else if let down = hold.downStamp { hold.tapMs = max(0, Int(((lifted - down) * 1000).rounded())) }
                    self.cancelHold(hold, why: "tap")
                    return
                }
                if let point = touch?.point, hypot(point.x - hold.origin.x, point.y - hold.origin.y) > Self.slop {
                    self.cancelHold(hold, why: "moved")
                    return
                }
                self.recognise(hold)
            }
        }
    }

    func touchMoved(to point: CGPoint, in webView: WKWebView) {
        guard let hold, hold.webView === webView else { return }
        // After the cue the finger's slides are the page's to answer: it
        // sees them on every surface, and gives their haptics.
        guard !hold.recognised else { return }
        if hypot(point.x - hold.origin.x, point.y - hold.origin.y) > Self.slop {
            cancelHold(hold, why: "moved")
        }
    }

    /// The finger went up (or the system took the touch). Before the hold
    /// was recognised it was a tap: whatever was prepared is taken down and
    /// nothing was recorded. After, the page ends the take as it does today.
    /// `timestamp` is the touch's own time (`UITouch.timestamp`).
    func touchEnded(in webView: WKWebView, timestamp: TimeInterval? = nil) {
        guard let hold, hold.webView === webView else { return }
        guard hold.recognised else {
            // Counted by the touch's own length: this block may run late.
            if let down = hold.downStamp, let up = timestamp {
                hold.tapMs = max(0, Int(((up - down) * 1000).rounded()))
            }
            cancelHold(hold, why: "tap")
            return
        }
        // A lift that reached the App only after the hold was recognised (the
        // system delivered it late), from before the threshold by the touch's
        // own times: a tap, and its audio is dropped as a tap's. The page
        // cannot have taken it yet: its answer comes after this block.
        if let down = hold.downStamp, let up = timestamp, (up - down) * 1000 < Double(hold.timings.holdMs),
           let take = running, take.hold === hold, !take.adopted {
            hold.tapMs = max(0, Int(((up - down) * 1000).rounded()))
            discard(take, why: "tap")
            return
        }
        hold.timings.releasedAt = since(hold)
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
        // The finger was just seen down: the input starts now if it has not.
        hold.cancelStart?()
        hold.cancelStart = nil
        if !hold.begun { begin(hold) }
        sendHoldEvent(hold.webView, ["phase": "recognised", "take": hold.take, "surface": hold.surface])
        hold.cancelTimer = after(Self.adoptionWait) { [weak self, weak hold] in
            guard let self, let hold, let take = self.running, take.hold === hold, !take.adopted else { return }
            self.discard(take, why: "unadopted")
        }
    }

    /// Starts the input before the hold is recognised, once it may: it is
    /// prepared (or nothing is prepared before the start), its earliest
    /// time has passed, and the finger is seen still down.
    ///
    /// **A lift before the start never opens the microphone.** The finger
    /// is asked on the main thread, once every touch event that has reached
    /// the App has been handled: UIKit reports no lift for a press the page
    /// took for itself, and a touch may only be asked here. So a main
    /// thread that is stalled starts nothing: a hold made then records from
    /// when it is free, and a tap made then opens no microphone.
    private func startIfDue(_ hold: Hold) {
        guard self.hold === hold, !hold.begun, !hold.recognised, hold.startDue,
              hold.armAnswered || !hold.prepared
        else { return }
        hold.cancelStart?()
        hold.cancelStart = onceTouchesAreHeard { [weak self, weak hold] in
            guard let self, let hold, self.hold === hold, !hold.begun, !hold.recognised else { return }
            hold.cancelStart = nil
            let touch = hold.touch?()
            if let touch, let lifted = touch.liftedAt {
                // A touch already gone lifted at a time not known: counted with its bound.
                if touch.gone { hold.lostTap = true }
                else if let down = hold.downStamp { hold.tapMs = max(0, Int(((lifted - down) * 1000).rounded())) }
                self.cancelHold(hold, why: "tap")
                return
            }
            if let point = touch?.point, hypot(point.x - hold.origin.x, point.y - hold.origin.y) > Self.slop {
                self.cancelHold(hold, why: "moved")
                return
            }
            self.begin(hold)
        }
    }

    /// Starts the input for `hold`; its audio waits in memory for the page.
    private func begin(_ hold: Hold) {
        hold.begun = true
        hold.timings.beginAt = since(hold)
        let webView = hold.webView
        let id = hold.take
        let send = sendCaptureEvent
        let take = Take(id: id, webView: webView, toPage: true) { [weak webView] event in
            send(webView, id, event)
        }
        take.hold = hold
        take.adopted = false
        running = take
        audio.begin(
            onPCM: receiver(for: take), onEnd: ender(for: take), onChange: changer(for: take)
        ) { [weak self, weak take, weak hold] result in
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
                // An adopted take's page hears the end. Another's is
                // dropped, and its page is told: started at touch-down, the
                // press would otherwise end with no cue and no event, and
                // the page could not tell it from a tap.
                guard !take.adopted else {
                    self.end(reason: step)
                    return
                }
                self.discard(take, why: step)
                self.sendHoldEvent(
                    hold.webView, ["phase": "failed", "take": hold.take, "surface": hold.surface, "step": step]
                )
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
        audio.taken()
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
        hold.cancelStart?()
        hold.cancelStart = nil
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
        case "tap": noteTap(hold.lostTap ? lostTap(hold, atMost: since(hold)) : tap(hold, ms: hold.tapMs ?? since(hold), moved: false))
        case "moved": noteTap(tap(hold, ms: since(hold), moved: true))
        default: noteTimings(hold.timings.line)
        }
    }

    /// A press that was not a hold, as it is counted: its length, whether
    /// the microphone was started for it, and for each threshold in
    /// `NativeHoldTaps.thresholds` whether it would have been.
    ///
    /// With a threshold the input starts at the later of "prepared" and the
    /// threshold, if the finger is still down then. A press whose preparing
    /// had not answered when it ended was shorter than its preparing. A
    /// press that moved away is not judged: when it left the slop is not
    /// kept.
    ///
    /// The threshold and "prepared" count from the App's handling of the
    /// touch-down; a length by the touch's own times counts from the finger
    /// meeting the glass. So such a length is compared after adding how long
    /// the touch took to reach the App.
    private func tap(_ hold: Hold, ms: Int, moved: Bool) -> Tap {
        var tap = Tap(ms: ms, moved: moved, opened: hold.begun)
        guard !moved, hold.timings.variant != "late" else { return tap }
        let ready = hold.prepared ? hold.timings.armedAt : 0
        let delivery = hold.tapMs != nil ? hold.timings.deliveryMs ?? 0 : 0
        for threshold in NativeHoldTaps.thresholds {
            tap.wouldOpen[threshold] = ready.map { ms >= delivery + max($0, threshold) } ?? false
        }
        return tap
    }

    /// A tap whose lift time is not known, only that it came within
    /// `atMost` ms of the App's handling of its touch-down. It would have
    /// opened the microphone at a threshold only if the finger was still
    /// down once prepared and the threshold had passed: surely not when the
    /// bound comes before that, or when preparing had not answered by then.
    private func lostTap(_ hold: Hold, atMost: Int) -> Tap {
        var tap = Tap(ms: atMost, moved: false, opened: hold.begun, lengthUnknown: true)
        guard hold.timings.variant != "late" else { return tap }
        let ready = hold.prepared ? hold.timings.armedAt : 0
        for threshold in NativeHoldTaps.thresholds {
            tap.surelyClosed[threshold] = ready.map { atMost < max($0, threshold) } ?? true
        }
        return tap
    }

    /// Ends a hold's take that no page took: its audio never leaves memory.
    private func discard(_ take: Take, why: String) {
        take.detached = true
        take.pending = Data()
        end(reason: why)
    }

    /// Runs `work` on the main thread once it has handled every touch event
    /// that has reached the App; the returned closure cancels.
    ///
    /// UIKit hands touches to the App from a source of the main run loop.
    /// The loop calls its `beforeWaiting` observers only in a pass in which
    /// no such source had anything to do, so by then a lift that had arrived
    /// has been delivered. With the main thread idle that is at once.
    static func whenTouchesAreHeard(_ work: @escaping @MainActor () -> Void) -> (() -> Void) {
        let observer = CFRunLoopObserverCreateWithHandler(
            kCFAllocatorDefault, CFRunLoopActivity.beforeWaiting.rawValue, false, 0
        ) { _, _ in
            MainActor.assumeIsolated { work() }
        }
        CFRunLoopAddObserver(CFRunLoopGetMain(), observer, .commonModes)
        CFRunLoopWakeUp(CFRunLoopGetMain())
        return { CFRunLoopObserverInvalidate(observer) }
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
        // A list its last document asked for is not applied to the next.
        _ = surfacesRequested(by: gone)
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
        closing[take.id] = take
        // The input stops here. What it sent before it had stopped is still
        // on its way to the main actor and belongs to this take: the rest
        // of its audio and `ended` go once it has stopped (a hold's input
        // stops on its own queue, a moment after this).
        audio.stop { [weak self, afterQueued] in
            afterQueued {
                MainActor.assumeIsolated {
                    self?.closing[take.id] = nil
                    take.flush()
                    take.deliver(.ended(reason: reason))
                }
            }
        }
        if take.hold != nil, !take.adopted {
            // No page took it: what it recorded never leaves memory.
            take.detached = true
            take.pending = Data()
        }
        if let hold = take.hold {
            hold.cancelTimer?()
            hold.cancelTimer = nil
            hold.cancelStart?()
            hold.cancelStart = nil
            if self.hold === hold { self.hold = nil }
            hold.timings.end = reason ?? "stopped"
            note(hold)
            if take.adopted, !take.detached {
                // For the page's own trace of this take.
                var timings = hold.timings.fields
                // The touches that were not holds, as counted so far.
                if let taps = tapsTotal() { timings["taps"] = taps }
                if let lost = tapsLost() { timings["tapsLost"] = lost }
                sendHoldEvent(take.webView, ["phase": "timings", "take": take.id, "timings": timings])
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

    @MainActor
    static func system() -> NativeHoldHaptic {
        // The page's own cue for a hold (`impactFeedback('medium')`). The
        // slides into the cancel position and back are the page's to give:
        // the App does not see them on every surface.
        let start = UIImpactFeedbackGenerator(style: .medium)
        return NativeHoldHaptic(
            prepare: { start.prepare() },
            fire: { start.impactOccurred() }
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
///
/// And, to judge the time before the microphone may start: how many of them
/// had the microphone started (`open`), and how many of the taps would have
/// had with each of `thresholds` (`w50`…): the finger still down when the
/// input was prepared and the threshold had passed.
enum NativeHoldTaps {
    static let key = "avibe.voiceHold.taps"
    /// The same counts since the App was installed, never taken off: each
    /// hold's timings carry them to its page, so they reach the instance
    /// without an Action Button message.
    static let totalKey = "avibe.voiceHold.tapsTotal"
    static let bucketMs = 25
    static let moved = "moved"
    static let opened = "open"
    /// The times before the start that are judged, in ms.
    static let thresholds = [50, 80, 100, 120]
    private static let prefix = "hold taps"

    /// Taps whose length is not known (`Tap.lengthUnknown`): they are in the
    /// bucket of the most they can have lasted, and in no `w…` count.
    static let lost = "lost"

    private static func would(_ threshold: Int) -> String { "w\(threshold)" }
    /// Such taps that surely ended before the input could start at `threshold`.
    private static func closed(_ threshold: Int) -> String { "c\(threshold)" }

    /// The bucket a touch of `ms` falls in: its lower edge, capped at 975.
    static func bucket(ms: Int) -> String {
        String(min(max(0, ms), 975) / bucketMs * bucketMs)
    }

    static func adding(ms: Int, moved: Bool, to counts: [String: Int]) -> [String: Int] {
        adding(NativeHoldCapture.Tap(ms: ms, moved: moved), to: counts)
    }

    static func adding(_ tap: NativeHoldCapture.Tap, to counts: [String: Int]) -> [String: Int] {
        var counts = counts
        counts[tap.moved ? Self.moved : bucket(ms: tap.ms), default: 0] += 1
        if tap.opened { counts[opened, default: 0] += 1 }
        for (threshold, open) in tap.wouldOpen where open { counts[would(threshold), default: 0] += 1 }
        if tap.lengthUnknown { counts[lost, default: 0] += 1 }
        for (threshold, shut) in tap.surelyClosed where shut { counts[closed(threshold), default: 0] += 1 }
        return counts
    }

    /// `hold taps 50:3 75:12 100:4 moved=2 open=1 w50=9 w80=4 lost=2 c50=1`,
    /// or nil with nothing counted. The named counts are left out at zero.
    /// `lengths: false` leaves out `lost` and the `c…` counts: the line a
    /// page from before them reads, refusing any name it does not know.
    static func line(for counts: [String: Int], lengths: Bool = true) -> String? {
        let buckets = counts.filter { Int($0.key) != nil && $0.value > 0 }
            .sorted { (Int($0.key) ?? 0) < (Int($1.key) ?? 0) }
            .map { " \($0.key):\($0.value)" }
            .joined()
        let movedCount = counts[moved] ?? 0
        guard !buckets.isEmpty || movedCount > 0 else { return nil }
        let names = [moved, opened] + thresholds.map(would) + (lengths ? [lost] + thresholds.map(closed) : [])
        let named = names
            .compactMap { name in (counts[name] ?? 0) > 0 ? " \(name)=\(counts[name] ?? 0)" : nil }
            .joined()
        return prefix + buckets + named
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

    static func note(_ tap: NativeHoldCapture.Tap) {
        UserDefaults.standard.set(adding(tap, to: stored), forKey: key)
        let total = UserDefaults.standard.dictionary(forKey: totalKey) as? [String: Int] ?? [:]
        UserDefaults.standard.set(adding(tap, to: total), forKey: totalKey)
    }

    static func line() -> String? {
        line(for: stored)
    }

    private static var storedTotal: [String: Int] {
        UserDefaults.standard.dictionary(forKey: totalKey) as? [String: Int] ?? [:]
    }

    /// The counts since the App was installed, without the line's prefix
    /// (`50:3 75:12 moved=2 open=1 w50=9`), as every page reads them; nil
    /// with nothing counted.
    static func total() -> String? {
        line(for: storedTotal, lengths: false).map { String($0.dropFirst(prefix.count + 1)) }
    }

    /// Of those, the taps whose length is not known, and how many of them
    /// surely did not reach each threshold (`lost=3 c50=1 c80=2 c100=2
    /// c120=3`); nil with none.
    static func lostTotal() -> String? {
        lostLine(for: storedTotal)
    }

    static func lostLine(for counts: [String: Int]) -> String? {
        guard (counts[lost] ?? 0) > 0 else { return nil }
        return ([lost] + thresholds.map(closed)).map { "\($0)=\(counts[$0] ?? 0)" }.joined(separator: " ")
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
    /// `onChange` hears each change of the engine's configuration under the
    /// take, with whether the input started again; one it did not is
    /// followed by `onEnd("route")`.
    func begin(
        onPCM: @escaping @MainActor (Data) -> Void,
        onEnd: @escaping @MainActor (String) -> Void,
        onChange: @escaping @MainActor (NativeHoldInputChange) -> Void,
        completion: @escaping @MainActor (Result<NativeHoldCapture.BeginTimings, NativeHoldCapture.StartError>) -> Void
    )
    /// Takes down what `arm` prepared; nothing was captured.
    func disarm()
    /// The page took the take `begin` started. Until then the input passes
    /// on no more than `NativeHoldCapture.unadoptedBytes`, and past that
    /// stops itself and ends the take (`onEnd("unadopted")`), without
    /// waiting for the main thread.
    func taken()
    /// Returns once everything asked of the input so far (a preparing, a
    /// start, a taking down) has finished: the session is free for its next
    /// owner, and nothing will set or deactivate it afterwards.
    func settle()
    func prepare()
    /// Activates the session and starts the input. `onPCM` gets 16 kHz mono
    /// little-endian Int16 audio and `onEnd` the system's reason for ending
    /// the input, both on the main actor; `onChange` as for `begin`.
    func start(
        onPCM: @escaping @MainActor (Data) -> Void,
        onEnd: @escaping @MainActor (String) -> Void,
        onChange: @escaping @MainActor (NativeHoldInputChange) -> Void
    ) throws -> NativeHoldCapture.Timings
    /// Stops the input. `stopped` runs on the main actor once it has
    /// stopped and can send no more audio: at once for an input stopped
    /// here, later for one stopped on its own queue.
    func stop(stopped: @escaping @MainActor () -> Void)
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

/// A take's `AVAudioEngine` as `NativeHoldEngineRecovery` restarts it.
final class NativeHoldEngineInput: NativeHoldRestartableInput, @unchecked Sendable {
    private let engine: AVAudioEngine
    /// The tap for a converter fitted to the input's format.
    private let tap: (NativeHoldPCMConverter) -> AVAudioNodeTapBlock

    init(engine: AVAudioEngine, tap: @escaping (NativeHoldPCMConverter) -> AVAudioNodeTapBlock) {
        self.engine = engine
        self.tap = tap
    }

    static func state(of engine: AVAudioEngine) -> NativeHoldInputState? {
        let session = AVAudioSession.sharedInstance()
        guard session.isInputAvailable else { return nil }
        let format = engine.inputNode.outputFormat(forBus: 0)
        return .init(
            port: session.currentRoute.inputs.first?.portType.rawValue ?? "none",
            rate: Int(format.sampleRate),
            channels: Int(format.channelCount)
        )
    }

    func current() -> NativeHoldInputState? {
        Self.state(of: engine)
    }

    func restart() throws {
        let input = engine.inputNode
        input.removeTap(onBus: 0)
        let format = input.outputFormat(forBus: 0)
        guard let converter = NativeHoldPCMConverter(input: format) else {
            throw NSError(domain: "NativeHoldEngineInput.format", code: Int(format.sampleRate))
        }
        input.installTap(onBus: 0, bufferSize: 1024, format: format, block: tap(converter))
        engine.prepare()
        try engine.start()
    }
}

/// Where one engine's audio goes. Each engine has its own, emptied when it
/// is taken down: a callback of an engine that is gone can never hand its
/// buffer to the next press's take.
///
/// It also holds the bound on a take no page has taken. That audio waits for
/// the page in the App's memory, and reaches it by way of the main thread:
/// if the bound were kept only there, a main thread stalled for ten seconds
/// would let ten seconds pile up before anything dropped them. Here, on the
/// thread the audio arrives on, no more than `limit` bytes ever pass before
/// the page has taken the take; past that nothing is passed on, and
/// `overflow` is called once, to stop the input.
final class NativeHoldSink: @unchecked Sendable {
    private let lock = NSLock()
    private var onPCM: ((Data) -> Void)?
    private var overflow: (() -> Void)?
    /// How much may still pass before the page takes the take; nil once it has.
    private var left: Int?

    /// Passes audio on from here, at most `limit` bytes of it until `unlimit()`.
    func open(limit: Int, onPCM: @escaping (Data) -> Void, overflow: @escaping () -> Void) {
        lock.lock()
        self.onPCM = onPCM
        self.overflow = overflow
        left = limit
        lock.unlock()
    }

    /// The page took the take: its audio is the page's, however long it is.
    func unlimit() {
        lock.lock()
        left = nil
        lock.unlock()
    }

    /// Nothing more passes.
    func close() {
        lock.lock()
        onPCM = nil
        overflow = nil
        lock.unlock()
    }

    func send(_ data: Data) {
        lock.lock()
        var pass = onPCM
        var over: (() -> Void)?
        if let left, pass != nil {
            if data.count > left {
                // Past the bound: this and all that follows is dropped here.
                pass = nil
                over = overflow
                onPCM = nil
                overflow = nil
            } else {
                self.left = left - data.count
            }
        }
        lock.unlock()
        pass?(data)
        over?()
    }
}

/// The input of a hold the App recognises itself: made, started and taken
/// down on one serial queue, so activating the session never holds up the
/// touch that asked for it.
private final class NativeHoldHeldInput: @unchecked Sendable {
    /// The queue all of this runs on, for what the input must do by itself.
    private let queue: DispatchQueue

    init(queue: DispatchQueue) {
        self.queue = queue
    }

    private var engine: AVAudioEngine?
    private var observers: [NSObjectProtocol] = []
    private var armed: NativeHoldCapture.ArmTimings?
    private let lock = NSLock()
    private var sink: NativeHoldSink?
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
        let sink = NativeHoldSink()
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
            guard let data = converter.convert(buffer) else { return }
            sink.send(data)
        }
        // Allocates what the start needs; the hardware starts with `start()`.
        engine.prepare()
        self.engine = engine
        self.sink = sink
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
        onEnd: @escaping (String) -> Void,
        onChange: @escaping (NativeHoldInputChange) -> Void
    ) -> Result<NativeHoldCapture.BeginTimings, NativeHoldCapture.StartError> {
        guard isCurrent(ticket) else { return .failure(.failed("cancelled")) }
        var armedNow: NativeHoldCapture.ArmTimings?
        if engine == nil {
            switch arm() {
            case .success(let timings): armedNow = timings
            case .failure(let error): return .failure(error)
            }
        }
        guard let engine, let sink else { return .failure(.failed("engine:0")) }
        // Until the page takes the take, no more than the bound passes, and
        // the input stops itself there, on this queue: neither waits for the
        // main thread, which may be stalled.
        sink.open(limit: NativeHoldCapture.unadoptedBytes, onPCM: onPCM) { [weak self, weak engine, queue] in
            queue.async {
                // An input already taken down ended its take another way.
                guard let self, let engine, self.stopAtTheBound(engine) else { return }
                onEnd("unadopted")
            }
        }
        let start = DispatchTime.now()
        do {
            try engine.start()
        } catch {
            takeDown(deactivate: true)
            return .failure(.failed("engine:\((error as NSError).code)"))
        }
        let startMs = SystemNativeHoldCaptureAudio.ms(since: start)
        let recovery = NativeHoldEngineRecovery(input: NativeHoldEngineInput.state(of: engine))
        let restartable = NativeHoldEngineInput(engine: engine) { converter in
            { buffer, _ in
                guard let data = converter.convert(buffer) else { return }
                sink.send(data)
            }
        }
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
            center.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) {
                [weak self, queue, engineID = ObjectIdentifier(engine)] _ in
                // On this input's queue, as everything else it does.
                queue.async {
                    // An input already taken down ended its take another way.
                    guard let self, self.engine.map(ObjectIdentifier.init) == engineID else { return }
                    let change = recovery.changed(restartable)
                    onChange(change)
                    if !change.restarted { onEnd("route") }
                }
            },
        ]
        return .success(.init(armed: armedNow, startMs: startMs))
    }

    /// The page took the take: its audio is no longer bounded.
    func unlimit() {
        sink?.unlimit()
    }

    /// The input reached the bound of a take no page has taken: it stops
    /// here. The take's end, and the session's, follow from the main thread
    /// when it can (`takeDown`). False when `stopping` is no longer the
    /// input.
    private func stopAtTheBound(_ stopping: AVAudioEngine) -> Bool {
        guard engine === stopping else { return false }
        // Nothing starts it again: a change of its configuration now finds
        // no one listening.
        observers.forEach(NotificationCenter.default.removeObserver)
        observers = []
        stopping.inputNode.removeTap(onBus: 0)
        stopping.stop()
        return true
    }

    /// `stopped` runs once the input can send no more audio, before the
    /// session is deactivated (which can take a while).
    func takeDown(deactivate: Bool, stopped: (() -> Void)? = nil) {
        observers.forEach(NotificationCenter.default.removeObserver)
        observers = []
        armed = nil
        if let engine {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
        }
        // After the engine has stopped: what it sent until then is the
        // take's, and nothing reaches this sink again.
        sink?.close()
        sink = nil
        stopped?()
        guard engine != nil else { return }
        engine = nil
        guard deactivate else { return }
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
}

@MainActor
final class SystemNativeHoldCaptureAudio: NativeHoldCaptureAudio {
    private var engine: AVAudioEngine?
    private var observers: [NSObjectProtocol] = []
    private let queue: DispatchQueue
    private let held: NativeHoldHeldInput
    /// Whether `held` may hold an input (asked for on `queue`, not yet taken down).
    private var heldInUse = false

    init() {
        let queue = DispatchQueue(label: "bot.avibe.ios.hold-capture", qos: .userInteractive)
        self.queue = queue
        held = NativeHoldHeldInput(queue: queue)
    }

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
        onChange: @escaping @MainActor (NativeHoldInputChange) -> Void,
        completion: @escaping @MainActor (Result<NativeHoldCapture.BeginTimings, NativeHoldCapture.StartError>) -> Void
    ) {
        heldInUse = true
        let ticket = held.ticket()
        queue.async { [held] in
            let result = held.begin(
                ticket,
                onPCM: { data in DispatchQueue.main.async { MainActor.assumeIsolated { onPCM(data) } } },
                onEnd: { reason in DispatchQueue.main.async { MainActor.assumeIsolated { onEnd(reason) } } },
                onChange: { change in DispatchQueue.main.async { MainActor.assumeIsolated { onChange(change) } } }
            )
            DispatchQueue.main.async { MainActor.assumeIsolated { completion(result) } }
        }
    }

    func disarm() {
        takeDownHeld(stopped: nil)
    }

    func taken() {
        queue.async { [held] in held.unlimit() }
    }

    func settle() {
        // Nothing on this queue ever waits for the main thread.
        queue.sync {}
    }

    /// Asks for the held input's takedown; false when there is none (and
    /// `stopped` is the caller's to run).
    @discardableResult
    private func takeDownHeld(stopped: (@MainActor () -> Void)?) -> Bool {
        guard heldInUse else { return false }
        heldInUse = false
        held.askTakedown()
        // An Action Button take that has the session now keeps it.
        let deactivate = !VoiceCaptureAudioState.active
        queue.async { [held] in
            held.takeDown(deactivate: deactivate, stopped: stopped.map { stopped in
                // After whatever the input sent to the main queue before it stopped.
                { DispatchQueue.main.async { MainActor.assumeIsolated { stopped() } } }
            })
        }
        return true
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
        onEnd: @escaping @MainActor (String) -> Void,
        onChange: @escaping @MainActor (NativeHoldInputChange) -> Void
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
        let recovery = NativeHoldEngineRecovery(input: NativeHoldEngineInput.state(of: engine))
        let restartable = NativeHoldEngineInput(engine: engine) { converter in
            Self.tap(converter: converter, onPCM: onPCM)
        }
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
            ) { [weak self, engineID = ObjectIdentifier(engine)] _ in
                // The input changed under the take: it stopped. While an
                // input remains it starts again; with none, what was
                // recorded goes on to be sent.
                MainActor.assumeIsolated {
                    guard let self, self.engine.map(ObjectIdentifier.init) == engineID else { return }
                    let change = recovery.changed(restartable)
                    onChange(change)
                    if !change.restarted { onEnd("route") }
                }
            },
        ]
        return .init(activateMs: activateMs, engineMs: Self.ms(since: engineStart), categoryMs: categoryMs)
    }

    func stop(stopped: @escaping @MainActor () -> Void) {
        // A hold's input stops on its queue and says so from there; a
        // page-started take's stops here.
        let heldStops = takeDownHeld(stopped: stopped)
        defer { if !heldStops { stopped() } }
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

/// Sees the first finger on the instance WebView for the App's own hold
/// recognition (NativeHoldCapture). It never recognises, cancels or delays
/// anything: the page gets every touch exactly as before.
final class NativeHoldTouchObserver: UIGestureRecognizer, UIGestureRecognizerDelegate {
    private weak var tracked: UITouch?
    /// A touch was handed to the capture and its end not yet.
    private var following = false

    init() {
        super.init(target: nil, action: nil)
        cancelsTouchesInView = false
        delaysTouchesBegan = false
        delaysTouchesEnded = false
        delegate = self
    }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent) {
        guard tracked == nil, let touch = touches.first, let webView = view as? WKWebView else { return }
        tracked = touch
        following = true
        NativeHoldCapture.shared.touchDown(
            at: touch.location(in: webView), in: webView, timestamp: touch.timestamp,
            deliveredAfterMs: max(0, Int(((ProcessInfo.processInfo.systemUptime - touch.timestamp) * 1000).rounded())),
            // The touch itself knows where the finger is and when it lifts,
            // whether or not UIKit goes on telling this recogniser.
            touch: { [weak touch, weak webView] in Self.state(of: touch, in: webView) }
        )
    }

    /// What `touch` says of itself: when it lifted, by its own time (nil
    /// while the finger is down), and where it is. A touch that is gone has
    /// lifted.
    private static func state(of touch: UITouch?, in webView: WKWebView?) -> NativeHoldCapture.TouchState {
        guard let touch else { return .init(liftedAt: ProcessInfo.processInfo.systemUptime, point: nil, gone: true) }
        let lifted = touch.phase == .ended || touch.phase == .cancelled
        return .init(liftedAt: lifted ? touch.timestamp : nil, point: webView.map { touch.location(in: $0) })
    }

    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent) {
        guard let tracked, touches.contains(tracked), let webView = view as? WKWebView else { return }
        NativeHoldCapture.shared.touchMoved(to: tracked.location(in: webView), in: webView)
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent) {
        finish(touches)
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent) {
        finish(touches)
    }

    private func finish(_ touches: Set<UITouch>) {
        guard let tracked, touches.contains(tracked) else { return }
        self.tracked = nil
        following = false
        if let webView = view as? WKWebView {
            NativeHoldCapture.shared.touchEnded(in: webView, timestamp: tracked.timestamp)
        }
        // Never a gesture of its own: the next touch starts it afresh.
        state = .failed
    }

    override func reset() {
        super.reset()
        // UIKit took the gesture back without ending the touch for us: so it
        // is on a surface whose page took the press for itself. The lift's
        // time is still the touch's own.
        // A reset is not always a lift: the capture is told of an end only
        // when the touch is gone or says it lifted. A finger still down is
        // asked again when its cue is due.
        if following, let webView = view as? WKWebView,
           let lifted = Self.state(of: tracked, in: webView).liftedAt {
            NativeHoldCapture.shared.touchEnded(in: webView, timestamp: lifted)
        }
        following = false
        tracked = nil
    }

    override func canPrevent(_ preventedGestureRecognizer: UIGestureRecognizer) -> Bool { false }

    override func canBePrevented(by preventingGestureRecognizer: UIGestureRecognizer) -> Bool { false }

    func gestureRecognizer(
        _ gestureRecognizer: UIGestureRecognizer,
        shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer
    ) -> Bool {
        true
    }
}
