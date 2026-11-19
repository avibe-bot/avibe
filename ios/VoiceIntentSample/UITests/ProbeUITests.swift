import XCTest

/// Runs each probe App's App Shortcut from the Shortcuts app with the App in
/// the background, warm and then cold, first with nothing else playing, then
/// with the navigation-like helper playing and showing its Live Activity.
/// Evidence: each App's Caches/probe.log (collected by CI), the notes and
/// screenshots here.
final class ProbeUITests: XCTestCase {
    private let shortcuts = XCUIApplication(bundleIdentifier: "com.apple.shortcuts")
    private let helper = XCUIApplication(bundleIdentifier: "bot.avibe.sample.helper")

    func testEveryVariantWithAndWithoutOtherAudio() throws {
        continueAfterFailure = true
        for condition in ["quiet", "navigating"] {
            if condition == "navigating" {
                helper.launch()
                sleep(4)
                shot("helper-started")
                XCUIDevice.shared.press(.home)
                sleep(2)
            }
            for variant in ["A", "B", "C", "D", "E", "F"] {
                let probe = XCUIApplication(bundleIdentifier: "bot.avibe.sample.\(variant.lowercased())")
                for launch in ["warm", "cold"] {
                    if launch == "warm" {
                        probe.launch()
                        sleep(3)
                        XCUIDevice.shared.press(.home)
                        sleep(2)
                    } else {
                        // Not running when the shortcut fires: the intent
                        // launches the process.
                        probe.terminate()
                        sleep(2)
                        note("cold check: probe state before the press \(probe.state.rawValue) (1 = not running)")
                    }
                    note("RUN \(condition) \(variant) \(launch) at \(Date().timeIntervalSince1970) probe state \(probe.state.rawValue)")
                    run(variant: variant, label: "\(condition)-\(variant)-\(launch)")
                    sleep(7)
                    shot("\(condition)-\(variant)-\(launch)-after")
                    note("END \(condition) \(variant) \(launch) probe state \(probe.state.rawValue)")
                    shortcuts.terminate()
                    XCUIDevice.shared.press(.home)
                    sleep(1)
                }
            }
        }
    }

    private func run(variant: String, label: String) {
        shortcuts.launch()
        sleep(3)
        for title in ["Continue", "Not Now", "OK"] {
            let button = shortcuts.buttons[title]
            if button.exists, button.isHittable { button.tap(); sleep(1) }
        }
        let tile = shortcuts.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS[c] %@", "Record \(variant)")).firstMatch
        // Later Apps' groups sit below the fold, under the tab bar.
        for _ in 0..<4 where !(tile.exists && tile.isHittable) {
            shortcuts.swipeUp()
            sleep(1)
        }
        if !tile.waitForExistence(timeout: 4) {
            let search = shortcuts.searchFields.firstMatch
            if search.waitForExistence(timeout: 3) {
                search.tap()
                search.typeText("Record \(variant)")
                sleep(2)
            }
        }
        guard tile.waitForExistence(timeout: 5), tile.isHittable else {
            note("\(label): no Record \(variant) tile; tree:\n\(shortcuts.debugDescription.prefix(3000))")
            return
        }
        tile.tap()
        note("\(label): tapped at \(Date().timeIntervalSince1970)")
        sleep(2)
        shot("\(label)-ran")
        for title in ["OK", "Allow", "Always Allow"] {
            let button = shortcuts.buttons[title]
            if button.exists, button.isHittable {
                note("\(label): dialog button \(title) present")
                shot("\(label)-dialog")
            }
        }
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
