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

    /// Exercise the same single-tap path and real bridge receipt as the matrix.
    func testOpenFailedSelectionReportsNativeRejectionAndRendersRowError() {
        app.launchArguments = ["-AppleLanguages", "(en)", "-AppleLocale", "en_US"]
        app.launchEnvironment["AVIBE_FIXTURE_CAPTURE_STATE"] = "open-failed"
        app.launchEnvironment["AVIBE_FIXTURE_CAPTURE_THEME"] = "light"
        launchFixture()
        SheetFixtureReadiness.tapRow(containing: "Lab", in: app)
        SheetFixtureReadiness.requireFailedSelection(in: app)
    }

    private func launchFixture() {
        app.launch()
        SheetFixtureReadiness.waitForDocument(in: app)
    }

    func testMidListDownwardDragScrollsBeforeTheSheetDismisses() throws {
        launchFixture()
        _ = SheetFixtureReadiness.waitForGeometry(in: app)
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
        _ = SheetFixtureReadiness.waitForGeometry(in: app)
        let close = app.webViews.firstMatch.buttons["Close"]
        XCTAssertTrue(close.waitForExistence(timeout: 10))
        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.35))
        let end = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.9))
        start.press(forDuration: 0.1, thenDragTo: end, withVelocity: .slow, thenHoldForDuration: 0)
        XCTAssertFalse(close.waitForExistence(timeout: 3))
    }

    func testFastFlickAtTheTopDismissesTheSheet() {
        launchFixture()
        _ = SheetFixtureReadiness.waitForGeometry(in: app)
        let close = app.webViews.firstMatch.buttons["Close"]
        XCTAssertTrue(close.waitForExistence(timeout: 10))
        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.35))
        let end = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.95))
        start.press(forDuration: 0.01, thenDragTo: end, withVelocity: .fast, thenHoldForDuration: 0)
        XCTAssertFalse(close.waitForExistence(timeout: 3))
    }

    func testSheetSurvivesCompactHeightRotation() throws {
        launchFixture()
        _ = SheetFixtureReadiness.waitForGeometry(in: app)
        let close = app.webViews.firstMatch.buttons["Close"]
        XCTAssertTrue(close.waitForExistence(timeout: 10))
        let portraitGeometry = try XCTUnwrap(SheetFixtureReadiness.waitForGeometry(
            in: app, matching: { geometry in
                guard let frame = SheetFixtureReadiness.frame(from: geometry["sheetWindowFrame"]) else { return false }
                return geometry["orientation"] as? String == "portrait" && frame.height > frame.width
            }
        ))
        let portraitFrame = try XCTUnwrap(SheetFixtureReadiness.frame(from: portraitGeometry["sheetWindowFrame"]))

        XCUIDevice.shared.orientation = .landscapeLeft
        let compactGeometry = SheetFixtureReadiness.waitForGeometry(in: app, matching: { geometry in
            guard let frame = SheetFixtureReadiness.frame(from: geometry["sheetWindowFrame"]) else { return false }
            return geometry["orientation"] as? String == "landscape"
                && frame.width > portraitFrame.width
                && frame.height < portraitFrame.height
        })
        XCTAssertNotNil(compactGeometry)
        XCTAssertTrue(close.exists)

        XCUIDevice.shared.orientation = .portrait
        let restoredPortrait = SheetFixtureReadiness.waitForGeometry(in: app, matching: { geometry in
            guard let frame = SheetFixtureReadiness.frame(from: geometry["sheetWindowFrame"]) else { return false }
            return geometry["orientation"] as? String == "portrait"
                && abs(frame.width - portraitFrame.width) < 1
                && abs(frame.height - portraitFrame.height) < 1
        })
        XCTAssertNotNil(restoredPortrait)
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
/// assertions. Native geometry and bridge receipts stay on the fixture surface;
/// rendered content assertions use only scoped WebView AX queries.
enum SheetFixtureReadiness {
    static func requireFailedSelection(in app: XCUIApplication) {
        let surface = app.otherElements["fixture-document-surface"]
        let rejected = XCTNSPredicateExpectation(
            predicate: NSPredicate { _, _ in
                surface.label.contains("\"bridgeStage\":\"selection-rejected-invalid_instance\"")
            }, object: nil
        )
        XCTAssertEqual(XCTWaiter.wait(for: [rejected], timeout: 10), .completed,
                       "Expected native invalid_instance receipt; receipt=\(surface.label)")
        let rowError = XCTNSPredicateExpectation(
            predicate: NSPredicate { _, _ in
                surface.label.contains("\"bridgeStage\":\"row-error-rendered\"")
            }, object: nil
        )
        XCTAssertEqual(XCTWaiter.wait(for: [rowError], timeout: 10), .completed,
                       "Expected row-error-rendered DOM receipt; receipt=\(surface.label)")
    }

    static func waitForGeometry(
        in app: XCUIApplication,
        timeout: TimeInterval = 10,
        matching predicate: @escaping ([String: Any]) -> Bool = { geometry in
            guard let frame = SheetFixtureReadiness.frame(from: geometry["sheetWindowFrame"]) else { return false }
            return frame.width > 0 && frame.height > 0
        }
    ) -> [String: Any]? {
        let surface = app.otherElements["fixture-document-surface"]
        var result: [String: Any]?
        let matches: () -> Bool = {
            guard let data = surface.label.data(using: .utf8),
                  let evidence = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let geometry = evidence["geometry"] as? [String: Any],
                  predicate(geometry) else { return false }
            result = geometry
            return true
        }
        if matches() { return result }
        let ready = XCTNSPredicateExpectation(predicate: NSPredicate { _, _ in matches() }, object: nil)
        guard XCTWaiter.wait(for: [ready], timeout: timeout) == .completed else { return nil }
        return result
    }

    static func frame(from value: Any?) -> (width: Double, height: Double)? {
        guard let frame = value as? [String: Any],
              let width = frame["width"] as? Double,
              let height = frame["height"] as? Double else { return nil }
        return (width, height)
    }

    @discardableResult
    static func tapRow(containing name: String, in app: XCUIApplication) -> Bool {
        let row = app.webViews.firstMatch.buttons.matching(NSPredicate(format: "label CONTAINS %@", name)).firstMatch
        guard row.waitForExistence(timeout: 10) else {
            XCTFail("Row missing at the intended press: \(name)")
            return false
        }
        if !row.isHittable { app.swipeUp(velocity: .slow) }
        // Assert at the existing press point. Do not wait for a stalled/covered
        // target to become actionable and thereby hide the missed-input defect.
        let enabled = row.isEnabled
        let hittable = row.isHittable
        let frame = row.frame
        let evidence = XCTAttachment(string: """
        Intended XCTest row.tap center (window points): \(frame.midX), \(frame.midY)
        XCUI row frame: \(frame); enabled: \(enabled); hittable: \(hittable)
        Pre-press uptime: \(ProcessInfo.processInfo.systemUptime)
        Native evidence (observed asynchronously): \(app.otherElements["fixture-document-surface"].label)
        Actual synthesized coordinates remain in XCTest's Synthesized Event attachment.
        """)
        evidence.name = "\(name)-pre-press"
        evidence.lifetime = .keepAlways
        XCTContext.runActivity(named: "Record \(name) press geometry") { $0.add(evidence) }
        guard enabled && hittable else {
            XCTFail("Row not actionable at the intended press; no settling wait: \(name)")
            return false
        }
        row.tap()
        return true
    }

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
        let matches: () -> Bool = {
            // Re-resolve on each snapshot, and match the label rather
            // than relying on app-wide identifier resolution into a
            // different WebContent process.
            app.webViews.firstMatch.staticTexts.matching(
                NSPredicate(format: "label == %@", text)
            ).firstMatch.exists
        }
        // Evaluate once even when a short negative-check budget expires
        // before XCTNSPredicateExpectation schedules its first poll.
        if matches() { return true }
        let visible = XCTNSPredicateExpectation(
            predicate: NSPredicate { _, _ in matches() }, object: nil
        )
        return XCTWaiter.wait(for: [visible], timeout: timeout) == .completed
    }
}
