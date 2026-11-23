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
                            SheetFixtureReadiness.requireFailedSelection(in: app)
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
                        let marker: String? = state == "first-run"
                            ? (chinese ? "想从哪里开始？" : "Where shall we begin?")
                            : isBRoute ? nil : (chinese ? "正在使用" : "You’re in")
                        let attachment = XCTAttachment(screenshot: settledScreenshot(of: app, showing: marker))
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
            var attemptedTap = false
            if armed {
                arm.tap()
                attemptedTap = SheetFixtureReadiness.tapRow(containing: "Lab", in: app)
            }
            let surface = app.otherElements["fixture-document-surface"]
            let replied = XCTNSPredicateExpectation(
                predicate: NSPredicate { _, _ in
                    SheetFixtureReadiness.hasBridgeStages(
                        ["selection-rejected-invalid_instance"], in: surface.label
                    )
                }, object: nil
            )
            _ = XCTWaiter.wait(for: [replied], timeout: 10)
            let data = surface.label.data(using: .utf8) ?? Data()
            let evidence = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            let events = evidence["events"] as? [[String: Any]] ?? []
            trials.append([
                "trial": trial, "armed": armed, "attemptedTap": attemptedTap,
                "nativeTouch": events.contains { ($0["kind"] as? String) == "native-touch-began" },
                "rowPointerdown": events.contains { ($0["kind"] as? String) == "pointerdown" && ($0["onLab"] as? Bool) == true },
                "rowClick": events.contains { ($0["kind"] as? String) == "click" && ($0["onLab"] as? Bool) == true },
                "bridgeStage": evidence["bridgeStage"] ?? "unobserved",
                "bridgeStages": evidence["bridgeStages"] ?? [],
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

    /// For the SDK comparison (`ios-sdk-comparison`, which sets
    /// `AVIBE_NATIVE_CAPTURES`): the screen once its picture has stopped
    /// changing, two screenshots in a row with the same pixels, so a row
    /// still expanding or a list still fading in is not caught mid-way,
    /// differently on each run. And, when `text` is given, once that text
    /// is drawn where the page says it is: the page can be ready while
    /// WebKit has not painted it yet, and two blank pictures are also
    /// equal. Elsewhere the first screenshot, as before.
    private func settledScreenshot(of app: XCUIApplication, showing text: String?) -> XCUIScreenshot {
        var shot = app.screenshot()
        guard ProcessInfo.processInfo.environment["AVIBE_NATIVE_CAPTURES"] == "1" else {
            return shot
        }
        let element = text.map {
            app.webViews.firstMatch.staticTexts.matching(NSPredicate(format: "label == %@", $0)).firstMatch
        }
        for _ in 0..<30 {
            Thread.sleep(forTimeInterval: 0.3)
            let next = app.screenshot()
            let settled = next.pngRepresentation == shot.pngRepresentation
            shot = next
            if settled, element.map({ isDrawn($0.frame, in: next) }) ?? true { break }
        }
        if let text, let element { XCTAssertTrue(isDrawn(element.frame, in: shot), "Not drawn: \(text)") }
        return shot
    }

    /// Whether the screenshot shows more than one colour inside `frame`
    /// (window points): text drawn there, not an empty background.
    private func isDrawn(_ frame: CGRect, in shot: XCUIScreenshot) -> Bool {
        let image = shot.image
        guard !frame.isEmpty, let cgImage = image.cgImage else { return false }
        let scale = image.scale
        let pixels = CGRect(
            x: frame.minX * scale, y: frame.minY * scale,
            width: frame.width * scale, height: frame.height * scale
        ).integral.intersection(CGRect(x: 0, y: 0, width: cgImage.width, height: cgImage.height))
        guard !pixels.isEmpty, let crop = cgImage.cropping(to: pixels) else { return false }
        let width = crop.width, height = crop.height
        var buffer = [UInt8](repeating: 0, count: width * height * 4)
        let drawn = buffer.withUnsafeMutableBytes { bytes -> Bool in
            guard let context = CGContext(
                data: bytes.baseAddress, width: width, height: height, bitsPerComponent: 8,
                bytesPerRow: width * 4, space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            ) else { return false }
            context.draw(crop, in: CGRect(x: 0, y: 0, width: width, height: height))
            return true
        }
        guard drawn else { return false }
        let first = Array(buffer[0..<3])
        var differing = 0
        for index in stride(from: 0, to: buffer.count, by: 4)
        where abs(Int(buffer[index]) - Int(first[0])) + abs(Int(buffer[index + 1]) - Int(first[1]))
            + abs(Int(buffer[index + 2]) - Int(first[2])) > 60 {
            differing += 1
        }
        // A line of text inks a good share of its box; noise or a gradient does not.
        return differing * 50 > width * height
    }

    private func requireText(_ text: String, in app: XCUIApplication) {
        XCTAssertTrue(SheetFixtureReadiness.waitForText(text, in: app), text)
    }

}
