import XCTest

/// Real touches on a page in a WebView set up as the instance WebView is for
/// hold-to-talk: the production bridge, touch observer and hold capture
/// (`FixtureHoldPage.swift`). The page is a stand-in that speaks the
/// Workbench's protocol and treats its input as the composer does (no
/// default on a press it may hold, focus in `touchend`).
///
/// What this can show, and no unit test can: that the App's observer on the
/// WebView cancels and delays nothing. A tap still focuses, a drag still
/// scrolls, buttons and a sheet still get their own touches, and a hold ends
/// cleanly however the microphone answers.
///
/// The Simulator has no usable microphone on CI. With the App's default
/// (the microphone starts at touch-down) every touch on a surface there ends
/// as the App's `failed`, so those tests accept that; the same touches are
/// run with the microphone starting at recognition, where a tap is counted
/// as a tap and a hold is recognised whatever the microphone does. With a
/// microphone a hold is recorded and sent. Audio itself is a device check.
final class HoldTouchUITests: XCTestCase {
    private var app: XCUIApplication!

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchArguments = ["-AppleLanguages", "(en)", "-AppleLocale", "en_US"]
        app.launchEnvironment["AVIBE_FIXTURE_HOLD"] = "1"
    }

    override func tearDown() {
        // What the page and the App said at the end, pass or fail: the run's record.
        print("[hold smoke] \(name): \(facts())")
        super.tearDown()
    }

    // MARK: The page's own touches

    func testATapFocusesTheInputAndTheKeyboardTypesIntoIt() {
        tapTheInput(startsAtRecognition: false)
    }

    func testATapFocusesTheInputWhenTheMicrophoneStartsAtRecognition() {
        tapTheInput(startsAtRecognition: true)
        // Counted as a tap on a hold surface, with nothing said to the page.
        requireEvidence("hold taps", "The App did not count the tap")
        XCTAssertEqual(label(beginning: "events="), "events=", "A tap is no hold; \(facts())")
    }

    private func tapTheInput(startsAtRecognition: Bool) {
        launch(startsAtRecognition: startsAtRecognition)
        input.tap()
        XCTAssertTrue(waitFor("focus=yes"), "A tap on the input did not focus it; \(facts())")
        // Typing reaches it: the keyboard is up, or a hardware keyboard stands in for it.
        app.typeText("hi")
        XCTAssertTrue(waitFor("typed=2"), "Typing did not reach the input; \(facts())")
        print("[hold smoke] software keyboard shown: \(app.keyboards.firstMatch.waitForExistence(timeout: 3))")
        // The App followed that touch (or the test shows nothing about its observer).
        requireEvidence("hold ", "The App did not follow the tap")
        XCTAssertFalse(label(beginning: "events=")?.contains("recognised") ?? true, "A tap was taken for a hold; \(facts())")
        // Focused, the input is no hold surface, as a composer with its keyboard up.
        XCTAssertTrue(waitFor("surfaces=plain"), facts())
    }

    func testTheButtonBesideTheInputTakesShortAndLongPresses() {
        launch()
        let send = web.buttons["Send"]
        send.tap()
        XCTAssertTrue(waitFor("send=1/0"), facts())
        send.press(forDuration: 1.0)
        XCTAssertTrue(waitFor("send=1/1"), facts())
        XCTAssertEqual(label(beginning: "events="), "events=", facts())
        XCTAssertEqual(label(beginning: "state="), "state=idle", facts())
        // A button is no hold surface: the App followed nothing.
        XCTAssertEqual(kept(), "none")
    }

    /// The control for the drags below: a box the page did not name, so the
    /// App follows nothing on it.
    func testADragOnABoxThatIsNoSurfaceScrollsThePage() {
        launch()
        web.staticTexts["Control box"].swipeUp()
        XCTAssertTrue(waitFor("scrolled=yes"), "A drag on an unnamed box did not scroll; \(facts())")
        XCTAssertEqual(kept(), "none", "The App followed a touch on a box that is no surface; \(facts())")
    }

    /// A slower drag, as a finger's: down, a moment, then up the page.
    func testASlowDragThatStartsOnAHoldSurfaceScrollsThePage() {
        launch()
        let start = web.staticTexts["Plain surface"].coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: 0, dy: -220)))
        XCTAssertTrue(waitFor("scrolled=yes"), "A slow drag that began on a hold surface did not scroll; \(facts())")
        requireEvidence("hold ", "The App did not follow the drag")
        XCTAssertFalse(label(beginning: "events=")?.contains("recognised") ?? true, "A scroll was taken for a hold; \(facts())")
    }

    func testADragThatStartsOnAHoldSurfaceScrollsThePage() {
        dragOnTheSurface(startsAtRecognition: false)
    }

    func testADragScrollsWhenTheMicrophoneStartsAtRecognition() {
        dragOnTheSurface(startsAtRecognition: true)
        // The App saw the touch move away: a scroll, not a hold, with nothing said to the page.
        requireEvidence("moved=", "The App did not count the drag as one")
        XCTAssertEqual(label(beginning: "events="), "events=", "A scroll is no hold; \(facts())")
    }

    private func dragOnTheSurface(startsAtRecognition: Bool) {
        launch(startsAtRecognition: startsAtRecognition)
        // Named as a hold surface, with nothing of the page's in the touch's way.
        web.staticTexts["Plain surface"].swipeUp()
        XCTAssertTrue(waitFor("scrolled=yes"), "A drag that began on a hold surface did not scroll; \(facts())")
        requireEvidence("hold ", "The App did not follow the drag")
        XCTAssertFalse(label(beginning: "events=")?.contains("recognised") ?? true, "A scroll was taken for a hold; \(facts())")
    }

    func testASheetOverTheInputTakesItsOwnTouches() {
        launch()
        let frame = input.frame
        let spot = app.coordinate(withNormalizedOffset: .zero)
            .withOffset(CGVector(dx: frame.midX, dy: frame.midY))
        web.buttons["Open sheet"].tap()
        XCTAssertTrue(waitFor("surfaces=none"), facts())
        // Where the input lies, under the sheet.
        spot.tap()
        XCTAssertTrue(waitFor("sheet=1"), "The sheet did not get the tap over the input; \(facts())")
        spot.press(forDuration: 0.8)
        XCTAssertEqual(label(beginning: "events="), "events=", "A press on the sheet was taken for a hold; \(facts())")
        XCTAssertEqual(label(beginning: "state="), "state=idle", facts())
        // The page named no surface while the sheet was up: the App followed nothing.
        XCTAssertEqual(kept(), "none")
        web.buttons["Close sheet"].tap()
        XCTAssertTrue(waitFor("surfaces=input+plain"), facts())
    }

    // MARK: A hold

    func testAHoldEndsCleanlyAndTheInputStillWorks() {
        launch()
        holdTheInputAndRequireACleanEnd(count: 1)
        requireTheInputStillWorks()
    }

    /// The variant behind the App's switch: prepared at touch-down, started
    /// when the hold is recognised. The App's word then comes whatever the
    /// microphone does.
    func testAHoldEndsCleanlyWhenTheMicrophoneStartsAtRecognition() {
        launch(startsAtRecognition: true)
        holdTheInputAndRequireACleanEnd(count: 1)
        XCTAssertTrue(
            label(beginning: "events=")?.contains("recognised") == true,
            "The App did not recognise the hold; \(facts())"
        )
        requireTheInputStillWorks()
    }

    func testSlidingUpAndReleasingCancels() {
        launch()
        let start = input.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.8, thenDragTo: start.withOffset(CGVector(dx: 0, dy: -150)))
        XCTAssertEqual(
            waitForLabel(beginning: "holds=1:"), "holds=1:cancelled",
            "Sliding up and releasing did not cancel; \(facts())"
        )
        XCTAssertTrue(waitFor("state=idle"), facts())
        requireEvidence(" end=", "The App did not follow the hold")
    }

    func testAfterTheKeyboardCameAndWentAHoldStillLands() {
        launch()
        input.tap()
        XCTAssertTrue(waitFor("focus=yes"), facts())
        XCTAssertTrue(waitFor("surfaces=plain"), facts())
        web.buttons["Hide keyboard"].tap()
        XCTAssertTrue(waitFor("focus=no"), facts())
        // Named again, where it now is.
        XCTAssertTrue(waitFor("surfaces=input+plain"), facts())
        holdTheInputAndRequireACleanEnd(count: 1)
    }

    /// The App recognises a hold where the page has no press of its own (a
    /// surface the page named and does not hold): the page refuses the take,
    /// and nothing is left behind.
    func testAHoldThePageDoesNotSeeIsRefusedAndLeavesNothingBehind() {
        launch(startsAtRecognition: true)
        web.staticTexts["Plain surface"].press(forDuration: 1.0)
        _ = waitForLabel(beginning: "events=recognised")
        let refused = XCTNSPredicateExpectation(
            predicate: NSPredicate { [self] _, _ in label(beginning: "events=")?.contains("refused") == true },
            object: nil
        )
        XCTAssertEqual(XCTWaiter.wait(for: [refused], timeout: 5), .completed, "The page did not refuse the hold; \(facts())")
        XCTAssertEqual(label(beginning: "state="), "state=idle", facts())
        // Dropped by the App: refused by the page, or (with no microphone) failed first.
        requireEvidence(" end=", "The App did not drop the refused take")
        requireTheInputStillWorks()
    }

    // MARK: Helpers

    private var web: XCUIElement { app.webViews.firstMatch }
    private var input: XCUIElement { web.textViews["Message"] }

    private func launch(startsAtRecognition: Bool = false) {
        if startsAtRecognition {
            // `NativeHoldQuietSwitch`, as its diagnostics link sets it.
            app.launchArguments += ["-avibe.voiceHold.startsAtRecognition", "YES"]
        }
        app.launch()
        // Where the microphone was not allowed before the run, the system asks.
        let allow = XCUIApplication(bundleIdentifier: "com.apple.springboard").buttons["Allow"]
        if allow.waitForExistence(timeout: 2) { allow.tap() }
        XCTAssertTrue(
            waitFor("ready=yes", timeout: SheetFixtureReadiness.webContentLaunchSeconds),
            "The hold page did not load"
        )
        XCTAssertTrue(waitFor("surfaces=input+plain"), "The App did not take the page's surfaces; \(facts())")
        // The first touch after a launch is slow on a CI simulator (the fixture's other
        // tests warm up too): one on a line of text, which is nothing's surface.
        web.staticTexts["ready=yes"].tap()
        XCTAssertTrue(waitFor("warm=yes"), "The warm-up touch did not reach the page; \(facts())")
    }

    private func waitFor(_ text: String, timeout: TimeInterval = 10) -> Bool {
        SheetFixtureReadiness.waitForText(text, in: app, timeout: timeout)
    }

    /// The page's fact that begins with `prefix`, as it stands now.
    private func label(beginning prefix: String) -> String? {
        let match = web.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", prefix)).firstMatch
        return match.exists ? match.label : nil
    }

    private func waitForLabel(beginning prefix: String, timeout: TimeInterval = 10) -> String? {
        let found = XCTNSPredicateExpectation(
            predicate: NSPredicate { [self] _, _ in label(beginning: prefix) != nil }, object: nil
        )
        _ = XCTWaiter.wait(for: [found], timeout: timeout)
        return label(beginning: prefix)
    }

    /// What the fixture publishes: what the App kept of the touches it followed
    /// (`NativeHoldTimings`), then each touch as UIKit delivered it.
    private func evidence() -> String {
        (app.otherElements["fixture-hold-evidence"].value as? String) ?? "missing"
    }

    /// Only what the App kept: `none` when it followed nothing.
    private func kept() -> String {
        evidence().components(separatedBy: " || ").first ?? "missing"
    }

    private func requireEvidence(
        _ part: String, _ message: String, file: StaticString = #filePath, line: UInt = #line
    ) {
        let found = XCTNSPredicateExpectation(
            predicate: NSPredicate { [self] _, _ in kept().contains(part) }, object: nil
        )
        XCTAssertEqual(
            XCTWaiter.wait(for: [found], timeout: 5), .completed,
            "\(message): no '\(part)' in what the App kept (\(kept())). "
                + "With 'none', check that the fixture has the microphone. \(facts())",
            file: file, line: line
        )
    }

    /// Everything the page says of itself, for a failure's message.
    private func facts() -> String {
        let names = ["surfaces=", "state=", "events=", "holds=", "focus=", "typed=", "send=", "sheet=", "scrolled=", "press=", "drag="]
        return "page: " + names.compactMap { label(beginning: $0) }.joined(separator: " ")
            + "; App: " + evidence()
    }

    /// Holds the input for a second and a half. However the microphone
    /// answered, the hold is over, counted once, and the page is at rest.
    private func holdTheInputAndRequireACleanEnd(
        count: Int, file: StaticString = #filePath, line: UInt = #line
    ) {
        input.press(forDuration: 1.5)
        let outcome = waitForLabel(beginning: "holds=\(count):")
        // Sent; or, with no microphone, the page's own start failed, or the App ended the take
        // it had started.
        XCTAssertTrue(
            ["sent", "failed", "ended"].map { "holds=\(count):\($0)" }.contains(outcome ?? ""),
            "A hold did not end as sent, failed or ended (got \(outcome ?? "nothing")); \(facts())",
            file: file, line: line
        )
        XCTAssertTrue(waitFor("state=idle"), facts(), file: file, line: line)
        XCTAssertNotEqual(
            label(beginning: "events="), "events=",
            "The App said nothing about the hold; \(facts())", file: file, line: line
        )
        // The App followed it: its own line for the hold, with how it ended.
        requireEvidence(" end=", "The App did not follow the hold", file: file, line: line)
    }

    /// Not stuck: a tap focuses the input and typing reaches it.
    private func requireTheInputStillWorks(file: StaticString = #filePath, line: UInt = #line) {
        input.tap()
        XCTAssertTrue(waitFor("focus=yes"), "After the hold a tap no longer focuses the input; \(facts())", file: file, line: line)
        app.typeText("ok")
        XCTAssertTrue(waitFor("typed=2"), facts(), file: file, line: line)
    }
}
