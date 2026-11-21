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
        clock = 0
        haptics = []
        notes = []
        holdEvents = []
        heard = []
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
    /// The capture's clock, in ms.
    private var clock = 0
    private var haptics: [String] = []
    private var notes: [String] = []
    private var holdEvents: [[String: Any]] = []
    /// What reached the page of a hold's take.
    private var heard: [NativeHoldCapture.Event] = []
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
            uptimeMs: { [unowned self] in clock },
            haptic: NativeHoldHaptic(
                prepare: { [unowned self] in haptics.append("prepare") },
                fire: { [unowned self] in haptics.append("fire") }
            ),
            startsAtTouchDown: { [unowned self] in startsAtTouchDown },
            noteTimings: { [unowned self] in notes.append($0) },
            sendHoldEvent: { [unowned self] _, detail in holdEvents.append(detail) },
            sendCaptureEvent: { [unowned self] _, _, event in heard.append(event) }
        )
    }

    /// Runs the pending timer of `delay` seconds (the hold's 0.2 s, the
    /// adoption wait's 3 s); false when there is none.
    @discardableResult
    private func fire(_ delay: TimeInterval) -> Bool {
        guard let index = timers.firstIndex(where: { !$0.cancelled && $0.delay == delay }) else { return false }
        let timer = timers.remove(at: index)
        timer.work()
        return true
    }

    private static let composer = NativeHoldCapture.Surface(
        id: "composer", rect: CGRect(x: 0, y: 700, width: 390, height: 60)
    )
    private static let onComposer = CGPoint(x: 100, y: 720)

    /// A capture whose page registered its composer as a hold surface.
    private func makeHoldCapture(_ audio: FakeAudio) -> (NativeHoldCapture, WKWebView) {
        let capture = makeCapture(audio)
        let webView = WKWebView(frame: .zero)
        capture.setSurfaces([Self.composer], for: webView)
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

    /// A press on the composer held to recognition; the take's id.
    private func hold(_ capture: NativeHoldCapture, in webView: WKWebView) throws -> String {
        capture.touchDown(at: Self.onComposer, in: webView)
        clock += 200
        XCTAssertTrue(fire(NativeHoldCapture.holdSeconds))
        return try XCTUnwrap(holdEvents.last?["take"] as? String)
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
        XCTAssertEqual(NativeShellMetadata.capabilities["voiceHold"], 1)
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
        XCTAssertFalse(fire(NativeHoldCapture.holdSeconds))
        XCTAssertEqual(haptics, ["prepare"])
        XCTAssertTrue(holdEvents.isEmpty)
        XCTAssertTrue(heard.isEmpty)
        XCTAssertEqual(notes.count, 1)
        XCTAssertTrue(try XCTUnwrap(notes.first).hasPrefix("hold quiet Speaker"), notes.first ?? "")
        XCTAssertTrue(try XCTUnwrap(notes.first).hasSuffix("end=tap"), notes.first ?? "")
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
        XCTAssertTrue(fire(NativeHoldCapture.holdSeconds))

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
        // Taken once.
        assertGone(capture.adopt(take: take, webView: webView))
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
        XCTAssertTrue(try XCTUnwrap(notes.last).contains(" rec=200 hap=200 start=5 live=200 gap=0 "), notes.last ?? "")
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
        XCTAssertTrue(fire(NativeHoldCapture.adoptionWait))
        drain()
        XCTAssertNil(capture.currentTake)
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
            XCTAssertTrue(try XCTUnwrap(notes.last).hasPrefix("hold late:\(why) "), notes.last ?? "")

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
            capture.stop(take: take)
            drain()
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
        XCTAssertTrue(try XCTUnwrap(notes.last).hasSuffix("end=moved"), notes.last ?? "")
        XCTAssertFalse(fire(NativeHoldCapture.holdSeconds))
        XCTAssertEqual(haptics, ["prepare"])

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
        XCTAssertFalse(fire(NativeHoldCapture.holdSeconds))
    }

    /// The variant behind the switch: the input starts at touch-down, and a
    /// touch that is not a hold drops what it recorded in the App.
    func testStartingAtTouchDownDropsATapsAudioAndKeepsAHolds() throws {
        startsAtTouchDown = true
        let audio = FakeAudio()
        let (capture, webView) = makeHoldCapture(audio)

        capture.touchDown(at: Self.onComposer, in: webView)
        XCTAssertEqual(audio.calls, ["begin"])
        audio.onPCM?(Data([9, 0]))
        clock = 90
        capture.touchEnded(in: webView)
        drain()
        XCTAssertEqual(audio.calls, ["begin", "stop"])
        XCTAssertTrue(heard.isEmpty)
        XCTAssertTrue(holdEvents.isEmpty)
        XCTAssertEqual(haptics, ["prepare"])
        XCTAssertTrue(try XCTUnwrap(notes.last).hasPrefix("hold early "), notes.last ?? "")
        XCTAssertTrue(try XCTUnwrap(notes.last).hasSuffix("end=tap"), notes.last ?? "")

        clock = 1_000
        capture.touchDown(at: Self.onComposer, in: webView)
        audio.onPCM?(Data([1, 0]))
        clock = 1_200
        XCTAssertTrue(fire(NativeHoldCapture.holdSeconds))
        let take = try XCTUnwrap(holdEvents.last?["take"] as? String)
        let timings = try capture.adopt(take: take, webView: webView).get()
        // What was said before the cue is the take's.
        XCTAssertEqual(heard, [.pcm(Data([1, 0]))])
        XCTAssertEqual(timings["variant"] as? String, "early")
        XCTAssertEqual(timings["liveAt"] as? Int, 0)
        XCTAssertEqual(timings["cueToLive"] as? Int, -200)
        capture.stop(take: take)
        drain()
    }

    func testTheSwitchForStartingAtTouchDownIsForInternalBuildsOnly() throws {
        let suite = "NativeHoldEarlySwitch.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let on = try XCTUnwrap(URL(string: "avibe-diagnostics://voice/hold-early?on=1"))
        let off = try XCTUnwrap(URL(string: "avibe-diagnostics://voice/hold-early?on=0"))

        XCTAssertFalse(NativeHoldEarlySwitch.isOn(in: defaults, internalBuild: true))
        XCTAssertFalse(NativeHoldEarlySwitch.handle(on, allowed: false, defaults: defaults))
        XCTAssertFalse(NativeHoldEarlySwitch.isOn(in: defaults, internalBuild: true))
        XCTAssertTrue(NativeHoldEarlySwitch.handle(on, allowed: true, defaults: defaults))
        XCTAssertTrue(NativeHoldEarlySwitch.isOn(in: defaults, internalBuild: true))
        // An App Store build ignores what a TestFlight build had set.
        XCTAssertFalse(NativeHoldEarlySwitch.isOn(in: defaults, internalBuild: false))
        XCTAssertTrue(NativeHoldEarlySwitch.handle(off, allowed: true, defaults: defaults))
        XCTAssertFalse(NativeHoldEarlySwitch.isOn(in: defaults, internalBuild: true))
        XCTAssertFalse(NativeHoldEarlySwitch.handle(
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
        let invalid: [Any?] = [
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
        for (html, base, expected) in [
            (marked, "https://instance-one.example/chat",
             #"["ok","invalid_argument","invalid_argument","gone","ok","invalid_argument","forbidden"]"#),
            ("<!doctype html><title>t</title>", "https://instance-one.example/chat",
             #"["ok","invalid_argument","invalid_argument","consent","ok","invalid_argument","none"]"#),
            (marked, "https://elsewhere.example/chat", "forbidden"),
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
            let loader = Loader()
            webView.navigationDelegate = loader
            try await loader.load(html, baseURL: URL(string: base)!, in: webView)
            let result = try await webView.callAsyncJavaScript(script, arguments: [:], in: nil, contentWorld: .page)
            XCTAssertEqual(result as? String, expected, base)
            // Named from its own main frame only: a press there is followed.
            capture.touchDown(at: Self.onComposer, in: webView)
            XCTAssertEqual(capture.followsPress, expected != "forbidden", base)
            capture.touchEnded(in: webView)
        }
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
        respond {
            completion(.success(.init(
                armed: armed ? nil : .init(categoryMs: 7, activateMs: 12, prepareMs: 3), startMs: 5
            )))
        }
    }

    func disarm() {
        calls.append("disarm")
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

    func stop() {
        calls.append("stop")
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
