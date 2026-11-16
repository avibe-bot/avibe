import XCTest

/// DRAFT-ONLY CI PROBE (not for merge): runs the real Avibe App's
/// background voice intent from Spotlight, twice per round (start, then the
/// second run that stops and sends), with the App first suspended and then
/// terminated. Evidence is the screenshots here, the App's probe log, and
/// the stand-in instance's request log, which CI collects.
final class VoiceCaptureProbeUITests: XCTestCase {
    private let avibe = XCUIApplication(bundleIdentifier: "bot.avibe.ios")
    private let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
    private let spotlight = XCUIApplication(bundleIdentifier: "com.apple.Spotlight")

    func testBackgroundIntentRecordsAndSends() throws {
        continueAfterFailure = true
        note("action button available: \(XCUIDevice.shared.hasHardwareButton(.action))")

        avibe.launchArguments = [
            "-voiceCaptureProbeSetup", "http://127.0.0.1:8765",
            "-AppleLanguages", "(en)", "-AppleLocale", "en_US",
        ]
        avibe.launch()
        sleep(8)
        shot("1-setup-foreground")
        XCUIDevice.shared.press(.home)
        sleep(3)

        // Round 1: the App is suspended in the background.
        note("round 1 app state before: \(avibe.state.rawValue)")
        runShortcut(label: "r1-start")
        sleep(3)
        shot("2-r1-listening")
        note("round 1 app state while listening: \(avibe.state.rawValue)")
        sleep(8)
        shot("3-r1-after-11s")
        runShortcut(label: "r1-stop")
        sleep(6)
        shot("4-r1-after-stop")

        // Round 2: the App is not running.
        avibe.terminate()
        sleep(3)
        note("round 2 app state before: \(avibe.state.rawValue)")
        runShortcut(label: "r2-start")
        sleep(3)
        shot("5-r2-listening")
        note("round 2 app state while listening: \(avibe.state.rawValue)")
        sleep(5)
        runShortcut(label: "r2-stop")
        sleep(6)
        shot("6-r2-after-stop")
        note("round 2 app state after: \(avibe.state.rawValue)")
    }

    private func runShortcut(label: String) {
        XCUIDevice.shared.press(.home)
        sleep(1)
        springboard.activate()
        let from = springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.35))
        let to = springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.75))
        from.press(forDuration: 0.05, thenDragTo: to)
        sleep(1)
        let field = [spotlight.searchFields.firstMatch, springboard.searchFields.firstMatch]
            .first { $0.waitForExistence(timeout: 4) }
        guard let field else {
            shot("\(label)-no-spotlight")
            note("\(label): Spotlight search field not found")
            return
        }
        field.typeText("Speak to Main Bot")
        sleep(3)
        shot("\(label)-spotlight-results")
        let predicate = NSPredicate(format: "label CONTAINS[c] %@", "Speak to Main Bot")
        let candidates = [
            spotlight.buttons.matching(predicate).firstMatch,
            spotlight.cells.matching(predicate).firstMatch,
            spotlight.staticTexts.matching(predicate).firstMatch,
            springboard.buttons.matching(predicate).firstMatch,
            springboard.cells.matching(predicate).firstMatch,
            springboard.staticTexts.matching(predicate).firstMatch,
        ]
        if let target = candidates.first(where: { $0.exists && $0.isHittable }) {
            target.tap()
            note("\(label): tapped \(target.elementType.rawValue) '\(target.label)'")
        } else {
            note("\(label): no result; spotlight tree:\n\(spotlight.debugDescription.prefix(6000))")
        }
        sleep(2)
        shot("\(label)-after-tap")
        XCUIDevice.shared.press(.home)
    }

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
