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
        super.tearDown()
    }

    private var inBackground = false

    private func makeCapture(_ audio: FakeAudio) -> NativeHoldCapture {
        NativeHoldCapture(
            audio: audio,
            afterQueued: { [unowned self] in queued.append($0) },
            isInBackground: { [unowned self] in inBackground }
        )
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
        XCTAssertEqual(timings, .init(activateMs: 12, engineMs: 34))
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

    func testTheConverterTurnsTheInputInto16kHzMonoInt16() throws {
        let input = try XCTUnwrap(AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 1))
        let converter = try XCTUnwrap(NativeHoldPCMConverter(input: input))
        var samples: [Int16] = []
        // Two buffers in a row, as the input's tap delivers them.
        for part in 0..<2 {
            let buffer = try XCTUnwrap(AVAudioPCMBuffer(pcmFormat: input, frameCapacity: 4_800))
            buffer.frameLength = 4_800
            for index in 0..<4_800 {
                let time = Double(part * 4_800 + index) / 48_000
                buffer.floatChannelData![0][index] = Float(0.5 * sin(2 * .pi * 440 * time))
            }
            let data = try XCTUnwrap(converter.convert(buffer))
            samples += data.withUnsafeBytes { Array($0.bindMemory(to: Int16.self)) }
        }
        // 0.2 s at 16 kHz, give or take the resampler's latency.
        XCTAssertGreaterThan(samples.count, 3_000)
        XCTAssertLessThanOrEqual(samples.count, 3_200)
        let peak = samples.map { abs(Int($0)) }.max() ?? 0
        XCTAssertGreaterThan(peak, 12_000)
        XCTAssertLessThan(peak, 20_000)
    }

    func testOnlyTheInstancesOwnDocumentReachesTheMicrophone() async throws {
        let audio = FakeAudio()
        let capture = makeCapture(audio)
        for (base, expected) in [
            ("https://elsewhere.example", "forbidden"),
            ("https://instance-one.example", "ok"),
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
            try await loader.load("<!doctype html><title>t</title>", baseURL: URL(string: base)!, in: webView)
            let result = try await webView.callAsyncJavaScript(
                """
                if (!window.avibeNative) return "forbidden";
                try {
                  await window.avibeNative.call("voice.capturePrepare");
                  const started = await window.avibeNative.call("voice.captureStart", { take: "t1" });
                  await window.avibeNative.call("voice.captureStop", { take: "t1" });
                  return started.sampleRate === 16000 ? "ok" : JSON.stringify(started);
                } catch (error) {
                  return error.code;
                }
                """,
                arguments: [:],
                in: nil,
                contentWorld: .page
            )
            XCTAssertEqual(result as? String, expected, base)
        }
        XCTAssertEqual(audio.calls, ["prepare", "start", "stop"])
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
        try await loader.load("<!doctype html><title>t</title>", baseURL: instance.url, in: webView)
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
    }
}

@MainActor
private final class FakeAudio: NativeHoldCaptureAudio {
    var permissionGranted = true
    var failure: Error?
    var calls: [String] = []
    var onPCM: (@MainActor (Data) -> Void)?
    var onEnd: (@MainActor (String) -> Void)?

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
        return .init(activateMs: 12, engineMs: 34)
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
