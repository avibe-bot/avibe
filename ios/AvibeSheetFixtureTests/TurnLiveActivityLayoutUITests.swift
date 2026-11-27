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

/// The Action Button's voice card as the system draws it, through the same
/// DEBUG seam (`AVIBE_FIXTURE_VOICE_ACTIVITY`): a listening take's expanded
/// island must show its clock at the island's trailing side, not off-centre
/// above the title (11-26: a timer text of unbounded width filled its region
/// and drew at that region's leading edge).
final class VoiceLiveActivityLayoutUITests: XCTestCase {
    private let app = XCUIApplication(bundleIdentifier: "bot.avibe.ios")
    private let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
    private let titles: Set<String> = ["Listening", "正在听"]
    private let sends: Set<String> = ["Send", "发送"]
    private let clockPattern = #"^\d+:\d\d$"#

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    override func tearDown() {
        // No card outlives the test: the island would sit over later pages.
        springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.95))
            .press(forDuration: 0.1, thenDragTo: springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.3)))
        app.launchEnvironment = ["AVIBE_FIXTURE_VOICE_ACTIVITY": "end"]
        app.launch()
        XCUIDevice.shared.press(.home)
        let gone = XCTNSPredicateExpectation(
            predicate: NSPredicate { [self] _, _ in elements().allSatisfy { $0.kind != .clock } },
            object: nil
        )
        XCTAssertEqual(XCTWaiter.wait(for: [gone], timeout: 30), .completed, "The fixture voice card did not end")
        app.terminate()
        super.tearDown()
    }

    func testAListeningTakesClockSitsAtTheExpandedIslandsTrailingSide() {
        app.launchEnvironment["AVIBE_FIXTURE_VOICE_ACTIVITY"] = #"{"elapsed":54}"#
        app.launch()
        XCUIDevice.shared.press(.home)

        // The compact island first: the card is up (the extension's cold start can take 30 s on CI).
        var last: [Shown] = []
        let compact = XCTNSPredicateExpectation(
            predicate: NSPredicate { [self] _, _ in
                last = elements()
                return last.contains { $0.kind == .clock && onScreen($0.frame) }
            },
            object: nil
        )
        XCTAssertEqual(XCTWaiter.wait(for: [compact], timeout: 90), .completed, "No voice card in the compact island: \(last)")

        springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.035)).press(forDuration: 1.5)
        var clock: Shown?, title: Shown?, send: Shown?
        let expanded = XCTNSPredicateExpectation(
            predicate: NSPredicate { [self] _, _ in
                last = elements()
                clock = last.first { $0.kind == .clock }
                title = last.first { $0.kind == .title }
                send = last.first { $0.kind == .send }
                return [clock, title, send].allSatisfy { $0.map { onScreen($0.frame) } ?? false }
            },
            object: nil
        )
        let result = XCTWaiter.wait(for: [expanded], timeout: 30)
        // The frame itself, for a person to look at, whatever the result.
        let screenshot = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        screenshot.name = "voice expanded island"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        let tree = XCTAttachment(string: springboard.debugDescription)
        tree.name = "voice expanded island SpringBoard elements"
        tree.lifetime = .keepAlways
        add(tree)
        XCTAssertEqual(result, .completed, "The expanded voice island is not on screen: \(last)")
        guard let clock, let title, let send else { return }
        // To the right of the title, and at the island's trailing side: its
        // middle right of the Send button's, which fills the right half of
        // the island's width. A clock standing above the title fails both.
        XCTAssertGreaterThanOrEqual(clock.frame.minX, title.frame.maxX, "clock \(clock.frame), title \(title.frame)")
        XCTAssertGreaterThan(clock.frame.midX, send.frame.midX, "clock \(clock.frame), Send \(send.frame)")
    }

    private enum Kind { case clock, title, send }

    private struct Shown: CustomStringConvertible {
        let label: String
        let frame: CGRect
        let kind: Kind
        var description: String { "\(label) \(frame)" }
    }

    /// One snapshot of SpringBoard, read here: its query engine does not
    /// match the clock's label by pattern. The status bar's own time reads
    /// like the take's clock (`3:29`), so the status bar is left out.
    private func elements() -> [Shown] {
        guard let root = try? springboard.snapshot() else { return [] }
        var found: [Shown] = []
        var pending: [XCUIElementSnapshot] = [root]
        while let element = pending.popLast() {
            if element.elementType == .statusBar { continue }
            pending.append(contentsOf: element.children)
            let label = element.label
            if label.range(of: clockPattern, options: .regularExpression) != nil {
                found.append(Shown(label: label, frame: element.frame, kind: .clock))
            } else if titles.contains(label) {
                found.append(Shown(label: label, frame: element.frame, kind: .title))
            } else if sends.contains(label) {
                found.append(Shown(label: label, frame: element.frame, kind: .send))
            }
        }
        return found
    }

    private func onScreen(_ frame: CGRect) -> Bool {
        let screen = springboard.frame
        return [frame.minX, frame.minY, frame.width, frame.height].allSatisfy(\.isFinite)
            && frame.width > 0 && frame.height > 0 && screen.contains(frame)
    }
}
