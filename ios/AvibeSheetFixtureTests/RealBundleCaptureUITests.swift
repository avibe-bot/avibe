import XCTest

/// On-demand only: ios-shell skips this class; ios-mobile-captures selects it.
/// All state comes from the fixture's native fake transport and real user taps.
final class RealBundleCaptureUITests: XCTestCase {
    private var bDiagnosticApp: XCUIApplication?
    private var bDiagnosticName: String?

    override func record(_ issue: XCTIssue) {
        if let app = bDiagnosticApp, let name = bDiagnosticName {
            let hierarchy = XCTAttachment(string: app.debugDescription)
            hierarchy.name = "\(name)-accessibility-tree"
            hierarchy.lifetime = .keepAlways
            add(hierarchy)
        }
        super.record(issue)
    }

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
                        let isBRoute = state == "instances-named" || state == "account-root"
                        if isBRoute {
                            bDiagnosticApp = app
                            bDiagnosticName = name
                        }
                        app.launchArguments = [
                            "-AppleLanguages", "(\(language))",
                            "-AppleLocale", language == "zh" ? "zh_CN" : "en_US",
                        ]
                        app.launchEnvironment["AVIBE_FIXTURE_CAPTURE_STATE"] = state
                        app.launchEnvironment["AVIBE_FIXTURE_CAPTURE_THEME"] = theme
                        app.launch()
                        defer {
                            app.terminate()
                            bDiagnosticApp = nil
                            bDiagnosticName = nil
                        }
                        let chinese = language == "zh"
                        SheetFixtureReadiness.waitForDocument(in: app)
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
                            requireBState("Named instance sheet must expose its current-instance button") {
                                app.webViews.firstMatch.staticTexts.matching(NSPredicate(
                                    format: "label == %@", chinese ? "正在使用" : "You’re in"
                                )).firstMatch.exists
                                    && app.webViews.firstMatch.buttons.matching(NSPredicate(
                                        format: "label CONTAINS %@ AND label != %@", "Studio",
                                        chinese ? "关闭，回到 Studio" : "Close and return to Studio"
                                    )).firstMatch.exists
                                    && app.webViews.firstMatch.buttons.matching(NSPredicate(
                                        format: "label == %@", chinese ? "关闭，回到 Studio" : "Close and return to Studio"
                                    )).firstMatch.exists
                            }
                            XCTAssertFalse(app.webViews.firstMatch.staticTexts.matching(NSPredicate(
                                format: "label == %@", chinese ? "账号" : "Account"
                            )).firstMatch.exists)
                            XCTAssertFalse(app.webViews.firstMatch.buttons.matching(NSPredicate(
                                format: "label CONTAINS %@", "alex@example.invalid"
                            )).firstMatch.exists)
                        case "account-root":
                            // These are the same AX roles exercised by the A Account cell:
                            // sign-out/Close are buttons; the email is static text.
                            requireBState("Account root must expose sign-out, email and Close") {
                                app.webViews.firstMatch.buttons.matching(NSPredicate(
                                    format: "label == %@", chinese ? "在此设备退出" : "Sign out on this device"
                                )).firstMatch.exists
                                    && app.webViews.firstMatch.staticTexts.matching(NSPredicate(
                                        format: "label == %@", "alex@example.invalid"
                                    )).firstMatch.exists
                                    && app.webViews.firstMatch.buttons.matching(NSPredicate(
                                        format: "label == %@", chinese ? "关闭" : "Close"
                                    )).firstMatch.exists
                            }
                            XCTAssertFalse(app.webViews.firstMatch.buttons.matching(NSPredicate(
                                format: "label == %@", chinese ? "实例" : "Instances"
                            )).firstMatch.exists)
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

    /// Informational only: selected explicitly by the on-demand workflow, never
    /// by ordinary ios-shell or the normal capture-matrix selector.
    func testInformationalMainThreadStallInputReceipts() {
        continueAfterFailure = true
        var trials: [[String: Any]] = []
        for trial in 1...10 {
            let app = XCUIApplication()
            app.launchArguments = ["-AppleLanguages", "(en)", "-AppleLocale", "en_US"]
            app.launchEnvironment["AVIBE_FIXTURE_CAPTURE_STATE"] = "open-failed"
            app.launchEnvironment["AVIBE_FIXTURE_CAPTURE_THEME"] = "light"
            app.launchEnvironment["AVIBE_FIXTURE_STALL_DIAGNOSTIC"] = "1"
            app.launch()
            SheetFixtureReadiness.waitForDocument(in: app)
            let arm = app.buttons["fixture-arm-stall"]
            let armed = arm.waitForExistence(timeout: 10)
            if armed {
                arm.tap()
                SheetFixtureReadiness.tapRow(containing: "Lab", in: app)
            }
            let surface = app.otherElements["fixture-document-surface"]
            let replied = XCTNSPredicateExpectation(
                predicate: NSPredicate { _, _ in
                    surface.label.contains("\"bridgeStage\":\"selection-rejected-invalid_instance\"")
                }, object: nil
            )
            _ = XCTWaiter.wait(for: [replied], timeout: 10)
            let data = surface.label.data(using: .utf8) ?? Data()
            let evidence = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            let events = evidence["events"] as? [[String: Any]] ?? []
            trials.append([
                "trial": trial, "armed": armed,
                "nativeTouch": events.contains { ($0["kind"] as? String) == "native-touch-began" },
                "rowPointerdown": events.contains { ($0["kind"] as? String) == "pointerdown" && ($0["onLab"] as? Bool) == true },
                "rowClick": events.contains { ($0["kind"] as? String) == "click" && ($0["onLab"] as? Bool) == true },
                "bridgeStage": evidence["bridgeStage"] ?? "unobserved",
                "evidence": evidence,
            ])
            app.terminate()
        }
        let data = try! JSONSerialization.data(withJSONObject: trials, options: [.prettyPrinted, .sortedKeys])
        let report = XCTAttachment(data: data, uniformTypeIdentifier: "public.json")
        report.name = "informational-stall-input-10-trials"
        report.lifetime = .keepAlways
        add(report)
        // XCTest quiescence may defer the tap until AFTER the stall. The recorded
        // actual event times, not the requested schedule, determine any overlap.
    }

    private func requireBState(_ description: String, matching matches: @escaping () -> Bool) {
        let ready = XCTNSPredicateExpectation(
            predicate: NSPredicate { _, _ in matches() }, object: nil
        )
        XCTAssertEqual(XCTWaiter.wait(for: [ready], timeout: 10), .completed, description)
    }

    private func requireText(_ text: String, in app: XCUIApplication) {
        XCTAssertTrue(SheetFixtureReadiness.waitForText(text, in: app), text)
    }

}
