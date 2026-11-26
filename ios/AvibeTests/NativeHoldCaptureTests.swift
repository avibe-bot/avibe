import AVFoundation
import WebKit
import XCTest
@testable import Avibe

/// The in-App hold's microphone (NativeHoldCapture) and its bridge methods.
@MainActor
final class NativeHoldCaptureTests: XCTestCase {
    private let instance = WebOrigin.parseConfigured("https://instance-one.example")!

    /// What waits for the input's queued audio, run when a test says so.
    private var queued: [() -> Void] = []

    override func tearDown() {
        VoiceCaptureAudioState.active = false
        queued = []
        inBackground = false
        timers = []
        waitingForTouches = []
        clock = 0
        haptics = []
        notes = []
        holdEvents = []
        heard = []
        taps = []
        startsAtTouchDown = false
        super.tearDown()
    }

    private var inBackground = false

    /// A timer the capture asked for, run when a test says so.
    private final class PendingTimer {
        let delay: TimeInterval
        let work: @MainActor () -> Void
        var cancelled = false
        init(delay: TimeInterval, work: @escaping @MainActor () -> Void) {
            self.delay = delay
            self.work = work
        }
    }

    private var timers: [PendingTimer] = []
    /// What waits for the App to have handled the touch events that reached
    /// it, run when a test says so.
    private var waitingForTouches: [PendingTimer] = []
    /// The capture's clock, in ms.
    private var clock = 0
    private var haptics: [String] = []
    private var notes: [String] = []
    private var holdEvents: [[String: Any]] = []
    /// What reached the page of a hold's take.
    private var heard: [NativeHoldCapture.Event] = []
    /// Touches that were not holds, as counted: how long, and whether moved.
    private var taps: [String] = []
    /// The same, whole: whether the microphone was started, and would have been.
    private var tapRecords: [NativeHoldCapture.Tap] = []
    /// The counts of touches that were not holds that a hold's timings carry.
    private var tapsTotal: String?
    private var tapsLost: String?
    /// The earliest an input starts when the page names no time: its timer.
    private let startDelay = TimeInterval(NativeHoldCapture.defaultStartAfterMs) / 1000
    /// The App's default is to start at touch-down; these tests say which.
    private var startsAtTouchDown = false

    private func makeCapture(_ audio: FakeAudio) -> NativeHoldCapture {
        NativeHoldCapture(
            audio: audio,
            afterQueued: { [unowned self] in queued.append($0) },
            isInBackground: { [unowned self] in inBackground },
            after: { [unowned self] delay, work in
                let timer = PendingTimer(delay: delay, work: work)
                timers.append(timer)
                return { timer.cancelled = true }
            },
            onceTouchesAreHeard: { [unowned self] work in
                let waiting = PendingTimer(delay: 0, work: work)
                waitingForTouches.append(waiting)
                return { waiting.cancelled = true }
            },
            uptimeMs: { [unowned self] in clock },
            haptic: NativeHoldHaptic(
                prepare: { [unowned self] in haptics.append("prepare") },
                fire: { [unowned self] in haptics.append("fire") }
            ),
            startsAtTouchDown: { [unowned self] in startsAtTouchDown },
            noteTimings: { [unowned self] in notes.append($0) },
            noteTap: { [unowned self] tap in
                taps.append("\(tap.ms)\(tap.moved ? " moved" : "")")
                tapRecords.append(tap)
            },
            tapsTotal: { [unowned self] in tapsTotal },
            tapsLost: { [unowned self] in tapsLost },
            sendHoldEvent: { [unowned self] _, detail in holdEvents.append(detail) },
            sendCaptureEvent: { [unowned self] _, _, event in heard.append(event) }
        )
    }

    /// Runs the pending timer of `delay` seconds (the hold's 0.2 s, the
    /// adoption wait's 1 s); false when there is none.
    ///
    /// A hold's threshold only asks for the cue; it is given once the App has
    /// handled the touch events that reached it. `touchesHeard: false`
    /// leaves that to the test (`hearTouches()`): a lift is still on its way.
    @discardableResult
    private func fire(_ delay: TimeInterval, touchesHeard: Bool = true) -> Bool {
        guard let index = timers.firstIndex(where: { !$0.cancelled && $0.delay == delay }) else { return false }
        let timer = timers.remove(at: index)
        timer.work()
        if touchesHeard { hearTouches() }
        return true
    }

    /// The main thread has handled every touch event that reached the App.
    private func hearTouches() {
        let waiting = waitingForTouches
        waitingForTouches = []
        for wait in waiting where !wait.cancelled { wait.work() }
    }

    private static let composer = NativeHoldCapture.Surface(
        id: "composer", rect: CGRect(x: 0, y: 700, width: 390, height: 60)
    )
    private static let onComposer = CGPoint(x: 100, y: 720)

    /// A capture whose page registered its composer as a hold surface.
    private func makeHoldCapture(_ audio: FakeAudio) -> (NativeHoldCapture, WKWebView) {
        let capture = makeCapture(audio)
        let webView = WKWebView(frame: .zero)
        capture.watch(webView)
        XCTAssertTrue(capture.setSurfaces([Self.composer], for: webView))
        return (capture, webView)
    }

    private func assertGone(
        _ result: Result<[String: Any], NativeHoldCapture.StartError>,
        file: StaticString = #filePath, line: UInt = #line
    ) {
        guard case .failure(.failed("gone")) = result else {
            return XCTFail("\(result)", file: file, line: line)
        }
    }

    /// A press on the composer held to recognition; the take's id. Where
    /// the input may start before recognition, it starts at its time.
    private func hold(_ capture: NativeHoldCapture, in webView: WKWebView) throws -> String {
        capture.touchDown(at: Self.onComposer, in: webView)
        startEarly()
        clock += 200
        XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds))
        return try XCTUnwrap(holdEvents.last?["take"] as? String)
    }

    /// The earliest moment for the input to start has come, and the App has
    /// handled the touches that reached it: the input starts if the finger
    /// is down. False when no start was waiting for that moment.
    @discardableResult
    private func startEarly(touchesHeard: Bool = true) -> Bool {
        fire(startDelay, touchesHeard: touchesHeard)
    }

    private func drain() {
        let work = queued
        queued = []
        work.forEach { $0() }
    }

    func testTheShellAdvertisesVoiceCaptureAndItsThreeMethods() {
        XCTAssertEqual(NativeShellMetadata.capabilities["voiceCapture"], 1)
        XCTAssertEqual(
            NativeBridgeMethod.allCases.map(\.rawValue).filter { $0.hasPrefix("voice.capture") },
            ["voice.capturePrepare", "voice.captureStart", "voice.captureStop"]
        )
    }

    func testPrepareSetsTheSessionUpWithoutStartingAnything() {
        let audio = FakeAudio()
        let capture = makeCapture(audio)
        capture.prepare()
        XCTAssertEqual(audio.calls, ["prepare"])
        XCTAssertNil(capture.currentTake)
    }

    func testATakeDeliversItsAudioAndEndsOnceWhenStopped() throws {
        let audio = FakeAudio()
        let capture = makeCapture(audio)
        var events: [NativeHoldCapture.Event] = []

        let timings = try capture.start(take: "take-1", webView: nil) { events.append($0) }.get()
        XCTAssertEqual(timings, .init(activateMs: 12, engineMs: 34, categoryMs: 7))
        // The first audio goes at once: the page learns when the microphone began.
        audio.onPCM?(Data([1, 0, 2, 0]))
        XCTAssertEqual(events, [.pcm(Data([1, 0, 2, 0]))])
        audio.onPCM?(Data([3, 0]))
        capture.stop(take: "other")
        capture.stop(take: "take-1")
        XCTAssertEqual(audio.calls, ["start", "stop"])
        XCTAssertNil(capture.currentTake)
        // What the input had sent before it stopped still arrives, and is the take's.
        audio.onPCM?(Data([4, 0]))
        capture.stop(take: "take-1")
        drain()
        audio.onPCM?(Data([5, 0]))
        drain()

        XCTAssertEqual(events, [.pcm(Data([1, 0, 2, 0])), .pcm(Data([3, 0, 4, 0])), .ended(reason: nil)])
        XCTAssertEqual(audio.calls, ["start", "stop"])
    }

    func testAfterItsFirstAudioATakeCarriesAbout100msAnEvent() throws {
        let audio = FakeAudio()
        let capture = makeCapture(audio)
        var events: [NativeHoldCapture.Event] = []
        _ = try capture.start(take: "take-1", webView: nil) { events.append($0) }.get()

        audio.onPCM?(Data(count: 640))
        for _ in 0..<4 { audio.onPCM?(Data(count: 640)) }
        XCTAssertEqual(events.count, 1)
        audio.onPCM?(Data(count: 640))
        XCTAssertEqual(events, [.pcm(Data(count: 640)), .pcm(Data(count: 3_200))])
    }

    func testTheSystemEndingTheInputEndsTheTakeWithItsReason() throws {
        let audio = FakeAudio()
        let capture = makeCapture(audio)
        var events: [NativeHoldCapture.Event] = []
        _ = try capture.start(take: "take-1", webView: nil) { events.append($0) }.get()

        audio.onEnd?("interrupted")
        audio.onEnd?("interrupted")
        drain()

        XCTAssertEqual(events, [.ended(reason: "interrupted")])
        XCTAssertEqual(audio.calls, ["start", "stop"])
    }

    func testGoingToTheBackgroundEndsTheTake() throws {
        let audio = FakeAudio()
        let capture = makeCapture(audio)
        var events: [NativeHoldCapture.Event] = []
        _ = try capture.start(take: "take-1", webView: nil) { events.append($0) }.get()

        NotificationCenter.default.post(name: UIApplication.didEnterBackgroundNotification, object: nil)
        drain()

        XCTAssertEqual(events, [.ended(reason: "background")])
        XCTAssertEqual(audio.calls, ["start", "stop"])
    }

    func testATakeEndsWhenItsPageGoesOrTheActionButtonTakesTheMicrophone() throws {
        let audio = FakeAudio()
        let capture = makeCapture(audio)
        let page = WKWebView()
        var events: [NativeHoldCapture.Event] = []
        _ = try capture.start(take: "one", webView: page) { events.append($0) }.get()
        capture.stop(webView: WKWebView())
        XCTAssertEqual(capture.currentTake, "one", "another page's going is not this take's")
        capture.stop(webView: page)
        drain()
        XCTAssertEqual(events, [.ended(reason: "gone")])

        events = []
        _ = try capture.start(take: "two", webView: nil) { events.append($0) }.get()
        capture.yieldToActionButton()
        drain()
        XCTAssertEqual(events, [.ended(reason: "action_button")])
        XCTAssertEqual(audio.calls, ["start", "stop", "start", "stop"])
    }

    func testAudioForAPageThatWasDeallocatedEndsTheTake() throws {
        let audio = FakeAudio()
        let capture = makeCapture(audio)
        weak var released: WKWebView?
        try autoreleasepool {
            let page = WKWebView()
            released = page
            _ = try capture.start(take: "one", webView: page).get()
        }
        guard released == nil else { throw XCTSkip("WebKit kept the WebView alive") }
        audio.onPCM?(Data([1, 0]))
        XCTAssertNil(capture.currentTake)
        XCTAssertEqual(audio.calls, ["start", "stop"])
    }

    func testAHiddenAppStartsNoMicrophone() {
        let audio = FakeAudio()
        let capture = makeCapture(audio)
        inBackground = true
        XCTAssertEqual(capture.start(take: "one", webView: nil) { _ in }, .failure(.failed("background")))
        XCTAssertEqual(audio.calls, [])
    }

    func testNoTakeStartsWithoutThePermissionOrWhileTheActionButtonRecords() {
        let audio = FakeAudio()
        let capture = makeCapture(audio)
        audio.permissionGranted = false
        XCTAssertEqual(capture.start(take: "a", webView: nil) { _ in }, .failure(.permission))

        audio.permissionGranted = true
        VoiceCaptureAudioState.active = true
        XCTAssertEqual(capture.start(take: "a", webView: nil) { _ in }, .failure(.busy))
        capture.prepare()
        XCTAssertEqual(audio.calls, [], "nothing touches a session an Action Button take holds")
    }

    func testASecondPageCannotTakeTheMicrophoneButTheSamePageReplacesItsTake() throws {
        let audio = FakeAudio()
        let capture = makeCapture(audio)
        let page = WKWebView()
        var first: [NativeHoldCapture.Event] = []
        _ = try capture.start(take: "one", webView: page) { first.append($0) }.get()

        XCTAssertEqual(capture.start(take: "two", webView: WKWebView()) { _ in }, .failure(.busy))
        XCTAssertEqual(capture.start(take: "two", webView: nil) { _ in }, .failure(.busy))
        _ = try capture.start(take: "two", webView: page) { _ in }.get()
        drain()

        XCTAssertEqual(first, [.ended(reason: "replaced")])
        XCTAssertEqual(capture.currentTake, "two")
        XCTAssertEqual(audio.calls, ["start", "stop", "start"])
    }

    func testAFailedStartLeavesNoTake() {
        let audio = FakeAudio()
        audio.failure = NativeHoldCapture.StartError.failed("activate:561017449")
        let capture = makeCapture(audio)
        XCTAssertEqual(
            capture.start(take: "a", webView: nil) { _ in },
            .failure(.failed("activate:561017449"))
        )
        XCTAssertNil(capture.currentTake)
    }

    func testTakeIdentifiersAreShortAndPlain() {
        XCTAssertEqual(NativeHoldCapture.isValidTake("d-1_A"), "d-1_A")
        XCTAssertNil(NativeHoldCapture.isValidTake(""))
        XCTAssertNil(NativeHoldCapture.isValidTake(String(repeating: "a", count: 65)))
        XCTAssertNil(NativeHoldCapture.isValidTake("a\"); alert(1); //"))
        XCTAssertNil(NativeHoldCapture.isValidTake(7))
    }

    func testTheEventCarriesTheTakeAndItsAudioAsBase64() {
        XCTAssertEqual(
            NativeHoldCapture.eventScript(take: "t", event: .pcm(Data([1, 0, 255, 127]))),
            #"window.dispatchEvent(new CustomEvent("avibe:native:voice.capture", { detail: {"pcm":"AQD\/fw==","take":"t"} }));"#
        )
        XCTAssertEqual(
            NativeHoldCapture.eventScript(take: "t", event: .ended(reason: "route")),
            #"window.dispatchEvent(new CustomEvent("avibe:native:voice.capture", { detail: {"ended":true,"reason":"route","take":"t"} }));"#
        )
    }

    func testTheConverterTurnsTheInputInto16kHzMonoInt16AndLosesNone() throws {
        let input = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let converter = try XCTUnwrap(NativeHoldPCMConverter(input: input))
        var samples: [Int16] = []
        // Two seconds in 100 ms buffers, as the input's tap delivers them.
        for part in 0..<20 {
            let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: input, frameCapacity: 4_800))
            buffer.frameLength = 4_800
            for index in 0..<4_800 {
                let time = Double(part * 4_800 + index) / 48_000
                buffer.floatChannelData![0][index] = Float(0.5 * sin(2 * .pi * 440 * time))
            }
            let data = try XCTUnwrap(converter.convert(buffer))
            samples += data.withUnsafeBytes { Array($0.bindMemory(to: Int16.self)) }
            // The resampler holds a little back (about 15 ms, never more than
            // 50 ms) and gives it with the next buffer: nothing is lost.
            let behind = (part + 1) * 1_600 - samples.count
            XCTAssertGreaterThanOrEqual(behind, 0, "buffer \(part)")
            XCTAssertLessThan(behind, 800, "buffer \(part)")
        }
        let peak = samples.map { abs(Int($0)) }.max() ?? 0
        XCTAssertGreaterThan(peak, 12_000)
        XCTAssertLessThan(peak, 20_000)
    }

    // MARK: A hold the App recognises itself

    func testTheShellAdvertisesItsOwnHoldRecognition() {
        // 2: a tap is never taken for a hold. The page names surfaces from 2 on, so the one
        // build that announced 1 (TestFlight 50.1) follows nothing. 3: a surface may name when
        // its input starts (`startAfterMs`) and whether anything is prepared before (`prepare`).
        XCTAssertEqual(NativeShellMetadata.capabilities["voiceHold"], 3)
        XCTAssertEqual(
            NativeBridgeMethod.allCases.map(\.rawValue).filter { $0.hasPrefix("voice.hold") },
            ["voice.holdSurfaces", "voice.holdAdopt", "voice.holdReject"]
        )
    }

    func testATapPreparesTheMicrophoneWithoutStartingItAndTakesItDown() {
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)

        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertEqual(audio.calls, ["arm"])
        XCTAssertEqual(haptics, ["prepare"])
        XCTAssertTrue(capture.followsPress)

        clock = 120
        capture.touchEnded(in: webView)
        XCTAssertEqual(audio.calls, ["arm", "disarm"])
        XCTAssertFalse(capture.followsPress)
        XCTAssertNil(capture.currentTake)
        // Not a hold: no cue, nothing for the page, and its timer is spent.
        XCTAssertFalse(fire(NativeHoldCapture.defaultHoldSeconds))
        XCTAssertEqual(haptics, ["prepare"])
        XCTAssertTrue(holdEvents.isEmpty)
        XCTAssertTrue(heard.isEmpty)
        // Counted by how long it lasted, with no line of its own.
        XCTAssertEqual(taps, ["120"])
        XCTAssertTrue(notes.isEmpty)
    }

    /// The cue (the haptic here, the preview from the page's event) comes
    /// when the hold is recognised, while the microphone has answered
    /// neither its preparing nor its start.
    func testTheCueComesAtRecognitionWhateverTheMicrophoneIsDoing() throws {
        let audio = FakeAudio()
        audio.holdsAnswers = true
        let (capture, webView) = makeHoldCapture(audio)

        capture.touchDown(at: Self.onComposer, in: webView)
        clock = 200
        XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds))

        XCTAssertEqual(haptics, ["prepare", "fire"])
        XCTAssertEqual(holdEvents.count, 1)
        XCTAssertEqual(holdEvents.first?["phase"] as? String, "recognised")
        XCTAssertEqual(holdEvents.first?["surface"] as? String, "composer")
        let take = try XCTUnwrap(holdEvents.first?["take"] as? String)
        XCTAssertNotNil(NativeHoldCapture.isValidTake(take))
        XCTAssertEqual(capture.currentTake, take)
        // Asked to start, with no answer yet from either step.
        XCTAssertEqual(audio.calls, ["arm", "begin"])
        XCTAssertEqual(audio.pendingAnswers, 2)

        audio.answer()
        XCTAssertEqual(haptics, ["prepare", "fire"])
        XCTAssertEqual(holdEvents.count, 1)
    }

    func testAHoldsAudioWaitsInMemoryUntilItsPageTakesIt() throws {
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)
        let take = try hold(capture, in: webView)
        XCTAssertEqual(audio.calls, ["arm", "begin"])

        clock = 240
        audio.onPCM?(Data([1, 0]))
        audio.onPCM?(Data([2, 0]))
        XCTAssertTrue(heard.isEmpty)

        clock = 250
        let timings = try capture.adopt(take: take, webView: webView).get()
        // Everything since the input started, first and in one event.
        XCTAssertEqual(heard, [.pcm(Data([1, 0, 2, 0]))])
        XCTAssertEqual(timings["variant"] as? String, "quiet")
        XCTAssertEqual(timings["recognisedAt"] as? Int, 200)
        XCTAssertEqual(timings["hapticAt"] as? Int, 200)
        XCTAssertEqual(timings["liveAt"] as? Int, 200)
        XCTAssertEqual(timings["cueToLive"] as? Int, 0)
        XCTAssertEqual(timings["firstBufferAt"] as? Int, 240)
        XCTAssertEqual(timings["firstBufferBytes"] as? Int, 2)
        XCTAssertEqual(timings["adoptedAt"] as? Int, 250)
        XCTAssertEqual(timings["activateMs"] as? Int, 12)
        XCTAssertEqual(timings["startMs"] as? Int, 5)
        // Touch-down to the first captured audio: measured for the variant
        // that ran, and what the other would have given (7 + 12 + 3 + 5).
        XCTAssertEqual(timings["liveQuiet"] as? Int, 200)
        XCTAssertEqual(timings["liveEarly"] as? Int, 27)
        XCTAssertEqual(timings["holdMs"] as? Int, 200)
        // The input is told, so its bound on an untaken take is lifted.
        XCTAssertEqual(audio.takens, 1)
        // Taken once.
        assertGone(capture.adopt(take: take, webView: webView))
        XCTAssertEqual(audio.takens, 1)
        // No longer waited for.
        XCTAssertFalse(fire(NativeHoldCapture.adoptionWait))

        audio.onPCM?(Data([3, 0]))
        clock = 900
        capture.touchEnded(in: webView)
        capture.stop(take: take)
        drain()
        XCTAssertEqual(heard, [.pcm(Data([1, 0, 2, 0])), .pcm(Data([3, 0])), .ended(reason: nil)])
        XCTAssertEqual(audio.calls, ["arm", "begin", "stop"])
        XCTAssertFalse(capture.followsPress)
        XCTAssertEqual(holdEvents.last?["phase"] as? String, "timings")
        XCTAssertEqual((holdEvents.last?["timings"] as? [String: Any])?["releasedAt"] as? Int, 900)
        XCTAssertTrue(try XCTUnwrap(notes.last).contains(" rec=200 hap=200 start=5 live=200 gap=0 e=27 q=200 "), notes.last ?? "")
        XCTAssertTrue(try XCTUnwrap(notes.last).hasSuffix("end=stopped"), notes.last ?? "")
    }

    /// A surface that moved under the finger: the App recognises a hold the
    /// page does not. The haptic was the cost; nothing recorded is kept or
    /// reaches the page.
    func testAHoldThePageRefusesIsDroppedWithNothingDelivered() throws {
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)
        let take = try hold(capture, in: webView)
        XCTAssertEqual(haptics, ["prepare", "fire"])
        audio.onPCM?(Data([1, 0]))

        capture.reject(take: "other", webView: webView)
        XCTAssertEqual(capture.currentTake, take)
        capture.reject(take: take, webView: webView)
        XCTAssertNil(capture.currentTake)
        XCTAssertFalse(capture.followsPress)
        XCTAssertEqual(audio.calls, ["arm", "begin", "stop"])
        audio.onPCM?(Data([2, 0]))
        drain()
        XCTAssertTrue(heard.isEmpty)
        XCTAssertEqual(holdEvents.count, 1)
        XCTAssertTrue(try XCTUnwrap(notes.last).hasSuffix("end=rejected"), notes.last ?? "")
        assertGone(capture.adopt(take: take, webView: webView))
    }

    func testAHoldNoPageTakesIsDroppedAfterItsWaitOrItsLimit() throws {
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)
        _ = try hold(capture, in: webView)
        audio.onPCM?(Data([1, 0]))
        // The microphone stays open no longer than a second for a page
        // that does not answer, and the kept audio is bounded with it.
        XCTAssertEqual(NativeHoldCapture.adoptionWait, 1)
        XCTAssertEqual(NativeHoldCapture.unadoptedBytes, 64_000)
        XCTAssertTrue(fire(NativeHoldCapture.adoptionWait))
        drain()
        XCTAssertNil(capture.currentTake)
        XCTAssertEqual(audio.calls, ["arm", "begin", "stop"])
        XCTAssertTrue(heard.isEmpty)
        XCTAssertTrue(try XCTUnwrap(notes.last).hasSuffix("end=unadopted"), notes.last ?? "")

        // More audio than is kept for a page that has not taken it.
        let (second, secondWebView) = makeHoldCapture(audio)
        _ = try hold(second, in: secondWebView)
        audio.onPCM?(Data(count: NativeHoldCapture.unadoptedBytes))
        XCTAssertNotNil(second.currentTake)
        audio.onPCM?(Data([1, 0]))
        drain()
        XCTAssertNil(second.currentTake)
        XCTAssertTrue(heard.isEmpty)
        XCTAssertEqual(audio.takens, 0)
    }

    /// The input reached its bound by itself (the main thread was stalled,
    /// so nothing here could count): the take ends unheard, and the page
    /// can no longer take it.
    func testATakeItsInputEndedAtTheBoundIsDroppedUnheard() throws {
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)
        let take = try hold(capture, in: webView)
        audio.onPCM?(Data([1, 0]))
        audio.onEnd?("unadopted")
        drain()
        XCTAssertNil(capture.currentTake)
        XCTAssertFalse(capture.followsPress)
        XCTAssertEqual(audio.calls, ["arm", "begin", "stop"])
        XCTAssertTrue(heard.isEmpty)
        XCTAssertEqual(audio.takens, 0)
        XCTAssertTrue(try XCTUnwrap(notes.last).hasSuffix("end=unadopted"), notes.last ?? "")
        assertGone(capture.adopt(take: take, webView: webView))
        XCTAssertTrue(heard.isEmpty)
    }

    // MARK: The bound, where the audio arrives

    /// What the input passes on before the page has taken the take: never
    /// more than the bound, then nothing, and one call to stop the input.
    func testTheInputPassesOnNoMoreThanItsBoundUntilThePageTakesTheTake() {
        let sink = NativeHoldSink()
        var passed = 0
        var overflows = 0
        sink.open(limit: 10, onPCM: { passed += $0.count }, overflow: { overflows += 1 })
        sink.send(Data(count: 6))
        sink.send(Data(count: 4))
        XCTAssertEqual(passed, 10)
        XCTAssertEqual(overflows, 0)
        // The buffer that would pass the bound is dropped whole.
        sink.send(Data(count: 1))
        XCTAssertEqual(passed, 10)
        XCTAssertEqual(overflows, 1)
        sink.send(Data(count: 6))
        sink.unlimit()
        sink.send(Data(count: 6))
        XCTAssertEqual(passed, 10)
        XCTAssertEqual(overflows, 1)
    }

    func testATakenTakesAudioIsNotBounded() {
        let sink = NativeHoldSink()
        var passed = 0
        var overflows = 0
        sink.open(limit: 10, onPCM: { passed += $0.count }, overflow: { overflows += 1 })
        sink.send(Data(count: 10))
        sink.unlimit()
        sink.send(Data(count: 1_000))
        XCTAssertEqual(passed, 1_010)
        XCTAssertEqual(overflows, 0)
        sink.close()
        sink.send(Data(count: 4))
        XCTAssertEqual(passed, 1_010)
        XCTAssertEqual(overflows, 0)
    }

    /// The invariant itself: ten seconds of audio arrive on the input's own
    /// thread while the main thread does nothing (it waits here, as it
    /// would in a stall). No more than the App's bound of 2 s was passed on,
    /// and the input was told to stop once.
    func testTheBoundHoldsWhileTheMainThreadIsStalled() {
        let sink = NativeHoldSink()
        let counted = NSLock()
        var passed = 0
        var overflows = 0
        sink.open(
            limit: NativeHoldCapture.unadoptedBytes,
            onPCM: { data in
                counted.lock()
                passed += data.count
                counted.unlock()
            },
            overflow: {
                counted.lock()
                overflows += 1
                counted.unlock()
            }
        )
        let tenSeconds = 10 * 16_000 * 2
        DispatchQueue(label: "bot.avibe.ios.tests.hold-input").sync {
            for _ in 0..<(tenSeconds / nativeHoldBatchBytes) {
                sink.send(Data(count: nativeHoldBatchBytes))
            }
        }
        XCTAssertEqual(NativeHoldCapture.unadoptedBytes, 64_000)
        XCTAssertEqual(passed, 64_000)
        XCTAssertEqual(overflows, 1)
    }

    /// Activating would pause the music, or switch a headset to its call
    /// mode: nothing is prepared at touch-down. The cue still comes at
    /// recognition, and the input starts then.
    func testOtherAudioOrABluetoothRoutePreparesNothingAtTouchDown() throws {
        for (other, bluetooth, why) in [(true, false, "other"), (false, true, "bluetooth")] {
            let audio = FakeAudio()
            audio.otherAudioPlaying = other
            audio.routeIsBluetooth = bluetooth
            // The start's answer comes when the test gives it.
            audio.holdsAnswers = true
            let (capture, webView) = makeHoldCapture(audio)
            haptics = []
            holdEvents = []
            clock = 0

            capture.touchDown(at: Self.onComposer, in: webView)
            XCTAssertEqual(audio.calls, [], why)
            capture.touchEnded(in: webView)
            XCTAssertEqual(audio.calls, [], why)

            let take = try hold(capture, in: webView)
            XCTAssertEqual(haptics, ["prepare", "prepare", "fire"], why)
            XCTAssertEqual(audio.calls, ["begin"], why)
            clock += 150
            audio.answer()
            let timings = try capture.adopt(take: take, webView: webView).get()
            XCTAssertEqual(timings["variant"] as? String, "late", why)
            XCTAssertEqual(timings["why"] as? String, why)
            // The cue came before the microphone was live, by this much.
            XCTAssertEqual(timings["cueToLive"] as? Int, 150, why)
            XCTAssertEqual(timings["liveEarly"] as? Int, 27, why)
            XCTAssertEqual(timings["liveQuiet"] as? Int, 205, why)
            capture.stop(take: take)
            drain()
            XCTAssertTrue(try XCTUnwrap(notes.last).hasPrefix("hold late:\(why) Speaker "), notes.last ?? "")
        }
    }

    /// The privacy sentence: a touch that ends as a tap or a scroll opens
    /// the microphone (the input starts at touch-down), and its audio never
    /// leaves the App's memory. Nothing of it reaches the page, no event
    /// says it existed, and what is kept of the touch is one count.
    func testAudioOfATouchThatIsNotAHoldNeverLeavesMemory() {
        startsAtTouchDown = true
        for scroll in [false, true] {
            let audio = FakeAudio()
            let (capture, webView) = makeHoldCapture(audio)
            heard = []
            holdEvents = []
            notes = []
            taps = []
            tapRecords = []
            clock = 0

            capture.touchDown(at: Self.onComposer, in: webView)
            XCTAssertEqual(audio.calls, ["arm"])
            // Its time has come with the finger still down: the input starts.
            XCTAssertTrue(startEarly())
            XCTAssertEqual(audio.calls, ["arm", "begin"])
            audio.onPCM?(Data([1, 0, 2, 0]))
            clock = 150
            if scroll {
                capture.touchMoved(to: CGPoint(x: 100, y: 700), in: webView)
            } else {
                capture.touchEnded(in: webView)
            }
            // The input is stopped at once, and the take is gone.
            XCTAssertEqual(audio.calls, ["arm", "begin", "stop"], "\(scroll)")
            XCTAssertNil(capture.currentTake)
            XCTAssertFalse(capture.followsPress)
            // What the input had still on its way is dropped too.
            audio.onPCM?(Data([3, 0]))
            drain()
            XCTAssertTrue(heard.isEmpty, "\(scroll)")
            XCTAssertTrue(holdEvents.isEmpty, "\(scroll)")
            XCTAssertEqual(haptics.filter { $0 != "prepare" }, [], "\(scroll)")
            // Kept of it: how long it lasted, as a count.
            XCTAssertTrue(notes.isEmpty, "\(scroll)")
            XCTAssertEqual(taps, [scroll ? "150 moved" : "150"])
            XCTAssertEqual(tapRecords.map(\.opened), [true])
            // And no page can take it afterwards.
            XCTAssertFalse(fire(NativeHoldCapture.defaultHoldSeconds))
            capture.stop(take: "anything")
            drain()
            XCTAssertTrue(heard.isEmpty, "\(scroll)")
        }
    }

    /// A surface the page marked "at recognition" opens no microphone on a
    /// touch, though the App's default is to start at touch-down: a tap
    /// there only prepares the input, and a hold starts it with the cue.
    /// A surface without the mark, in the same list, starts at touch-down.
    func testASurfaceMarkedAtRecognitionStartsItsInputOnlyWithTheCue() throws {
        startsAtTouchDown = true
        let button = NativeHoldCapture.Surface(
            id: "main", rect: CGRect(x: 150, y: 780, width: 90, height: 50), startsAtRecognition: true
        )
        let onButton = CGPoint(x: 190, y: 800)
        let audio = FakeAudio()
        let capture = makeCapture(audio)
        let webView = WKWebView(frame: .zero)
        capture.watch(webView)
        XCTAssertTrue(capture.setSurfaces([Self.composer, button], for: webView))

        // A tap on the marked surface: prepared, never started.
        capture.touchDown(at: onButton, in: webView)
        XCTAssertEqual(audio.calls, ["arm"])
        clock = 80
        capture.touchEnded(in: webView)
        XCTAssertEqual(audio.calls, ["arm", "disarm"])
        XCTAssertFalse(capture.followsPress)
        XCTAssertNil(audio.onPCM)

        // A hold on it: the input starts when the hold is recognised.
        audio.calls = []
        clock = 1_000
        capture.touchDown(at: onButton, in: webView)
        XCTAssertEqual(audio.calls, ["arm"])
        clock = 1_200
        XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds))
        XCTAssertEqual(audio.calls, ["arm", "begin"])
        XCTAssertEqual(holdEvents.last?["phase"] as? String, "recognised")
        XCTAssertEqual(holdEvents.last?["surface"] as? String, "main")
        let take = try XCTUnwrap(holdEvents.last?["take"] as? String)
        let timings = try capture.adopt(take: take, webView: webView).get()
        XCTAssertEqual(timings["variant"] as? String, "quiet")
        capture.touchEnded(in: webView)
        capture.stop(take: take)
        drain()
        XCTAssertNil(capture.currentTake)

        // The unmarked surface beside it keeps the App's default.
        audio.calls = []
        clock = 3_000
        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertTrue(startEarly())
        XCTAssertEqual(audio.calls, ["arm", "begin"])
        clock = 3_200
        XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds))
        let second = try XCTUnwrap(holdEvents.last?["take"] as? String)
        XCTAssertEqual(try capture.adopt(take: second, webView: webView).get()["variant"] as? String, "early")
    }

    // MARK: When the input may start

    /// The invariant: **a lift before the input starts never opens the
    /// microphone.** The input is prepared at touch-down and starts only
    /// once it is prepared, its time has come, and the finger, asked itself
    /// after the App has handled the touches that reached it, is still down
    /// and where it came down. A tap that lifted first, told to the App or
    /// not, ends as a tap with nothing started.
    func testALiftBeforeTheStartNeverOpensTheMicrophone() {
        startsAtTouchDown = true
        for told in [true, false] {
            let audio = FakeAudio()
            let (capture, webView) = makeHoldCapture(audio)
            heard = []
            holdEvents = []
            haptics = []
            taps = []
            tapRecords = []
            clock = 0
            var lifted: TimeInterval?
            capture.touchDown(
                at: Self.onComposer, in: webView, timestamp: 50,
                touch: { .init(liftedAt: lifted, point: Self.onComposer) }
            )
            XCTAssertEqual(audio.calls, ["arm"], "\(told)")
            // A 30 ms tap.
            lifted = 50.03
            clock = 30
            if told { capture.touchEnded(in: webView, timestamp: 50.03) }
            // Its time comes; nothing is started.
            clock = 50
            XCTAssertEqual(startEarly(), !told, "\(told)")
            XCTAssertEqual(audio.calls, ["arm", "disarm"], "\(told)")
            XCTAssertFalse(capture.followsPress, "\(told)")
            XCTAssertNil(capture.currentTake, "\(told)")
            XCTAssertNil(audio.onPCM, "\(told)")
            XCTAssertFalse(fire(NativeHoldCapture.defaultHoldSeconds), "\(told)")
            XCTAssertEqual(haptics, ["prepare"], "\(told)")
            XCTAssertTrue(holdEvents.isEmpty, "\(told)")
            XCTAssertEqual(taps, ["30"], "\(told)")
            XCTAssertEqual(tapRecords.map(\.opened), [false], "\(told)")
        }
    }

    /// A finger that has moved away when the input could start: a drag,
    /// and nothing is started.
    func testAFingerThatHasMovedAwayByTheStartOpensNothing() {
        startsAtTouchDown = true
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)
        var point = Self.onComposer
        capture.touchDown(at: Self.onComposer, in: webView, timestamp: 50, touch: { .init(liftedAt: nil, point: point) })
        point = CGPoint(x: Self.onComposer.x, y: Self.onComposer.y - 30)
        clock = 50
        XCTAssertTrue(startEarly())
        XCTAssertEqual(audio.calls, ["arm", "disarm"])
        XCTAssertFalse(capture.followsPress)
        XCTAssertEqual(taps, ["50 moved"])
        XCTAssertEqual(tapRecords.map(\.opened), [false])
    }

    /// The start comes at the later of "prepared" and the surface's time:
    /// preparing quicker than the time waits for it, preparing slower is
    /// waited for. The cue is not moved by either.
    func testTheInputStartsAtTheLaterOfPreparedAndItsTime() throws {
        startsAtTouchDown = true
        // Preparing answers at 30 ms; the time is 80 ms.
        let audio = FakeAudio()
        audio.holdsAnswers = true
        let capture = makeCapture(audio)
        let webView = WKWebView(frame: .zero)
        capture.watch(webView)
        let surface = NativeHoldCapture.Surface(id: "composer", rect: Self.composer.rect, startAfterMs: 80)
        XCTAssertTrue(capture.setSurfaces([surface], for: webView))
        capture.touchDown(at: Self.onComposer, in: webView)
        clock = 30
        audio.answer()
        XCTAssertEqual(audio.calls, ["arm"])
        clock = 80
        XCTAssertTrue(fire(0.08))
        XCTAssertEqual(audio.calls, ["arm", "begin"])
        audio.answer()
        clock = 200
        XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds))
        var take = try XCTUnwrap(holdEvents.last?["take"] as? String)
        var timings = try capture.adopt(take: take, webView: webView).get()
        XCTAssertEqual(timings["startAfterMs"] as? Int, 80)
        XCTAssertEqual(timings["armedAt"] as? Int, 30)
        XCTAssertEqual(timings["beginAt"] as? Int, 80)
        XCTAssertEqual(timings["liveAt"] as? Int, 80)
        XCTAssertEqual(timings["recognisedAt"] as? Int, 200)
        capture.touchEnded(in: webView)
        capture.stop(take: take)
        drain()

        // Preparing answers at 120 ms, after the time: the start is then.
        audio.calls = []
        clock = 1_000
        capture.touchDown(at: Self.onComposer, in: webView)
        clock = 1_080
        XCTAssertTrue(fire(0.08))
        XCTAssertEqual(audio.calls, ["arm"])
        clock = 1_120
        audio.answer()
        XCTAssertEqual(audio.calls, ["arm"])
        hearTouches()
        XCTAssertEqual(audio.calls, ["arm", "begin"])
        audio.answer()
        clock = 1_200
        XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds))
        take = try XCTUnwrap(holdEvents.last?["take"] as? String)
        timings = try capture.adopt(take: take, webView: webView).get()
        XCTAssertEqual(timings["armedAt"] as? Int, 120)
        XCTAssertEqual(timings["beginAt"] as? Int, 120)
        capture.touchEnded(in: webView)
        capture.stop(take: take)
        drain()
    }

    /// The App's default when the page names no time: 50 ms. A surface may
    /// name 0 (the start as soon as prepared, the finger still asked), and
    /// a surface at recognition starts with the cue.
    func testTheSurfacesTimeOrTheAppsDefault() throws {
        startsAtTouchDown = true
        XCTAssertEqual(NativeHoldCapture.defaultStartAfterMs, 50)
        let audio = FakeAudio()
        let capture = makeCapture(audio)
        let webView = WKWebView(frame: .zero)
        capture.watch(webView)

        XCTAssertTrue(capture.setSurfaces([Self.composer], for: webView))
        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertTrue(timers.contains { !$0.cancelled && $0.delay == 0.05 })
        capture.touchEnded(in: webView)

        let now = NativeHoldCapture.Surface(id: "composer", rect: Self.composer.rect, startAfterMs: 0)
        XCTAssertTrue(capture.setSurfaces([now], for: webView))
        audio.calls = []
        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertTrue(fire(0))
        XCTAssertEqual(audio.calls, ["arm", "begin"])
        clock += 200
        XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds))
        var take = try XCTUnwrap(holdEvents.last?["take"] as? String)
        XCTAssertEqual(try capture.adopt(take: take, webView: webView).get()["startAfterMs"] as? Int, 0)
        capture.touchEnded(in: webView)
        capture.stop(take: take)
        drain()

        // A time past the threshold is the threshold: the start is the cue's.
        let late = NativeHoldCapture.Surface(id: "composer", rect: Self.composer.rect, startAfterMs: 400)
        XCTAssertTrue(capture.setSurfaces([late], for: webView))
        audio.calls = []
        timers = []
        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertEqual(timers.filter { !$0.cancelled }.map(\.delay), [NativeHoldCapture.defaultHoldSeconds])
        clock += 200
        XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds))
        take = try XCTUnwrap(holdEvents.last?["take"] as? String)
        let timings = try capture.adopt(take: take, webView: webView).get()
        XCTAssertEqual(timings["variant"] as? String, "quiet")
        XCTAssertEqual(timings["startAfterMs"] as? Int, 200)
        capture.touchEnded(in: webView)
        capture.stop(take: take)
        drain()
    }

    /// `prepare: "start"`: nothing is activated before the input starts.
    /// A tap then touches the session not at all.
    func testASurfaceThatPreparesAtTheStartActivatesNothingBeforeIt() throws {
        startsAtTouchDown = true
        let audio = FakeAudio()
        let capture = makeCapture(audio)
        let webView = WKWebView(frame: .zero)
        capture.watch(webView)
        let surface = NativeHoldCapture.Surface(id: "composer", rect: Self.composer.rect, preparesAtStart: true)
        XCTAssertTrue(capture.setSurfaces([surface], for: webView))

        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertEqual(audio.calls, [])
        clock = 40
        capture.touchEnded(in: webView)
        XCTAssertFalse(startEarly())
        XCTAssertEqual(audio.calls, [])
        XCTAssertEqual(taps, ["40"])

        clock = 1_000
        capture.touchDown(at: Self.onComposer, in: webView)
        clock = 1_050
        XCTAssertTrue(startEarly())
        // The start prepares as it starts.
        XCTAssertEqual(audio.calls, ["begin"])
        clock = 1_200
        XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds))
        let take = try XCTUnwrap(holdEvents.last?["take"] as? String)
        let timings = try capture.adopt(take: take, webView: webView).get()
        XCTAssertEqual(timings["bare"] as? Bool, true)
        XCTAssertEqual(timings["beginAt"] as? Int, 50)
        XCTAssertEqual(timings["activateMs"] as? Int, 12)
        capture.touchEnded(in: webView)
        capture.stop(take: take)
        drain()
    }

    /// For each threshold judged, a tap says whether the input would have
    /// started with it: the finger still down once prepared and the
    /// threshold had passed. And whether it did start.
    func testATapSaysWhetherTheMicrophoneOpenedAndWouldHaveForEachThreshold() {
        startsAtTouchDown = true
        XCTAssertEqual(NativeHoldTaps.thresholds, [50, 80, 100, 120])
        // Prepared at 60 ms; a 90 ms tap, the input started at 60 ms.
        let audio = FakeAudio()
        audio.holdsAnswers = true
        let (capture, webView) = makeHoldCapture(audio)
        capture.touchDown(at: Self.onComposer, in: webView, timestamp: 10)
        clock = 50
        XCTAssertTrue(startEarly())
        clock = 60
        audio.answer()
        hearTouches()
        XCTAssertEqual(audio.calls, ["arm", "begin"])
        clock = 90
        capture.touchEnded(in: webView, timestamp: 10.09)
        var tap = tapRecords.last
        XCTAssertEqual(tap?.ms, 90)
        XCTAssertEqual(tap?.opened, true)
        XCTAssertEqual(tap?.wouldOpen, [50: true, 80: true, 100: false, 120: false])
        audio.answer()
        drain()

        // A 40 ms tap whose preparing had not answered: no threshold opens it.
        audio.calls = []
        clock = 1_000
        capture.touchDown(at: Self.onComposer, in: webView, timestamp: 20)
        clock = 1_040
        capture.touchEnded(in: webView, timestamp: 20.04)
        tap = tapRecords.last
        XCTAssertEqual(tap?.ms, 40)
        XCTAssertEqual(tap?.opened, false)
        XCTAssertEqual(tap?.wouldOpen, [50: false, 80: false, 100: false, 120: false])
        XCTAssertEqual(audio.calls, ["arm", "disarm"])
        audio.answer()

        // A 120 ms tap whose touch reached the App 100 ms late, prepared 10 ms
        // after that: by the App's clock the finger was down for 20 ms only,
        // so no threshold would have opened it.
        clock = 2_000
        capture.touchDown(at: Self.onComposer, in: webView, timestamp: 30, deliveredAfterMs: 100)
        clock = 2_010
        audio.answer()
        clock = 2_020
        capture.touchEnded(in: webView, timestamp: 30.12)
        tap = tapRecords.last
        XCTAssertEqual(tap?.ms, 120)
        XCTAssertEqual(tap?.wouldOpen, [50: false, 80: false, 100: false, 120: false])
    }

    /// The counts keep `open` and each threshold's `w…`, and come back from
    /// their line; a hold's timings carry the counts to its page.
    func testTheTapCountsKeepOpenAndWouldOpenAndAHoldCarriesThem() throws {
        var tap = NativeHoldCapture.Tap(ms: 90, moved: false, opened: true)
        tap.wouldOpen = [50: true, 80: true, 100: false, 120: false]
        var counts = NativeHoldTaps.adding(tap, to: [:])
        counts = NativeHoldTaps.adding(NativeHoldCapture.Tap(ms: 40, moved: false, wouldOpen: [50: false]), to: counts)
        counts = NativeHoldTaps.adding(NativeHoldCapture.Tap(ms: 300, moved: true), to: counts)
        let line = try XCTUnwrap(NativeHoldTaps.line(for: counts))
        XCTAssertEqual(line, "hold taps 25:1 75:1 moved=1 open=1 w50=1 w80=1")
        XCTAssertEqual(NativeHoldTaps.counts(in: line), counts)

        tapsTotal = "75:1 moved=1 open=1 w50=1"
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)
        let take = try hold(capture, in: webView)
        _ = try capture.adopt(take: take, webView: webView).get()
        capture.stop(take: take)
        drain()
        let timings = try XCTUnwrap(holdEvents.last { $0["phase"] as? String == "timings" }?["timings"] as? [String: Any])
        XCTAssertEqual(timings["taps"] as? String, "75:1 moved=1 open=1 w50=1")
    }

    /// How long the touch took to reach the App is kept with the hold.
    func testAHoldKeepsHowLongItsTouchTookToReachTheApp() throws {
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)
        capture.touchDown(at: Self.onComposer, in: webView, deliveredAfterMs: 18)
        clock = 200
        XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds))
        let take = try XCTUnwrap(holdEvents.last?["take"] as? String)
        XCTAssertEqual(try capture.adopt(take: take, webView: webView).get()["deliveryMs"] as? Int, 18)
        capture.stop(take: take)
        drain()
        XCTAssertTrue(try XCTUnwrap(notes.last).contains(" dly=18 "), notes.last ?? "")
    }

    /// Started before recognition too, the cue does not wait for the
    /// microphone: preparing that has not answered keeps the start waiting,
    /// and recognition starts it with the cue.
    func testTheCueComesAtRecognitionWhileAStartBeforeItHasNotAnswered() {
        startsAtTouchDown = true
        let audio = FakeAudio()
        audio.holdsAnswers = true
        let (capture, webView) = makeHoldCapture(audio)

        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertEqual(audio.calls, ["arm"])
        // Its time has come, but preparing has not answered: the start waits.
        clock = 50
        XCTAssertTrue(startEarly())
        XCTAssertEqual(audio.calls, ["arm"])
        clock = 200
        XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds))
        XCTAssertEqual(haptics, ["prepare", "fire"])
        XCTAssertEqual(holdEvents.first?["phase"] as? String, "recognised")
        XCTAssertEqual(audio.calls, ["arm", "begin"])
        XCTAssertEqual(audio.pendingAnswers, 2)
        // Preparing answers later: nothing more is started.
        audio.answer()
        XCTAssertEqual(audio.calls, ["arm", "begin"])
    }

    /// A recognised hold whose page never took it ends however it ends
    /// (its page stops it, the App leaves the front or goes to the
    /// background, the Action Button takes the microphone, the page starts
    /// a take of its own, the WebView goes): its audio is dropped.
    func testAnUntakenHoldsAudioIsDroppedHoweverItEnds() throws {
        let enders: [(String, (NativeHoldCapture, WKWebView, String) -> Void)] = [
            ("stopped", { capture, _, take in capture.stop(take: take) }),
            ("inactive", { capture, _, _ in capture.appWillResignActive() }),
            ("background", { _, _, _ in
                NotificationCenter.default.post(name: UIApplication.didEnterBackgroundNotification, object: nil)
            }),
            ("action_button", { capture, _, _ in capture.yieldToActionButton() }),
            ("replaced", { capture, webView, _ in _ = capture.start(take: "own", webView: webView) { _ in } }),
            ("gone", { capture, webView, _ in capture.stop(webView: webView) }),
        ]
        for early in [false, true] {
            for (name, end) in enders {
                startsAtTouchDown = early
                let audio = FakeAudio()
                let (capture, webView) = makeHoldCapture(audio)
                heard = []
                notes = []
                let take = try hold(capture, in: webView)
                audio.onPCM?(Data([1, 0]))

                end(capture, webView, take)
                audio.onPCM?(Data([2, 0]))
                drain()
                XCTAssertTrue(heard.isEmpty, "\(name) \(early)")
                XCTAssertFalse(capture.followsPress, "\(name) \(early)")
                XCTAssertTrue(
                    notes.contains { $0.hasSuffix("end=\(name)") }, "\(name) \(early): \(notes)"
                )
                assertGone(capture.adopt(take: take, webView: webView))
                capture.stop(webView: webView)
                drain()
            }
        }
    }

    /// A press that is only pressing ends when the App leaves the front
    /// (App Lock) or goes to the background; a take its page has taken is
    /// the page's to end, as before.
    func testLeavingTheFrontEndsAPressButNotATakeItsPageHas() throws {
        for early in [false, true] {
            startsAtTouchDown = early
            let audio = FakeAudio()
            let (capture, webView) = makeHoldCapture(audio)
            capture.touchDown(at: Self.onComposer, in: webView)
            XCTAssertEqual(startEarly(), early)
            capture.appWillResignActive()
            XCTAssertFalse(capture.followsPress, "\(early)")
            XCTAssertNil(capture.currentTake)
            XCTAssertEqual(audio.calls, early ? ["arm", "begin", "stop"] : ["arm", "disarm"])
            XCTAssertFalse(fire(NativeHoldCapture.defaultHoldSeconds))

            heard = []
            let take = try hold(capture, in: webView)
            _ = try capture.adopt(take: take, webView: webView).get()
            capture.appWillResignActive()
            XCTAssertEqual(capture.currentTake, take, "\(early)")
            capture.stop(take: take)
            drain()
            XCTAssertEqual(heard, [.ended(reason: nil)], "\(early)")
        }
    }

    /// The session has one owner at a time: before the Action Button takes
    /// it, and before a page's own start sets it, whatever a press asked of
    /// the input has finished.
    func testThePressesWorkIsFinishedBeforeTheSessionsNextOwner() throws {
        let audio = FakeAudio()
        audio.listsSettle = true
        let (capture, webView) = makeHoldCapture(audio)

        capture.touchDown(at: Self.onComposer, in: webView)
        capture.yieldToActionButton()
        XCTAssertEqual(audio.calls, ["arm", "disarm", "settle"])

        // A tap, then the page's own take.
        audio.calls = []
        capture.touchDown(at: Self.onComposer, in: webView)
        capture.touchEnded(in: webView)
        _ = try capture.start(take: "own", webView: webView) { _ in }.get()
        XCTAssertEqual(audio.calls, ["arm", "disarm", "settle", "start"])

        // A press still pressing when the page starts its own.
        capture.stop(take: "own")
        drain()
        audio.calls = []
        capture.touchDown(at: Self.onComposer, in: webView)
        _ = try capture.start(take: "own-2", webView: webView) { _ in }.get()
        XCTAssertEqual(audio.calls, ["arm", "disarm", "settle", "start"])
        XCTAssertFalse(capture.followsPress)
        XCTAssertFalse(fire(NativeHoldCapture.defaultHoldSeconds))
    }

    func testAFailedStartEndsTheHoldWithItsStep() throws {
        // Before its page took it: dropped.
        let audio = FakeAudio()
        audio.beginFailure = .failed("engine:1")
        let (capture, webView) = makeHoldCapture(audio)
        let take = try hold(capture, in: webView)
        XCTAssertNil(capture.currentTake)
        drain()
        XCTAssertTrue(heard.isEmpty)
        XCTAssertTrue(try XCTUnwrap(notes.last).hasSuffix("end=engine:1"), notes.last ?? "")
        assertGone(capture.adopt(take: take, webView: webView))
        // Its page is told which step, so it can start the take itself.
        let failed = try XCTUnwrap(holdEvents.first { $0["phase"] as? String == "failed" })
        XCTAssertEqual(failed["take"] as? String, take)
        XCTAssertEqual(failed["surface"] as? String, "composer")
        XCTAssertEqual(failed["step"] as? String, "engine:1")

        // After: its page hears the end, as for any take.
        audio.holdsAnswers = true
        holdEvents = []
        let later = try hold(capture, in: webView)
        _ = try capture.adopt(take: later, webView: webView).get()
        audio.answer()
        drain()
        XCTAssertEqual(heard, [.ended(reason: "engine:1")])
        XCTAssertNil(capture.currentTake)
        XCTAssertFalse(holdEvents.contains { $0["phase"] as? String == "failed" })

        // Started at touch-down, a start that fails before the threshold
        // leaves no press and gives no cue: the page is told, or it could
        // not tell the press from a tap.
        startsAtTouchDown = true
        heard = []
        holdEvents = []
        haptics = []
        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertTrue(capture.followsPress)
        // Prepared; then its time comes, and the start fails.
        audio.answer()
        XCTAssertTrue(startEarly())
        audio.answer()
        XCTAssertFalse(capture.followsPress)
        XCTAssertNil(capture.currentTake)
        XCTAssertEqual(holdEvents.count, 1)
        XCTAssertEqual(holdEvents.first?["phase"] as? String, "failed")
        XCTAssertEqual(holdEvents.first?["surface"] as? String, "composer")
        XCTAssertEqual(holdEvents.first?["step"] as? String, "engine:1")
        XCTAssertFalse(fire(NativeHoldCapture.defaultHoldSeconds))
        XCTAssertEqual(haptics, ["prepare"])
        drain()
        XCTAssertTrue(heard.isEmpty)
    }

    /// A finger lifted just before the threshold while the main thread was
    /// busy: the hold's timer can run before the touch's end is handled.
    /// By the touch's own times it was a tap, and its audio is a tap's.
    func testATouchShorterThanTheThresholdIsATapEvenWhenItsEndIsHandledLate() throws {
        startsAtTouchDown = true
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)

        capture.touchDown(at: Self.onComposer, in: webView, timestamp: 10)
        XCTAssertTrue(startEarly())
        audio.onPCM?(Data([1, 0]))
        clock = 230
        XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds))
        let take = try XCTUnwrap(holdEvents.last?["take"] as? String)
        XCTAssertEqual(capture.currentTake, take)
        // The touch ended at 190 ms; its end is handled after the timer.
        capture.touchEnded(in: webView, timestamp: 10.19)
        XCTAssertNil(capture.currentTake)
        XCTAssertFalse(capture.followsPress)
        XCTAssertEqual(audio.calls, ["arm", "begin", "stop"])
        assertGone(capture.adopt(take: take, webView: webView))
        audio.onPCM?(Data([2, 0]))
        drain()
        XCTAssertTrue(heard.isEmpty)
        // Counted as the tap it was, by its own length.
        XCTAssertEqual(taps, ["190"])
        XCTAssertTrue(notes.isEmpty)

        // Past the threshold, a hold: the release is only noted.
        clock = 1_000
        capture.touchDown(at: Self.onComposer, in: webView, timestamp: 20)
        clock = 1_200
        XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds))
        let held = try XCTUnwrap(holdEvents.last?["take"] as? String)
        capture.touchEnded(in: webView, timestamp: 20.25)
        XCTAssertEqual(capture.currentTake, held)
        // And one its page has taken is the page's, whatever its length.
        capture.stop(take: held)
        drain()
        clock = 2_000
        capture.touchDown(at: Self.onComposer, in: webView, timestamp: 30)
        clock = 2_200
        XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds))
        let taken = try XCTUnwrap(holdEvents.last?["take"] as? String)
        _ = try capture.adopt(take: taken, webView: webView).get()
        capture.touchEnded(in: webView, timestamp: 30.1)
        XCTAssertEqual(capture.currentTake, taken)
        capture.stop(take: taken)
        drain()
    }

    /// The page's calls count in the order it made them: a list is applied
    /// only while it is still its WebView's last call, in the same document.
    func testAListOvertakenByALaterCallOrItsDocumentGoingIsNotApplied() {
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)
        let other = WKWebView(frame: .zero)

        let first = capture.surfacesRequested(by: webView)
        XCTAssertTrue(capture.isLatestSurfacesRequest(first, of: webView))
        XCTAssertFalse(capture.isLatestSurfacesRequest(first, of: other))
        let second = capture.surfacesRequested(by: webView)
        XCTAssertFalse(capture.isLatestSurfacesRequest(first, of: webView))
        XCTAssertTrue(capture.isLatestSurfacesRequest(second, of: webView))
        // Another WebView's calls are its own.
        _ = capture.surfacesRequested(by: other)
        XCTAssertTrue(capture.isLatestSurfacesRequest(second, of: webView))
        // Its document went (a navigation, a reload).
        capture.stop(webView: webView)
        XCTAssertFalse(capture.isLatestSurfacesRequest(second, of: webView))
    }

    /// A hold's input stops on its own queue, a moment after the take ends:
    /// what it sends until then is the take's, and `ended` comes after it.
    func testATakesLastAudioArrivesBeforeItsEndWhenTheInputStopsLater() throws {
        let audio = FakeAudio()
        audio.holdsStop = true
        let (capture, webView) = makeHoldCapture(audio)
        let take = try hold(capture, in: webView)
        _ = try capture.adopt(take: take, webView: webView).get()
        audio.onPCM?(Data([1, 0]))
        XCTAssertEqual(heard, [.pcm(Data([1, 0]))])

        capture.stop(take: take)
        XCTAssertNil(capture.currentTake)
        // The main queue's next turn comes before the input has stopped.
        drain()
        audio.onPCM?(Data([2, 0]))
        drain()
        XCTAssertEqual(heard, [.pcm(Data([1, 0]))])

        audio.finishStop()
        drain()
        XCTAssertEqual(heard, [.pcm(Data([1, 0])), .pcm(Data([2, 0])), .ended(reason: nil)])
        // Nothing after its end.
        audio.onPCM?(Data([3, 0]))
        drain()
        XCTAssertEqual(heard.count, 3)

        // An untaken hold's is dropped all the same.
        heard = []
        let untaken = try hold(capture, in: webView)
        audio.onPCM?(Data([4, 0]))
        capture.reject(take: untaken, webView: webView)
        audio.onPCM?(Data([5, 0]))
        audio.finishStop()
        drain()
        XCTAssertTrue(heard.isEmpty)
    }

    /// Adopted before any audio (the usual order: the page answers within a
    /// few tens of ms), the first audio still goes to the page at once.
    func testTheFirstAudioGoesAtOnceWhenThePageTookTheHoldBeforeIt() throws {
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)
        let take = try hold(capture, in: webView)
        _ = try capture.adopt(take: take, webView: webView).get()
        XCTAssertTrue(heard.isEmpty)
        audio.onPCM?(Data([1, 0]))
        XCTAssertEqual(heard, [.pcm(Data([1, 0]))])
        audio.onPCM?(Data([2, 0]))
        XCTAssertEqual(heard, [.pcm(Data([1, 0]))])
        capture.stop(take: take)
        drain()
    }

    /// The page takes a surface back while a finger is on it and not yet a
    /// hold: that press is no longer followed, and gets no cue.
    func testAPressOnASurfaceThePageTakesBackIsNoLongerFollowed() {
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)
        capture.touchDown(at: Self.onComposer, in: webView)
        // Named again as it is: still followed.
        XCTAssertTrue(capture.setSurfaces([Self.composer], for: webView))
        XCTAssertTrue(capture.followsPress)
        // Moved from under the finger, or gone.
        XCTAssertTrue(capture.setSurfaces(
            [.init(id: "composer", rect: CGRect(x: 0, y: 400, width: 390, height: 60))], for: webView
        ))
        XCTAssertFalse(capture.followsPress)
        XCTAssertEqual(audio.calls, ["arm", "disarm"])
        XCTAssertFalse(fire(NativeHoldCapture.defaultHoldSeconds))
        XCTAssertEqual(haptics, ["prepare"])
        XCTAssertTrue(holdEvents.isEmpty)
        XCTAssertEqual(notes.count, 1)
        XCTAssertTrue(try XCTUnwrap(notes.last).hasSuffix("end=withdrawn"), notes.last ?? "")

        XCTAssertTrue(capture.setSurfaces([Self.composer], for: webView))
        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertTrue(capture.setSurfaces([], for: webView))
        XCTAssertFalse(capture.followsPress)
    }

    /// The tap that focuses the composer: the page takes the press for
    /// itself, so the App is not told of the lift, and the page takes the
    /// focused composer back from its list before the App learns the finger
    /// has gone. Asked, the touch says it lifted: the press is counted as
    /// the tap it was, with its own length, whether the microphone was
    /// started for it, and what each judged time would have done.
    func testATapThatFocusesTheComposerIsCountedAsATap() {
        for early in [false, true] {
            startsAtTouchDown = early
            let audio = FakeAudio()
            let (capture, webView) = makeHoldCapture(audio)
            taps = []
            tapRecords = []
            notes = []
            clock = 0
            var lifted: TimeInterval?
            capture.touchDown(
                at: Self.onComposer, in: webView, timestamp: 50,
                touch: { .init(liftedAt: lifted, point: Self.onComposer) }
            )
            // At 50 ms the finger is down: where the input may start then, it starts.
            XCTAssertEqual(startEarly(), early, "\(early)")
            // A 120 ms tap; nothing tells the App, and the page takes the surface back.
            lifted = 50.12
            clock = 160
            XCTAssertTrue(capture.setSurfaces([], for: webView), "\(early)")
            XCTAssertFalse(capture.followsPress, "\(early)")
            XCTAssertNil(capture.currentTake, "\(early)")
            XCTAssertEqual(audio.calls, early ? ["arm", "begin", "stop"] : ["arm", "disarm"], "\(early)")
            XCTAssertEqual(taps, ["120"], "\(early)")
            XCTAssertEqual(tapRecords.map(\.opened), [early], "\(early)")
            // Prepared at once (the fake answers at touch-down): a 120 ms tap lasts as long as every judged time.
            XCTAssertEqual(tapRecords.first?.wouldOpen, [50: true, 80: true, 100: true, 120: true], "\(early)")
            XCTAssertTrue(notes.isEmpty, "\(early): \(notes)")
            capture.stop(webView: webView)
            drain()
        }
    }

    /// The tap that focuses the composer on a phone: by the time the page
    /// takes the composer back (the keyboard has come up), UIKit has let its
    /// touch go. It lifted before now, at a time not known: it is counted as
    /// a tap, with the most it can have lasted, whether the microphone was
    /// started for it, and for which thresholds it surely ended first.
    func testATapWhoseTouchWasGoneWhenItWasTakenBackIsCountedWithItsBound() {
        for early in [false, true] {
            startsAtTouchDown = early
            let audio = FakeAudio()
            let (capture, webView) = makeHoldCapture(audio)
            taps = []
            tapRecords = []
            notes = []
            clock = 0
            var gone = false
            capture.touchDown(
                at: Self.onComposer, in: webView, timestamp: 50,
                touch: { gone ? .init(liftedAt: 50.4, point: nil, gone: true) : .init(liftedAt: nil, point: Self.onComposer) }
            )
            // Prepared at once (the fake answers at touch-down); where it may, it starts at 50 ms.
            XCTAssertEqual(startEarly(), early, "\(early)")
            gone = true
            clock = 90
            XCTAssertTrue(capture.setSurfaces([], for: webView), "\(early)")
            XCTAssertFalse(capture.followsPress, "\(early)")
            XCTAssertNil(capture.currentTake, "\(early)")
            XCTAssertEqual(audio.calls, early ? ["arm", "begin", "stop"] : ["arm", "disarm"], "\(early)")
            XCTAssertTrue(notes.isEmpty, "\(early): \(notes)")
            let tap = tapRecords.first
            XCTAssertEqual(tapRecords.count, 1, "\(early)")
            XCTAssertEqual(tap?.ms, 90, "\(early)")
            XCTAssertEqual(tap?.lengthUnknown, true, "\(early)")
            XCTAssertEqual(tap?.opened, early, "\(early)")
            XCTAssertEqual(tap?.wouldOpen, [:], "\(early)")
            // At most 90 ms down: it surely ended before 100 and 120, not surely before 50 or 80.
            XCTAssertEqual(tap?.surelyClosed, [50: false, 80: false, 100: true, 120: true], "\(early)")
            capture.stop(webView: webView)
            drain()
        }

        // Preparing had not answered when it was taken back: no threshold could have opened it.
        startsAtTouchDown = true
        let audio = FakeAudio()
        audio.holdsAnswers = true
        let (capture, webView) = makeHoldCapture(audio)
        tapRecords = []
        clock = 0
        capture.touchDown(
            at: Self.onComposer, in: webView, timestamp: 50,
            touch: { .init(liftedAt: 50.4, point: nil, gone: true) }
        )
        clock = 300
        XCTAssertTrue(capture.setSurfaces([], for: webView))
        XCTAssertEqual(tapRecords.first?.surelyClosed, [50: true, 80: true, 100: true, 120: true])
        XCTAssertEqual(tapRecords.first?.opened, false)
        capture.stop(webView: webView)
        audio.answer()
        drain()
    }

    /// The same when the start's look at the finger, or the cue's, finds its
    /// touch already gone: counted with the bound of that moment, not as a
    /// tap as long as the time of asking.
    func testATapWhoseTouchWasGoneAtTheStartOrTheCueIsCountedWithItsBound() {
        // At the start, 50 ms in.
        startsAtTouchDown = true
        var audio = FakeAudio()
        var (capture, webView) = makeHoldCapture(audio)
        tapRecords = []
        clock = 0
        capture.touchDown(at: Self.onComposer, in: webView, timestamp: 50, touch: { .init(liftedAt: 51, point: nil, gone: true) })
        clock = 50
        XCTAssertTrue(startEarly())
        XCTAssertEqual(audio.calls, ["arm", "disarm"])
        XCTAssertEqual(tapRecords.map(\.ms), [50])
        XCTAssertEqual(tapRecords.first?.lengthUnknown, true)
        XCTAssertEqual(tapRecords.first?.wouldOpen, [:])
        XCTAssertEqual(tapRecords.first?.surelyClosed, [50: false, 80: true, 100: true, 120: true])
        capture.stop(webView: webView)

        // At the cue, 200 ms in: it may have been down long enough for any threshold.
        startsAtTouchDown = false
        audio = FakeAudio()
        (capture, webView) = makeHoldCapture(audio)
        tapRecords = []
        clock = 0
        capture.touchDown(at: Self.onComposer, in: webView, timestamp: 50, touch: { .init(liftedAt: 51, point: nil, gone: true) })
        clock = 200
        XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds))
        XCTAssertTrue(holdEvents.isEmpty)
        XCTAssertEqual(tapRecords.map(\.ms), [200])
        XCTAssertEqual(tapRecords.first?.lengthUnknown, true)
        XCTAssertEqual(tapRecords.first?.surelyClosed, [50: false, 80: false, 100: false, 120: false])
        capture.stop(webView: webView)
    }

    /// Such taps are in their bound's bucket and in no `w…` count, counted
    /// apart (`lost`, `c…`). The line a page reads leaves those names out: a
    /// page from before them refuses a line with a name it does not know.
    func testTapsOfUnknownLengthAreCountedApartAndOldPagesStillReadTheCounts() throws {
        var lost = NativeHoldCapture.Tap(ms: 90, moved: false, opened: true, lengthUnknown: true)
        lost.surelyClosed = [50: false, 80: false, 100: true, 120: true]
        var counts = NativeHoldTaps.adding(lost, to: [:])
        var known = NativeHoldCapture.Tap(ms: 60, moved: false)
        known.wouldOpen = [50: true, 80: false, 100: false, 120: false]
        counts = NativeHoldTaps.adding(known, to: counts)
        let line = try XCTUnwrap(NativeHoldTaps.line(for: counts))
        XCTAssertEqual(line, "hold taps 50:1 75:1 open=1 w50=1 lost=1 c100=1 c120=1")
        XCTAssertEqual(NativeHoldTaps.counts(in: line), counts)
        XCTAssertEqual(NativeHoldTaps.line(for: counts, lengths: false), "hold taps 50:1 75:1 open=1 w50=1")
        XCTAssertEqual(NativeHoldTaps.lostLine(for: counts), "lost=1 c50=0 c80=0 c100=1 c120=1")
        XCTAssertNil(NativeHoldTaps.lostLine(for: NativeHoldTaps.adding(known, to: [:])))

        // A hold's timings carry both.
        tapsTotal = "50:1 75:1 open=1 w50=1"
        tapsLost = "lost=1 c50=0 c80=0 c100=1 c120=1"
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)
        let take = try hold(capture, in: webView)
        _ = try capture.adopt(take: take, webView: webView).get()
        capture.stop(take: take)
        drain()
        let timings = try XCTUnwrap(holdEvents.last { $0["phase"] as? String == "timings" }?["timings"] as? [String: Any])
        XCTAssertEqual(timings["taps"] as? String, "50:1 75:1 open=1 w50=1")
        XCTAssertEqual(timings["tapsLost"] as? String, "lost=1 c50=0 c80=0 c100=1 c120=1")
    }

    /// A press the page takes back while the finger is still down is not a
    /// tap: it stays a line of its own, and counts nothing.
    func testAPressWithdrawnWithTheFingerStillDownIsNotATap() throws {
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)
        capture.touchDown(
            at: Self.onComposer, in: webView, timestamp: 50,
            touch: { .init(liftedAt: nil, point: Self.onComposer) }
        )
        clock = 120
        XCTAssertTrue(capture.setSurfaces([], for: webView))
        XCTAssertFalse(capture.followsPress)
        XCTAssertTrue(taps.isEmpty)
        XCTAssertEqual(notes.count, 1)
        XCTAssertTrue(try XCTUnwrap(notes.last).hasSuffix("end=withdrawn"), notes.last ?? "")
    }

    /// One WebView's surfaces at a time, and only for a WebView whose
    /// touches the App sees.
    func testSurfacesBelongToTheWebViewThatNamedThem() {
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)
        let popup = WKWebView(frame: .zero)

        // Not watched: refused, and the first one's surfaces stand.
        XCTAssertFalse(capture.setSurfaces([Self.composer], for: popup))
        // Another WebView cannot take them back.
        XCTAssertTrue(capture.setSurfaces([], for: popup))
        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertTrue(capture.followsPress)

        // A watched WebView's own list replaces it, and ends that press.
        let second = WKWebView(frame: .zero)
        capture.watch(second)
        XCTAssertTrue(capture.setSurfaces([Self.composer], for: second))
        XCTAssertFalse(capture.followsPress)
        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertFalse(capture.followsPress)
        capture.touchDown(at: Self.onComposer, in: second)
        XCTAssertTrue(capture.followsPress)
        capture.touchEnded(in: second)
    }

    func testOnlyItsOwnWebViewTakesOrRefusesAHold() throws {
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)
        let other = WKWebView(frame: .zero)
        let take = try hold(capture, in: webView)
        audio.onPCM?(Data([1, 0]))

        assertGone(capture.adopt(take: take, webView: other))
        capture.reject(take: take, webView: other)
        XCTAssertEqual(capture.currentTake, take)
        _ = try capture.adopt(take: take, webView: webView).get()
        XCTAssertEqual(heard, [.pcm(Data([1, 0]))])
        capture.stop(take: take)
        drain()
    }

    /// After the cue a finger's slides are the page's: it sees them on every
    /// surface and gives their haptics. The App gives none, and a slide
    /// does not end the take.
    func testAfterTheCueTheFingersSlidesAreThePages() throws {
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)
        let take = try hold(capture, in: webView)
        haptics = []
        for up: CGFloat in [81, 60, 120, 0] {
            capture.touchMoved(to: CGPoint(x: Self.onComposer.x, y: Self.onComposer.y - up), in: webView)
        }
        XCTAssertEqual(haptics, [])
        XCTAssertEqual(capture.currentTake, take)
        capture.stop(take: take)
        drain()
    }

    /// The page's hold threshold is the App's: one number, named with the
    /// surfaces.
    func testTheHoldThresholdIsTheOneThePageNames() throws {
        XCTAssertEqual(NativeHoldCapture.holdMs(from: 250), 250)
        // At most half a second: the privacy policy calls what a touch that
        // is not a hold records "that fraction of a second".
        XCTAssertEqual(NativeHoldCapture.holdMsRange, 100...500)
        XCTAssertEqual(NativeHoldCapture.holdMs(from: 100), 100)
        XCTAssertEqual(NativeHoldCapture.holdMs(from: 500), 500)
        XCTAssertTrue(NativeHoldCapture.holdMsRange.contains(Int((NativeHoldCapture.defaultHoldSeconds * 1000).rounded())))
        let invalid: [Any?] = [nil, 99, 501, 1000, 250.5, true, "250"]
        for value in invalid {
            XCTAssertNil(NativeHoldCapture.holdMs(from: value), String(describing: value))
        }
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)
        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertEqual(timers.last?.delay, 0.2)
        capture.touchEnded(in: webView)

        XCTAssertTrue(capture.setSurfaces([Self.composer], holdMs: 250, for: webView))
        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertEqual(timers.last?.delay, 0.25)
        clock = 250
        XCTAssertTrue(fire(0.25))
        let take = try XCTUnwrap(holdEvents.last?["take"] as? String)
        let timings = try capture.adopt(take: take, webView: webView).get()
        XCTAssertEqual(timings["holdMs"] as? Int, 250)
        XCTAssertEqual(timings["recognisedAt"] as? Int, 250)
        // Prepared at touch-down and started at recognition.
        XCTAssertEqual(timings["liveQuiet"] as? Int, 250)
        capture.stop(take: take)
        drain()

        // A list that names none is timed with the default again.
        XCTAssertTrue(capture.setSurfaces([Self.composer], for: webView))
        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertEqual(timers.last?.delay, 0.2)
        capture.touchEnded(in: webView)
    }

    /// Touches on a surface that were not holds are counted by how long
    /// they lasted, in 25 ms steps, and those that moved away apart.
    func testTapsAreCountedByDurationToJudgeTheThreshold() {
        var counts: [String: Int] = [:]
        for (ms, moved) in [(0, false), (24, false), (25, false), (120, false), (124, false), (199, false), (5_000, false), (60, true), (130, true)] {
            counts = NativeHoldTaps.adding(ms: ms, moved: moved, to: counts)
        }
        XCTAssertEqual(counts, ["0": 2, "25": 1, "100": 2, "175": 1, "975": 1, "moved": 2])
        let line = NativeHoldTaps.line(for: counts)
        XCTAssertEqual(line, "hold taps 0:2 25:1 100:2 175:1 975:1 moved=2")
        XCTAssertNil(NativeHoldTaps.line(for: [:]))
        XCTAssertEqual(NativeHoldTaps.counts(in: line ?? ""), counts)
        XCTAssertNil(NativeHoldTaps.counts(in: "hold quiet Speaker rec=200"))
        // What a message carried is taken off; touches counted since stay.
        XCTAssertEqual(
            NativeHoldTaps.removing(["0": 2, "100": 1, "moved": 2], from: counts),
            ["25": 1, "100": 1, "175": 1, "975": 1]
        )
    }

    /// The invariant: a touch whose lift happened before the threshold, by
    /// the touch's own times, never gets the cue (no haptic, no word to the
    /// page) and never keeps the microphone, however late the App hears of
    /// the lift. On a busy main thread the threshold's timer runs while the
    /// lift still waits to be handled (TestFlight 50.1 gave the cue then).
    func testATapWhoseLiftIsHeardLateGetsNoCueAndKeepsNoMicrophone() {
        for early in [true, false] {
            startsAtTouchDown = early
            let audio = FakeAudio()
            let (capture, webView) = makeHoldCapture(audio)
            haptics = []
            holdEvents = []
            heard = []
            taps = []
            tapRecords = []
            notes = []
            clock = 0

            // A tap of 80 ms. The main thread is busy for a second from its touch-down.
            capture.touchDown(at: Self.onComposer, in: webView, timestamp: 50)
            clock = 1_000
            // The timers, late, run before the lift is handled: the start
            // waits for the App to have heard the touches, as the cue does.
            XCTAssertEqual(startEarly(touchesHeard: false), early, "\(early)")
            XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds, touchesHeard: false), "\(early)")
            XCTAssertEqual(haptics, ["prepare"], "\(early)")
            XCTAssertTrue(holdEvents.isEmpty, "\(early)")
            // No start without the finger seen down: the microphone is not opened.
            XCTAssertEqual(audio.calls, ["arm"], "\(early)")

            // The lift is handled: a tap, with what a tap leaves.
            capture.touchEnded(in: webView, timestamp: 50.08)
            XCTAssertFalse(capture.followsPress, "\(early)")
            XCTAssertNil(capture.currentTake, "\(early)")
            XCTAssertEqual(audio.calls, ["arm", "disarm"], "\(early)")
            XCTAssertEqual(tapRecords.map(\.opened), [false], "\(early)")
            // Nothing more once the App has caught up, and nothing later.
            hearTouches()
            audio.onPCM?(Data([2, 0]))
            drain()
            XCTAssertFalse(fire(NativeHoldCapture.adoptionWait), "\(early)")
            XCTAssertEqual(haptics, ["prepare"], "\(early)")
            XCTAssertTrue(holdEvents.isEmpty, "\(early)")
            XCTAssertTrue(heard.isEmpty, "\(early)")
            // Counted as the tap it was, by its own length, not by when it was heard.
            XCTAssertEqual(taps, ["80"], "\(early)")
            XCTAssertTrue(notes.isEmpty, "\(early)")
            capture.stop(webView: webView)
        }
    }

    /// On a surface whose page took the press for itself, UIKit does not tell
    /// the App that the finger lifted (it resets the observer, later). The
    /// touch itself is asked before the cue: lifted, it was a tap.
    func testATapTheAppWasNeverToldOfGetsNoCueAndKeepsNoMicrophone() {
        for early in [true, false] {
            startsAtTouchDown = early
            let audio = FakeAudio()
            let (capture, webView) = makeHoldCapture(audio)
            haptics = []
            holdEvents = []
            heard = []
            taps = []
            notes = []
            clock = 0
            var lifted: TimeInterval?

            capture.touchDown(
                at: Self.onComposer, in: webView, timestamp: 50,
                touch: { .init(liftedAt: lifted, point: Self.onComposer) }
            )
            // At 50 ms the finger is down: the input starts where it may.
            XCTAssertEqual(startEarly(), early, "\(early)")
            audio.onPCM?(Data([1, 0]))
            // The finger lifts after 70 ms; nothing tells the App.
            lifted = 50.07
            clock = 500
            XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds), "\(early)")
            XCTAssertFalse(capture.followsPress, "\(early)")
            XCTAssertNil(capture.currentTake, "\(early)")
            XCTAssertEqual(haptics, ["prepare"], "\(early)")
            XCTAssertTrue(holdEvents.isEmpty, "\(early)")
            XCTAssertEqual(audio.calls, early ? ["arm", "begin", "stop"] : ["arm", "disarm"], "\(early)")
            audio.onPCM?(Data([2, 0]))
            drain()
            XCTAssertTrue(heard.isEmpty, "\(early)")
            XCTAssertEqual(taps, ["70"], "\(early)")
            XCTAssertTrue(notes.isEmpty, "\(early)")
            // UIKit's late word of the lift finds nothing left to end.
            capture.touchEnded(in: webView, timestamp: 50.07)
            XCTAssertEqual(taps, ["70"], "\(early)")
            capture.stop(webView: webView)
        }
    }

    /// Nor is the App told of the finger's moves there. A finger still down
    /// at the threshold but away from where it came down is a drag, which
    /// the page has already given back: no cue.
    func testADragTheAppWasNeverToldOfGetsNoCue() {
        for early in [true, false] {
            startsAtTouchDown = early
            let audio = FakeAudio()
            let (capture, webView) = makeHoldCapture(audio)
            haptics = []
            holdEvents = []
            taps = []
            clock = 0
            var point = Self.onComposer

            capture.touchDown(at: Self.onComposer, in: webView, timestamp: 50, touch: { .init(liftedAt: nil, point: point) })
            // At 50 ms the finger is where it came down: the input starts where it may.
            XCTAssertEqual(startEarly(), early, "\(early)")
            // The finger moves 40 pt, past the slop, with no word of a move to the App.
            point = CGPoint(x: Self.onComposer.x + 40, y: Self.onComposer.y)
            clock = 200
            XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds), "\(early)")
            XCTAssertFalse(capture.followsPress, "\(early)")
            XCTAssertNil(capture.currentTake, "\(early)")
            XCTAssertEqual(haptics, ["prepare"], "\(early)")
            XCTAssertTrue(holdEvents.isEmpty, "\(early)")
            XCTAssertEqual(audio.calls, early ? ["arm", "begin", "stop"] : ["arm", "disarm"], "\(early)")
            XCTAssertEqual(taps, ["200 moved"], "\(early)")
            capture.stop(webView: webView)
            drain()
        }
    }

    /// The same wait gives a finger still down its cue: at once on an idle
    /// main thread, and as soon as a busy one has caught up.
    func testAHoldGetsItsCueOnceTheTouchesThatArrivedAreHandled() throws {
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)
        // Its touch, asked, says the finger is down, within the slop of where it came down.
        capture.touchDown(
            at: Self.onComposer, in: webView, timestamp: 50,
            touch: { .init(liftedAt: nil, point: CGPoint(x: Self.onComposer.x + 4, y: Self.onComposer.y)) }
        )
        clock = 450
        XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds, touchesHeard: false))
        XCTAssertEqual(haptics, ["prepare"])
        // A move that had been waiting, within the slop: still a press.
        capture.touchMoved(to: CGPoint(x: 100, y: 724), in: webView)
        hearTouches()
        XCTAssertEqual(haptics, ["prepare", "fire"])
        let take = try XCTUnwrap(holdEvents.last?["take"] as? String)
        XCTAssertEqual(holdEvents.last?["phase"] as? String, "recognised")
        // When the cue was given, by the App's clock: the main thread's lateness shows.
        XCTAssertEqual(try capture.adopt(take: take, webView: webView).get()["recognisedAt"] as? Int, 450)
        // Asked once: catching up again gives nothing more.
        hearTouches()
        XCTAssertEqual(haptics, ["prepare", "fire"])
        capture.stop(take: take)
        drain()
    }

    /// The App's own wait (the main run loop about to rest): it ends at once
    /// on an idle main thread, and a cancelled one never runs.
    func testTheWaitForTouchesEndsOnAnIdleMainThreadAndCanBeCancelled() async {
        var cancelledRan = false
        let cancel = NativeHoldCapture.whenTouchesAreHeard { cancelledRan = true }
        cancel()
        let idle = expectation(description: "the main thread has handled what reached it")
        _ = NativeHoldCapture.whenTouchesAreHeard { idle.fulfill() }
        await fulfillment(of: [idle], timeout: 5)
        XCTAssertFalse(cancelledRan)
    }

    /// Whatever ends a press between its threshold and its cue ends the wait
    /// for the cue too.
    func testAPressThatEndsWhileItsCueWaitsGetsNone() {
        let enders: [(String, (NativeHoldCapture, WKWebView) -> Void)] = [
            ("moved", { capture, webView in capture.touchMoved(to: CGPoint(x: 100, y: 760), in: webView) }),
            ("withdrawn", { capture, webView in _ = capture.setSurfaces([], for: webView) }),
            ("inactive", { capture, _ in capture.appWillResignActive() }),
            ("action_button", { capture, _ in capture.yieldToActionButton() }),
            ("replaced", { capture, webView in _ = capture.start(take: "own", webView: webView) { _ in } }),
        ]
        for early in [true, false] {
            for (name, end) in enders {
                startsAtTouchDown = early
                let audio = FakeAudio()
                let (capture, webView) = makeHoldCapture(audio)
                haptics = []
                holdEvents = []
                capture.touchDown(at: Self.onComposer, in: webView)
                clock += 400
                XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds, touchesHeard: false), "\(name) \(early)")
                end(capture, webView)
                hearTouches()
                XCTAssertFalse(capture.followsPress, "\(name) \(early)")
                XCTAssertEqual(haptics, ["prepare"], "\(name) \(early)")
                XCTAssertTrue(holdEvents.isEmpty, "\(name) \(early)")
                capture.stop(webView: webView)
                drain()
            }
        }
    }

    func testAPressThatMovesOrIsNotOnASurfaceIsNotAHold() {
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)

        // Off the surface, or in a WebView that registered none.
        capture.touchDown(at: CGPoint(x: 100, y: 300), in: webView)
        capture.touchDown(at: Self.onComposer, in: WKWebView(frame: .zero))
        XCTAssertFalse(capture.followsPress)

        // Within the slop it stays a press; past it, it is a scroll.
        capture.touchDown(at: Self.onComposer, in: webView)
        capture.touchMoved(to: CGPoint(x: 100, y: 729), in: webView)
        XCTAssertTrue(capture.followsPress)
        capture.touchMoved(to: CGPoint(x: 100, y: 731), in: webView)
        XCTAssertFalse(capture.followsPress)
        XCTAssertEqual(audio.calls, ["arm", "disarm"])
        XCTAssertEqual(taps, ["0 moved"])
        XCTAssertFalse(fire(NativeHoldCapture.defaultHoldSeconds))
        XCTAssertEqual(haptics, ["prepare"])

        // A second finger while one is followed is not a press of its own.
        capture.touchDown(at: Self.onComposer, in: webView)
        capture.touchDown(at: CGPoint(x: 200, y: 720), in: webView)
        XCTAssertEqual(audio.calls, ["arm", "disarm", "arm"])
        capture.touchEnded(in: webView)

        // The page took its surfaces back.
        capture.setSurfaces([], for: webView)
        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertFalse(capture.followsPress)
    }

    func testNoPressIsFollowedWithoutThePermissionInTheBackgroundOrUnderAnActionButtonTake() {
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)

        audio.permissionGranted = false
        capture.touchDown(at: Self.onComposer, in: webView)
        audio.permissionGranted = true
        inBackground = true
        capture.touchDown(at: Self.onComposer, in: webView)
        inBackground = false
        VoiceCaptureAudioState.active = true
        capture.touchDown(at: Self.onComposer, in: webView)
        VoiceCaptureAudioState.active = false
        XCTAssertFalse(capture.followsPress)
        XCTAssertEqual(audio.calls, [])

        // An Action Button take ends a press that was only prepared.
        capture.touchDown(at: Self.onComposer, in: webView)
        capture.yieldToActionButton()
        XCTAssertFalse(capture.followsPress)
        XCTAssertEqual(audio.calls, ["arm", "disarm"])
        XCTAssertFalse(fire(NativeHoldCapture.defaultHoldSeconds))
    }

    /// The variant behind the switch: the input starts at touch-down, and a
    /// touch that is not a hold drops what it recorded in the App.
    func testStartingAtTouchDownDropsATapsAudioAndKeepsAHolds() throws {
        startsAtTouchDown = true
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)

        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertEqual(audio.calls, ["arm"])
        XCTAssertTrue(startEarly())
        XCTAssertEqual(audio.calls, ["arm", "begin"])
        audio.onPCM?(Data([9, 0]))
        clock = 90
        capture.touchEnded(in: webView)
        drain()
        XCTAssertEqual(audio.calls, ["arm", "begin", "stop"])
        XCTAssertTrue(heard.isEmpty)
        XCTAssertTrue(holdEvents.isEmpty)
        XCTAssertEqual(haptics, ["prepare"])
        XCTAssertEqual(taps, ["90"])
        XCTAssertTrue(notes.isEmpty)

        clock = 1_000
        capture.touchDown(at: Self.onComposer, in: webView)
        clock = 1_050
        XCTAssertTrue(startEarly())
        audio.onPCM?(Data([1, 0]))
        clock = 1_200
        XCTAssertTrue(fire(NativeHoldCapture.defaultHoldSeconds))
        let take = try XCTUnwrap(holdEvents.last?["take"] as? String)
        let timings = try capture.adopt(take: take, webView: webView).get()
        // What was said before the cue is the take's.
        XCTAssertEqual(heard, [.pcm(Data([1, 0]))])
        XCTAssertEqual(timings["variant"] as? String, "early")
        XCTAssertEqual(timings["startAfterMs"] as? Int, 50)
        XCTAssertEqual(timings["armedAt"] as? Int, 0)
        XCTAssertEqual(timings["beginAt"] as? Int, 50)
        XCTAssertEqual(timings["liveAt"] as? Int, 50)
        XCTAssertEqual(timings["cueToLive"] as? Int, -150)
        // Had it started at touch-down with no wait: its own set-up and start (7 + 12 + 3 + 5).
        XCTAssertEqual(timings["liveEarly"] as? Int, 27)
        XCTAssertEqual(timings["liveQuiet"] as? Int, 205)
        capture.stop(take: take)
        drain()
        XCTAssertTrue(try XCTUnwrap(notes.last).hasPrefix("hold early Speaker "), notes.last ?? "")
    }

    /// Starting at touch-down is the default; the switch for starting at
    /// recognition is off, and only an internal build can turn it on.
    func testTheSwitchForStartingAtRecognitionIsOffAndForInternalBuildsOnly() throws {
        let suite = "NativeHoldQuietSwitch.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let on = try XCTUnwrap(URL(string: "avibe-diagnostics://voice/hold-quiet?on=1"))
        let off = try XCTUnwrap(URL(string: "avibe-diagnostics://voice/hold-quiet?on=0"))

        XCTAssertFalse(NativeHoldQuietSwitch.isOn(in: defaults, internalBuild: true))
        XCTAssertFalse(NativeHoldQuietSwitch.handle(on, allowed: false, defaults: defaults))
        XCTAssertFalse(NativeHoldQuietSwitch.isOn(in: defaults, internalBuild: true))
        XCTAssertTrue(NativeHoldQuietSwitch.handle(on, allowed: true, defaults: defaults))
        XCTAssertTrue(NativeHoldQuietSwitch.isOn(in: defaults, internalBuild: true))
        // An App Store build ignores what a TestFlight build had set.
        XCTAssertFalse(NativeHoldQuietSwitch.isOn(in: defaults, internalBuild: false))
        XCTAssertTrue(NativeHoldQuietSwitch.handle(off, allowed: true, defaults: defaults))
        XCTAssertFalse(NativeHoldQuietSwitch.isOn(in: defaults, internalBuild: true))
        XCTAssertFalse(NativeHoldQuietSwitch.handle(
            try XCTUnwrap(URL(string: "avibe-diagnostics://voice/end-after-background?on=1")),
            allowed: true, defaults: defaults
        ))
    }

    /// A page of an older Avibe starts its take itself: that still works,
    /// and its timings are kept on the phone (it drops them).
    func testAPagesOwnStartKeepsItsTimingsOnThePhone() throws {
        let audio = FakeAudio()
        audio.otherAudioPlaying = true
        let capture = makeCapture(audio)
        _ = try capture.start(take: "take-1", webView: nil) { _ in }.get()
        XCTAssertEqual(notes, ["hold page Speaker cat=7 act=12 eng=34 other=1"])
        XCTAssertEqual(audio.settles, 1)
        XCTAssertEqual(
            NativeHoldTimings.adding("x", to: (1...12).map(String.init)),
            (2...12).map(String.init) + ["x"]
        )
        XCTAssertEqual(NativeHoldTimings.removing(["a", "a"], from: ["a", "b", "a", "a"]), ["b", "a"])
    }

    func testTheSurfacesArgumentIsAShortListOfPlainRectangles() {
        let surface: [String: Any] = ["id": "composer", "x": 0, "y": 700.5, "width": 390, "height": 60]
        XCTAssertEqual(
            NativeHoldCapture.surfaces(from: [surface]),
            [.init(id: "composer", rect: CGRect(x: 0, y: 700.5, width: 390, height: 60))]
        )
        XCTAssertEqual(NativeHoldCapture.surfaces(from: [[String: Any]]()), [])
        // When a surface's input starts: at touch-down unless the page says
        // at recognition.
        let rect = CGRect(x: 0, y: 700.5, width: 390, height: 60)
        XCTAssertEqual(
            NativeHoldCapture.surfaces(from: [surface.merging(["start": "recognition"]) { _, new in new }]),
            [.init(id: "composer", rect: rect, startsAtRecognition: true)]
        )
        XCTAssertEqual(
            NativeHoldCapture.surfaces(from: [surface.merging(["start": "touch"]) { _, new in new }]),
            [.init(id: "composer", rect: rect, startsAtRecognition: false)]
        )
        XCTAssertEqual(NativeHoldCapture.surfaces(from: [surface])?.first?.startsAtRecognition, false)
        // The earliest the input may start, and whether anything is prepared before it.
        XCTAssertEqual(
            NativeHoldCapture.surfaces(from: [surface.merging(["startAfterMs": 80, "prepare": "start"]) { _, new in new }]),
            [.init(id: "composer", rect: rect, startAfterMs: 80, preparesAtStart: true)]
        )
        XCTAssertEqual(
            NativeHoldCapture.surfaces(from: [surface.merging(["startAfterMs": 0, "prepare": "touch", "start": "touch"]) { _, new in new }]),
            [.init(id: "composer", rect: rect, startAfterMs: 0)]
        )
        XCTAssertNil(NativeHoldCapture.surfaces(from: [surface])?.first?.startAfterMs)
        XCTAssertEqual(NativeHoldCapture.startAfterMs(from: 500), 500)
        for value in [-1, 501, 50.5, true, "50"] as [Any] {
            XCTAssertNil(NativeHoldCapture.startAfterMs(from: value), String(describing: value))
        }
        let invalid: [Any?] = [
            [surface.merging(["startAfterMs": 501]) { _, new in new }],
            [surface.merging(["startAfterMs": "50"]) { _, new in new }],
            [surface.merging(["startAfterMs": 50, "start": "recognition"]) { _, new in new }],
            [surface.merging(["prepare": "later"]) { _, new in new }],
            [surface.merging(["prepare": true]) { _, new in new }],
            [surface.merging(["startAfterMs": 50, "prepare": "touch", "start": "touch", "extra": 1]) { _, new in new }],
            [surface.merging(["start": "later"]) { _, new in new }],
            [surface.merging(["start": true]) { _, new in new }],
            [surface.merging(["start": 1]) { _, new in new }],
            [surface.merging(["start": "recognition", "extra": 1]) { _, new in new }],
            nil,
            "x",
            Array(repeating: surface, count: 9),
            [surface.merging(["width": 0]) { _, new in new }],
            [surface.merging(["x": Double.infinity]) { _, new in new }],
            [surface.merging(["y": true]) { _, new in new }],
            [surface.merging(["id": "not plain!"]) { _, new in new }],
            [surface.merging(["extra": 1]) { _, new in new }],
            [surface.filter { $0.key != "height" }],
        ]
        for value in invalid {
            XCTAssertNil(NativeHoldCapture.surfaces(from: value), String(describing: value))
        }
    }

    /// Only the selected instance's own document, in its main frame, names
    /// hold surfaces or takes a hold's take; a document that does not ask
    /// for voice consent gets none of its audio.
    func testOnlyTheInstancesOwnMainFrameNamesSurfacesAndTakesAHold() async throws {
        let marked = #"<!doctype html><meta name="avibe-voice-consent" content="1"><iframe id="child" srcdoc="<p>x</p>"></iframe>"#
        let surfaces = #"{surfaces: [{id: "composer", x: 0, y: 700, width: 390, height: 60}]}"#
        let script = """
            if (!window.avibeNative) return "forbidden";
            const code = async (method, params) => {
              try { await window.avibeNative.call(method, params); return "ok"; }
              catch (error) { return error.code === "failed" ? error.message : error.code; }
            };
            const subframe = await document.querySelector("#child")?.contentWindow.webkit
              ?.messageHandlers.avibeNative.postMessage({method: "voice.holdSurfaces", params: \(surfaces)});
            const codes = [
              await code("voice.holdSurfaces", \(surfaces)),
              await code("voice.holdSurfaces", {surfaces: [{id: "composer"}]}),
              await code("voice.holdSurfaces", {}),
              await code("voice.holdAdopt", {take: "nothing"}),
              await code("voice.holdReject", {take: "nothing"}),
              await code("voice.holdAdopt", {}),
              subframe?.error?.code ?? (subframe ? "ok" : "none"),
            ];
            if (codes.every((value) => value === "forbidden" || value === "none")) return "forbidden";
            return JSON.stringify(codes);
            """
        for (html, base, watched, expected) in [
            (marked, "https://instance-one.example/chat", true,
             #"["ok","invalid_argument","invalid_argument","gone","ok","invalid_argument","forbidden"]"#),
            // A document that does not ask for voice consent names no
            // surface: no touch there opens the microphone.
            ("<!doctype html><title>t</title>", "https://instance-one.example/chat", true,
             #"["consent","invalid_argument","invalid_argument","consent","ok","invalid_argument","none"]"#),
            // A WebView whose touches the App does not see (a popup).
            (marked, "https://instance-one.example/chat", false,
             #"["unwatched","invalid_argument","invalid_argument","gone","ok","invalid_argument","forbidden"]"#),
            (marked, "https://elsewhere.example/chat", true, "forbidden"),
        ] {
            let audio = FakeAudio()
            let capture = makeCapture(audio)
            let webView = WKWebView(
                frame: .zero,
                configuration: NativeBridgeConfiguration.make(
                    instanceOrigin: instance,
                    setOuterScrollLocked: { _, _ in false },
                    voiceCapture: capture
                )
            )
            if watched { capture.watch(webView) }
            let loader = Loader()
            webView.navigationDelegate = loader
            try await loader.load(html, baseURL: URL(string: base)!, in: webView)
            let result = try await webView.callAsyncJavaScript(script, arguments: [:], in: nil, contentWorld: .page)
            XCTAssertEqual(result as? String, expected, base)
            // Only a surface that was taken makes the App follow a press.
            capture.touchDown(at: Self.onComposer, in: webView)
            XCTAssertEqual(capture.followsPress, expected.hasPrefix(#"["ok""#), "\(base) \(expected)")
            capture.touchEnded(in: webView)
        }
    }

    /// A list and then its withdrawal, sent one after the other: the list's
    /// consent answer comes back after the withdrawal was applied, and must
    /// not put the surface back.
    func testAWithdrawalSentRightAfterAListIsNotOvertakenByIt() async throws {
        let marked = #"<!doctype html><meta name="avibe-voice-consent" content="1"><title>t</title>"#
        let surfaces = #"{surfaces: [{id: "composer", x: 0, y: 700, width: 390, height: 60}]}"#
        let audio = FakeAudio()
        let capture = makeCapture(audio)
        let webView = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: instance,
                setOuterScrollLocked: { _, _ in false },
                voiceCapture: capture
            )
        )
        capture.watch(webView)
        let loader = Loader()
        webView.navigationDelegate = loader
        try await loader.load(marked, baseURL: URL(string: "https://instance-one.example/chat")!, in: webView)
        let result = try await webView.callAsyncJavaScript(
            """
            const code = (method, params) => window.avibeNative.call(method, params).then(
              () => "ok",
              (error) => (error.code === "failed" ? error.message : error.code),
            );
            const named = code("voice.holdSurfaces", \(surfaces));
            const withdrawn = code("voice.holdSurfaces", {surfaces: []});
            const codes = [await named, await withdrawn];
            // A threshold over half a second is refused, with its list.
            codes.push(await code("voice.holdSurfaces", {...\(surfaces), holdMs: 501}));
            codes.push(await code("voice.holdSurfaces", {...\(surfaces), holdMs: 500}));
            return JSON.stringify(codes);
            """,
            arguments: [:], in: nil, contentWorld: .page
        )
        XCTAssertEqual(result as? String, #"["superseded","ok","invalid_argument","ok"]"#)
        // The last call stands: a list named on its own is applied, with
        // its threshold.
        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertTrue(capture.followsPress)
        XCTAssertEqual(timers.last?.delay, 0.5)
        capture.touchEnded(in: webView)

        let again = try await webView.callAsyncJavaScript(
            """
            const code = (method, params) => window.avibeNative.call(method, params).then(
              () => "ok",
              (error) => (error.code === "failed" ? error.message : error.code),
            );
            const named = code("voice.holdSurfaces", \(surfaces));
            const withdrawn = code("voice.holdSurfaces", {surfaces: []});
            return JSON.stringify([await named, await withdrawn]);
            """,
            arguments: [:], in: nil, contentWorld: .page
        )
        XCTAssertEqual(again as? String, #"["superseded","ok"]"#)
        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertFalse(capture.followsPress)
    }

    func testOnlyTheInstancesOwnDocumentReachesTheMicrophone() async throws {
        let audio = FakeAudio()
        let capture = makeCapture(audio)
        let marked = #"<!doctype html><meta name="avibe-voice-consent" content="1"><title>t</title>"#
        for (base, html, expected) in [
            ("https://elsewhere.example", marked, "forbidden"),
            // As WebKit's own microphone: a page that does not ask each account
            // first (an older Workbench, a Show Page on its own) gets none.
            ("https://instance-one.example", "<!doctype html><title>t</title>", "consent"),
            ("https://instance-one.example", marked, "ok"),
        ] {
            let webView = WKWebView(
                frame: .zero,
                configuration: NativeBridgeConfiguration.make(
                    instanceOrigin: instance,
                    setOuterScrollLocked: { _, _ in false },
                    voiceCapture: capture
                )
            )
            let loader = Loader()
            webView.navigationDelegate = loader
            try await loader.load(html, baseURL: URL(string: base)!, in: webView)
            let result = try await webView.callAsyncJavaScript(
                """
                if (!window.avibeNative) return "forbidden";
                try {
                  await window.avibeNative.call("voice.capturePrepare");
                  const started = await window.avibeNative.call("voice.captureStart", { take: "t1" });
                  await window.avibeNative.call("voice.captureStop", { take: "t1" });
                  return started.sampleRate === 16000 ? "ok" : JSON.stringify(started);
                } catch (error) {
                  return error.code === "failed" ? error.message : error.code;
                }
                """,
                arguments: [:],
                in: nil,
                contentWorld: .page
            )
            XCTAssertEqual(result as? String, expected, base)
        }
        // Both instance documents may ready the session (it starts nothing);
        // only the one that asks for consent first records.
        XCTAssertEqual(audio.calls, ["prepare", "prepare", "start", "stop"])
    }

    func testThePageHearsItsTakeAsEvents() async throws {
        let audio = FakeAudio()
        let capture = makeCapture(audio)
        let webView = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: instance,
                setOuterScrollLocked: { _, _ in false },
                voiceCapture: capture
            )
        )
        let loader = Loader()
        webView.navigationDelegate = loader
        try await loader.load(
            #"<!doctype html><meta name="avibe-voice-consent" content="1"><title>t</title>"#,
            baseURL: instance.url,
            in: webView
        )
        _ = try await webView.callAsyncJavaScript(
            """
            window.heard = [];
            window.avibeNative.on("voice.capture", (detail) => window.heard.push(detail));
            await window.avibeNative.call("voice.captureStart", { take: "t1" });
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        audio.onPCM?(Data([1, 0]))
        audio.onEnd?("route")
        drain()
        let heard = try await webView.callAsyncJavaScript(
            """
            for (let i = 0; i < 50 && window.heard.length < 2; i += 1) {
              await new Promise((resolve) => setTimeout(resolve, 20));
            }
            return JSON.stringify(window.heard);
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        XCTAssertEqual(
            heard as? String,
            #"[{"pcm":"AQA=","take":"t1"},{"ended":true,"reason":"route","take":"t1"}]"#
        )

        // A take still flushing when its document is replaced: the next
        // document hears none of it.
        _ = try await webView.callAsyncJavaScript(
            """
            window.heard = [];
            await window.avibeNative.call("voice.captureStart", { take: "t2" });
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        audio.onPCM?(Data([2, 0]))
        audio.onPCM?(Data([3, 0]))
        capture.stop(take: "t2")
        capture.stop(webView: webView)
        drain()
        let after = try await webView.callAsyncJavaScript(
            """
            await new Promise((resolve) => setTimeout(resolve, 100));
            return JSON.stringify(window.heard);
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        XCTAssertEqual(after as? String, #"[{"pcm":"AgA=","take":"t2"}]"#)
    }
}

@MainActor
private final class FakeAudio: NativeHoldCaptureAudio {
    var permissionGranted = true
    var failure: Error?
    var calls: [String] = []
    var onPCM: (@MainActor (Data) -> Void)?
    var onEnd: (@MainActor (String) -> Void)?
    var otherAudioPlaying = false
    var route = "Speaker"
    var routeIsBluetooth = false
    /// The input's start fails with this.
    var beginFailure: NativeHoldCapture.StartError?
    private(set) var settles = 0
    /// Also lists `settle` in `calls`, for the tests of its order.
    var listsSettle = false
    /// Answers to `arm` and `begin` wait for `answer()`.
    var holdsAnswers = false
    private var answers: [() -> Void] = []
    var pendingAnswers: Int { answers.count }

    /// Gives the waiting answers, oldest first.
    func answer() {
        let waiting = answers
        answers = []
        waiting.forEach { $0() }
    }

    private func respond(_ work: @escaping () -> Void) {
        if holdsAnswers { answers.append(work) } else { work() }
    }

    func arm(completion: @escaping @MainActor (Result<NativeHoldCapture.ArmTimings, NativeHoldCapture.StartError>) -> Void) {
        calls.append("arm")
        respond { completion(.success(.init(categoryMs: 7, activateMs: 12, prepareMs: 3))) }
    }

    func begin(
        onPCM: @escaping @MainActor (Data) -> Void,
        onEnd: @escaping @MainActor (String) -> Void,
        completion: @escaping @MainActor (Result<NativeHoldCapture.BeginTimings, NativeHoldCapture.StartError>) -> Void
    ) {
        let armed = calls.last == "arm"
        calls.append("begin")
        self.onPCM = onPCM
        self.onEnd = onEnd
        let failure = beginFailure
        respond {
            if let failure {
                completion(.failure(failure))
                return
            }
            completion(.success(.init(
                armed: armed ? nil : .init(categoryMs: 7, activateMs: 12, prepareMs: 3), startMs: 5
            )))
        }
    }

    func disarm() {
        calls.append("disarm")
    }

    /// How often the input was told that the page took its take.
    private(set) var takens = 0

    func taken() {
        takens += 1
    }

    func settle() {
        settles += 1
        if listsSettle { calls.append("settle") }
    }

    func prepare() {
        calls.append("prepare")
    }

    func start(
        onPCM: @escaping @MainActor (Data) -> Void,
        onEnd: @escaping @MainActor (String) -> Void
    ) throws -> NativeHoldCapture.Timings {
        if let failure { throw failure }
        calls.append("start")
        self.onPCM = onPCM
        self.onEnd = onEnd
        return .init(activateMs: 12, engineMs: 34, categoryMs: 7)
    }

    /// The input says it has stopped only when `finishStop()` is called.
    var holdsStop = false
    private var stops: [@MainActor () -> Void] = []

    func finishStop() {
        let waiting = stops
        stops = []
        waiting.forEach { $0() }
    }

    func stop(stopped: @escaping @MainActor () -> Void) {
        calls.append("stop")
        if holdsStop { stops.append(stopped) } else { stopped() }
    }
}

private final class Loader: NSObject, WKNavigationDelegate {
    private var continuation: CheckedContinuation<Void, Error>?

    @MainActor
    func load(_ html: String, baseURL: URL, in webView: WKWebView) async throws {
        try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            webView.loadHTMLString(html, baseURL: baseURL)
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        continuation?.resume()
        continuation = nil
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        continuation?.resume(throwing: error)
        continuation = nil
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        continuation?.resume(throwing: error)
        continuation = nil
    }
}
