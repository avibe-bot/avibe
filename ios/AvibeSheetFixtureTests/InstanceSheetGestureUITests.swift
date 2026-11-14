import XCTest

final class InstanceSheetGestureUITests: XCTestCase {
    private var app: XCUIApplication!

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
        app = XCUIApplication()
        app.launch()
    }

    func testMidListDownwardDragScrollsBeforeTheSheetDismisses() throws {
        let close = app.buttons["Close"]
        XCTAssertTrue(close.waitForExistence(timeout: 10))
        let initialOffset = app.staticTexts["scroll-offset-0"]
        XCTAssertTrue(initialOffset.waitForExistence(timeout: 10))

        app.swipeUp(velocity: .slow)
        let scrolled = app.staticTexts.matching(
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
        let close = app.buttons["Close"]
        XCTAssertTrue(close.waitForExistence(timeout: 10))
        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.35))
        let end = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.9))
        start.press(forDuration: 0.1, thenDragTo: end, withVelocity: .slow, thenHoldForDuration: 0)
        XCTAssertFalse(close.waitForExistence(timeout: 3))
    }

    func testFastFlickAtTheTopDismissesTheSheet() {
        let close = app.buttons["Close"]
        XCTAssertTrue(close.waitForExistence(timeout: 10))
        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.35))
        let end = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.95))
        start.press(forDuration: 0.01, thenDragTo: end, withVelocity: .fast, thenHoldForDuration: 0)
        XCTAssertFalse(close.waitForExistence(timeout: 3))
    }

    func testSheetSurvivesCompactHeightRotation() {
        let close = app.buttons["Close"]
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
        let state = app.staticTexts.matching(
            NSPredicate(format: "label BEGINSWITH 'scroll-offset-'")
        ).firstMatch
        guard state.exists else { return nil }
        return Int(state.label.dropFirst("scroll-offset-".count))
    }
}
