import XCTest

/// On-demand only: ios-shell skips this class; ios-mobile-captures selects it.
/// All state comes from the fixture's native fake transport and real user taps.
final class RealBundleCaptureUITests: XCTestCase {
    func testRealBundledPageCaptureMatrix() {
        continueAfterFailure = false
        XCUIDevice.shared.orientation = .portrait
        let states = ["default", "offline-open", "open-failed", "stale", "account", "first-run"]
        for language in ["en", "zh"] {
            for theme in ["light", "dark"] {
                for state in states {
                    let name = "mobile-\(state)-\(language)-\(theme)"
                    XCTContext.runActivity(named: name) { _ in
                        let app = XCUIApplication()
                        app.launchArguments = [
                            "-AppleLanguages", "(\(language))",
                            "-AppleLocale", language == "zh" ? "zh_CN" : "en_US",
                        ]
                        app.launchEnvironment["AVIBE_FIXTURE_CAPTURE_STATE"] = state
                        app.launchEnvironment["AVIBE_FIXTURE_CAPTURE_THEME"] = theme
                        app.launch()
                        defer { app.terminate() }
                        let chinese = language == "zh"
                        XCTAssertTrue(app.webViews.firstMatch.waitForExistence(timeout: 15))
                        // Static gesture HTML cannot pass these localized real-page assertions.
                        if state == "first-run" {
                            requireText(chinese ? "想从哪里开始？" : "Where shall we begin?", in: app)
                            XCTAssertFalse(app.buttons.matching(NSPredicate(
                                format: "label CONTAINS %@", chinese ? "关闭，回到" : "Close and return to"
                            )).firstMatch.exists)
                        } else {
                            requireText(chinese ? "正在使用" : "You’re in", in: app)
                            XCTAssertTrue(app.buttons[chinese ? "关闭，回到 Studio" : "Close and return to Studio"].waitForExistence(timeout: 10))
                            let refreshed = XCTNSPredicateExpectation(
                                predicate: NSPredicate(format: "value == %@", "fetch-2"),
                                object: app.otherElements["real-bundle-capture-surface"]
                            )
                            XCTAssertEqual(XCTWaiter.wait(for: [refreshed], timeout: 10), .completed)
                            let settled = XCTNSPredicateExpectation(
                                predicate: NSPredicate { _, _ in
                                    !app.staticTexts[chinese ? "正在更新…" : "Updating…"].exists
                                }, object: nil
                            )
                            XCTAssertEqual(XCTWaiter.wait(for: [settled], timeout: 10), .completed)
                        }
                        switch state {
                        case "offline-open":
                            tapRow(containing: "Home", in: app)
                            requireText(chinese
                                ? "Home 现在离线。在那台电脑上打开 Avibe 并联网，它就会出现为在线。"
                                : "Home is offline. Open Avibe on that computer and connect it to the internet, and it shows as online.", in: app)
                        case "open-failed":
                            tapRow(containing: "Lab", in: app)
                            requireText(chinese ? "没能打开 Lab，再试一次。" : "Couldn’t open Lab. Try again.", in: app)
                        case "stale":
                            requireText(chinese
                                ? "没能更新实例列表，下面是上次的结果，在线状态可能已经变了。"
                                : "Couldn’t update the list. These are the last results; who is online may have changed.", in: app)
                        case "account":
                            let account = app.buttons.matching(NSPredicate(
                                format: "label CONTAINS %@", "alex@example.invalid"
                            )).firstMatch
                            XCTAssertTrue(account.waitForExistence(timeout: 10))
                            if !account.isHittable { app.swipeUp(velocity: .slow) }
                            account.tap()
                            XCTAssertTrue(app.buttons[chinese ? "在此设备退出" : "Sign out on this device"].waitForExistence(timeout: 10))
                            requireText("alex@example.invalid", in: app)
                        default:
                            break
                        }
                        // Synthetic data only; no credential UI is reachable. Neither
                        // native nor fallback clipboard paths are called by this flow.
                        let attachment = XCTAttachment(screenshot: app.screenshot())
                        attachment.name = name
                        attachment.lifetime = .keepAlways
                        add(attachment)
                    }
                }
            }
        }
    }

    private func requireText(_ text: String, in app: XCUIApplication) {
        XCTAssertTrue(app.staticTexts[text].waitForExistence(timeout: 10), text)
    }

    private func tapRow(containing name: String, in app: XCUIApplication) {
        let row = app.buttons.matching(NSPredicate(format: "label CONTAINS %@", name)).firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        if !row.isHittable { app.swipeUp(velocity: .slow) }
        row.tap()
    }
}
