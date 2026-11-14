import XCTest

final class InstanceSheetGestureUITests: XCTestCase {
    private var app: XCUIApplication!

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
        app = XCUIApplication()
        app.launch()
    }

    func testMidListDownwardDragScrollsBeforeTheSheetDismisses() {
        let close = app.buttons["Close"]
        XCTAssertTrue(close.waitForExistence(timeout: 10))
        let initialOffset = app.staticTexts["scroll-offset-0"]
        XCTAssertTrue(initialOffset.waitForExistence(timeout: 10))

        app.swipeUp(velocity: .slow)
        let scrolled = app.staticTexts.matching(
            NSPredicate(format: "label BEGINSWITH 'scroll-offset-' AND label != 'scroll-offset-0'")
        ).firstMatch
        XCTAssertTrue(scrolled.waitForExistence(timeout: 5))

        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.42))
        let end = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.78))
        start.press(forDuration: 0.1, thenDragTo: end, withVelocity: .slow, thenHoldForDuration: 0)

        XCTAssertTrue(close.waitForExistence(timeout: 5))
        XCTAssertTrue(
            app.staticTexts.matching(
                NSPredicate(format: "label BEGINSWITH 'scroll-offset-' AND label != 'scroll-offset-0'")
            ).firstMatch.exists
        )
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
        XCUIDevice.shared.orientation = .landscapeLeft
        XCTAssertTrue(close.waitForExistence(timeout: 5))
        XCUIDevice.shared.orientation = .portrait
        XCTAssertTrue(close.waitForExistence(timeout: 5))
    }
}
