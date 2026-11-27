import XCTest

/// The Turn card as the system draws it: the real App (installed next to the
/// fixture by the workflow) starts a running fixture card through its DEBUG
/// seam (`TurnActivityFixtureLaunch`), and SpringBoard's own elements must lie
/// on screen in the compact island, the expanded island and on the Lock
/// Screen. A card laid out at an unbounded size reaches SpringBoard with all
/// its text but frames about 1.8e308 wide, and the phone shows a black block
/// (11-26, a fixed-size timer).
final class TurnLiveActivityLayoutUITests: XCTestCase {
    private let app = XCUIApplication(bundleIdentifier: "bot.avibe.ios")
    private let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
    private let title = "修复登录页的验证码"
    /// The card's clock past one hour, its widest value (1:02:03 at start).
    private let clockPattern = #"^\d+:\d\d:\d\d$"#

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    override func tearDown() {
        // No card outlives the test: the island would sit over later pages.
        unlock()
        app.launchEnvironment = ["AVIBE_FIXTURE_TURN_ACTIVITY": "end"]
        app.launch()
        XCUIDevice.shared.press(.home)
        let gone = XCTNSPredicateExpectation(
            predicate: NSPredicate { [self] _, _ in !springboard.images["AvibeMark"].exists },
            object: nil
        )
        XCTAssertEqual(XCTWaiter.wait(for: [gone], timeout: 30), .completed, "The fixture card did not end")
        app.terminate()
        super.tearDown()
    }

    func testARunningTurnCardLiesOnScreenInTheIslandAndOnTheLockScreen() {
        app.launchEnvironment["AVIBE_FIXTURE_TURN_ACTIVITY"] = #"{"phase":"working","elapsed":3723}"#
        app.launch()
        XCUIDevice.shared.press(.home)

        // The first card waits on the extension's cold start (about 30 s on CI).
        requireOnScreen("compact island", timeout: 90) { elements in
            elements.contains { $0.label == "AvibeMark" } && elements.contains { $0.isClock }
        }

        springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.035)).press(forDuration: 1.5)
        requireOnScreen("expanded island", timeout: 30) { elements in
            elements.contains { $0.label == self.title } && elements.contains { $0.isClock }
        }

        springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.8)).tap()
        XCUIDevice.shared.perform(NSSelectorFromString("pressLockButton"))
        XCUIDevice.shared.press(.home)
        requireOnScreen("Lock Screen", timeout: 30) { elements in
            elements.contains { $0.label == self.title } && elements.contains { $0.isClock }
        }
    }

    private struct Shown {
        let label: String
        let frame: CGRect
        let isClock: Bool
    }

    /// Waits until the card's elements (its title, the mark, the clock) are
    /// all on screen and include what `complete` asks for. Any of them laid
    /// out off screen or at an infinite size fails the surface.
    private func requireOnScreen(_ surface: String, timeout: TimeInterval, complete: @escaping ([Shown]) -> Bool) {
        var last: [Shown] = []
        let shown = XCTNSPredicateExpectation(
            predicate: NSPredicate { [self] _, _ in
                last = cardElements()
                return !last.isEmpty && last.allSatisfy { onScreen($0.frame) } && complete(last)
            },
            object: nil
        )
        let result = XCTWaiter.wait(for: [shown], timeout: timeout)
        if result != .completed {
            let screenshot = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
            screenshot.name = "\(surface) screenshot"
            add(screenshot)
            let tree = XCTAttachment(string: springboard.debugDescription)
            tree.name = "\(surface) SpringBoard elements"
            add(tree)
        }
        XCTAssertEqual(result, .completed, """
            The Turn card is not on screen in the \(surface): \
            \(last.map { "\($0.label) \($0.frame)" })
            """)
    }

    /// One snapshot of SpringBoard, read here: its query engine does not
    /// match the clock's label by pattern.
    private func cardElements() -> [Shown] {
        guard let root = try? springboard.snapshot() else { return [] }
        var found: [Shown] = []
        var pending: [XCUIElementSnapshot] = [root]
        while let element = pending.popLast() {
            pending.append(contentsOf: element.children)
            let label = element.label
            let isClock = label.range(of: clockPattern, options: .regularExpression) != nil
            if label == title || (label == "AvibeMark" && element.elementType == .image) || isClock {
                found.append(Shown(label: label, frame: element.frame, isClock: isClock))
            }
        }
        return found
    }

    private func onScreen(_ frame: CGRect) -> Bool {
        let screen = springboard.frame
        return [frame.minX, frame.minY, frame.width, frame.height].allSatisfy(\.isFinite)
            && frame.width > 0 && frame.height > 0 && screen.contains(frame)
    }

    /// Swipes up from the bottom edge: opens a locked phone, and is a no-op
    /// on the Home Screen.
    private func unlock() {
        springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.95))
            .press(forDuration: 0.1, thenDragTo: springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.3)))
    }
}
