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
/// No check depends on a microphone. The CI Simulator has had a working one
/// in every run, and a hold there is recorded and sent. Where a simulator
/// has none, a touch on a surface ends as the App's `failed` with the App's
/// default (the microphone starts at touch-down), so those tests accept
/// that; the same touches are run with the microphone starting at
/// recognition, where a hold is recognised whatever the microphone does.
/// Audio itself is a device check.
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
        // What the page and the App said at the end, pass or fail: the run's record. A test
        // that launches the App more than once has printed each launch's, and ended the last.
        if app.state == .runningForeground {
            print("[hold smoke] \(name): \(facts())")
        }
        super.tearDown()
    }

    // MARK: The page's own touches

    func testATapFocusesTheInputAndTheKeyboardTypesIntoIt() {
        tapTheInput(startsAtRecognition: false)
    }

    func testATapFocusesTheInputWhenTheMicrophoneStartsAtRecognition() {
        tapTheInput(startsAtRecognition: true)
    }

    /// The first touch after a cold launch, with no warm-up: the App's main
    /// thread is busy from before the finger lifts until past the threshold.
    /// A tap all the same: no cue, nothing said to the page. (TestFlight
    /// 50.1 recognised a hold here.)
    ///
    /// And it focuses the input, though no ordinary tap has reached the
    /// WebView yet. On iOS 27 a page has no focus until one has, and the
    /// press on the input is not one (the page prevents its default): the
    /// App gives the page the focus at touch-down (`PageFocusTouchObserver`).
    /// Without that this tap raises no keyboard on iOS 27.
    func testTheFirstTapAfterAColdLaunchIsNoHold() {
        firstTap(startsAtRecognition: false)
    }

    func testTheFirstTapAfterAColdLaunchIsNoHoldWhenTheMicrophoneStartsAtRecognition() {
        firstTap(startsAtRecognition: true)
    }

    /// The control: the page names no surface, so the App's hold recognition
    /// follows nothing, and the page treats its input as before (no default
    /// on `touchstart`, the focus in `touchend`). The first tap focuses it
    /// all the same: the focus does not depend on the hold's path.
    func testTheFirstTapAfterAColdLaunchFocusesTheInputWithNoSurfaceNamed() {
        app.launchEnvironment["AVIBE_FIXTURE_HOLD_NO_SURFACES"] = "1"
        launch(warm: false, surfaces: "surfaces=control")
        requireTheFirstTapFocusesAndTypes("no surface named")
        XCTAssertEqual(kept(), "none", "The App followed a touch with no surface named; \(facts())")
    }

    private func firstTap(startsAtRecognition: Bool) {
        launch(startsAtRecognition: startsAtRecognition, warm: false)
        requireTheFirstTapFocusesAndTypes("startsAtRecognition=\(startsAtRecognition)")
        requireFollowedWithNoCue("The first tap after a cold launch")
    }

    /// The first tap on the input: it takes the focus, and typing reaches it. A focus event
    /// alone is not the keyboard: the input must have its input session too.
    private func requireTheFirstTapFocusesAndTypes(_ what: String) {
        input.tap()
        let focused = waitFor("focus=yes")
        var typed = false
        if focused {
            // Typing reaches it: the keyboard is up, or a hardware keyboard stands in for it.
            app.typeText("hi")
            typed = waitFor("typed=2")
        }
        // Long enough for a late cue to have come, and for the page's later reading of its focus.
        Thread.sleep(forTimeInterval: 2)
        print("[hold smoke] first tap, \(what), focused=\(focused), typed=\(typed): \(facts())")
        XCTAssertTrue(focused, "The first tap did not focus the input; \(facts())")
        XCTAssertTrue(typed, "The first tap focused the input, but typing did not reach it; \(facts())")
    }

    private func tapTheInput(startsAtRecognition: Bool) {
        launch(startsAtRecognition: startsAtRecognition)
        input.tap()
        XCTAssertTrue(waitFor("focus=yes"), "A tap on the input did not focus it; \(facts())")
        // Typing reaches it: the keyboard is up, or a hardware keyboard stands in for it.
        app.typeText("hi")
        XCTAssertTrue(waitFor("typed=2"), "Typing did not reach the input; \(facts())")
        print("[hold smoke] software keyboard shown: \(app.keyboards.firstMatch.waitForExistence(timeout: 3))")
        // No cue, though the tap raised the keyboard and the App learnt late that the finger lifted.
        requireFollowedWithNoCue("A tap")
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
        drag(from: web.staticTexts["Control box"])
        XCTAssertTrue(waitFor("scrolled=yes"), "A drag on an unnamed box did not scroll; \(facts())")
        XCTAssertEqual(kept(), "none", "The App followed a touch on a box that is no surface; \(facts())")
    }

    func testADragThatStartsOnAHoldSurfaceScrollsThePage() {
        dragOnTheSurface(startsAtRecognition: false)
    }

    func testADragScrollsWhenTheMicrophoneStartsAtRecognition() {
        dragOnTheSurface(startsAtRecognition: true)
    }

    private func dragOnTheSurface(startsAtRecognition: Bool) {
        launch(startsAtRecognition: startsAtRecognition)
        // Named as a hold surface, with nothing of the page's in the touch's way.
        drag(from: web.staticTexts["Plain surface"])
        XCTAssertTrue(waitFor("scrolled=yes"), "A drag that began on a hold surface did not scroll; \(facts())")
        // A scroll, not a hold.
        requireFollowedWithNoCue("A drag")
    }

    /// A drag as a finger makes one: down, a moment, then up the page.
    /// (XCUITest's own swipe arrives as a 17 ms touch with no moves on this
    /// simulator, which scrolls nothing anywhere.)
    private func drag(from element: XCUIElement) {
        let start = element.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: 0, dy: -220)))
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

    /// The input is a surface whose page takes the press for itself, as the
    /// composer does: UIKit then tells the App's observer of no move and no
    /// lift. The App still gives a finger that stays down its cue (it asks
    /// the touch, which must not read as lifted while it is down).
    func testAHoldOnTheInputGetsTheAppsOwnCue() {
        // With the microphone starting at recognition the cue comes whatever the microphone
        // does, so this holds on any simulator. With the default (it starts at touch-down), a
        // simulator with no microphone ends the press at its failed start, before any cue:
        // that is accepted, and anything else must be the cue.
        for startsAtRecognition in [true, false] {
            app = XCUIApplication()
            app.launchArguments = ["-AppleLanguages", "(en)", "-AppleLocale", "en_US"]
            app.launchEnvironment["AVIBE_FIXTURE_HOLD"] = "1"
            launch(startsAtRecognition: startsAtRecognition)
            input.press(forDuration: 1.2)
            _ = waitForLabel(beginning: "holds=1:")
            print("[hold smoke] cue, startsAtRecognition=\(startsAtRecognition): \(facts())")
            let events = label(beginning: "events=") ?? ""
            if !startsAtRecognition, events.hasPrefix("events=failed") {
                app.terminate()
                continue
            }
            XCTAssertTrue(events.hasPrefix("events=recognised"), "The App did not give the hold its cue; \(facts())")
            requireEvidence(" rec=", "The App did not recognise the hold")
            app.terminate()
        }
    }

    /// A finger that comes down on the input, moves away and stays down past
    /// the threshold: a drag, which the page gave back. The App is told of
    /// no move there, and gives no cue all the same.
    ///
    /// With the microphone starting at recognition as well: there the press
    /// is still waiting for its cue when the threshold comes whatever the
    /// microphone did, so only the App's asking the touch where it is keeps
    /// the cue from it.
    func testADragOnTheInputHeldPastTheThresholdGetsNoCue() {
        for startsAtRecognition in [true, false] {
            app = XCUIApplication()
            app.launchArguments = ["-AppleLanguages", "(en)", "-AppleLocale", "en_US"]
            app.launchEnvironment["AVIBE_FIXTURE_HOLD"] = "1"
            launch(startsAtRecognition: startsAtRecognition)
            let start = input.coordinate(withNormalizedOffset: CGVector(dx: 0.3, dy: 0.5))
            start.press(
                forDuration: 0.05, thenDragTo: start.withOffset(CGVector(dx: 60, dy: 0)),
                withVelocity: .default, thenHoldForDuration: 0.8
            )
            XCTAssertTrue(waitFor("state=idle"), facts())
            print("[hold smoke] drag on the input, startsAtRecognition=\(startsAtRecognition): \(facts())")
            requireFollowedWithNoCue("A drag on the input")
            app.terminate()
        }
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

    private func launch(
        startsAtRecognition: Bool = false, warm: Bool = true, surfaces: String = "surfaces=input+plain"
    ) {
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
        XCTAssertTrue(waitFor(surfaces), "The App did not take the page's surfaces; \(facts())")
        guard warm else { return }
        // The first touch after a launch is slow on a CI simulator, and can be lost on its way
        // to the page (the fixture's other tests warm up too): one on a line of text, which is
        // nothing's surface, again until the page has seen it.
        for _ in 0..<3 {
            web.staticTexts["ready=yes"].tap()
            if waitFor("warm=yes", timeout: 4) { return }
        }
        XCTFail("No warm-up touch reached the page; \(facts())")
    }

    /// The App followed the touch, and it was no hold: no cue was given (nothing recognised
    /// by the App, no word of a hold to the page). How the press ended is the App's to say:
    /// counted as a tap or a move; `withdrawn`, when the page took the surface back first (a
    /// tap focuses the input, which is then no hold surface); or, on a simulator with no
    /// microphone, the failed start.
    private func requireFollowedWithNoCue(
        _ what: String, file: StaticString = #filePath, line: UInt = #line
    ) {
        // A line of the App's own following, which only its observer's path writes: the page's
        // own start leaves `hold page …`, and proves nothing of the observer.
        let own = ["hold taps", "hold early", "hold quiet", "hold late"]
        let followed = XCTNSPredicateExpectation(
            predicate: NSPredicate { [self] _, _ in own.contains { kept().contains($0) } }, object: nil
        )
        XCTAssertEqual(
            XCTWaiter.wait(for: [followed], timeout: 5), .completed,
            "The App did not follow the touch: none of \(own) in what it kept (\(kept())). "
                + "With 'none', check that the fixture has the microphone. \(facts())",
            file: file, line: line
        )
        XCTAssertFalse(kept().contains(" rec="), "\(what) was recognised as a hold by the App; \(facts())", file: file, line: line)
        XCTAssertFalse(
            label(beginning: "events=")?.contains("recognised") ?? true,
            "\(what) was taken for a hold; \(facts())", file: file, line: line
        )
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
        let names = ["surfaces=", "state=", "events=", "holds=", "focus=", "typed=", "send=", "sheet=", "scrolled=", "press=", "drag=", "tap="]
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
