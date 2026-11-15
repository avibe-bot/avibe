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

    func testReadinessMutationGuardsRejectMissingReceipts() {
        let rejectedOnly = "{\"bridgeStage\":\"selection-rejected-invalid_instance\"}"
        XCTAssertFalse(SheetFixtureReadiness.hasBridgeStages(
            ["selection-rejected-invalid_instance", "row-error-rendered"], in: rejectedOnly
        ))
        let missingHistory = "{\"bridgeStage\":\"row-error-rendered\",\"bridgeStages\":[\"row-error-rendered\"]}"
        XCTAssertFalse(SheetFixtureReadiness.hasBridgeStages(
            ["selection-rejected-invalid_instance", "row-error-rendered"], in: missingHistory
        ))

        let portrait = [
            "sequence": 1, "orientation": "portrait",
            "sheetWindowFrame": ["x": 0.0, "y": 0.0, "width": 390.0, "height": 844.0],
            "webViewWindowFrame": ["x": 0.0, "y": 100.0, "width": 390.0, "height": 744.0],
        ] as [String: Any]
        let unchanged = portrait
        let sequenceOnly = [
            "sequence": 2, "orientation": "portrait",
            "sheetWindowFrame": portrait["sheetWindowFrame"]!,
            "webViewWindowFrame": portrait["webViewWindowFrame"]!,
        ] as [String: Any]
        let orientationOnly = [
            "sequence": 2, "orientation": "landscape",
            "sheetWindowFrame": portrait["sheetWindowFrame"]!,
            "webViewWindowFrame": portrait["webViewWindowFrame"]!,
        ] as [String: Any]
        let landscape = [
            "sequence": 2, "orientation": "landscape",
            "sheetWindowFrame": ["x": 0.0, "y": 0.0, "width": 844.0, "height": 390.0],
            "webViewWindowFrame": ["x": 100.0, "y": 0.0, "width": 744.0, "height": 390.0],
        ] as [String: Any]
        XCTAssertFalse(SheetFixtureReadiness.isLandscapeTransition(unchanged, from: portrait))
        XCTAssertFalse(SheetFixtureReadiness.isLandscapeTransition(sequenceOnly, from: portrait))
        XCTAssertFalse(SheetFixtureReadiness.isLandscapeTransition(orientationOnly, from: portrait))
        XCTAssertTrue(SheetFixtureReadiness.isLandscapeTransition(landscape, from: portrait))
        XCTAssertFalse(SheetFixtureReadiness.isRestoredPortrait(unchanged, from: portrait))
    }

    func testMidListDownwardDragScrollsBeforeTheSheetDismisses() throws {
        launchFixture()
        guard SheetFixtureReadiness.waitForGeometry(in: app) != nil else {
            XCTFail("Native geometry receipt did not become ready before the mid-list drag")
            return
        }
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
        guard SheetFixtureReadiness.waitForGeometry(in: app) != nil else {
            XCTFail("Native geometry receipt did not become ready before the top drag")
            return
        }
        let close = app.webViews.firstMatch.buttons["Close"]
        XCTAssertTrue(close.waitForExistence(timeout: 10))
        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.35))
        let end = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.9))
        start.press(forDuration: 0.1, thenDragTo: end, withVelocity: .slow, thenHoldForDuration: 0)
        XCTAssertFalse(close.waitForExistence(timeout: 3))
    }

    func testFastFlickAtTheTopDismissesTheSheet() {
        launchFixture()
        guard SheetFixtureReadiness.waitForGeometry(in: app) != nil else {
            XCTFail("Native geometry receipt did not become ready before the fast flick")
            return
        }
        let close = app.webViews.firstMatch.buttons["Close"]
        XCTAssertTrue(close.waitForExistence(timeout: 10))
        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.35))
        let end = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.95))
        start.press(forDuration: 0.01, thenDragTo: end, withVelocity: .fast, thenHoldForDuration: 0)
        XCTAssertFalse(close.waitForExistence(timeout: 3))
    }

    func testSheetSurvivesCompactHeightRotation() throws {
        launchFixture()
        let close = app.webViews.firstMatch.buttons["Close"]
        XCTAssertTrue(close.waitForExistence(timeout: 10))
        let portraitGeometry = try XCTUnwrap(SheetFixtureReadiness.waitForGeometry(
            in: app, timeout: 5, matching: { geometry in
                SheetFixtureReadiness.isPortraitGeometry(geometry)
            }
        ))

        XCUIDevice.shared.orientation = .landscapeLeft
        let compactGeometry = SheetFixtureReadiness.waitForGeometry(in: app, timeout: 5, matching: { geometry in
            SheetFixtureReadiness.isLandscapeTransition(geometry, from: portraitGeometry)
        })
        XCTAssertNotNil(compactGeometry)
        XCTAssertTrue(close.exists)

        XCUIDevice.shared.orientation = .portrait
        let restoredPortrait = SheetFixtureReadiness.waitForGeometry(in: app, timeout: 5, matching: { geometry in
            SheetFixtureReadiness.isRestoredPortrait(geometry, from: portraitGeometry)
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
        let requiredStages = [
            "selection-rejected-invalid_instance",
            "row-error-rendered",
        ]
        let receipt = XCTNSPredicateExpectation(
            predicate: NSPredicate { _, _ in
                SheetFixtureReadiness.hasBridgeStages(requiredStages, in: surface.label)
            }, object: nil
        )
        XCTAssertEqual(XCTWaiter.wait(for: [receipt], timeout: 10), .completed,
                       "Expected native rejection and rendered row error receipts; receipt=\(surface.label)")
    }

    static func hasBridgeStages(_ stages: [String], in label: String) -> Bool {
        guard let data = label.data(using: .utf8),
              let evidence = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let recorded = evidence["bridgeStages"] as? [String] else { return false }
        return stages.allSatisfy { stage in
            recorded.firstIndex(of: stage) != nil
        } && stages == recorded.filter { stages.contains($0) }
    }

    static func isPortraitGeometry(_ geometry: [String: Any]) -> Bool {
        guard let sheet = frame(from: geometry["sheetWindowFrame"]),
              let web = frame(from: geometry["webViewWindowFrame"]) else { return false }
        return geometry["orientation"] as? String == "portrait"
            && sheet.height > sheet.width
            && web.height > web.width
    }

    static func isLandscapeTransition(_ geometry: [String: Any], from portrait: [String: Any]) -> Bool {
        guard let sheet = frame(from: geometry["sheetWindowFrame"]),
              let web = frame(from: geometry["webViewWindowFrame"]),
              let oldSheet = frame(from: portrait["sheetWindowFrame"]),
              let oldWeb = frame(from: portrait["webViewWindowFrame"]),
              let sequence = geometry["sequence"] as? Int,
              let oldSequence = portrait["sequence"] as? Int else { return false }
        return sequence > oldSequence
            && geometry["orientation"] as? String == "landscape"
            && sheet.width > oldSheet.width
            && sheet.height < oldSheet.height
            && web.width > oldWeb.width
            && web.height < oldWeb.height
    }

    static func isRestoredPortrait(_ geometry: [String: Any], from portrait: [String: Any]) -> Bool {
        guard let sheet = frame(from: geometry["sheetWindowFrame"]),
              let web = frame(from: geometry["webViewWindowFrame"]),
              let oldSheet = frame(from: portrait["sheetWindowFrame"]),
              let oldWeb = frame(from: portrait["webViewWindowFrame"]),
              let sequence = geometry["sequence"] as? Int,
              let oldSequence = portrait["sequence"] as? Int else { return false }
        return sequence > oldSequence
            && geometry["orientation"] as? String == "portrait"
            && framesMatch(sheet, oldSheet)
            && framesMatch(web, oldWeb)
    }

    private static func framesMatch(
        _ lhs: (x: Double, y: Double, width: Double, height: Double),
        _ rhs: (x: Double, y: Double, width: Double, height: Double)
    ) -> Bool {
        abs(lhs.x - rhs.x) < 1
            && abs(lhs.y - rhs.y) < 1
            && abs(lhs.width - rhs.width) < 1
            && abs(lhs.height - rhs.height) < 1
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

    static func frame(from value: Any?) -> (x: Double, y: Double, width: Double, height: Double)? {
        guard let frame = value as? [String: Any],
              let x = frame["x"] as? Double,
              let y = frame["y"] as? Double,
              let width = frame["width"] as? Double,
              let height = frame["height"] as? Double else { return nil }
        return (x, y, width, height)
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
