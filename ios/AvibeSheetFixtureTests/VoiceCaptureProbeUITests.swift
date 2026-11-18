import XCTest

/// DRAFT-ONLY CI PROBE (not for merge): runs the real 「和主伙伴说话」 /
/// "Talk to Main Bot" App Shortcut with Avibe in the background, the way the
/// owner's Action Button does (the Simulator has no Action Button), first
/// from the Shortcuts app, then from Spotlight. Evidence: screenshots, the
/// App's probe log, and the stand-in instance's log.
final class VoiceCaptureProbeUITests: XCTestCase {
    private let avibe = XCUIApplication(bundleIdentifier: "bot.avibe.ios")
    private let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
    private let shortcuts = XCUIApplication(bundleIdentifier: "com.apple.shortcuts")
    private let server = URL(string: "http://127.0.0.1:8765")!

    func testTheShortcutRecordsAndSendsWithAvibeInTheBackground() throws {
        continueAfterFailure = true
        avibe.launchArguments = [
            "-voiceCaptureProbeSetup", server.absoluteString,
            "-AppleLanguages", "(en)", "-AppleLocale", "en_US",
        ]
        avibe.launch()
        sleep(8)
        shot("0-avibe-setup")
        XCUIDevice.shared.press(.home)
        sleep(3)
        note("avibe state after home: \(avibe.state.rawValue)")

        for (label, trigger) in [("shortcuts", runFromShortcuts), ("spotlight", runFromSpotlight)] {
            let before = messages()
            note("\(label): start, avibe state \(avibe.state.rawValue)")
            guard trigger("\(label)-1-start") else { continue }
            sleep(8)
            shot("\(label)-2-after-start")
            note("\(label): avibe state while it should listen \(avibe.state.rawValue)")
            sleep(4)
            note("\(label): second run (stop and send)")
            _ = trigger("\(label)-3-stop")
            sleep(10)
            shot("\(label)-4-after-stop")
            let after = messages()
            note("\(label): messages to the main Bot \(before) -> \(after)")
            if after > before { return }
            XCUIDevice.shared.press(.home)
            sleep(2)
        }
    }

    // MARK: Triggers

    private func runFromShortcuts(_ label: String) -> Bool {
        shortcuts.launch()
        sleep(3)
        for title in ["Continue", "Not Now", "OK"] {
            let button = shortcuts.buttons[title]
            if button.exists, button.isHittable { button.tap(); sleep(1) }
        }
        shot("\(label)-shortcuts-open")
        let tile = shortcuts.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS[c] %@", "Talk to Main Bot")).firstMatch
        if !tile.waitForExistence(timeout: 3) {
            // App Shortcuts sit in the Gallery or under the App's name; search.
            let search = shortcuts.searchFields.firstMatch
            if !search.exists { shortcuts.swipeDown() }
            if search.waitForExistence(timeout: 3) {
                search.tap()
                search.typeText("Talk to Main Bot")
                sleep(2)
            } else {
                let avibeEntry = shortcuts.descendants(matching: .any)
                    .matching(NSPredicate(format: "label == %@", "Avibe")).firstMatch
                if avibeEntry.waitForExistence(timeout: 3) { avibeEntry.tap(); sleep(2) }
            }
        }
        shot("\(label)-shortcuts-found")
        guard tile.waitForExistence(timeout: 5), tile.isHittable else {
            note("\(label): no Talk to Main Bot in Shortcuts; tree:\n\(shortcuts.debugDescription.prefix(4000))")
            shortcuts.terminate()
            return false
        }
        tile.tap()
        note("\(label): tapped in Shortcuts at \(Date())")
        sleep(2)
        shot("\(label)-shortcuts-ran")
        return true
    }

    private func runFromSpotlight(_ label: String) -> Bool {
        XCUIDevice.shared.press(.home)
        sleep(1)
        springboard.swipeDown()
        sleep(2)
        let field = springboard.searchFields.firstMatch
        guard field.waitForExistence(timeout: 5) else {
            note("\(label): no Spotlight field")
            return false
        }
        field.typeText("Talk to Main Bot")
        sleep(3)
        shot("\(label)-spotlight-results")
        let result = springboard.descendants(matching: .any)
            .matching(NSPredicate(format: "label BEGINSWITH[c] %@", "Talk to Main Bot")).firstMatch
        guard result.waitForExistence(timeout: 5), result.isHittable else {
            note("\(label): no Spotlight result; tree:\n\(springboard.debugDescription.prefix(4000))")
            return false
        }
        result.tap()
        note("\(label): tapped in Spotlight at \(Date())")
        return true
    }

    // MARK: Evidence

    private func messages() -> Int {
        guard let data = try? Data(contentsOf: server.appending(path: "probe/log")),
              let text = String(data: data, encoding: .utf8)
        else { return -1 }
        return text.split(separator: "\n").filter { $0.contains("/api/sessions/ses_main/messages") }.count
    }

    private func shot(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    private func note(_ text: String) {
        print("PROBE NOTE: \(text)")
        let attachment = XCTAttachment(string: text)
        attachment.name = "note"
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
