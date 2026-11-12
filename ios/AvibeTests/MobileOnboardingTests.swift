import AuthenticationServices
import WebKit
import XCTest
@testable import Avibe

@MainActor
final class MobileOnboardingTests: XCTestCase {
    func testBundledPageLoadsItsReactApplicationThroughTheAppScheme() async throws {
        let resourceDirectoryURL = try XCTUnwrap(
            Bundle.main.resourceURL?.appendingPathComponent(
                "MobileApp",
                isDirectory: true
            )
        )
        let schemeHandler = MobileBundleSchemeHandler(
            rootDirectoryURL: resourceDirectoryURL
        )
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.setURLSchemeHandler(
            schemeHandler,
            forURLScheme: MobileBundleSchemeHandler.scheme
        )
        let webView = WKWebView(frame: .zero, configuration: configuration)
        let navigation = WebViewLoadProbe()
        webView.navigationDelegate = navigation

        webView.load(
            URLRequest(
                url: URL(string: "avibe-app://bundle/mobile.html")!
            )
        )
        try await navigation.waitForLoad()

        let rendered = try await waitForJavaScriptValue(in: webView) {
            """
            document.querySelector("#root button") !== null
            """
        }
        XCTAssertTrue(rendered)
    }

    func testBundledPageFillsItsContainerAndUsesTheKeyboardViewportOwner() throws {
        let controller = MobileOnboardingViewController(
            coordinator: makeCoordinator(
                defaults: makeDefaults(),
                transports: [FakeMobileAuthTransport()]
            )
        )
        controller.loadViewIfNeeded()

        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        let constraints = controller.view.constraints
        XCTAssertTrue(
            constraints.contains {
                $0.firstItem === webView
                    && $0.firstAttribute == .top
                    && $0.secondItem === controller.view
                    && $0.secondAttribute == .top
            }
        )
        XCTAssertTrue(
            constraints.contains {
                $0.firstItem === webView
                    && $0.firstAttribute == .leading
                    && $0.secondItem === controller.view
                    && $0.secondAttribute == .leading
            }
        )
        XCTAssertTrue(
            constraints.contains {
                $0.firstItem === webView
                    && $0.firstAttribute == .trailing
                    && $0.secondItem === controller.view
                    && $0.secondAttribute == .trailing
            }
        )
        XCTAssertTrue(
            constraints.contains {
                $0.firstItem === webView
                    && $0.firstAttribute == .bottom
                    && $0.secondItem === controller.view
                    && $0.secondAttribute == .bottom
            }
        )
    }

    func testBundledPageReachesTheWindowEdgesAndPadsEachSafeAreaOnce() async throws {
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(accountID: "account-1")
        let controller = MobileOnboardingViewController(
            coordinator: makeCoordinator(
                defaults: makeDefaults(),
                transports: [transport]
            )
        )
        let window = try await makeHomeIndicatorWindow(
            rootViewController: controller
        )
        defer {
            window.rootViewController = nil
            window.isHidden = true
        }
        controller.beginAppearanceTransition(true, animated: false)
        controller.endAppearanceTransition()
        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        let loaded = try await waitForJavaScriptValue(in: webView) {
            """
            document.body.textContent.includes("Personal Avibe")
            """
        }
        XCTAssertTrue(loaded)

        window.layoutIfNeeded()
        let frame = webView.convert(webView.bounds, to: window)
        XCTAssertEqual(frame.minY, window.bounds.minY, accuracy: 0.5)
        XCTAssertEqual(frame.maxY, window.bounds.maxY, accuracy: 0.5)

        // The page pads each edge with max(1.5rem, env(safe-area-inset-*)),
        // so a second, native inset would show as a WebKit content inset.
        let safeAreaInsets = window.safeAreaInsets
        let paddedOnce = try await waitForJavaScriptValue(in: webView) {
            """
            (() => {
              const column = document.querySelector("main > div");
              if (!column) return false;
              const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
              const style = getComputedStyle(column);
              return Math.abs(
                  parseFloat(style.paddingTop) - Math.max(1.5 * rem, \(safeAreaInsets.top))
                ) < 0.5
                && Math.abs(
                  parseFloat(style.paddingBottom) - Math.max(1.5 * rem, \(safeAreaInsets.bottom))
                ) < 0.5;
            })()
            """
        }
        XCTAssertTrue(paddedOnce)
        XCTAssertEqual(webView.scrollView.adjustedContentInset.top, 0)
        XCTAssertEqual(webView.scrollView.adjustedContentInset.bottom, 0)

        // The gradient viewport ends at the screen bottom, behind the Home
        // indicator.
        let viewportBottom = try await webView.evaluateJavaScript(
            """
            document.querySelector("main").getBoundingClientRect().bottom
            """
        ) as? Double
        XCTAssertEqual(
            try XCTUnwrap(viewportBottom),
            Double(webView.bounds.height),
            accuracy: 0.5
        )
    }

    func testManyInstancesScrollsTheLastCardFullyIntoViewAndSelectsIt() async throws {
        let instances = (1...8).map { index in
            MobileInstance(
                id: "instance-\(index)",
                name: "Instance \(index)",
                host: "instance-\(index).avibe.bot",
                url: URL(string: "https://instance-\(index).avibe.bot")!,
                relationship: index.isMultiple(of: 2) ? "shared" : "owner",
                ownerEmail: index.isMultiple(of: 2)
                    ? "owner-\(index)@example.com"
                    : nil,
                available: true
            )
        }
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(
            accountID: "account-1",
            instances: instances
        )
        let selected = expectation(description: "last instance selected")
        var selectedInstance: MobileInstance?
        let controller = MobileOnboardingViewController(
            coordinator: makeCoordinator(
                defaults: makeDefaults(),
                transports: [transport],
                onOpen: {
                    selectedInstance = $0.instance
                    selected.fulfill()
                }
            )
        )
        let (window, webView) = try present(
            controller,
            size: CGSize(width: 390, height: 620)
        )
        defer {
            window.isHidden = true
        }

        let rendered = try await waitForJavaScriptValue(in: webView) {
            """
            Array.from(document.querySelectorAll("button")).some(
              (button) => button.textContent?.includes("Instance 8")
            )
            """
        }
        XCTAssertTrue(rendered)

        let overflows = try await webView.evaluateJavaScript(
            """
            (() => {
              const viewport = document.querySelector("main");
              return Boolean(viewport && viewport.scrollHeight > viewport.clientHeight);
            })()
            """
        ) as? Bool
        XCTAssertEqual(overflows, true)

        _ = try await webView.evaluateJavaScript(
            """
            (() => {
              const target = Array.from(document.querySelectorAll("button")).find(
                (button) => button.textContent?.includes("Instance 8")
              );
              target?.scrollIntoView({block: "end"});
              return Boolean(target);
            })()
            """
        )
        let fullyVisible = try await waitForJavaScriptValue(in: webView) {
            """
            (() => {
              const viewport = document.querySelector("main");
              const target = Array.from(document.querySelectorAll("button")).find(
                (button) => button.textContent?.includes("Instance 8")
              );
              if (!viewport || !target || viewport.scrollTop <= 0) return false;
              const viewportRect = viewport.getBoundingClientRect();
              const targetRect = target.getBoundingClientRect();
              return targetRect.top >= viewportRect.top - 1
                && targetRect.bottom <= viewportRect.bottom + 1;
            })()
            """
        }
        XCTAssertTrue(fullyVisible)

        _ = try await webView.evaluateJavaScript(
            """
            (() => {
              const target = Array.from(document.querySelectorAll("button")).find(
                (button) => button.textContent?.includes("Instance 8")
              );
              target?.click();
              return Boolean(target);
            })()
            """
        )
        await fulfillment(of: [selected], timeout: 30)
        XCTAssertEqual(selectedInstance, instances.last)
    }

    func testOTPActionRemainsReachableInAReducedHeightWebView() async throws {
        let signedOutTransport = FakeMobileAuthTransport()
        let loginTransport = FakeMobileAuthTransport()
        loginTransport.session = snapshot(accountID: "account-1")
        let controller = MobileOnboardingViewController(
            coordinator: makeCoordinator(
                defaults: makeDefaults(),
                transports: [signedOutTransport, loginTransport]
            )
        )
        let (window, webView) = try present(
            controller,
            size: CGSize(width: 320, height: 360)
        )
        defer {
            window.isHidden = true
        }

        let welcome = try await waitForJavaScriptValue(in: webView) {
            """
            (() => {
              const button = document.querySelector("main button");
              return button instanceof HTMLButtonElement && !button.disabled;
            })()
            """
        }
        XCTAssertTrue(welcome)
        let openedEmail = try await webView.evaluateJavaScript(
            """
            (() => {
              const button = document.querySelector("main button");
              if (!(button instanceof HTMLButtonElement) || button.disabled) {
                return false;
              }
              button.click();
              return true;
            })()
            """
        ) as? Bool
        XCTAssertEqual(openedEmail, true)

        let emailReady = try await waitForJavaScriptValue(in: webView) {
            """
            document.querySelector("#mobile-login-email") !== null
            """
        }
        XCTAssertTrue(emailReady)
        let enteredEmail = try await replaceText(
            "alex@example.com",
            in: webView,
            selector: "#mobile-login-email"
        )
        XCTAssertTrue(enteredEmail)
        let emailSubmitReady = try await waitForJavaScriptValue(in: webView) {
            """
            (() => {
              const button = document.querySelector(
                "form button[type=submit]"
              );
              return button instanceof HTMLButtonElement && !button.disabled;
            })()
            """
        }
        XCTAssertTrue(emailSubmitReady)
        let submittedEmail = try await webView.evaluateJavaScript(
            """
            (() => {
              const button = document.querySelector(
                "form button[type=submit]"
              );
              if (!(button instanceof HTMLButtonElement) || button.disabled) {
                return false;
              }
              button.click();
              return true;
            })()
            """
        ) as? Bool
        XCTAssertEqual(submittedEmail, true)

        let codeReady = try await waitForJavaScriptValue(in: webView) {
            """
            document.querySelector("#mobile-login-code") !== null
            """
        }
        XCTAssertTrue(codeReady)
        // One digit short: the sixth would send the code before the layout
        // checks below.
        let enteredCode = try await replaceText(
            "12345",
            in: webView,
            selector: "#mobile-login-code"
        )
        XCTAssertTrue(enteredCode)

        window.frame = CGRect(x: 0, y: 0, width: 320, height: 280)
        controller.view.frame = window.bounds
        controller.view.setNeedsLayout()
        controller.view.layoutIfNeeded()

        let focusedWithoutOuterRing = try await waitForJavaScriptValue(in: webView) {
            """
            (() => {
              const input = document.querySelector("#mobile-login-code");
              const wrapper = input?.parentElement;
              return document.activeElement === input
                && !wrapper?.className.includes("focus-within:ring");
            })()
            """
        }
        XCTAssertTrue(focusedWithoutOuterRing)

        let overflows = try await webView.evaluateJavaScript(
            """
            (() => {
              const viewport = document.querySelector("main");
              return Boolean(viewport && viewport.scrollHeight > viewport.clientHeight);
            })()
            """
        ) as? Bool
        XCTAssertEqual(overflows, true)

        let scrolledToActions = try await webView.evaluateJavaScript(
            """
            (() => {
              const target = document.querySelector("[data-mobile-code-actions]");
              target?.scrollIntoView({block: "end"});
              return Boolean(target);
            })()
            """
        ) as? Bool
        XCTAssertEqual(scrolledToActions, true)

        let verifyVisible = try await waitForJavaScriptValue(in: webView) {
            """
            (() => {
              const viewport = document.querySelector("main");
              const target = document.querySelector("[data-mobile-code-actions] button");
              if (!viewport || !target) return false;
              const viewportRect = viewport.getBoundingClientRect();
              const targetRect = target.getBoundingClientRect();
              return viewport.scrollTop > 0
                && targetRect.top >= viewportRect.top - 1
                && targetRect.bottom <= viewportRect.bottom + 1;
            })()
            """
        }
        XCTAssertTrue(verifyVisible)

        let completedCode = try await webView.callAsyncJavaScript(
            """
            const input = document.querySelector("#mobile-login-code");
            if (!(input instanceof HTMLInputElement)) return false;
            input.focus();
            input.setSelectionRange(input.value.length, input.value.length);
            return document.execCommand("insertText", false, "6");
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        ) as? Bool
        XCTAssertEqual(completedCode, true)
        let signedIn = try await waitForJavaScriptValue(in: webView) {
            """
            document.body.textContent.includes("Personal Avibe")
            """
        }
        XCTAssertTrue(signedIn)
    }

    func testBundledPageInstallsTheMobileBridgeAtItsTrustedOrigin() async throws {
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(accountID: "account-1")
        let controller = MobileOnboardingViewController(
            coordinator: makeCoordinator(
                defaults: makeDefaults(),
                transports: [transport]
            )
        )
        let window = UIWindow(frame: UIScreen.main.bounds)
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.beginAppearanceTransition(true, animated: false)
        controller.endAppearanceTransition()

        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        let loaded = try await waitForJavaScriptValue(in: webView) {
            """
            document.body.textContent.includes("Personal Avibe")
            """
        }
        XCTAssertTrue(loaded)

        let evaluated = try await webView.evaluateJavaScript(
            """
            JSON.stringify({
              href: window.location.href,
              protocol: window.location.protocol,
              hostname: window.location.hostname,
              port: window.location.port,
              bridge: typeof window.avibeMobile
            })
            """
        )
        let details = try XCTUnwrap(evaluated as? String)
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: Data(details.utf8)
            ) as? [String: String]
        )
        XCTAssertEqual(object["href"], "avibe-app://bundle/mobile.html#instances")
        XCTAssertEqual(object["protocol"], "avibe-app:")
        XCTAssertEqual(object["hostname"], "bundle")
        XCTAssertEqual(object["port"], "")
        XCTAssertEqual(object["bridge"], "object", details)
    }

    func testBundledPageSendsACompletedAuthenticatorCodeThroughTheBridge() async throws {
        let transport = FakeMobileAuthTransport()
        transport.secondFactor = MobileSecondFactorRequirement(
            email: "alex@example.com",
            gate: .challenge,
            methods: [.totp]
        )
        let proved = snapshot(accountID: "account-1")
        transport.verifySecondFactorHandler = { _ in
            transport.secondFactor = nil
            transport.session = proved
            return .signedIn(proved)
        }
        let controller = MobileOnboardingViewController(
            coordinator: makeCoordinator(
                defaults: makeDefaults(),
                transports: [transport]
            )
        )
        let (window, webView) = try present(
            controller,
            size: CGSize(width: 390, height: 844)
        )
        defer {
            window.isHidden = true
        }

        let owed = try await waitForJavaScriptValue(in: webView) {
            """
            document.querySelector("#mobile-login-code") !== null
              && document.body.textContent.includes("alex@example.com")
            """
        }
        XCTAssertTrue(owed)
        let entered = try await replaceText(
            "287082",
            in: webView,
            selector: "#mobile-login-code"
        )
        XCTAssertTrue(entered)

        // The sixth digit sends the code; no button press is needed.
        let signedIn = try await waitForJavaScriptValue(in: webView) {
            """
            document.body.textContent.includes("Personal Avibe")
            """
        }
        XCTAssertTrue(signedIn)
        XCTAssertEqual(transport.proofs.count, 1)
        XCTAssertEqual(transport.proofs.first?.codeProof?.0, .totp)
        XCTAssertEqual(transport.proofs.first?.codeProof?.1, "287082")
    }

    func testMobileBridgeMethodSurfaceIsClosed() {
        XCTAssertEqual(
            Set(MobileBridgeMethod.allCases.map(\.rawValue)),
            [
                "session.bootstrap",
                "auth.requestOtp",
                "auth.verifyOtp",
                "auth.signInWithPassword",
                "auth.verifySecondFactor",
                "auth.verifyPasskey",
                "session.refresh",
                "session.selectInstance",
                "session.logout",
                "app.openExternal",
                "app.returnToInstance",
            ]
        )
        XCTAssertThrowsError(
            try MobileBridgeRequest.parse(["method": "auth.fetch"])
        )
    }

    func testBundledPageSignsInWithThePasswordThroughTheBridge() async throws {
        let signedOutTransport = FakeMobileAuthTransport()
        let loginTransport = FakeMobileAuthTransport()
        let signedIn = snapshot(accountID: "account-1")
        loginTransport.signInWithPasswordHandler = { _, _ in
            MobilePasswordSignInDiscovery(
                discovery: .signedIn(signedIn),
                weakPasswordReasons: nil
            )
        }
        let controller = MobileOnboardingViewController(
            coordinator: makeCoordinator(
                defaults: makeDefaults(),
                transports: [signedOutTransport, loginTransport]
            )
        )
        let (window, webView) = try present(
            controller,
            size: CGSize(width: 390, height: 844)
        )
        defer {
            window.isHidden = true
        }

        let welcome = try await waitForJavaScriptValue(in: webView) {
            """
            (() => {
              const button = document.querySelector("main button");
              return button instanceof HTMLButtonElement && !button.disabled;
            })()
            """
        }
        XCTAssertTrue(welcome)
        let openedEmail = try await webView.evaluateJavaScript(
            """
            (() => {
              const button = document.querySelector("main button");
              if (!(button instanceof HTMLButtonElement) || button.disabled) {
                return false;
              }
              button.click();
              return true;
            })()
            """
        ) as? Bool
        XCTAssertEqual(openedEmail, true)
        let emailReady = try await waitForJavaScriptValue(in: webView) {
            """
            document.querySelector("#mobile-login-email") !== null
            """
        }
        XCTAssertTrue(emailReady)

        // "Sign in with password" is the email form's one secondary button. It
        // switches at once, before any email is typed.
        let openedPassword = try await webView.evaluateJavaScript(
            """
            (() => {
              const buttons = document.querySelectorAll("form button[type=button]");
              const button = buttons[0];
              if (buttons.length !== 1 || !(button instanceof HTMLButtonElement) || button.disabled) {
                return false;
              }
              button.click();
              return true;
            })()
            """
        ) as? Bool
        XCTAssertEqual(openedPassword, true)
        // One form: the visible email field is the saved login's username.
        let passwordReady = try await waitForJavaScriptValue(in: webView) {
            """
            (() => {
              const email = document.querySelector("#mobile-login-email");
              const password = document.querySelector("#mobile-login-password");
              return email instanceof HTMLInputElement
                && password instanceof HTMLInputElement
                && email.type === "email"
                && email.autocomplete === "username"
                && !email.readOnly
                && email.value === ""
                && document.activeElement === email
                && password.type === "password"
                && password.autocomplete === "current-password"
                && email.form === password.form
                && document.querySelectorAll("input[autocomplete=username]").length === 1;
            })()
            """
        }
        XCTAssertTrue(passwordReady)
        let enteredEmail = try await replaceText(
            "alex@example.com",
            in: webView,
            selector: "#mobile-login-email"
        )
        XCTAssertTrue(enteredEmail)
        let enteredPassword = try await replaceText(
            "correct horse battery",
            in: webView,
            selector: "#mobile-login-password"
        )
        XCTAssertTrue(enteredPassword)
        let submitted = try await webView.evaluateJavaScript(
            """
            (() => {
              const button = document.querySelector("form button[type=submit]");
              if (!(button instanceof HTMLButtonElement) || button.disabled) {
                return false;
              }
              button.click();
              return true;
            })()
            """
        ) as? Bool
        XCTAssertEqual(submitted, true)

        let reachedInstances = try await waitForJavaScriptValue(in: webView) {
            """
            document.body.textContent.includes("Personal Avibe")
            """
        }
        XCTAssertTrue(reachedInstances)
        XCTAssertEqual(loginTransport.passwordAttempts.map(\.email), ["alex@example.com"])
        XCTAssertEqual(
            loginTransport.passwordAttempts.map(\.password),
            ["correct horse battery"]
        )
    }

    func testBridgeRequestDescriptionsNameParametersWithoutValues() throws {
        let request = try MobileBridgeRequest.parse([
            "method": "auth.signInWithPassword",
            "params": [
                "email": "alex@example.com",
                "password": "correct horse battery",
            ],
        ])
        XCTAssertEqual(request.method, .authSignInWithPassword)
        var dumped = ""
        dump(request, to: &dumped)
        for rendered in [
            String(describing: request),
            String(reflecting: request),
            "\(request)",
            dumped,
        ] {
            XCTAssertFalse(rendered.contains("correct horse battery"), rendered)
            XCTAssertFalse(rendered.contains("alex@example.com"), rendered)
            XCTAssertTrue(rendered.contains("password"), rendered)
        }
    }

    func testBundledOriginAndResourcePathStayClosed() throws {
        XCTAssertTrue(
            MobileBundleSchemeHandler.isTrustedBundleURL(
                URL(string: "avibe-app://bundle/mobile.html#account")
            )
        )
        for url in [
            "avibe-app://bundle:443/mobile.html",
            "avibe-app://user@bundle/mobile.html",
            "avibe-app://other/mobile.html",
            "https://bundle/mobile.html",
        ] {
            XCTAssertFalse(
                MobileBundleSchemeHandler.isTrustedBundleURL(URL(string: url)),
                url
            )
        }

        let handler = MobileBundleSchemeHandler(
            rootDirectoryURL: URL(fileURLWithPath: "/tmp/MobileApp")
        )
        XCTAssertThrowsError(
            try handler.resolveResourceURL(
                URL(string: "avibe-app://bundle/%2E%2E/Info.plist")!
            )
        )
    }

    func testRuntimeBridgeKeepsCapabilityMethodsAndInternalHistorySignalClosed() throws {
        let showAccountRequest = try XCTUnwrap(
            NativeBridgeRequest.parse(["method": "app.showAccount"]).success
        )
        XCTAssertEqual(showAccountRequest.method, .appShowAccount)
        let registrationRequest = try XCTUnwrap(
            NativeBridgeRequest.parse(
                ["method": "app.setAccountActionAvailable"]
            ).success
        )
        XCTAssertEqual(
            registrationRequest.method,
            .appSetAccountActionAvailable
        )
        let historyRequest = try XCTUnwrap(
            NativeBridgeRequest.parse(
                ["method": "navigation.historyChanged"]
            ).success
        )
        XCTAssertEqual(historyRequest.method, .navigationHistoryChanged)
        XCTAssertNil(NativeShellMetadata.capabilities["navigation"])
        XCTAssertEqual(
            Set(NativeBridgeMethod.allCases.map(\.rawValue)),
            [
                "appearance.colorSchemeChanged",
                "app.info",
                "app.openExternal",
                "app.setAccountActionAvailable",
                "app.showAccount",
                "badge.set",
                "direct.status",
                "haptics.impact",
                "haptics.notify",
                "haptics.selection",
                "navigation.historyChanged",
                "push.status",
                "push.subscribe",
                "push.unsubscribe",
                "push.openedAck",
                "viewport.setOuterScrollLocked",
                "voice.permission",
            ]
        )
    }

    func testRuntimeConfigurationUsesTheAccountWebsiteDataStore() {
        let store = WKWebsiteDataStore.nonPersistent()
        let configuration = NativeBridgeConfiguration.make(
            instanceOrigin: WebOrigin(url: instance.url)!,
            websiteDataStore: store,
            setOuterScrollLocked: { _, _ in true }
        )

        XCTAssertTrue(configuration.websiteDataStore === store)
    }

    func testSessionJSONUsesExplicitNulls() throws {
        let state = MobileSessionState.signedIn(
            snapshot: snapshot(accountID: "account-1"),
            selectedInstanceID: nil
        )

        XCTAssertTrue(JSONSerialization.isValidJSONObject(state.jsonObject))
        let data = try JSONSerialization.data(withJSONObject: state.jsonObject)
        let object = try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any]
        )
        XCTAssertTrue(object["selected_instance_id"] is NSNull)
        let instances = try XCTUnwrap(object["instances"] as? [[String: Any]])
        XCTAssertTrue(instances[0]["owner_email"] is NSNull)

        let owed = MobileSessionState.twoFactorRequired(
            MobileSecondFactorRequirement(email: nil, gate: .enroll, methods: [])
        ).jsonObject
        XCTAssertTrue(JSONSerialization.isValidJSONObject(owed))
        XCTAssertEqual(owed["status"] as? String, "twoFactorRequired")
        XCTAssertTrue(owed["email"] is NSNull)
        XCTAssertEqual(owed["gate"] as? String, "enroll")
        XCTAssertEqual(owed["methods"] as? [String], [])
    }

    func testExternalURLAllowlistAddsOnlyTheAccountSecurityPage() {
        XCTAssertTrue(
            MobileExternalURLPolicy.allows(
                URL(string: "https://avibe.bot/app/account/security")!
            )
        )
        for url in [
            "http://avibe.bot/app/account/security",
            "https://avibe.bot/app/account/security?next=/",
            "https://avibe.bot/app/account",
            "https://avibe.bot.example.com/app/account/security",
        ] {
            XCTAssertFalse(MobileExternalURLPolicy.allows(URL(string: url)!), url)
        }
    }

    func testExternalURLAllowlistAddsOnlyTheBundledLicenseSources() throws {
        let archive = "https://static.crates.io/crates/uniffi/uniffi-0.31.2.crate"
        let upstream = "https://github.com/mozilla/uniffi-rs/tree/05b45b110f652d58d94c653992aeb3adc3a094f4"
        let data = try JSONSerialization.data(withJSONObject: [
            "sources": [
                ["version": "0.31.2", "upstream": upstream, "crates": [["name": "uniffi", "registry": archive]]],
                ["version": "1", "upstream": "http://github.com/x/y", "crates": [["name": "x", "registry": "https://example.com/x.crate"]]],
            ],
            "groups": [["title": "MIT", "text": "https://github.com/linked/in-a-text", "components": []]],
        ])
        let sources = MobileExternalURLPolicy.licenseSourceURLs(from: data)
        XCTAssertEqual(sources, [archive, upstream])
        XCTAssertTrue(MobileExternalURLPolicy.allows(URL(string: archive)!, licenseSources: sources))
        XCTAssertTrue(MobileExternalURLPolicy.allows(URL(string: upstream)!, licenseSources: sources))
        for url in [
            "https://github.com/mozilla/uniffi-rs",
            "https://github.com/linked/in-a-text",
            "http://github.com/x/y",
            "https://example.com/x.crate",
            "\(archive)?download=1",
        ] {
            XCTAssertFalse(MobileExternalURLPolicy.allows(URL(string: url)!, licenseSources: sources), url)
        }
        XCTAssertEqual(MobileExternalURLPolicy.licenseSourceURLs(from: Data("not json".utf8)), [])
    }

    func testDebugControlPlaneOverrideIsBuildBounded() {
        let override = URL(string: "http://127.0.0.1:43127")!
        let configuration = MobileControlPlaneConfiguration(
            arguments: [
                "Avibe",
                "-AvibeControlPlaneURL",
                override.absoluteString,
            ]
        )

        #if DEBUG
        XCTAssertEqual(configuration.baseURL, override)
        #else
        XCTAssertEqual(
            configuration.baseURL,
            MobileControlPlaneConfiguration.productionURL
        )
        #endif
        XCTAssertFalse(
            MobileControlPlaneConfiguration.isAllowedDebugURL(
                URL(string: "http://example.com")!
            )
        )
    }

    func testSelectionRestoresOnlyForTheSameAuthorizedAccount() async throws {
        let defaults = makeDefaults()
        let firstTransport = FakeMobileAuthTransport()
        firstTransport.session = snapshot(accountID: "account-1")
        var opened: MobileInstance?
        let first = makeCoordinator(
            defaults: defaults,
            transports: [firstTransport],
            onOpen: { opened = $0.instance }
        )
        first.activateAccountSurface()

        _ = try await first.bootstrap()
        let selected = try first.selectInstance(id: instance.id)

        XCTAssertEqual(opened, selected)
        XCTAssertTrue(firstTransport.invalidated)

        let restoredTransport = FakeMobileAuthTransport()
        restoredTransport.session = snapshot(accountID: "account-1")
        let restored = makeCoordinator(
            defaults: defaults,
            transports: [restoredTransport]
        )
        restored.activateAccountSurface()
        let restoredState = try await restored.bootstrap()
        XCTAssertEqual(
            restored.restorableRuntime(from: restoredState)?.instance,
            instance
        )

        let otherAccountTransport = FakeMobileAuthTransport()
        otherAccountTransport.session = snapshot(accountID: "account-2")
        let replacementTransport = FakeMobileAuthTransport()
        let mismatchedStore = makeAccountStore(defaults: defaults)
        let mismatchedStoreIdentifier = mismatchedStore.identifier
        let otherAccount = makeCoordinator(
            defaults: defaults,
            websiteDataStore: mismatchedStore,
            transports: [otherAccountTransport, replacementTransport]
        )
        otherAccount.activateAccountSurface()
        let otherState = try await otherAccount.bootstrap()
        XCTAssertEqual(otherState, .signedOut)
        XCTAssertNotEqual(
            mismatchedStore.identifier,
            mismatchedStoreIdentifier
        )
        XCTAssertNil(otherAccount.restorableRuntime(from: otherState))
    }

    func testSignedOutBootstrapPreparesPushLogoutBeforeStoreReplacement()
        async throws
    {
        let defaults = makeDefaults()
        let transport = FakeMobileAuthTransport()
        var events: [String] = []
        var coordinator: MobileSessionCoordinator!
        transport.pushManagementHandler = { _ in
            events.append("push.management")
            return PushManagementResponse(statusCode: 200, body: [:])
        }
        coordinator = makeCoordinator(
            defaults: defaults,
            transports: [transport, FakeMobileAuthTransport()],
            onPrepareAccountTransition: {
                events.append("account.transition")
                return []
            },
            onPreparePushLogout: {
                events.append("push.logout")
                _ = try? await coordinator.pushManagementRequest(
                    PushManagementRequest(
                        method: "POST",
                        path: "/api/push/apns/installations/revoke",
                        headers: [:],
                        body: nil
                    )
                )
            }
        )
        coordinator.activateAccountSurface()

        let state = try await coordinator.bootstrap()
        XCTAssertEqual(state, .signedOut)
        XCTAssertEqual(
            events,
            ["push.logout", "push.management", "account.transition"]
        )
    }

    /// What the App icon badge checks before a runtime page sets it: an
    /// ordinary push-management request keeps the runtime selected; once
    /// signing out has begun (push revocation first), it is not.
    func testARuntimeStaysSelectedThroughPushManagementButNotOnceSignOutBegins()
        async throws
    {
        let defaults = makeDefaults()
        let first = FakeMobileAuthTransport()
        first.session = snapshot(accountID: "account-1")
        let management = FakeMobileAuthTransport()
        var runtime: MobileRuntimeContext?
        var coordinator: MobileSessionCoordinator!
        var duringManagement: [Bool] = []
        var duringPushLogout: [Bool] = []
        management.pushManagementHandler = { _ in
            duringManagement.append(coordinator.isSelectedRuntime(runtime!))
            duringManagement.append(coordinator.isCurrentRuntime(runtime!))
            return PushManagementResponse(statusCode: 200, body: [:])
        }
        coordinator = makeCoordinator(
            defaults: defaults,
            transports: [
                first,
                management,
                FakeMobileAuthTransport(),
                FakeMobileAuthTransport(),
                FakeMobileAuthTransport(),
            ],
            onOpen: { runtime = $0 },
            onPreparePushLogout: {
                duringPushLogout.append(coordinator.isSelectedRuntime(runtime!))
            }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)
        let opened = try XCTUnwrap(runtime)
        XCTAssertTrue(coordinator.isSelectedRuntime(opened))

        _ = try await coordinator.pushManagementRequest(
            PushManagementRequest(
                method: "POST",
                path: "/api/push/apns/installations",
                headers: [:],
                body: nil
            )
        )
        // Selected, though not idle, while the request runs.
        XCTAssertEqual(duringManagement, [true, false])
        XCTAssertTrue(coordinator.isSelectedRuntime(opened))

        _ = try await coordinator.logout()
        XCTAssertEqual(duringPushLogout, [false])
        XCTAssertFalse(coordinator.isSelectedRuntime(opened))
    }

    func testOtherAccountBootstrapUsesTheSamePushLogoutPreparation()
        async throws
    {
        let defaults = makeDefaults()
        let firstTransport = FakeMobileAuthTransport()
        firstTransport.session = snapshot(accountID: "account-1")
        let first = makeCoordinator(
            defaults: defaults,
            transports: [firstTransport]
        )
        first.activateAccountSurface()
        _ = try await first.bootstrap()
        _ = try first.selectInstance(id: instance.id)

        let otherTransport = FakeMobileAuthTransport()
        otherTransport.session = snapshot(accountID: "account-2")
        var pushLogoutCalls = 0
        let other = makeCoordinator(
            defaults: defaults,
            transports: [otherTransport, FakeMobileAuthTransport()],
            onPreparePushLogout: {
                pushLogoutCalls += 1
            }
        )
        other.activateAccountSurface()

        let state = try await other.bootstrap()
        XCTAssertEqual(state, .signedOut)
        XCTAssertEqual(pushLogoutCalls, 1)
    }

    func testRefreshFromAccountToAnotherAccountLogsOutPushWithoutSelection()
        async throws
    {
        let defaults = makeDefaults()
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(accountID: "account-1")
        var pushLogoutCalls = 0
        let coordinator = makeCoordinator(
            defaults: defaults,
            transports: [transport, FakeMobileAuthTransport()],
            onPreparePushLogout: {
                pushLogoutCalls += 1
            }
        )
        coordinator.activateAccountSurface()
        guard case .signedIn(_, nil) = try await coordinator.bootstrap() else {
            return XCTFail("Expected an account without a persisted selection")
        }

        transport.session = snapshot(accountID: "account-2")
        let state = try await coordinator.refresh()

        XCTAssertEqual(state, .signedOut)
        XCTAssertEqual(pushLogoutCalls, 1)
    }

    func testRefreshSignedOutAnswerLogsOutPushBeforeStoreReplacement()
        async throws
    {
        let defaults = makeDefaults()
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(accountID: "account-1")
        var pushLogoutCalls = 0
        let coordinator = makeCoordinator(
            defaults: defaults,
            transports: [transport, FakeMobileAuthTransport()],
            onPreparePushLogout: {
                pushLogoutCalls += 1
            }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()

        transport.session = nil
        let state = try await coordinator.refresh()
        XCTAssertEqual(state, .signedOut)
        XCTAssertEqual(pushLogoutCalls, 1)
    }

    func testHiddenTransportStopsBeforeRuntimeAndRestartsOnlyForAccountSurface() async throws {
        let defaults = makeDefaults()
        var events: [String] = []
        let firstTransport = FakeMobileAuthTransport()
        firstTransport.session = snapshot(accountID: "account-1")
        firstTransport.onInvalidate = {
            events.append("transport.invalidated")
        }
        let secondTransport = FakeMobileAuthTransport()
        let coordinator = makeCoordinator(
            defaults: defaults,
            transports: [firstTransport, secondTransport],
            onCreateTransport: {
                events.append("transport.created")
            },
            onOpen: { _ in
                events.append("runtime.opened")
            }
        )

        XCTAssertFalse(coordinator.isAccountSurfaceActive)
        coordinator.activateAccountSurface()
        XCTAssertTrue(coordinator.isAccountSurfaceActive)
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)

        XCTAssertFalse(coordinator.isAccountSurfaceActive)
        XCTAssertEqual(
            events,
            [
                "transport.created",
                "transport.invalidated",
                "runtime.opened",
            ]
        )

        coordinator.activateAccountSurface()
        XCTAssertTrue(coordinator.isAccountSurfaceActive)
        XCTAssertEqual(events.last, "transport.created")
    }

    func testAccountOverARuntimeStartsTheTransportOnlyAfterReleasingIt() async throws {
        for operation in ["refresh", "logout"] {
            let defaults = makeDefaults()
            var events: [String] = []
            let bootstrapTransport = FakeMobileAuthTransport()
            bootstrapTransport.session = snapshot(accountID: "account-1")
            let accountTransport = FakeMobileAuthTransport()
            accountTransport.session = snapshot(accountID: "account-1")
            accountTransport.logoutHandler = {
                events.append("transport.logout")
            }
            let coordinator = makeCoordinator(
                defaults: defaults,
                transports: [
                    bootstrapTransport,
                    accountTransport,
                    FakeMobileAuthTransport(),
                ],
                onCreateTransport: {
                    events.append("transport.created")
                },
                onPrepareAccountTransition: {
                    events.append("runtime.released")
                    return []
                }
            )
            coordinator.activateAccountSurface()
            _ = try await coordinator.bootstrap()
            _ = try coordinator.selectInstance(id: instance.id)
            events.removeAll()

            // The account page over the runtime reads the session snapshot.
            guard case .signedIn = try await coordinator.bootstrap() else {
                return XCTFail("Expected the covered runtime's account")
            }
            XCTAssertFalse(coordinator.isAccountSurfaceActive, operation)
            XCTAssertEqual(events, [], operation)

            if operation == "refresh" {
                _ = try await coordinator.refresh()
                XCTAssertEqual(
                    events,
                    ["runtime.released", "transport.created"],
                    operation
                )
            } else {
                let state = try await coordinator.logout()
                XCTAssertEqual(state, .signedOut)
                XCTAssertEqual(
                    events,
                    [
                        "runtime.released",
                        "transport.created",
                        "transport.logout",
                        "transport.created",
                    ],
                    operation
                )
            }
            XCTAssertTrue(coordinator.isAccountSurfaceActive, operation)
        }
    }

    func testLogoutRevokesRuntimeAdmissionBeforeTheFirstSuspension() async throws {
        let defaults = makeDefaults()
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(accountID: "account-1")
        var finishLogout: CheckedContinuation<Void, Never>?
        transport.logoutHandler = {
            await withCheckedContinuation { continuation in
                finishLogout = continuation
            }
        }
        var transitions = 0
        let coordinator = makeCoordinator(
            defaults: defaults,
            transports: [transport, FakeMobileAuthTransport()],
            onPrepareAccountTransition: {
                transitions += 1
                return []
            }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()

        let logout = Task {
            try await coordinator.logout()
        }
        await Task.yield()

        XCTAssertEqual(transitions, 1)
        XCTAssertThrowsError(try coordinator.selectInstance(id: instance.id)) {
            XCTAssertEqual($0 as? MobileAccountError, .busy)
        }

        finishLogout?.resume()
        let state = try await logout.value
        XCTAssertEqual(state, .signedOut)
    }

    func testAccountSwitchInvalidatesADelayedRuntimeOpen() async throws {
        let defaults = makeDefaults()
        let firstTransport = FakeMobileAuthTransport()
        firstTransport.session = snapshot(accountID: "account-1")
        let loginTransport = FakeMobileAuthTransport()
        var finishLogin: CheckedContinuation<MobileSessionSnapshot, Error>?
        loginTransport.verifyOTPHandler = { _, _ in
            try await withCheckedThrowingContinuation { continuation in
                finishLogin = continuation
            }
        }
        var pendingRuntime: MobileRuntimeContext?
        let coordinator = makeCoordinator(
            defaults: defaults,
            transports: [firstTransport, loginTransport],
            onOpen: {
                pendingRuntime = $0
            }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)
        let oldRuntime = try XCTUnwrap(pendingRuntime)
        XCTAssertTrue(coordinator.isCurrentRuntime(oldRuntime))

        coordinator.activateAccountSurface()
        let login = Task {
            try await coordinator.verifyOTP(
                email: "new-account@example.com",
                code: "654321"
            )
        }
        await Task.yield()

        XCTAssertFalse(coordinator.isCurrentRuntime(oldRuntime))
        finishLogin?.resume(returning: snapshot(accountID: "account-2"))
        guard case .signedIn(let fresh, _) = try await login.value else {
            return XCTFail("Expected the new account to sign in")
        }
        XCTAssertEqual(fresh.account.id, "account-2")
        XCTAssertFalse(coordinator.isCurrentRuntime(oldRuntime))
    }

    func testSingleFlightRejectsDuplicateOTPRequests() async throws {
        let defaults = makeDefaults()
        let bootstrapTransport = FakeMobileAuthTransport()
        let transport = FakeMobileAuthTransport()
        var finish: CheckedContinuation<Int, Error>?
        transport.requestOTPHandler = { _ in
            try await withCheckedThrowingContinuation { continuation in
                finish = continuation
            }
        }
        let coordinator = makeCoordinator(
            defaults: defaults,
            transports: [bootstrapTransport, transport]
        )
        coordinator.activateAccountSurface()
        let bootstrapState = try await coordinator.bootstrap()
        XCTAssertEqual(bootstrapState, .signedOut)

        let first = Task {
            try await coordinator.requestOTP(email: "alex@example.com")
        }
        await Task.yield()

        do {
            _ = try await coordinator.requestOTP(email: "alex@example.com")
            XCTFail("Expected a busy error")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error, .busy)
        }

        finish?.resume(returning: 17)
        let result = try await first.value
        XCTAssertEqual(result, 17)
    }

    func testSingleFlightRejectsDuplicateOTPVerificationWithoutStalingTheFirst() async throws {
        let defaults = makeDefaults()
        let transport = FakeMobileAuthTransport()
        var finish: CheckedContinuation<MobileSessionSnapshot, Error>?
        transport.verifyOTPHandler = { _, _ in
            try await withCheckedThrowingContinuation { continuation in
                finish = continuation
            }
        }
        let coordinator = makeCoordinator(
            defaults: defaults,
            transports: [transport]
        )
        coordinator.activateAccountSurface()

        let first = Task {
            try await coordinator.verifyOTP(
                email: "alex@example.com",
                code: "123456"
            )
        }
        await Task.yield()

        do {
            _ = try await coordinator.verifyOTP(
                email: "alex@example.com",
                code: "123456"
            )
            XCTFail("Expected a busy error")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error, .busy)
        }

        finish?.resume(returning: snapshot(accountID: "account-1"))
        let state = try await first.value
        guard case .signedIn(let fresh, _) = state else {
            return XCTFail("Expected the first verification to succeed")
        }
        XCTAssertEqual(fresh.account.id, "account-1")
    }

    func testLogoutRotatesStoreAndRejectsLateResultsEvenWhenRemoteLogoutFails() async throws {
        let defaults = makeDefaults()
        let bootstrapTransport = FakeMobileAuthTransport()
        let oldTransport = FakeMobileAuthTransport()
        var finish: CheckedContinuation<Int, Error>?
        oldTransport.requestOTPHandler = { _ in
            try await withCheckedThrowingContinuation { continuation in
                finish = continuation
            }
        }
        oldTransport.logoutError = MobileAccountError.network
        let replacement = FakeMobileAuthTransport()
        let store = makeAccountStore(defaults: defaults)
        let coordinator = makeCoordinator(
            defaults: defaults,
            websiteDataStore: store,
            transports: [bootstrapTransport, oldTransport, replacement]
        )
        coordinator.activateAccountSurface()
        let bootstrapState = try? await coordinator.bootstrap()
        XCTAssertEqual(bootstrapState, .signedOut)
        let oldIdentifier = store.identifier

        let lateRequest = Task {
            try await coordinator.requestOTP(email: "alex@example.com")
        }
        await Task.yield()
        let state = try await coordinator.logout()
        finish?.resume(returning: 21)

        XCTAssertEqual(state, .signedOut)
        XCTAssertNotEqual(store.identifier, oldIdentifier)
        XCTAssertTrue(oldTransport.cancelled)
        XCTAssertTrue(oldTransport.invalidated)
        do {
            _ = try await lateRequest.value
            XCTFail("Expected the old generation to be rejected")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error.code, "stale_session")
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testSecondFactorKeepsTheSignInStoreUntilItPasses() async throws {
        let defaults = makeDefaults()
        let store = makeAccountStore(defaults: defaults)
        let transport = FakeMobileAuthTransport()
        transport.secondFactor = MobileSecondFactorRequirement(
            email: nil,
            gate: .challenge,
            methods: [.totp]
        )
        let coordinator = makeCoordinator(
            defaults: defaults,
            websiteDataStore: store,
            transports: [transport]
        )
        coordinator.activateAccountSurface()
        let storeIdentifier = store.identifier

        let owed = try await coordinator.verifyOTP(
            email: " alex@example.com ",
            code: "123456"
        )
        XCTAssertEqual(
            owed,
            .twoFactorRequired(
                MobileSecondFactorRequirement(
                    email: "alex@example.com",
                    gate: .challenge,
                    methods: [.totp]
                )
            )
        )
        XCTAssertThrowsError(try coordinator.selectInstance(id: instance.id)) {
            XCTAssertEqual(($0 as? MobileAccountError)?.code, "unauthorized_instance")
        }

        do {
            _ = try await coordinator.verifySecondFactor(method: .totp, code: "000000")
            XCTFail("Expected the rejected code to fail")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error, .secondFactorInvalid)
        }
        XCTAssertEqual(store.identifier, storeIdentifier)
        let relaunched = try await coordinator.bootstrap()
        XCTAssertEqual(relaunched, owed)

        let proved = snapshot(accountID: "account-1")
        transport.verifySecondFactorHandler = { _ in
            transport.secondFactor = nil
            transport.session = proved
            return .signedIn(proved)
        }
        let state = try await coordinator.verifySecondFactor(
            method: .totp,
            code: " 287082 "
        )
        XCTAssertEqual(
            state,
            .signedIn(snapshot: proved, selectedInstanceID: nil)
        )
        XCTAssertEqual(transport.proofs.count, 2)
        XCTAssertEqual(transport.proofs.last?.codeProof?.0, .totp)
        XCTAssertEqual(transport.proofs.last?.codeProof?.1, "287082")
        XCTAssertEqual(store.identifier, storeIdentifier)
        XCTAssertFalse(transport.invalidated)
        XCTAssertEqual(try coordinator.selectInstance(id: instance.id), instance)
    }

    func testPasswordSignInRotatesTheStoreAfterEveryFailureAndKeepsItOnSuccess() async throws {
        let defaults = makeDefaults()
        let store = makeAccountStore(defaults: defaults)
        let rejected = FakeMobileAuthTransport()
        let endedSession = FakeMobileAuthTransport()
        endedSession.signInWithPasswordHandler = { _, _ in
            throw MobileAccountError.signedOut
        }
        let accepted = FakeMobileAuthTransport()
        accepted.signInWithPasswordHandler = { _, _ in
            MobilePasswordSignInDiscovery(
                discovery: .secondFactorRequired(
                    MobileSecondFactorRequirement(
                        email: nil,
                        gate: .challenge,
                        methods: [.totp]
                    )
                ),
                weakPasswordReasons: [.pwned]
            )
        }
        let coordinator = makeCoordinator(
            defaults: defaults,
            websiteDataStore: store,
            transports: [rejected, endedSession, accepted]
        )
        coordinator.activateAccountSurface()
        var storeIdentifier = store.identifier

        // The page requires only a password; native repeats the email check.
        for (email, password) in [
            ("alex@example.com", ""),
            ("alex@", "correct-horse"),
        ] {
            do {
                _ = try await coordinator.signInWithPassword(
                    email: email,
                    password: password
                )
                XCTFail("Expected a malformed request for \(email)")
            } catch let error as MobileAccountError {
                XCTAssertEqual(error, .invalidRequest)
            }
        }
        XCTAssertTrue(rejected.passwordAttempts.isEmpty)
        XCTAssertEqual(store.identifier, storeIdentifier)

        do {
            _ = try await coordinator.signInWithPassword(
                email: " alex@example.com ",
                password: " correct horse "
            )
            XCTFail("Expected the rejected password to fail")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error, .invalidCredentials)
        }
        XCTAssertEqual(rejected.passwordAttempts.map(\.email), ["alex@example.com"])
        // A password is never trimmed.
        XCTAssertEqual(rejected.passwordAttempts.map(\.password), [" correct horse "])
        XCTAssertTrue(rejected.invalidated)
        XCTAssertNotEqual(store.identifier, storeIdentifier)
        storeIdentifier = store.identifier

        // The session's 401 is the explicit signed-out answer; it rotates too.
        let ended = try await coordinator.signInWithPassword(
            email: "alex@example.com",
            password: "correct-horse"
        )
        XCTAssertEqual(
            ended,
            MobilePasswordSignInResult(state: .signedOut, weakPasswordReasons: nil)
        )
        XCTAssertNil(ended.jsonObject["weak_password"])
        XCTAssertTrue(endedSession.invalidated)
        XCTAssertNotEqual(store.identifier, storeIdentifier)
        storeIdentifier = store.identifier

        let owed = try await coordinator.signInWithPassword(
            email: "alex@example.com",
            password: "correct-horse"
        )
        XCTAssertEqual(
            owed,
            MobilePasswordSignInResult(
                state: .twoFactorRequired(
                    MobileSecondFactorRequirement(
                        email: "alex@example.com",
                        gate: .challenge,
                        methods: [.totp]
                    )
                ),
                weakPasswordReasons: [.pwned]
            )
        )
        XCTAssertTrue(JSONSerialization.isValidJSONObject(owed.jsonObject))
        XCTAssertEqual(owed.jsonObject["status"] as? String, "twoFactorRequired")
        XCTAssertEqual(
            owed.jsonObject["weak_password"] as? NSDictionary,
            ["reasons": ["pwned"]]
        )
        XCTAssertEqual(store.identifier, storeIdentifier)
        XCTAssertFalse(accepted.invalidated)

        // The second factor continues on the same store, unchanged.
        let proved = snapshot(accountID: "account-1")
        accepted.verifySecondFactorHandler = { _ in .signedIn(proved) }
        let state = try await coordinator.verifySecondFactor(
            method: .totp,
            code: "287082"
        )
        XCTAssertEqual(state, .signedIn(snapshot: proved, selectedInstanceID: nil))
        XCTAssertEqual(store.identifier, storeIdentifier)
        XCTAssertEqual(try coordinator.selectInstance(id: instance.id), instance)
    }

    func testColdLaunchSecondFactorKeepsTheSelectionForTheSameAccount() async throws {
        let defaults = makeDefaults()
        let firstTransport = FakeMobileAuthTransport()
        firstTransport.session = snapshot(accountID: "account-1")
        let first = makeCoordinator(
            defaults: defaults,
            transports: [firstTransport]
        )
        first.activateAccountSurface()
        _ = try await first.bootstrap()
        _ = try first.selectInstance(id: instance.id)

        let store = makeAccountStore(defaults: defaults)
        let storeIdentifier = store.identifier
        let transport = FakeMobileAuthTransport()
        transport.secondFactor = MobileSecondFactorRequirement(
            email: "account-1@example.com",
            gate: .enroll,
            methods: []
        )
        let relaunched = makeCoordinator(
            defaults: defaults,
            websiteDataStore: store,
            transports: [transport]
        )
        relaunched.activateAccountSurface()
        let owed = try await relaunched.bootstrap()
        XCTAssertEqual(
            owed,
            .twoFactorRequired(
                MobileSecondFactorRequirement(
                    email: "account-1@example.com",
                    gate: .enroll,
                    methods: []
                )
            )
        )
        XCTAssertNil(relaunched.restorableRuntime(from: owed))

        transport.secondFactor = MobileSecondFactorRequirement(
            email: "account-1@example.com",
            gate: .challenge,
            methods: [.passkey]
        )
        guard case .twoFactorRequired(let enrolled) = try await relaunched.refresh() else {
            return XCTFail("Expected the session to still owe the second factor")
        }
        XCTAssertEqual(enrolled.gate, .challenge)

        transport.secondFactor = nil
        transport.session = snapshot(accountID: "account-1")
        let state = try await relaunched.refresh()
        XCTAssertEqual(
            state,
            .signedIn(
                snapshot: snapshot(accountID: "account-1"),
                selectedInstanceID: instance.id
            )
        )
        XCTAssertEqual(store.identifier, storeIdentifier)
    }

    func testSecondFactorSignedOutAnswerReplacesTheStore() async throws {
        let defaults = makeDefaults()
        let store = makeAccountStore(defaults: defaults)
        let transport = FakeMobileAuthTransport()
        transport.secondFactor = MobileSecondFactorRequirement(
            email: "alex@example.com",
            gate: .challenge,
            methods: [.passkey, .totp]
        )
        transport.verifySecondFactorHandler = { _ in
            throw MobileAccountError.signedOut
        }
        let replacement = FakeMobileAuthTransport()
        var pushLogoutCalls = 0
        let coordinator = makeCoordinator(
            defaults: defaults,
            websiteDataStore: store,
            transports: [transport, replacement],
            onPreparePushLogout: {
                pushLogoutCalls += 1
            }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        let storeIdentifier = store.identifier

        let state = try await coordinator.verifySecondFactor(
            method: .recovery,
            code: "k7m2p-9xq4r"
        )

        XCTAssertEqual(state, .signedOut)
        XCTAssertNotEqual(store.identifier, storeIdentifier)
        XCTAssertTrue(transport.invalidated)
        XCTAssertTrue(coordinator.isAccountSurfaceActive)
        XCTAssertEqual(pushLogoutCalls, 1)
    }

    func testPasskeyExchangeStaysBetweenTransportAndNativeCode() async throws {
        let transport = FakeMobileAuthTransport()
        transport.secondFactor = MobileSecondFactorRequirement(
            email: "alex@example.com",
            gate: .challenge,
            methods: [.passkey, .totp]
        )
        let request = MobilePasskeyRequest(
            challenge: Data([1, 2, 3]),
            allowedCredentialIDs: [Data("cred-1".utf8)]
        )
        transport.passkeyChallengeHandler = {
            MobilePasskeyChallenge(request: request, token: "signed-challenge")
        }
        let authenticator = FakePasskeyAuthenticator()
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [transport],
            passkeyAuthenticator: authenticator
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        let anchor = UIWindow(frame: .zero)

        do {
            _ = try await coordinator.verifyPasskey(presentationAnchor: anchor)
            XCTFail("Expected the cancelled passkey to fail")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error, .passkeyCancelled)
        }
        XCTAssertTrue(transport.proofs.isEmpty)

        let assertion = MobilePasskeyAssertion(
            credentialID: Data("cred-1".utf8),
            clientDataJSON: Data("{}".utf8),
            authenticatorData: Data([1]),
            signature: Data([2]),
            userHandle: nil
        )
        authenticator.result = .success(assertion)
        let proved = snapshot(accountID: "account-1")
        transport.verifySecondFactorHandler = { _ in .signedIn(proved) }
        let state = try await coordinator.verifyPasskey(presentationAnchor: anchor)

        XCTAssertEqual(state, .signedIn(snapshot: proved, selectedInstanceID: nil))
        XCTAssertEqual(authenticator.requests, [request, request])
        let proof = try XCTUnwrap(transport.proofs.last?.passkeyProof)
        XCTAssertEqual(proof.token, "signed-challenge")
        XCTAssertEqual(
            proof.response as NSDictionary,
            assertion.authenticationResponseJSON as NSDictionary
        )

        let unavailable = FakeMobileAuthTransport()
        unavailable.secondFactor = transport.secondFactor
        let noPasskey = makeCoordinator(
            defaults: makeDefaults(),
            transports: [unavailable],
            passkeyAuthenticator: authenticator
        )
        noPasskey.activateAccountSurface()
        _ = try await noPasskey.bootstrap()
        do {
            _ = try await noPasskey.verifyPasskey(presentationAnchor: anchor)
            XCTFail("Expected an account without a passkey to fail")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error, .passkeyUnavailable)
        }
        XCTAssertEqual(authenticator.requests.count, 2)
    }

    func testPasskeyOptionsAndAssertionUseWebAuthnJSON() throws {
        XCTAssertEqual(MobileBase64URL.decode("AAEC_-8"), Data([0, 1, 2, 255, 239]))
        XCTAssertEqual(MobileBase64URL.encode(Data([0xfb, 0xff])), "-_8")
        for invalid in ["", "a", "AAEC+-8", "AAEC/-8", "AAEC_-8="] {
            XCTAssertNil(MobileBase64URL.decode(invalid), invalid)
        }

        let request = try MobilePasskeyRequest(
            options: [
                "challenge": "AAEC_-8",
                "rpId": "avibe.bot",
                "userVerification": "required",
                "allowCredentials": [
                    ["id": "Y3JlZC0x", "type": "public-key", "transports": ["internal"]],
                ],
            ]
        )
        XCTAssertEqual(request.challenge, Data([0, 1, 2, 255, 239]))
        XCTAssertEqual(request.allowedCredentialIDs, [Data("cred-1".utf8)])

        XCTAssertThrowsError(
            try MobilePasskeyRequest(options: ["challenge": "AAEC_-8", "rpId": "evil.example"])
        ) {
            XCTAssertEqual($0 as? MobileAccountError, .passkeyUnavailable)
        }
        XCTAssertThrowsError(
            try MobilePasskeyRequest(options: ["challenge": "AAEC_-8"])
        ) {
            XCTAssertEqual($0 as? MobileAccountError, .passkeyUnavailable)
        }
        XCTAssertThrowsError(
            try MobilePasskeyRequest(options: ["challenge": "not base64url!", "rpId": "avibe.bot"])
        ) {
            XCTAssertEqual($0 as? MobileAccountError, .network)
        }

        let response = MobilePasskeyAssertion(
            credentialID: Data("cred-1".utf8),
            clientDataJSON: Data("{}".utf8),
            authenticatorData: Data([1]),
            signature: Data([2]),
            userHandle: Data()
        ).authenticationResponseJSON
        let inner = try XCTUnwrap(response["response"] as? [String: Any])
        XCTAssertEqual(response["id"] as? String, "Y3JlZC0x")
        XCTAssertEqual(response["rawId"] as? String, "Y3JlZC0x")
        XCTAssertEqual(response["type"] as? String, "public-key")
        XCTAssertEqual(inner["clientDataJSON"] as? String, "e30")
        XCTAssertNil(inner["userHandle"])

        XCTAssertEqual(
            MobilePasskeyAuthenticator.mappedError(ASAuthorizationError(.canceled)),
            .passkeyCancelled
        )
        for error: Error in [
            ASAuthorizationError(.failed),
            ASAuthorizationError(.notHandled),
            URLError(.notConnectedToInternet),
        ] {
            XCTAssertEqual(
                MobilePasskeyAuthenticator.mappedError(error),
                .passkeyUnavailable
            )
        }
    }

    func testNamedStoreRemovalFailurePersistsUntilALaterSweep() async {
        let defaults = makeDefaults()
        let registry = FakeNamedDataStoreRegistry()
        let store = AccountWebsiteDataStore(
            defaults: defaults,
            operations: registry.operations
        )
        let oldIdentifier = store.identifier
        registry.failingIdentifiers.insert(oldIdentifier)

        await store.rotate()

        XCTAssertTrue(registry.identifiers.contains(oldIdentifier))
        XCTAssertTrue(registry.identifiers.contains(store.identifier))
        XCTAssertEqual(registry.removalAttempts, [oldIdentifier])

        let relaunched = AccountWebsiteDataStore(
            defaults: defaults,
            operations: registry.operations
        )

        registry.failingIdentifiers.remove(oldIdentifier)
        await relaunched.sweepOrphans()

        XCTAssertFalse(registry.identifiers.contains(oldIdentifier))
        XCTAssertTrue(registry.identifiers.contains(relaunched.identifier))
        XCTAssertEqual(
            registry.removalAttempts.filter { $0 == oldIdentifier }.count,
            2
        )
        XCTAssertFalse(
            registry.removalAttempts.contains(relaunched.identifier)
        )
    }

    func testARemovedStoreThatComesBackIsRemovedAgainAndForgottenOnlyOnceGone() async {
        let defaults = makeDefaults()
        let registry = FakeNamedDataStoreRegistry()
        let store = AccountWebsiteDataStore(
            defaults: defaults,
            operations: registry.operations
        )
        let oldIdentifier = store.identifier
        // WebKit reports the removal as done, and the store's directory comes
        // back while its network session finishes closing (#3418).
        registry.returningIdentifiers.insert(oldIdentifier)

        await store.rotate()

        XCTAssertEqual(registry.removalAttempts, [oldIdentifier])
        XCTAssertTrue(registry.identifiers.contains(oldIdentifier))
        XCTAssertTrue(store.ownedIdentifiers.contains(oldIdentifier))

        // The next launch removes it again, its session long closed.
        registry.returningIdentifiers.remove(oldIdentifier)
        let relaunched = AccountWebsiteDataStore(
            defaults: defaults,
            operations: registry.operations
        )
        await relaunched.sweepOrphans()

        XCTAssertEqual(registry.removalAttempts, [oldIdentifier, oldIdentifier])
        XCTAssertFalse(registry.identifiers.contains(oldIdentifier))
        XCTAssertTrue(relaunched.ownedIdentifiers.contains(oldIdentifier))

        // Gone now: the launch after forgets it, without removing it again.
        let gone = AccountWebsiteDataStore(
            defaults: defaults,
            operations: registry.operations
        )
        await gone.sweepOrphans()

        XCTAssertEqual(registry.removalAttempts, [oldIdentifier, oldIdentifier])
        XCTAssertEqual(gone.ownedIdentifiers, [gone.identifier])
        XCTAssertEqual(
            AccountWebsiteDataStore(
                defaults: defaults,
                operations: registry.operations
            ).ownedIdentifiers,
            [gone.identifier]
        )
    }

    func testLaunchSweepIgnoresUnownedStoresAndPreservesTheCurrentStore() async {
        let defaults = makeDefaults()
        let registry = FakeNamedDataStoreRegistry()
        let store = AccountWebsiteDataStore(
            defaults: defaults,
            operations: registry.operations
        )
        let orphan = UUID()
        registry.identifiers.insert(orphan)

        await store.sweepOrphans()

        XCTAssertTrue(registry.identifiers.contains(orphan))
        XCTAssertTrue(registry.identifiers.contains(store.identifier))
        XCTAssertTrue(registry.removalAttempts.isEmpty)
    }

    func testStoreRemovalWaitIsBoundedAndDefersCleanupForARetainedWebView() async throws {
        let defaults = makeDefaults()
        let registry = FakeNamedDataStoreRegistry()
        let store = AccountWebsiteDataStore(
            defaults: defaults,
            operations: registry.operations
        )
        let oldIdentifier = store.identifier
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        var retainedWebView: WKWebView? = WKWebView(
            frame: .zero,
            configuration: configuration
        )
        var secondRetainedWebView: WKWebView? = WKWebView(
            frame: .zero,
            configuration: configuration
        )
        let releaseBarrier = WebsiteDataStoreReleaseBarrier(
            webViews: [try XCTUnwrap(retainedWebView)]
        )
        let secondReleaseBarrier = WebsiteDataStoreReleaseBarrier(
            webViews: [try XCTUnwrap(secondRetainedWebView)]
        )
        let coordinator = makeCoordinator(
            defaults: defaults,
            websiteDataStore: store,
            transports: [
                FakeMobileAuthTransport(),
                FakeMobileAuthTransport(),
            ],
            onPrepareAccountTransition: {
                [releaseBarrier, secondReleaseBarrier]
            }
        )
        coordinator.activateAccountSurface()

        let clock = ContinuousClock()
        let startedAt = clock.now
        let state = try await coordinator.logout()
        let elapsed = startedAt.duration(to: clock.now)

        XCTAssertEqual(state, .signedOut)
        XCTAssertLessThan(elapsed, .milliseconds(1500))
        XCTAssertNotEqual(store.identifier, oldIdentifier)
        XCTAssertTrue(registry.identifiers.contains(oldIdentifier))
        XCTAssertTrue(registry.removalAttempts.isEmpty)

        retainedWebView = nil
        secondRetainedWebView = nil
        let unreleased = await WebsiteDataStoreReleaseBarrier.waitForRelease(
            of: [releaseBarrier, secondReleaseBarrier]
        )
        XCTAssertTrue(unreleased.isEmpty)
        await store.sweepOrphans()

        XCTAssertFalse(registry.identifiers.contains(oldIdentifier))
        XCTAssertEqual(registry.removalAttempts, [oldIdentifier])
    }

    func testAccountTransitionReleasesARealPopupBeforeStoreRemoval() async throws {
        let defaults = makeDefaults()
        let registry = FakeNamedDataStoreRegistry()
        let store = AccountWebsiteDataStore(
            defaults: defaults,
            operations: registry.operations
        )
        let oldIdentifier = store.identifier
        let controller = WebViewController(
            instanceOrigin: WebOrigin.parseConfigured(
                "https://alice-app.avibe.bot"
            )!,
            websiteDataStore: store.dataStore,
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        let window = UIWindow(
            frame: CGRect(x: 0, y: 0, width: 390, height: 700)
        )
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        controller.view.frame = window.bounds
        controller.view.layoutIfNeeded()
        defer {
            window.isHidden = true
        }

        var runtimeWebView: WKWebView? = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        runtimeWebView?.stopLoading()
        runtimeWebView?.loadHTMLString(
            """
            <!doctype html>
            <html>
              <body>
                <button id="open-popup" onclick="window.open('about:blank', '_blank')">
                  Open
                </button>
              </body>
            </html>
            """,
            baseURL: URL(string: "https://alice-app.avibe.bot")!
        )
        let popupButtonReady = try await waitForJavaScriptValue(
            in: try XCTUnwrap(runtimeWebView)
        ) {
            """
            document.querySelector("#open-popup") instanceof HTMLButtonElement
            """
        }
        XCTAssertTrue(popupButtonReady)
        _ = try await runtimeWebView?.evaluateJavaScript(
            """
            document.querySelector("#open-popup").click()
            """
        )

        var popupNavigationController: UINavigationController?
        for _ in 0..<50 {
            if let presented = controller.presentedViewController
                as? UINavigationController
            {
                popupNavigationController = presented
                break
            }
            try await Task.sleep(for: .milliseconds(100))
        }
        var popupController: UIViewController? = try XCTUnwrap(
            popupNavigationController?.topViewController
        )
        popupController?.loadViewIfNeeded()

        var childWebView: WKWebView? = try XCTUnwrap(
            descendantWebViews(in: try XCTUnwrap(popupController?.view)).first
        )
        let navigationProbe = NavigationCallbackProbe()
        childWebView?.navigationDelegate = navigationProbe
        childWebView?.loadHTMLString(
            "<!doctype html><title>Child</title><p>Loading</p>",
            baseURL: nil
        )
        await fulfillment(
            of: [navigationProbe.started],
            timeout: 5
        )

        let runtimeReference = WeakObjectReference(runtimeWebView)
        let childReference = WeakObjectReference(childWebView)
        let popupNavigationReference = WeakObjectReference(
            popupNavigationController
        )
        runtimeWebView = nil
        childWebView = nil
        popupController = nil
        popupNavigationController = nil

        var releasedBeforeRemoval = false
        registry.beforeRemoval = { identifier in
            if identifier == oldIdentifier {
                releasedBeforeRemoval =
                    runtimeReference.value == nil
                    && childReference.value == nil
            }
        }
        let coordinator = makeCoordinator(
            defaults: defaults,
            websiteDataStore: store,
            transports: [
                FakeMobileAuthTransport(),
                FakeMobileAuthTransport(),
            ],
            onPrepareAccountTransition: {
                [controller.invalidateForAccountTransition()]
            }
        )
        coordinator.activateAccountSurface()

        _ = try await coordinator.logout()

        XCTAssertTrue(releasedBeforeRemoval)
        XCTAssertNil(runtimeReference.value)
        XCTAssertNil(childReference.value)
        var popupWasDismissed = false
        for _ in 0..<50 {
            if controller.presentedViewController == nil {
                popupWasDismissed = true
                break
            }
            try await Task.sleep(for: .milliseconds(100))
        }
        XCTAssertTrue(popupWasDismissed)
        XCTAssertNil(
            popupNavigationReference.value?.presentingViewController
        )

        let callbackCountAfterLogout = navigationProbe.callbackCount
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertEqual(
            navigationProbe.callbackCount,
            callbackCountAfterLogout
        )
    }

    func testInvalidatedRuntimeControllerDoesNotRecreateAStoreWebView() {
        let controller = WebViewController(
            instanceOrigin: WebOrigin.parseConfigured(
                "https://alice-app.avibe.bot"
            )!,
            websiteDataStore: .nonPersistent(),
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )

        _ = controller.invalidateForAccountTransition()
        controller.loadViewIfNeeded()

        XCTAssertTrue(
            controller.view.subviews.compactMap { $0 as? WKWebView }.isEmpty
        )
    }

    private var instance: MobileInstance {
        MobileInstance(
            id: "instance-1",
            name: "Personal Avibe",
            host: "alex-app.avibe.bot",
            url: URL(string: "https://alex-app.avibe.bot")!,
            relationship: "owner",
            ownerEmail: nil,
            available: true
        )
    }

    private func snapshot(
        accountID: String,
        instances: [MobileInstance]? = nil
    ) -> MobileSessionSnapshot {
        MobileSessionSnapshot(
            account: MobileAccount(
                id: accountID,
                email: "\(accountID)@example.com"
            ),
            instances: instances ?? [instance]
        )
    }

    private func makeDefaults() -> UserDefaults {
        let suite = "MobileOnboardingTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        addTeardownBlock {
            defaults.removePersistentDomain(forName: suite)
        }
        return defaults
    }

    private func makeCoordinator(
        defaults: UserDefaults,
        websiteDataStore: AccountWebsiteDataStore? = nil,
        transports: [FakeMobileAuthTransport],
        passkeyAuthenticator: MobilePasskeyAuthenticating? = nil,
        onCreateTransport: @escaping () -> Void = {},
        onOpen: @escaping (MobileRuntimeContext) -> Void = { _ in },
        onPrepareAccountTransition: @escaping () -> [
            WebsiteDataStoreReleaseBarrier
        ] = { [] },
        onPreparePushLogout: @escaping @MainActor () async -> Void = {}
    ) -> MobileSessionCoordinator {
        var queue = transports
        return MobileSessionCoordinator(
            defaults: defaults,
            controlPlane: MobileControlPlaneConfiguration(
                testBaseURL: URL(string: "http://127.0.0.1:43127")!
            ),
            websiteDataStore: websiteDataStore
                ?? makeAccountStore(defaults: defaults),
            transportFactory: { _, _ in
                precondition(!queue.isEmpty)
                onCreateTransport()
                return queue.removeFirst()
            },
            passkeyAuthenticator: passkeyAuthenticator ?? FakePasskeyAuthenticator(),
            onOpenRuntime: onOpen,
            onPrepareAccountTransition: onPrepareAccountTransition,
            onPreparePushLogout: onPreparePushLogout
        )
    }

    private func makeAccountStore(
        defaults: UserDefaults
    ) -> AccountWebsiteDataStore {
        let registry = FakeNamedDataStoreRegistry()
        return AccountWebsiteDataStore(
            defaults: defaults,
            operations: registry.operations
        )
    }

    private func descendantWebViews(in view: UIView) -> [WKWebView] {
        let direct = view.subviews.compactMap { $0 as? WKWebView }
        return direct + view.subviews.flatMap(descendantWebViews(in:))
    }

    private func present(
        _ controller: UIViewController,
        size: CGSize
    ) throws -> (UIWindow, WKWebView) {
        let window = UIWindow(
            frame: CGRect(origin: .zero, size: size)
        )
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.beginAppearanceTransition(true, animated: false)
        controller.endAppearanceTransition()
        controller.view.frame = window.bounds
        controller.view.layoutIfNeeded()
        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        return (window, webView)
    }

    private func replaceText(
        _ text: String,
        in webView: WKWebView,
        selector: String
    ) async throws -> Bool {
        let result = try await webView.callAsyncJavaScript(
            """
            const input = document.querySelector(selector);
            if (!(input instanceof HTMLInputElement)) return false;
            input.focus();
            // WebKit emits the editing events React observes for keyboard input.
            return document.execCommand("insertText", false, text)
              && input.value === text;
            """,
            arguments: [
                "selector": selector,
                "text": text,
            ],
            in: nil,
            contentWorld: .page
        )
        return result as? Bool == true
    }

    private func waitForJavaScriptValue(
        in webView: WKWebView,
        script: () -> String
    ) async throws -> Bool {
        for _ in 0..<50 {
            if try await webView.evaluateJavaScript(script()) as? Bool == true {
                return true
            }
            try await Task.sleep(for: .milliseconds(100))
        }
        return false
    }
}

@MainActor
private final class WebViewLoadProbe: NSObject, WKNavigationDelegate {
    private var continuation: CheckedContinuation<Void, Error>?
    private var result: Result<Void, Error>?

    func waitForLoad() async throws {
        if let result {
            return try result.get()
        }
        return try await withCheckedThrowingContinuation {
            continuation = $0
        }
    }

    func webView(
        _ webView: WKWebView,
        didFinish navigation: WKNavigation?
    ) {
        finish(.success(()))
    }

    func webView(
        _ webView: WKWebView,
        didFail navigation: WKNavigation?,
        withError error: Error
    ) {
        finish(.failure(error))
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation?,
        withError error: Error
    ) {
        finish(.failure(error))
    }

    private func finish(_ result: Result<Void, Error>) {
        guard self.result == nil else {
            return
        }
        self.result = result
        continuation?.resume(with: result)
        continuation = nil
    }
}

@MainActor
private final class FakeNamedDataStoreRegistry {
    var identifiers = Set<UUID>()
    var failingIdentifiers = Set<UUID>()
    /// Stores whose removal reports success while their directory stays (or
    /// comes straight back), as WebKit's do while their session closes.
    var returningIdentifiers = Set<UUID>()
    var removalAttempts: [UUID] = []
    var beforeRemoval: ((UUID) -> Void)?

    var operations: AccountWebsiteDataStoreOperations {
        AccountWebsiteDataStoreOperations(
            makeDataStore: { [self] identifier in
                identifiers.insert(identifier)
                return .nonPersistent()
            },
            removeIdentifier: { [self] identifier in
                beforeRemoval?(identifier)
                removalAttempts.append(identifier)
                guard !failingIdentifiers.contains(identifier) else {
                    return NSError(
                        domain: "FakeNamedDataStoreRegistry",
                        code: 1
                    )
                }
                if !returningIdentifiers.contains(identifier) {
                    identifiers.remove(identifier)
                }
                return nil
            },
            listIdentifiers: { [self] in
                identifiers
            }
        )
    }
}

@MainActor
private final class NavigationCallbackProbe: NSObject, WKNavigationDelegate {
    let started = XCTestExpectation(description: "child navigation started")
    private(set) var callbackCount = 0

    func webView(
        _ webView: WKWebView,
        didStartProvisionalNavigation navigation: WKNavigation?
    ) {
        callbackCount += 1
        started.fulfill()
    }

    func webView(
        _ webView: WKWebView,
        didCommit navigation: WKNavigation?
    ) {
        callbackCount += 1
    }

    func webView(
        _ webView: WKWebView,
        didFinish navigation: WKNavigation?
    ) {
        callbackCount += 1
    }

    func webView(
        _ webView: WKWebView,
        didFail navigation: WKNavigation?,
        withError error: Error
    ) {
        callbackCount += 1
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation?,
        withError error: Error
    ) {
        callbackCount += 1
    }
}

private final class WeakObjectReference<Value: AnyObject> {
    weak var value: Value?

    init(_ value: Value?) {
        self.value = value
    }
}

@MainActor
private final class FakeMobileAuthTransport: MobileAuthTransporting {
    var session: MobileSessionSnapshot?
    /// When set, the session read answers `403 two_factor_required`.
    var secondFactor: MobileSecondFactorRequirement?
    var passkeyChallengeHandler: () async throws -> MobilePasskeyChallenge = {
        throw MobileAccountError.passkeyUnavailable
    }
    var verifySecondFactorHandler: (
        MobileSecondFactorProof
    ) async throws -> MobileSessionDiscovery = { _ in
        throw MobileAccountError.secondFactorInvalid
    }
    private(set) var proofs: [MobileSecondFactorProof] = []
    var signInWithPasswordHandler: (
        String,
        String
    ) async throws -> MobilePasswordSignInDiscovery = { _, _ in
        throw MobileAccountError.invalidCredentials
    }
    private(set) var passwordAttempts: [(email: String, password: String)] = []
    var logoutError: Error?
    var logoutHandler: () async throws -> Void = {}
    var pushManagementHandler: (
        PushManagementRequest
    ) async throws -> PushManagementResponse = { _ in
        PushManagementResponse(statusCode: 200, body: [:])
    }
    var requestOTPHandler: (String) async throws -> Int = { _ in 60 }
    var verifyOTPHandler: (
        String,
        String
    ) async throws -> MobileSessionSnapshot = { _, _ in
        throw MobileAccountError.signedOut
    }
    private(set) var cancelled = false
    private(set) var invalidated = false
    var onInvalidate: () -> Void = {}

    func requestOTP(email: String) async throws -> Int {
        try await requestOTPHandler(email)
    }

    func verifyOTP(
        email: String,
        code: String
    ) async throws -> MobileSessionDiscovery {
        if let secondFactor {
            return .secondFactorRequired(secondFactor)
        }
        if let session {
            return .signedIn(session)
        }
        return .signedIn(try await verifyOTPHandler(email, code))
    }

    func signInWithPassword(
        email: String,
        password: String
    ) async throws -> MobilePasswordSignInDiscovery {
        passwordAttempts.append((email, password))
        return try await signInWithPasswordHandler(email, password)
    }

    func fetchSession() async throws -> MobileSessionDiscovery? {
        if let secondFactor {
            return .secondFactorRequired(secondFactor)
        }
        return session.map(MobileSessionDiscovery.signedIn)
    }

    func passkeyChallenge() async throws -> MobilePasskeyChallenge {
        try await passkeyChallengeHandler()
    }

    func verifySecondFactor(
        _ proof: MobileSecondFactorProof
    ) async throws -> MobileSessionDiscovery {
        proofs.append(proof)
        return try await verifySecondFactorHandler(proof)
    }

    func logout() async throws {
        try await logoutHandler()
        if let logoutError {
            throw logoutError
        }
    }

    func pushManagementRequest(
        _ request: PushManagementRequest
    ) async throws -> PushManagementResponse {
        try await pushManagementHandler(request)
    }

    func cancelAll() async {
        cancelled = true
    }

    func invalidate() -> WebsiteDataStoreReleaseBarrier? {
        invalidated = true
        onInvalidate()
        return nil
    }
}

@MainActor
private final class FakePasskeyAuthenticator: MobilePasskeyAuthenticating {
    var result: Result<MobilePasskeyAssertion, MobileAccountError> = .failure(
        .passkeyCancelled
    )
    private(set) var requests: [MobilePasskeyRequest] = []

    func assert(
        _ request: MobilePasskeyRequest,
        presentationAnchor: ASPresentationAnchor
    ) async throws -> MobilePasskeyAssertion {
        requests.append(request)
        return try result.get()
    }
}

private extension MobileSecondFactorProof {
    var codeProof: (MobileSecondFactorCodeMethod, String)? {
        guard case .code(let method, let code) = self else {
            return nil
        }
        return (method, code)
    }

    var passkeyProof: (token: String, response: [String: Any])? {
        guard case .passkey(let token, let response) = self else {
            return nil
        }
        return (token, response)
    }
}

private extension Result where Success == NativeBridgeRequest, Failure == NativeBridgeError {
    var success: NativeBridgeRequest? {
        guard case .success(let value) = self else {
            return nil
        }
        return value
    }
}
