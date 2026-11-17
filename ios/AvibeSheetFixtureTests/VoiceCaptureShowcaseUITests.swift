import XCTest

/// DRAFT-ONLY (not for merge): captures the real Live Activity, as the
/// system draws it, in each state: the compact Dynamic Island on the Home
/// Screen, the expanded island (touch and hold), and the Lock Screen
/// presentation (Notification Center). The workflow sets the Simulator's
/// language; this test sets light and dark.
final class VoiceCaptureShowcaseUITests: XCTestCase {
    private let avibe = XCUIApplication(bundleIdentifier: "bot.avibe.ios")
    private let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")

    func testCaptureLiveActivityStates() {
        continueAfterFailure = true
        let zh = (Locale.preferredLanguages.first ?? "").hasPrefix("zh")
        let heard = zh ? "帮我把明天上午的会挪到下午三点，再提醒我带电脑" : "Move tomorrow morning's meeting to 3 pm and remind me to bring my laptop"
        let states: [(String, [String])] = [
            ("listening", ["listening"]),
            ("sending", ["sending"]),
            ("sent", ["sent", "-", heard]),
            ("failed-offline", ["failed", "offline"]),
            ("needs-app-consent", ["needsApp", "consent"]),
            ("failed-update", ["failed", "updateAvibe"]),
        ]
        for appearance in [XCUIDevice.Appearance.light, .dark] {
            XCUIDevice.shared.appearance = appearance
            let mode = appearance == .dark ? "dark" : "light"
            for (name, arguments) in states {
                avibe.terminate()
                avibe.launchArguments = ["-voiceCaptureShowcase"] + arguments
                avibe.launch()
                sleep(3)
                XCUIDevice.shared.press(.home)
                sleep(3)
                shot("\(mode)-\(name)-1-compact")
                springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.025)).press(forDuration: 1.2)
                sleep(2)
                shot("\(mode)-\(name)-2-expanded")
                XCUIDevice.shared.press(.home)
                sleep(1)
                let top = springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.2, dy: 0.005))
                top.press(forDuration: 0.05, thenDragTo: springboard.coordinate(withNormalizedOffset: CGVector(dx: 0.2, dy: 0.6)))
                sleep(2)
                shot("\(mode)-\(name)-3-lockscreen")
                XCUIDevice.shared.press(.home)
                sleep(1)
            }
        }
        avibe.terminate()
    }

    private func shot(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
