import XCTest

/// DRAFT-ONLY CI PROBE (not for merge): runs the real Avibe App's
/// background voice intent twice per round (start, then the second run that
/// stops and sends), with the App first suspended and then terminated.
/// It tries the Action Button (configured through Settings), Spotlight, and
/// Siri text, and keeps the first that reaches the stand-in instance.
/// Evidence: screenshots here, the App's probe log, the stand-in's log.
final class VoiceCaptureProbeUITests: XCTestCase {
    private let avibe = XCUIApplication(bundleIdentifier: "bot.avibe.ios")
    private let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
    private let spotlight = XCUIApplication(bundleIdentifier: "com.apple.Spotlight")
    private let settings = XCUIApplication(bundleIdentifier: "com.apple.Preferences")
    private let server = URL(string: "http://127.0.0.1:8765")!

    private enum Trigger: String {
        case actionButton, spotlight, siri
    }

    func testBackgroundIntentRecordsAndSends() throws {
        continueAfterFailure = true
        note("action button available: \(XCUIDevice.shared.hasHardwareButton(.action))")

        avibe.launchArguments = [
            "-voiceCaptureProbeSetup", server.absoluteString,
            "-AppleLanguages", "(en)", "-AppleLocale", "en_US",
        ]
        avibe.launch()
        sleep(8)
        shot("1-setup-foreground")
        XCUIDevice.shared.press(.home)
        sleep(2)

        let actionButtonReady = configureActionButton()
        note("action button configured: \(actionButtonReady)")

        // Round 1: the App is suspended in the background.
        note("round 1 app state before: \(avibe.state.rawValue)")
        let candidates: [Trigger] = actionButtonReady ? [.actionButton] : []
        var working: Trigger?
        for trigger in candidates {
            let before = serverRequestCount()
            run(trigger, label: "r1-start-\(trigger.rawValue)")
            sleep(10)
            let after = serverRequestCount()
            note("r1 start via \(trigger.rawValue): stand-in requests \(before) -> \(after)")
            if after > before {
                working = trigger
                break
            }
        }
        guard let working else {
            note("no trigger reached the stand-in instance")
            return
        }
        shot("2-r1-listening")
        note("round 1 app state while listening: \(avibe.state.rawValue)")
        sleep(8)
        shot("3-r1-after-13s")
        run(working, label: "r1-stop")
        sleep(8)
        shot("4-r1-after-stop")
        note("after r1: \(serverPaths())")

        // Round 2: the App is not running.
        avibe.terminate()
        sleep(3)
        note("round 2 app state before: \(avibe.state.rawValue)")
        run(working, label: "r2-start")
        sleep(5)
        shot("5-r2-listening")
        note("round 2 app state while listening: \(avibe.state.rawValue)")
        sleep(5)
        run(working, label: "r2-stop")
        sleep(8)
        shot("6-r2-after-stop")
        note("after r2: \(serverPaths())")
    }

    // MARK: Triggers

    private func run(_ trigger: Trigger, label: String) {
        switch trigger {
        case .actionButton:
            note("\(label): pressing the Action Button at \(Date())")
            XCUIDevice.shared.press(.action)
            sleep(4)
            shot("\(label)-after-press")
        case .spotlight:
            runFromSpotlight(label: label)
        case .siri:
            XCUIDevice.shared.siriService.activate(voiceRecognitionText: "Speak to Main Bot in Avibe")
            sleep(4)
            shot("\(label)-siri")
        }
    }

    private func runFromSpotlight(label: String) {
        XCUIDevice.shared.press(.home)
        sleep(1)
        springboard.activate()
        springboard.swipeDown()
        sleep(2)
        let fields = [
            spotlight.searchFields.firstMatch,
            springboard.searchFields.firstMatch,
            spotlight.textFields.firstMatch,
            springboard.textFields.firstMatch,
        ]
        guard let field = fields.first(where: { $0.waitForExistence(timeout: 2) }) else {
            shot("\(label)-no-spotlight")
            note("\(label): no search field; springboard tree:\n\(springboard.debugDescription.prefix(5000))")
            return
        }
        field.typeText("Speak to Main Bot")
        sleep(3)
        shot("\(label)-spotlight-results")
        let predicate = NSPredicate(format: "label CONTAINS[c] %@", "Speak to Main Bot")
        let hits = [spotlight, springboard].flatMap { app in
            [app.buttons, app.cells, app.staticTexts, app.otherElements]
                .map { $0.matching(predicate).firstMatch }
        }
        if let hit = hits.first(where: { $0.exists && $0.isHittable }) {
            hit.tap()
            note("\(label): tapped '\(hit.label)'")
        } else {
            note("\(label): no result; spotlight tree:\n\(spotlight.debugDescription.prefix(5000))")
        }
        sleep(2)
        shot("\(label)-after-tap")
        XCUIDevice.shared.press(.home)
    }

    /// Settings › Action Button › Shortcut › Avibe › Speak to Main Bot.
    /// The chooser is a remote view that loads slowly; two attempts.
    private func configureActionButton() -> Bool {
        guard XCUIDevice.shared.hasHardwareButton(.action) else { return false }
        for attempt in 1...2 {
            if configureActionButtonOnce(attempt: attempt) { return true }
            settings.terminate()
            XCUIDevice.shared.press(.home)
            sleep(2)
        }
        return false
    }

    private func configureActionButtonOnce(attempt: Int) -> Bool {
        settings.launch()
        sleep(3)
        let row = settings.cells.containing(NSPredicate(format: "label CONTAINS[c] %@", "Action Button")).firstMatch
        if !row.waitForExistence(timeout: 5) { settings.swipeUp() }
        guard row.waitForExistence(timeout: 5) else {
            note("ab\(attempt): no Settings row")
            return false
        }
        row.tap()
        sleep(3)
        let choose = settings.buttons.matching(NSPredicate(format: "label BEGINSWITH[c] %@", "Choose a Shortcut")).firstMatch
        for _ in 0..<14 {
            if choose.exists && choose.isHittable { break }
            settings.swipeLeft()
            sleep(2)
        }
        shot("ab\(attempt)-shortcut-page")
        guard choose.exists, choose.isHittable else {
            note("ab\(attempt): no choose button; tree:\n\(settings.debugDescription.prefix(4000))")
            return false
        }
        choose.tap()
        let any = settings.descendants(matching: .any)
        let avibeEntry = any.matching(NSPredicate(format: "label == %@", "Avibe")).firstMatch
        let direct = any.matching(NSPredicate(format: "label CONTAINS[c] %@", "Speak to Main Bot")).firstMatch
        let deadline = Date().addingTimeInterval(30)
        while Date() < deadline, !(direct.exists && direct.isHittable), !(avibeEntry.exists && avibeEntry.isHittable) {
            sleep(1)
        }
        shot("ab\(attempt)-chooser")
        if !(direct.exists && direct.isHittable), avibeEntry.exists, avibeEntry.isHittable {
            avibeEntry.tap()
            _ = direct.waitForExistence(timeout: 20)
        }
        shot("ab\(attempt)-avibe")
        guard direct.exists, direct.isHittable else {
            note("ab\(attempt): no Speak to Main Bot; tree:\n\(settings.debugDescription.prefix(4000))")
            return false
        }
        direct.tap()
        sleep(2)
        shot("ab\(attempt)-chosen")
        settings.terminate()
        XCUIDevice.shared.press(.home)
        sleep(1)
        return true
    }

    // MARK: Stand-in instance

    private func serverLines() -> [[String: Any]] {
        let done = expectation(description: "log")
        var lines: [[String: Any]] = []
        URLSession.shared.dataTask(with: server.appending(path: "probe/log")) { data, _, _ in
            if let data, let text = String(data: data, encoding: .utf8) {
                lines = text.split(separator: "\n").compactMap {
                    try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any]
                }
            }
            done.fulfill()
        }.resume()
        wait(for: [done], timeout: 5)
        return lines
    }

    private func serverRequestCount() -> Int {
        serverLines().filter { ($0["path"] as? String)?.hasPrefix("/api/") == true }.count
    }

    private func serverPaths() -> String {
        serverLines().map { "\($0["method"] ?? "") \($0["path"] ?? "") \($0["status"] ?? "") cookie=\($0["session_cookie"] ?? "")" }
            .joined(separator: "; ")
    }

    // MARK: Evidence

    private func shot(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func note(_ text: String) {
        let attachment = XCTAttachment(string: text)
        attachment.name = "note"
        attachment.lifetime = .keepAlways
        add(attachment)
        print("PROBE NOTE: \(text)")
    }
}
