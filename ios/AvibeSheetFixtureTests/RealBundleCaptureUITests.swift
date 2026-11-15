import XCTest

/// On-demand only: ios-shell skips this class; ios-mobile-captures selects it.
/// All state comes from the fixture's native fake transport and real user taps.
final class RealBundleCaptureUITests: XCTestCase {
    func testRealBundledPageCaptureMatrix() {
        continueAfterFailure = false
        XCUIDevice.shared.orientation = .portrait
        let states = ["default", "offline-open", "open-failed", "stale", "account", "first-run", "instances-named", "account-root"]
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
                        SheetFixtureReadiness.waitForDocument(in: app)
                        let isBRoute = state == "instances-named" || state == "account-root"
                        if state != "first-run" {
                            let refreshed = XCTNSPredicateExpectation(
                                predicate: NSPredicate(format: "value == %@", "document-ready;fetch-2"),
                                object: app.otherElements["fixture-document-surface"]
                            )
                            XCTAssertEqual(XCTWaiter.wait(for: [refreshed], timeout: 10), .completed)
                        }
                        // B routes are opened over a capable runtime fixture and
                        // therefore do not begin at the normal `#sheet` content.
                        // Their route-specific assertions below are the readiness gate.
                        if state == "first-run" {
                            requireText(chinese ? "想从哪里开始？" : "Where shall we begin?", in: app)
                            XCTAssertFalse(app.webViews.firstMatch.buttons.matching(NSPredicate(
                                format: "label CONTAINS %@", chinese ? "关闭，回到" : "Close and return to"
                            )).firstMatch.exists)
                        } else if !isBRoute {
                            requireText(chinese ? "正在使用" : "You’re in", in: app)
                            XCTAssertTrue(app.webViews.firstMatch.buttons[chinese ? "关闭，回到 Studio" : "Close and return to Studio"].waitForExistence(timeout: 10))
                            let settled = XCTNSPredicateExpectation(
                                predicate: NSPredicate { _, _ in
                                    !app.webViews.firstMatch.staticTexts.matching(NSPredicate(
                                        format: "label == %@", chinese ? "正在更新…" : "Updating…"
                                    )).firstMatch.exists
                                }, object: nil
                            )
                            XCTAssertEqual(XCTWaiter.wait(for: [settled], timeout: 10), .completed)
                        }
                        switch state {
                        case "instances-named":
                            requireText("Studio", in: app)
                            XCTAssertTrue(app.webViews.firstMatch.buttons[
                                chinese ? "关闭，回到 Studio" : "Close and return to Studio"
                            ].waitForExistence(timeout: 10))
                            XCTAssertFalse(app.webViews.firstMatch.staticTexts[chinese ? "账号" : "Account"].exists)
                            XCTAssertFalse(app.webViews.firstMatch.buttons.matching(NSPredicate(
                                format: "label CONTAINS %@", "alex@example.invalid"
                            )).firstMatch.exists)
                        case "account-root":
                            requireText(chinese ? "账号" : "Account", in: app)
                            XCTAssertTrue(app.webViews.firstMatch.buttons[chinese ? "关闭" : "Close"].waitForExistence(timeout: 10))
                            XCTAssertFalse(app.webViews.firstMatch.buttons.matching(NSPredicate(
                                format: "label CONTAINS %@", chinese ? "返回" : "Back"
                            )).firstMatch.exists)
                        case "offline-open":
                            SheetFixtureReadiness.tapRow(containing: "Home", in: app)
                            requireText(chinese
                                ? "Home 现在离线。在那台电脑上打开 Avibe 并联网，它就会出现为在线。"
                                : "Home is offline. Open Avibe on that computer and connect it to the internet, and it shows as online.", in: app)
                        case "open-failed":
                            SheetFixtureReadiness.tapRow(containing: "Lab", in: app)
                            SheetFixtureReadiness.requireFailedSelection(chinese ? "没能打开 Lab，再试一次。" : "Couldn’t open Lab. Try again.", in: app)
                        case "stale":
                            requireText(chinese
                                ? "没能更新实例列表，下面是上次的结果，在线状态可能已经变了。"
                                : "Couldn’t update the list. This is the last result; online status may have changed.", in: app)
                        case "account":
                            let account = app.webViews.firstMatch.buttons.matching(NSPredicate(
                                format: "label CONTAINS %@", "alex@example.invalid"
                            )).firstMatch
                            XCTAssertTrue(account.waitForExistence(timeout: 10))
                            if !account.isHittable { app.swipeUp(velocity: .slow) }
                            account.tap()
                            XCTAssertTrue(app.webViews.firstMatch.buttons[chinese ? "在此设备退出" : "Sign out on this device"].waitForExistence(timeout: 10))
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
        XCTAssertTrue(SheetFixtureReadiness.waitForText(text, in: app), text)
    }

}
