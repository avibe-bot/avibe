import XCTest

final class InstanceSheetGestureUITests: XCTestCase {
    private var app: XCUIApplication!

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
        app = XCUIApplication()
    }

    /// Runs the formerly failing first matrix cell in ordinary simulator CI.
    /// A native ready signal must not make absent required web text pass.
    func testColdRealBundleNeedsItsOwnWebContentAfterNativeReadiness() {
        app.launchArguments = ["-AppleLanguages", "(en)", "-AppleLocale", "en_US"]
        app.launchEnvironment["AVIBE_FIXTURE_CAPTURE_STATE"] = "default"
        app.launchEnvironment["AVIBE_FIXTURE_CAPTURE_THEME"] = "light"
        launchFixture()
        XCTAssertTrue(SheetFixtureReadiness.waitForText("You’re in", in: app))
        XCTAssertTrue(app.webViews.firstMatch.buttons["Close and return to Studio"].waitForExistence(timeout: 10))
        XCTAssertFalse(SheetFixtureReadiness.waitForText(
            "This required text does not exist", in: app, timeout: 0.2
        ))
    }

    private func launchFixture() {
        app.launch()
        SheetFixtureReadiness.waitForDocument(in: app)
    }

    func testMidListDownwardDragScrollsBeforeTheSheetDismisses() throws {
        launchFixture()
        let close = app.webViews.firstMatch.buttons["Close"]
        XCTAssertTrue(close.waitForExistence(timeout: 10))
        let initialOffset = app.webViews.firstMatch.staticTexts["scroll-offset-0"]
        XCTAssertTrue(initialOffset.waitForExistence(timeout: 10))

        app.swipeUp(velocity: .slow)
        let scrolled = app.webViews.firstMatch.staticTexts.matching(
            NSPredicate(format: "label BEGINSWITH 'scroll-offset-' AND label != 'scroll-offset-0'")
        ).firstMatch
        XCTAssertTrue(scrolled.waitForExistence(timeout: 5))
        let offsetBeforeDrag = try XCTUnwrap(scrollOffset())
        XCTAssertGreaterThan(offsetBeforeDrag, 0)

        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.42))
        let end = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.78))
        start.press(forDuration: 0.1, thenDragTo: end, withVelocity: .slow, thenHoldForDuration: 0)

        let movedBackWithinTheList = XCTNSPredicateExpectation(
            predicate: NSPredicate { [self] _, _ in
                guard let offset = scrollOffset() else { return false }
                return offset > 0 && offset < offsetBeforeDrag
            },
            object: nil
        )
        XCTAssertEqual(
            XCTWaiter.wait(for: [movedBackWithinTheList], timeout: 5),
            .completed
        )
        XCTAssertTrue(close.exists)
    }

    func testTopDownwardDragDismissesTheSheet() {
        launchFixture()
        let close = app.webViews.firstMatch.buttons["Close"]
        XCTAssertTrue(close.waitForExistence(timeout: 10))
        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.35))
        let end = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.9))
        start.press(forDuration: 0.1, thenDragTo: end, withVelocity: .slow, thenHoldForDuration: 0)
        XCTAssertFalse(close.waitForExistence(timeout: 3))
    }

    func testFastFlickAtTheTopDismissesTheSheet() {
        launchFixture()
        let close = app.webViews.firstMatch.buttons["Close"]
        XCTAssertTrue(close.waitForExistence(timeout: 10))
        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.35))
        let end = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.95))
        start.press(forDuration: 0.01, thenDragTo: end, withVelocity: .fast, thenHoldForDuration: 0)
        XCTAssertFalse(close.waitForExistence(timeout: 3))
    }

    func testSheetSurvivesCompactHeightRotation() {
        launchFixture()
        let close = app.webViews.firstMatch.buttons["Close"]
        XCTAssertTrue(close.waitForExistence(timeout: 10))
        let document = app.webViews.firstMatch
        XCTAssertTrue(document.waitForExistence(timeout: 5))
        let portraitFrame = document.frame
        XCTAssertGreaterThan(portraitFrame.height, portraitFrame.width)

        XCUIDevice.shared.orientation = .landscapeLeft
        let compactHeight = XCTNSPredicateExpectation(
            predicate: NSPredicate { _, _ in
                let frame = document.frame
                return frame.width > portraitFrame.width && frame.height < portraitFrame.height
            },
            object: nil
        )
        XCTAssertEqual(XCTWaiter.wait(for: [compactHeight], timeout: 5), .completed)
        XCTAssertTrue(close.exists)

        XCUIDevice.shared.orientation = .portrait
        let restoredPortrait = XCTNSPredicateExpectation(
            predicate: NSPredicate { _, _ in
                let frame = document.frame
                return abs(frame.width - portraitFrame.width) < 1
                    && abs(frame.height - portraitFrame.height) < 1
            },
            object: nil
        )
        XCTAssertEqual(XCTWaiter.wait(for: [restoredPortrait], timeout: 5), .completed)
        XCTAssertTrue(close.exists)
    }

    private func scrollOffset() -> Int? {
        let state = app.webViews.firstMatch.staticTexts.matching(
            NSPredicate(format: "label BEGINSWITH 'scroll-offset-'")
        ).firstMatch
        guard state.exists else { return nil }
        return Int(state.label.dropFirst("scroll-offset-".count))
    }
}

/// Keep WebKit process/document startup separate from the short web-state
/// assertions. The native marker is fixture-only and leaves the web subtree
/// exposed; content is still checked through a fresh, scoped AX query.
enum SheetFixtureReadiness {
    static func waitForDocument(
        in app: XCUIApplication,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let loaded = XCTNSPredicateExpectation(
            predicate: NSPredicate { _, _ in
                let surface = app.otherElements["fixture-document-surface"]
                return surface.exists
                    && (surface.value as? String)?.hasPrefix("document-ready;") == true
            }, object: nil
        )
        // Same process-launch budget as AvibeTests.WebKitReadiness. The web
        // content assertions below retain their existing 10-second budget.
        XCTAssertEqual(XCTWaiter.wait(for: [loaded], timeout: 120), .completed,
                       "Fixture did not load its bundled document", file: file, line: line)
    }

    static func waitForText(
        _ text: String, in app: XCUIApplication, timeout: TimeInterval = 10
    ) -> Bool {
        let visible = XCTNSPredicateExpectation(
            predicate: NSPredicate { _, _ in
                // Re-resolve on each snapshot, and match the label rather
                // than relying on app-wide identifier resolution into a
                // different WebContent process.
                app.webViews.firstMatch.staticTexts.matching(
                    NSPredicate(format: "label == %@", text)
                ).firstMatch.exists
            }, object: nil
        )
        return XCTWaiter.wait(for: [visible], timeout: timeout) == .completed
    }
}
