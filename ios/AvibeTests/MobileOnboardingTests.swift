import AuthenticationServices
import CryptoKit
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
                "app.dismiss",
                "app.returnToInstance",
                "app.openPage",
                "auth.signInWithProvider",
            ]
        )
        XCTAssertThrowsError(
            try MobileBridgeRequest.parse(["method": "auth.fetch"])
        )
    }

    // The bundled page names a page, never an address: each page has one
    // fixed address on the control plane.
    func testAvibePagesOpenOnlyTheirFixedControlPlaneAddresses() {
        let production = MobileControlPlaneConfiguration.productionURL
        XCTAssertEqual(Set(AvibePage.allCases.map(\.rawValue)), ["accountDeletion", "privacyPolicy"])
        XCTAssertEqual(
            AvibePage.accountDeletion.url(controlPlane: production).absoluteString,
            "https://avibe.bot/account/delete"
        )
        XCTAssertEqual(
            AvibePage.privacyPolicy.url(controlPlane: production).absoluteString,
            "https://avibe.bot/privacy"
        )
        XCTAssertEqual(
            AvibePage.accountDeletion.url(
                controlPlane: URL(string: "http://127.0.0.1:43127")!
            ).absoluteString,
            "http://127.0.0.1:43127/account/delete"
        )
        XCTAssertTrue(AvibePage.accountDeletion.usesAccountSignIn)
        XCTAssertFalse(AvibePage.privacyPolicy.usesAccountSignIn)
    }

    func testAvibePagesStayOnTheControlPlaneAndOnlyDeletionFinishesAtDone() {
        let origin = WebOrigin.parseConfigured("https://avibe.bot")!
        let deletion = AvibePagePolicy(page: .accountDeletion, origin: origin)
        let privacy = AvibePagePolicy(page: .privacyPolicy, origin: origin)
        let cases: [(String, AvibePageNavigation, AvibePageNavigation)] = [
            ("https://avibe.bot/account/delete", .allow, .allow),
            ("https://avibe.bot/login?next=/account/delete", .allow, .allow),
            ("https://avibe.bot/app/organizations/org-1/settings", .allow, .allow),
            ("https://avibe.bot/account/delete/done", .finished, .allow),
            ("https://avibe.bot/account/delete/done?lang=zh#top", .finished, .allow),
            ("https://avibe.bot/account/delete/done/more", .allow, .allow),
            ("https://avibe.bot/account/delete/doneish", .allow, .allow),
            ("http://avibe.bot/account/delete/done", .openOutside, .openOutside),
            ("https://evil.avibe.bot/account/delete/done", .openOutside, .openOutside),
            ("https://github.com/login/oauth/authorize", .openOutside, .openOutside),
            ("mailto:support@avibe.bot", .openOutside, .openOutside),
            ("about:blank", .allow, .allow),
            ("javascript:alert(1)", .cancel, .cancel),
            ("file:///etc/hosts", .cancel, .cancel),
        ]
        for (raw, deletionExpected, privacyExpected) in cases {
            let url = URL(string: raw)!
            XCTAssertEqual(deletion.mainFrameNavigation(to: url), deletionExpected, raw)
            XCTAssertEqual(privacy.mainFrameNavigation(to: url), privacyExpected, raw)
        }
        XCTAssertEqual(deletion.subframeNavigation(to: URL(string: "https://challenges.example.com/")!), .allow)
        XCTAssertEqual(deletion.subframeNavigation(to: URL(string: "mailto:a@example.com")!), .cancel)
        XCTAssertEqual(deletion.newWindow(to: URL(string: "https://avibe.bot/privacy")!), .openOutside)
        XCTAssertEqual(deletion.newWindow(to: URL(string: "javascript:void(0)")!), .cancel)
    }

    // The deletion page is signed in with the account's own store, and the
    // sign-out its completion causes waits for it like for the runtime.
    func testAccountDeletionPageSharesTheSignedInStoreAndHoldsItsCleanup() async throws {
        let defaults = makeDefaults()
        let registry = FakeNamedDataStoreRegistry()
        let store = AccountWebsiteDataStore(defaults: defaults, operations: registry.operations)
        let transport = FakeMobileAuthTransport()
        let coordinator = makeCoordinator(
            defaults: defaults,
            websiteDataStore: store,
            transports: [transport, FakeMobileAuthTransport(), FakeMobileAuthTransport()]
        )
        coordinator.activateAccountSurface()
        XCTAssertThrowsError(try coordinator.accountWebPageDataStore()) { error in
            XCTAssertEqual((error as? MobileAccountError)?.code, MobileAccountError.signedOut.code)
        }

        transport.session = snapshot(accountID: "account-1")
        _ = try await coordinator.bootstrap()
        XCTAssertTrue(try coordinator.accountWebPageDataStore() === store.dataStore)
        let oldIdentifier = store.identifier
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        var page: WKWebView? = WKWebView(frame: .zero, configuration: configuration)
        coordinator.beginAccountWebPage(try XCTUnwrap(page))

        // The account is gone: the session reads signed out.
        transport.session = nil
        let state = try await coordinator.refresh()
        XCTAssertEqual(state, .signedOut)
        XCTAssertNotEqual(store.identifier, oldIdentifier)
        XCTAssertTrue(registry.removalAttempts.isEmpty, "The page still uses the old store")
        XCTAssertThrowsError(try coordinator.accountWebPageDataStore())

        page = nil
        await store.sweepOrphans()
        XCTAssertEqual(registry.removalAttempts, [oldIdentifier])
    }

    // A notification tap while the deletion page is open must not open an
    // instance of an account the page may be deleting.
    func testNoInstanceOpensWhileTheAccountPageIsOpen() async throws {
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(accountID: "account-1")
        var opened = 0
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [transport],
            onOpen: { _ in opened += 1 }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let page = WKWebView(frame: .zero, configuration: configuration)

        coordinator.beginAccountWebPage(page)
        XCTAssertThrowsError(try coordinator.selectInstance(id: instance.id)) { error in
            XCTAssertEqual((error as? MobileAccountError)?.code, MobileAccountError.busy.code)
        }
        XCTAssertEqual(opened, 0)

        coordinator.endAccountWebPage(page)
        XCTAssertEqual(try coordinator.selectInstance(id: instance.id).id, instance.id)
        XCTAssertEqual(opened, 1)
    }

    // `app.openPage` answers exactly once however the page closes: the
    // deletion page's done navigation, or Done.
    func testOpenPagePresentsTheDeletionPageAndAnswersOnceForEachClose() async throws {
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(accountID: "account-1")
        let controller = MobileOnboardingViewController(
            coordinator: makeCoordinator(defaults: makeDefaults(), transports: [transport]),
            startScreen: .sheet
        )
        let (window, webView) = try present(controller, size: CGSize(width: 390, height: 844))
        defer { window.isHidden = true }
        _ = try await waitForJavaScriptValue(in: webView) {
            "document.body.textContent.includes(\"Personal Avibe\")"
        }

        for closesWithDone in [false, true] {
            let call = Task { @MainActor in
                try await webView.callAsyncJavaScript(
                    "return await window.avibeMobile.call('app.openPage', {page: 'accountDeletion'});",
                    arguments: [:],
                    in: nil,
                    contentWorld: .page
                ) as? [String: Any]
            }
            var page: AvibePageViewController?
            for _ in 0..<100 where page == nil {
                page = (controller.presentedViewController as? UINavigationController)?
                    .topViewController as? AvibePageViewController
                try await Task.sleep(for: .milliseconds(50))
            }
            let shown = try XCTUnwrap(page)
            XCTAssertEqual(
                shown.webView.configuration.applicationNameForUserAgent,
                NativeShellMetadata().userAgentSuffix
            )
            if closesWithDone {
                let done = try XCTUnwrap(shown.navigationItem.leftBarButtonItem)
                _ = done.target?.perform(done.action, with: done)
            } else {
                shown.webView.load(URLRequest(url: URL(string: "http://127.0.0.1:43127/account/delete/done")!))
            }
            let result = try await call.value
            XCTAssertEqual(result?["completed"] as? Bool, !closesWithDone)
            for _ in 0..<100 where controller.presentedViewController != nil {
                try await Task.sleep(for: .milliseconds(50))
            }
            XCTAssertNil(controller.presentedViewController)
        }
    }

    func testNativeSheetUsesOneLargeDetentAndTheDocumentScrollView() async throws {
        let modes: [(MobileOnboardingStartScreen, String)] = [
            (.sheet, "#sheet"), (.sheetInstances, "#sheet-instances"),
            (.sheetAccount, "#sheet-account"),
        ]
        for (mode, hash) in modes {
            try await exerciseNativeSheet(mode, expectedHash: hash)
        }
    }

    private func exerciseNativeSheet(
        _ startScreen: MobileOnboardingStartScreen, expectedHash: String
    ) async throws {
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(
            accountID: "account-1",
            instances: (1...8).map { index in
                MobileInstance(
                    id: "fixture-\(index)",
                    name: "Fixture instance \(index)",
                    host: "fixture-\(index).avibe.bot",
                    url: URL(string: "https://fixture-\(index).avibe.bot")!,
                    relationship: "owner",
                    ownerEmail: nil,
                    available: true
                )
            }
        )
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [transport]
        )
        let controller = MobileOnboardingViewController(
            coordinator: coordinator,
            startScreen: startScreen,
            dismissSurface: { true }
        )
        let root = UIViewController()
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 844))
        window.rootViewController = root
        window.makeKeyAndVisible()
        root.present(controller, animated: false)
        defer {
            root.dismiss(animated: false)
            window.rootViewController = nil
            window.isHidden = true
        }

        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        _ = try await waitForJavaScriptValue(in: webView) {
            "document.body.textContent.includes(\"Fixture instance 8\")"
        }
        let hash = try await webView.evaluateJavaScript("window.location.hash") as? String
        XCTAssertEqual(hash, expectedHash)
        let sheet = try XCTUnwrap(controller.sheetPresentationController)
        XCTAssertEqual(sheet.detents.count, 1)
        XCTAssertEqual(sheet.detents.first?.identifier, .large)
        XCTAssertFalse(sheet.prefersScrollingExpandsWhenScrolledToEdge)
        XCTAssertTrue(controller.contentScrollView(for: .top) === webView.scrollView)
        XCTAssertTrue(controller.contentScrollView(for: .bottom) === webView.scrollView)
    }

    func testAppDismissIsStrictAndReturnsNullFromTheSheet() async throws {
        for mode in [MobileOnboardingStartScreen.sheet, .sheetInstances, .sheetAccount] {
            try await exerciseSheetDismissal(mode)
        }
    }

    private func exerciseSheetDismissal(_ startScreen: MobileOnboardingStartScreen) async throws {
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(accountID: "account-1")
        var dismissCalls = 0
        let controller = MobileOnboardingViewController(
            coordinator: makeCoordinator(
                defaults: makeDefaults(),
                transports: [transport]
            ),
            startScreen: startScreen,
            dismissSurface: {
                dismissCalls += 1
                return true
            }
        )
        let (window, webView) = try present(
            controller,
            size: CGSize(width: 390, height: 844)
        )
        defer { window.isHidden = true }

        _ = try await waitForJavaScriptValue(in: webView) {
            "document.body.textContent.includes(\"Personal Avibe\")"
        }
        let result = try await webView.callAsyncJavaScript(
            "return await window.avibeMobile.call('app.dismiss');",
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        XCTAssertTrue(result is NSNull)
        XCTAssertEqual(dismissCalls, 1)

        let invalid = try await webView.callAsyncJavaScript(
            """
            try {
              await window.avibeMobile.call('app.dismiss', {unexpected: true});
              return 'missing-error';
            } catch (error) {
              return error.code;
            }
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        ) as? String
        XCTAssertEqual(invalid, MobileAccountError.invalidRequest.code)
        XCTAssertEqual(dismissCalls, 1)
    }

    func testOpenPageAcceptsOnlyANamedPage() async throws {
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(accountID: "account-1")
        let controller = MobileOnboardingViewController(
            coordinator: makeCoordinator(
                defaults: makeDefaults(),
                transports: [transport]
            ),
            startScreen: .sheet
        )
        let (window, webView) = try present(
            controller,
            size: CGSize(width: 390, height: 844)
        )
        defer { window.isHidden = true }

        _ = try await waitForJavaScriptValue(in: webView) {
            "document.body.textContent.includes(\"Personal Avibe\")"
        }
        for params in [
            "{}",
            "{page: 'https://avibe.bot/account/delete'}",
            "{page: 'accountDeletion', url: 'https://example.com/'}",
            "{page: 'settings'}",
        ] {
            let code = try await webView.callAsyncJavaScript(
                """
                try {
                  await window.avibeMobile.call('app.openPage', \(params));
                  return 'missing-error';
                } catch (error) {
                  return error.code;
                }
                """,
                arguments: [:],
                in: nil,
                contentWorld: .page
            ) as? String
            XCTAssertEqual(code, "invalid_argument", params)
        }
        XCTAssertNil(controller.presentedViewController)
    }

    // MARK: Google and GitHub sign-in (contract: _tmp/ios-login/contract-backend.md)

    func testProviderSecretsAreRandomUnpaddedBase64URLWithAnS256Challenge() throws {
        // RFC 7636, Appendix B.
        XCTAssertEqual(
            MobileProviderSecrets.challenge(for: "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        )
        let first = try MobileProviderSecrets.generate()
        let second = try MobileProviderSecrets.generate()
        for value in [first.verifier, first.state, second.verifier, second.state] {
            XCTAssertEqual(value.count, 43)
            XCTAssertNotNil(value.range(of: #"^[A-Za-z0-9_-]{43}$"#, options: .regularExpression))
        }
        XCTAssertEqual(Set([first.verifier, first.state, second.verifier, second.state]).count, 4)
    }

    func testTheAppOpensOnlyTheControlPlaneLaunchRoute() {
        let controlPlane = MobileControlPlaneConfiguration.productionURL
        let launch = "LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLA"
        XCTAssertTrue(MobileProviderRoute.isAllowedBrowserURL(
            URL(string: "https://avibe.bot/api/mobile/auth/browser?launch=\(launch)")!, controlPlane: controlPlane
        ))
        for raw in [
            "http://avibe.bot/api/mobile/auth/browser?launch=\(launch)",
            "https://evil.avibe.bot/api/mobile/auth/browser?launch=\(launch)",
            "https://avibe.bot:8443/api/mobile/auth/browser?launch=\(launch)",
            "https://user@avibe.bot/api/mobile/auth/browser?launch=\(launch)",
            "https://avibe.bot/api/mobile/auth/browser?launch=abc",
            "https://avibe.bot/api/mobile/auth/browser?launch=LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLB",
            "https://avibe.bot/api/mobile/auth/browser?launch=LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLA=",
            "https://avibe.bot:443/api/mobile/auth/browser?launch=LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLA",
            "https://avibe.bot/api/mobile/auth/%62rowser?launch=LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLA",
            "https://avibe.bot/api/mobile/auth/browser?launch=\(launch)#x",
            "https://avibe.bot/api/mobile/auth/browser?launch=",
            "https://avibe.bot/api/mobile/auth/browser",
            "https://avibe.bot/api/mobile/auth/browser?launch=a&launch=b",
            "https://avibe.bot/api/mobile/auth/browser?launch=a&next=/",
            "https://avibe.bot/api/mobile/auth/browser/?launch=\(launch)",
            "https://accounts.google.com/o/oauth2/v2/auth?launch=\(launch)",
        ] {
            XCTAssertFalse(
                MobileProviderRoute.isAllowedBrowserURL(URL(string: raw)!, controlPlane: controlPlane), raw
            )
        }
    }

    func testTheCallbackIsReadOnlyInItsExactShapeAndState() {
        let controlPlane = MobileControlPlaneConfiguration.productionURL
        let state = "state-123"
        func read(_ raw: String) -> MobileProviderRoute.Callback {
            MobileProviderRoute.callback(URL(string: raw)!, controlPlane: controlPlane, state: state)
        }
        let longCode = String(repeating: "c", count: 4096)
        XCTAssertEqual(read("https://avibe.bot/mobile/auth/complete?code=abc&state=state-123"), .code("abc"))
        // An empty `#` keeps the browser from carrying an earlier hop's fragment along.
        XCTAssertEqual(read("https://avibe.bot/mobile/auth/complete?code=abc&state=state-123#"), .code("abc"))
        XCTAssertEqual(read("https://avibe.bot/mobile/auth/complete?state=state-123&code=\(longCode)"), .code(longCode))
        XCTAssertEqual(read("https://avibe.bot/mobile/auth/complete?error=cancelled&state=state-123"), .cancelled)
        XCTAssertEqual(read("https://avibe.bot/mobile/auth/complete?error=provider_email_unverified&state=state-123"), .emailUnverified)
        XCTAssertEqual(read("https://avibe.bot/mobile/auth/complete?error=provider_unavailable&state=state-123"), .unavailable)
        for raw in [
            "https://avibe.bot/mobile/auth/complete?code=abc&state=state-124",
            "https://avibe.bot/mobile/auth/complete?code=abc&state=state-12",
            "https://avibe.bot/mobile/auth/complete?code=abc",
            "https://avibe.bot/mobile/auth/complete?state=state-123",
            "https://avibe.bot/mobile/auth/complete?code=abc&error=cancelled&state=state-123",
            "https://avibe.bot/mobile/auth/complete?code=abc&code=abd&state=state-123",
            "https://avibe.bot/mobile/auth/complete?code=abc&state=state-123&extra=1",
            "https://avibe.bot/mobile/auth/complete?code=&state=state-123",
            "https://avibe.bot/mobile/auth/complete?code=\(longCode)c&state=state-123",
            "https://avibe.bot/mobile/auth/complete?code=abc&state=state-123#fragment",
            "https://avibe.bot/mobile/auth/complete?code=abc&state=state-123#error=access_denied",
            "https://avibe.bot/mobile/auth/complete?error=invalid_or_expired&state=state-123",
            "https://avibe.bot/mobile/auth/complete?error=something_else&state=state-123",
            "https://avibe.bot:444/mobile/auth/complete?code=abc&state=state-123",
            "https://evil.avibe.bot/mobile/auth/complete?code=abc&state=state-123",
            "http://avibe.bot/mobile/auth/complete?code=abc&state=state-123",
            "https://avibe.bot/mobile/auth/completed?code=abc&state=state-123",
            "https://avibe.bot/auth/callback?code=abc&state=state-123",
        ] {
            XCTAssertEqual(read(raw), .invalid, raw)
        }
    }

    func testProviderSignInExchangesTheCallbackCodeWithThisAttemptsVerifier() async throws {
        let defaults = makeDefaults()
        let store = makeAccountStore(defaults: defaults)
        let transport = FakeMobileAuthTransport()
        let web = FakeWebAuthenticator()
        var openedState: String?
        web.handler = { url, host, path in
            XCTAssertEqual(url.absoluteString, "http://127.0.0.1:43127/api/mobile/auth/browser?launch=LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLA")
            XCTAssertEqual(host, "127.0.0.1")
            XCTAssertEqual(path, "/mobile/auth/complete")
            return URL(string: "https://127.0.0.1/mobile/auth/complete?code=one-time&state=\(openedState!)")!
        }
        transport.providerStartHandler = { _, _, state in
            openedState = state
            return .browser(
                transactionID: "transaction-1",
                url: URL(string: "http://127.0.0.1:43127/api/mobile/auth/browser?launch=LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLA")!
            )
        }
        let fresh = snapshot(accountID: "account-1", instances: [])
        transport.providerExchangeHandler = { _, _, _ in
            MobileProviderExchange(discovery: .signedIn(fresh), accountCreated: true)
        }
        let coordinator = makeCoordinator(
            defaults: defaults,
            websiteDataStore: store,
            // The first store is not fresh: the attempt rotates and starts on the second transport.
            transports: [FakeMobileAuthTransport(), transport, FakeMobileAuthTransport()],
            webAuthenticator: web
        )
        coordinator.activateAccountSurface()

        let state = try await coordinator.signInWithProvider(.github, presentationAnchor: UIWindow())

        XCTAssertEqual(state, .signedIn(snapshot: fresh, selectedInstanceID: nil))
        let start = try XCTUnwrap(transport.providerStarts.first)
        let exchange = try XCTUnwrap(transport.providerExchanges.first)
        XCTAssertEqual(start.provider, .github)
        XCTAssertEqual(exchange.transactionID, "transaction-1")
        XCTAssertEqual(exchange.code, "one-time")
        XCTAssertEqual(MobileProviderSecrets.challenge(for: exchange.verifier), start.challenge)
        XCTAssertNotEqual(exchange.verifier, start.state)
        XCTAssertTrue(coordinator.accountWasJustCreated)

        // Any account transition retires the hint.
        _ = try await coordinator.logout()
        XCTAssertFalse(coordinator.accountWasJustCreated)
    }

    func testAProviderSignInThatOwesTheSecondFactorKeepsItsStoreAndShowsNoNewAccountHint() async throws {
        let defaults = makeDefaults()
        let store = makeAccountStore(defaults: defaults)
        let transport = FakeMobileAuthTransport()
        let web = FakeWebAuthenticator()
        var sentState = ""
        transport.providerStartHandler = { _, _, state in
            sentState = state
            return .browser(
                transactionID: "transaction-1",
                url: URL(string: "http://127.0.0.1:43127/api/mobile/auth/browser?launch=LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLA")!
            )
        }
        web.handler = { _, _, _ in URL(string: "https://127.0.0.1/mobile/auth/complete?code=one-time&state=\(sentState)")! }
        let requirement = MobileSecondFactorRequirement(email: "alex@example.com", gate: .challenge, methods: [.totp])
        // The store that receives the first-factor cookies is the one in use at the exchange.
        var exchangedIn: UUID?
        transport.providerExchangeHandler = { _, _, _ in
            exchangedIn = store.identifier
            return MobileProviderExchange(discovery: .secondFactorRequired(requirement), accountCreated: true)
        }
        let coordinator = makeCoordinator(
            defaults: defaults,
            websiteDataStore: store,
            transports: [FakeMobileAuthTransport(), transport, FakeMobileAuthTransport()],
            webAuthenticator: web
        )
        coordinator.activateAccountSurface()

        let state = try await coordinator.signInWithProvider(.google, presentationAnchor: UIWindow())

        guard case .twoFactorRequired = state else {
            return XCTFail("Expected the second factor, got \(state)")
        }
        XCTAssertNotNil(exchangedIn)
        XCTAssertEqual(store.identifier, exchangedIn)
        XCTAssertEqual(transport.providerExchanges.count, 1)
        XCTAssertFalse(coordinator.accountWasJustCreated)
    }

    func testAnExchangeThatFailsRotatesTheStoreAndIsNeverRetried() async throws {
        for failure in [
            MobileAccountError.invalidOrExpired,
            .providerEmailUnverified,
            MobileAccountError(code: "rate_limited", message: "slow down", retryAfterSeconds: 30),
            .network,
        ] {
            let defaults = makeDefaults()
            let store = makeAccountStore(defaults: defaults)
            let transport = FakeMobileAuthTransport()
            let web = FakeWebAuthenticator()
            var sentState = ""
            transport.providerStartHandler = { _, _, state in
                sentState = state
                return .browser(
                    transactionID: "transaction-1",
                    url: URL(string: "http://127.0.0.1:43127/api/mobile/auth/browser?launch=LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLA")!
                )
            }
            web.handler = { _, _, _ in URL(string: "https://127.0.0.1/mobile/auth/complete?code=one-time&state=\(sentState)")! }
            transport.providerExchangeHandler = { _, _, _ in throw failure }
            let coordinator = makeCoordinator(
                defaults: defaults,
                websiteDataStore: store,
                transports: [FakeMobileAuthTransport(), transport, FakeMobileAuthTransport()],
                webAuthenticator: web
            )
            coordinator.activateAccountSurface()
            let started = store.identifier

            do {
                _ = try await coordinator.signInWithProvider(.github, presentationAnchor: UIWindow())
                XCTFail("Expected \(failure.code)")
            } catch let error as MobileAccountError {
                XCTAssertEqual(error.code, failure.code)
            }
            XCTAssertEqual(transport.providerExchanges.count, 1, failure.code)
            XCTAssertNotEqual(store.identifier, started, failure.code)
            XCTAssertNil(coordinator.currentAccount)
        }
    }

    func testProviderSignInThatDoesNotSignInRotatesTheStoreAndNeverExchanges() async throws {
        let cases: [(callback: (String) -> String, error: String)] = [
            ({ _ in "cancel" }, MobileAccountError.providerCancelled.code),
            ({ "https://127.0.0.1/mobile/auth/complete?error=cancelled&state=\($0)" }, MobileAccountError.providerCancelled.code),
            ({ "https://127.0.0.1/mobile/auth/complete?error=provider_email_unverified&state=\($0)" }, MobileAccountError.providerEmailUnverified.code),
            ({ "https://127.0.0.1/mobile/auth/complete?error=provider_unavailable&state=\($0)" }, MobileAccountError.providerUnavailable.code),
            ({ _ in "https://127.0.0.1/mobile/auth/complete?code=one-time&state=someone-else" }, MobileAccountError.invalidOrExpired.code),
        ]
        for (callback, expected) in cases {
            let defaults = makeDefaults()
            let store = makeAccountStore(defaults: defaults)
            let transport = FakeMobileAuthTransport()
            let web = FakeWebAuthenticator()
            var sentState = ""
            transport.providerStartHandler = { _, _, state in
                sentState = state
                return .browser(
                    transactionID: "transaction-1",
                    url: URL(string: "http://127.0.0.1:43127/api/mobile/auth/browser?launch=LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLA")!
                )
            }
            web.handler = { _, _, _ in
                let raw = callback(sentState)
                if raw == "cancel" { throw MobileAccountError.providerCancelled }
                return URL(string: raw)!
            }
            let coordinator = makeCoordinator(
                defaults: defaults,
                websiteDataStore: store,
                transports: [FakeMobileAuthTransport(), transport, FakeMobileAuthTransport()],
                webAuthenticator: web
            )
            coordinator.activateAccountSurface()
            let started = store.identifier

            do {
                _ = try await coordinator.signInWithProvider(.google, presentationAnchor: UIWindow())
                XCTFail("Expected \(expected)")
            } catch let error as MobileAccountError {
                XCTAssertEqual(error.code, expected)
            }
            XCTAssertTrue(transport.providerExchanges.isEmpty, expected)
            XCTAssertNotEqual(store.identifier, started, expected)
            XCTAssertNil(coordinator.currentAccount)
            XCTAssertFalse(coordinator.accountWasJustCreated)
        }
    }

    func testProviderSignInRefusesABrowserAddressOffTheControlPlane() async throws {
        let transport = FakeMobileAuthTransport()
        let web = FakeWebAuthenticator()
        transport.providerStartHandler = { _, _, _ in
            .browser(
                transactionID: "transaction-1",
                url: URL(string: "https://accounts.google.com/o/oauth2/v2/auth?launch=x")!
            )
        }
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [FakeMobileAuthTransport(), transport, FakeMobileAuthTransport()],
            webAuthenticator: web
        )
        coordinator.activateAccountSurface()
        do {
            _ = try await coordinator.signInWithProvider(.google, presentationAnchor: UIWindow())
            XCTFail("Expected network")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error.code, MobileAccountError.network.code)
        }
        XCTAssertTrue(web.opened.isEmpty)
        XCTAssertTrue(transport.providerExchanges.isEmpty)
    }

    func testAppleSignInSendsTheHashedServerNonceAndPostsTheTokenAndCode() async throws {
        let transport = FakeMobileAuthTransport()
        let apple = FakeAppleAuthorizer()
        var sentState = ""
        transport.providerStartHandler = { _, _, state in
            sentState = state
            return .apple(transactionID: "transaction-1", nonce: "server-nonce")
        }
        apple.handler = { nonce, state in
            XCTAssertEqual(nonce, MobileAppleNonce.hashed("server-nonce"))
            XCTAssertEqual(state, sentState)
            return MobileAppleCredential(identityToken: "id-token", authorizationCode: "auth-code", state: state)
        }
        let fresh = snapshot(accountID: "account-1")
        transport.appleExchangeHandler = { _, _, _, _ in
            MobileProviderExchange(discovery: .signedIn(fresh), accountCreated: false)
        }
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [FakeMobileAuthTransport(), transport, FakeMobileAuthTransport()],
            appleAuthorizer: apple
        )
        coordinator.activateAccountSurface()

        let state = try await coordinator.signInWithProvider(.apple, presentationAnchor: UIWindow())

        XCTAssertEqual(state, .signedIn(snapshot: fresh, selectedInstanceID: nil))
        // Lowercase hex of SHA256 over the raw nonce.
        XCTAssertEqual(
            MobileAppleNonce.hashed("server-nonce"),
            SHA256.hash(data: Data("server-nonce".utf8)).map { String(format: "%02x", $0) }.joined()
        )
        let start = try XCTUnwrap(transport.providerStarts.first)
        let exchange = try XCTUnwrap(transport.appleExchanges.first)
        XCTAssertEqual(start.provider, .apple)
        XCTAssertEqual(exchange.transactionID, "transaction-1")
        XCTAssertEqual(exchange.identityToken, "id-token")
        XCTAssertEqual(exchange.authorizationCode, "auth-code")
        XCTAssertEqual(MobileProviderSecrets.challenge(for: exchange.verifier), start.challenge)
        XCTAssertTrue(transport.providerExchanges.isEmpty)
        XCTAssertFalse(coordinator.accountWasJustCreated)
    }

    func testAppleSignInThatDoesNotSignInRotatesTheStoreAndNeverExchanges() async throws {
        let cases: [(answer: (String) throws -> MobileAppleCredential, start: MobileProviderStart, error: String)] = [
            ({ _ in throw MobileAccountError.providerCancelled }, .apple(transactionID: "t", nonce: "n"), "provider_cancelled"),
            ({ _ in throw MobileAccountError.network }, .apple(transactionID: "t", nonce: "n"), "network"),
            ({ _ in MobileAppleCredential(identityToken: "i", authorizationCode: "c", state: "someone-else") }, .apple(transactionID: "t", nonce: "n"), "invalid_or_expired"),
            ({ _ in MobileAppleCredential(identityToken: "i", authorizationCode: "c", state: nil) }, .apple(transactionID: "t", nonce: "n"), "invalid_or_expired"),
            // The backend answered the browser class for Apple: nothing opens.
            ({ MobileAppleCredential(identityToken: "i", authorizationCode: "c", state: $0) },
             .browser(transactionID: "t", url: URL(string: "http://127.0.0.1:43127/api/mobile/auth/browser?launch=l")!), "network"),
        ]
        for (answer, start, expected) in cases {
            let defaults = makeDefaults()
            let store = makeAccountStore(defaults: defaults)
            let transport = FakeMobileAuthTransport()
            let apple = FakeAppleAuthorizer()
            let web = FakeWebAuthenticator()
            transport.providerStartHandler = { _, _, _ in start }
            apple.handler = { _, state in try answer(state) }
            let coordinator = makeCoordinator(
                defaults: defaults,
                websiteDataStore: store,
                transports: [FakeMobileAuthTransport(), transport, FakeMobileAuthTransport()],
                webAuthenticator: web,
                appleAuthorizer: apple
            )
            coordinator.activateAccountSurface()
            let started = store.identifier
            do {
                _ = try await coordinator.signInWithProvider(.apple, presentationAnchor: UIWindow())
                XCTFail("Expected \(expected)")
            } catch let error as MobileAccountError {
                XCTAssertEqual(error.code, expected)
            }
            XCTAssertTrue(transport.appleExchanges.isEmpty, expected)
            XCTAssertTrue(web.opened.isEmpty, expected)
            XCTAssertNotEqual(store.identifier, started, expected)
        }
    }

    func testSigningOutWhileTheSignInWindowIsOpenClosesItAndDropsTheAttempt() async throws {
        let transport = FakeMobileAuthTransport()
        let web = FakeWebAuthenticator()
        let opened = expectation(description: "window open")
        var close: CheckedContinuation<URL, Error>?
        web.handler = { _, _, _ in
            try await withCheckedThrowingContinuation { continuation in
                close = continuation
                opened.fulfill()
            }
        }
        web.onCancel = {
            close?.resume(throwing: MobileAccountError.providerCancelled)
            close = nil
        }
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [FakeMobileAuthTransport(), transport, FakeMobileAuthTransport(), FakeMobileAuthTransport()],
            webAuthenticator: web
        )
        coordinator.activateAccountSurface()
        let attempt = Task { try await coordinator.signInWithProvider(.google, presentationAnchor: UIWindow()) }
        await fulfillment(of: [opened], timeout: 30)

        let state = try await coordinator.logout()

        XCTAssertEqual(state, .signedOut)
        // One at the attempt's own start, one from the sign-out.
        XCTAssertGreaterThanOrEqual(web.cancels, 2)
        switch await attempt.result {
        case .success:
            XCTFail("The open attempt must not sign in")
        case .failure(let error):
            XCTAssertEqual((error as? MobileAccountError)?.code, MobileAccountError.providerCancelled.code)
        }
        XCTAssertTrue(transport.providerExchanges.isEmpty)
    }

    // One token-owning operation at a time: while a sign-in window is open, no other sign-in
    // may start, and the late result of an attempt never touches a newer generation's store.
    func testAnOpenProviderAttemptHoldsTheCoordinatorAndItsLateResultLeavesTheNewerStore() async throws {
        let defaults = makeDefaults()
        let store = makeAccountStore(defaults: defaults)
        let transport = FakeMobileAuthTransport()
        let web = FakeWebAuthenticator()
        let opened = expectation(description: "window open")
        var release: CheckedContinuation<URL, Error>?
        web.handler = { _, _, _ in
            try await withCheckedThrowingContinuation { continuation in
                release = continuation
                opened.fulfill()
            }
        }
        let coordinator = makeCoordinator(
            defaults: defaults,
            websiteDataStore: store,
            transports: [FakeMobileAuthTransport(), transport, FakeMobileAuthTransport(), FakeMobileAuthTransport()],
            webAuthenticator: web
        )
        coordinator.activateAccountSurface()
        let attempt = Task { try await coordinator.signInWithProvider(.google, presentationAnchor: UIWindow()) }
        await fulfillment(of: [opened], timeout: 30)

        do {
            _ = try await coordinator.signInWithProvider(.github, presentationAnchor: UIWindow())
            XCTFail("A second attempt must not start")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error, .busy)
        }
        do {
            _ = try await coordinator.requestOTP(email: "alex@example.com")
            XCTFail("An email code must not start either")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error, .busy)
        }
        XCTAssertEqual(transport.providerStarts.count, 1)

        // Sign-out wins; the attempt's window is closed, and its own late answer arrives after.
        web.onCancel = {}
        _ = try await coordinator.logout()
        let afterSignOut = store.identifier
        release?.resume(returning: URL(string: "https://127.0.0.1/mobile/auth/complete?code=late&state=x")!)
        _ = await attempt.result
        XCTAssertEqual(store.identifier, afterSignOut)
        XCTAssertTrue(transport.providerExchanges.isEmpty)
    }

    // The advertised list decorates signed-out answers; a slow read never holds them up.
    func testTheAdvertisedListIsReadWithinItsBoundOrLeftOut() async throws {
        let transport = FakeMobileAuthTransport()
        transport.advertised = [.github, .apple]
        let coordinator = makeCoordinator(defaults: makeDefaults(), transports: [transport])
        coordinator.activateAccountSurface()

        let quick = await coordinator.advertisedProviders(within: 5)
        XCTAssertEqual(quick, [.github, .apple])

        transport.advertisedDelay = 5_000_000_000
        let clock = ContinuousClock()
        let started = clock.now
        let slow = await coordinator.advertisedProviders(within: 0.05)
        XCTAssertNil(slow)
        XCTAssertLessThan(clock.now - started, .seconds(2))
    }

    func testSignInWithProviderAcceptsOnlyGoogleGitHubOrApple() async throws {
        let controller = MobileOnboardingViewController(
            coordinator: makeCoordinator(
                defaults: makeDefaults(),
                transports: [FakeMobileAuthTransport(), FakeMobileAuthTransport(), FakeMobileAuthTransport()]
            ),
            startScreen: .instances
        )
        let (window, webView) = try present(controller, size: CGSize(width: 390, height: 844))
        defer { window.isHidden = true }
        try await WebKitReadiness.waitForDocument(in: webView)
        for params in [
            "{}",
            "{provider: 'microsoft'}",
            "{provider: 'GOOGLE'}",
            "{provider: 'google', url: 'https://example.com/'}",
            "{provider: 1}",
        ] {
            let code = try await webView.callAsyncJavaScript(
                """
                try {
                  await window.avibeMobile.call('auth.signInWithProvider', \(params));
                  return 'missing-error';
                } catch (error) {
                  return error.code;
                }
                """,
                arguments: [:],
                in: nil,
                contentWorld: .page
            ) as? String
            XCTAssertEqual(code, "invalid_argument", params)
        }
    }

    func testSheetRejectsAuthMethodsWithoutMutatingTheRuntime() async throws {
        for mode in [MobileOnboardingStartScreen.sheet, .sheetInstances, .sheetAccount] {
            try await exerciseSheetAuthRestriction(mode)
        }
    }

    private func exerciseSheetAuthRestriction(_ startScreen: MobileOnboardingStartScreen) async throws {
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(accountID: "account-1")
        var requestCount = 0
        transport.requestOTPHandler = { _ in
            requestCount += 1
            return 60
        }
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [transport]
        )
        let controller = MobileOnboardingViewController(
            coordinator: coordinator,
            startScreen: startScreen
        )
        let (window, webView) = try present(
            controller,
            size: CGSize(width: 390, height: 844)
        )
        defer { window.isHidden = true }

        _ = try await waitForJavaScriptValue(in: webView) {
            "document.body.textContent.includes(\"Personal Avibe\")"
        }
        let result = try await webView.callAsyncJavaScript(
            """
            try {
              await window.avibeMobile.call('auth.requestOtp', {email: 'alex@example.com'});
              return 'missing-error';
            } catch (error) {
              return error.code;
            }
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        ) as? String
        XCTAssertEqual(result, MobileAccountError.forbidden.code)
        XCTAssertEqual(requestCount, 0)
        let provider = try await webView.callAsyncJavaScript(
            """
            try {
              await window.avibeMobile.call('auth.signInWithProvider', {provider: 'google'});
              return 'missing-error';
            } catch (error) {
              return error.code;
            }
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        ) as? String
        XCTAssertEqual(provider, MobileAccountError.forbidden.code)
        XCTAssertTrue(transport.providerStarts.isEmpty)
        XCTAssertEqual(coordinator.currentAccount?.id, "account-1")
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
        XCTAssertEqual(NativeShellMetadata.capabilities["systemSchedule"], 1)
        XCTAssertEqual(
            Set(NativeBridgeMethod.allCases.map(\.rawValue)),
            [
                "appearance.colorSchemeChanged",
                "app.info",
                "app.openExternal",
                "app.setAccountActionAvailable",
                "app.showAccount",
                "app.showInstances",
                "badge.set",
                "direct.status",
                "haptics.impact",
                "haptics.notify",
                "haptics.selection",
                "navigation.historyChanged",
                "notifications.reconcile",
                "push.status",
                "push.subscribe",
                "push.unsubscribe",
                "push.openedAck",
                "systemSchedule.cancel",
                "systemSchedule.capabilities",
                "systemSchedule.request",
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

    func testExternalURLAllowlistAddsOnlyTheAccountSecurityAndDownloadPages() {
        XCTAssertTrue(
            MobileExternalURLPolicy.allows(
                URL(string: "https://avibe.bot/app/account/security")!
            )
        )
        XCTAssertTrue(
            MobileExternalURLPolicy.allows(
                URL(string: "https://avibe.bot/#install")!
            )
        )
        for url in [
            "http://avibe.bot/#install",
            "https://avibe.bot/",
            "https://avibe.bot/#install?next=/",
            "https://avibe.bot/#install/other",
            "https://avibe.bot.example.com/#install",
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

    /// The owner's morning, end to end below the view: notifications on with
    /// a device token held, sign out, sign in again in the same process,
    /// open the instance, and turn notifications on. The account
    /// coordinator and the push coordinator are the real ones, wired as
    /// `AppModel` wires them; the account transport, the push keychain, the
    /// notification center, the system permission and APNs are stand-ins.
    /// APNs answers the subscription's registration with a main-queue block
    /// queued ahead of its provider reconciliation (#3837's race). The
    /// subscription must still register.
    func testEnableNotificationsAfterSignOutAndSignInInSameSessionSucceeds()
        async throws
    {
        let defaults = makeDefaults()
        let token = Data(repeating: 41, count: 32)
        let installationID = UUID()
        let keychain = InMemoryPushKeychainStore()
        keychain.installation = PushInstallationState(
            installationID: installationID,
            installationSecret: Data(repeating: 12, count: 32),
            accountID: "account-1",
            bindGeneration: 1,
            providerGeneration: 5,
            provider: PushProviderState(
                deviceToken: token,
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 5
            ),
            pendingBulkRevokeThroughGeneration: nil
        )
        let generationStore = InMemoryPushGenerationStore()
        generationStore.generation = 1
        try keychain.saveRecord(PushKeyRecord(
            kid: "morning-key",
            instanceOrigin: instance.url.absoluteString,
            accountID: "account-1",
            accountGeneration: 1,
            bindGeneration: 1,
            privateKey: P256.KeyAgreement.PrivateKey().rawRepresentation,
            authSecret: Data(repeating: 3, count: 16),
            vapidKeyHash: "morning",
            state: .active,
            pendingRevoke: false,
            replacementRegistrationIDs: []
        ))
        let marker = InMemoryPushInstallationMarkerStore()
        marker.installationID = installationID

        // Every account transport the coordinator makes reaches the same
        // account and the same push service, which answers 200.
        var requests: [String] = []
        let transports = (0..<24).map { _ in
            let transport = FakeMobileAuthTransport()
            transport.session = snapshot(accountID: "account-1")
            transport.pushManagementHandler = { request in
                let kind = request.path.split(separator: "/").last.map(String.init) ?? ""
                requests.append("\(request.method) \(request.path.contains("/registrations/") ? "registration" : kind)")
                return PushManagementResponse(statusCode: 200, body: [:])
            }
            return transport
        }
        var push: NativePushCoordinator!
        var registrations = 0
        let coordinator = makeCoordinator(
            defaults: defaults,
            transports: transports,
            // `AppModel.openRuntime` resumes push registration on every open.
            onOpen: { _ in push.resumeAfterAccountActivation() },
            onPreparePushLogout: { await push.prepareForLogout() }
        )
        push = NativePushCoordinator(
            keychain: keychain,
            generationStore: generationStore,
            managementRequest: { request in
                try await coordinator.pushManagementRequest(request)
            },
            accountProvider: { coordinator.currentAccount },
            registerForRemoteNotifications: {
                registrations += 1
                // UIKit hands the token back as a main-queue block.
                DispatchQueue.main.async {
                    push.didRegister(deviceToken: token, environment: "sandbox")
                }
            },
            requestNotificationAuthorization: { true },
            notificationAuthorizationStatus: { .authorized },
            installationMarker: marker,
            notificationCenter: QuietPushNotificationCenter()
        )

        // Signed in, the instance open, notifications on.
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)
        try await settlePush()

        // Sign out: the push installation is revoked for this account.
        coordinator.activateAccountSurface()
        let signedOut = try await coordinator.logout()
        XCTAssertEqual(signedOut, .signedOut)
        XCTAssertTrue(requests.contains { $0.hasPrefix("POST revoke") }, "\(requests)")

        // Sign in again in the same process, and open the instance.
        let signedIn = try await coordinator.verifyOTP(
            email: "account-1@example.com",
            code: "123456"
        )
        guard case .signedIn = signedIn else {
            return XCTFail("Signing in again ended at \(signedIn).")
        }
        _ = try coordinator.selectInstance(id: instance.id)
        try await settlePush()

        // Turn notifications on.
        let beforeEnable = requests.count
        let registrationsBeforeEnable = registrations
        let vapidPublicKey = PushBase64URL.encode(
            P256.KeyAgreement.PrivateKey().publicKey.x963Representation
        )
        let subscription = try await push.subscribe(
            vapidPublicKey: vapidPublicKey,
            instanceOrigin: instance.url.absoluteString
        )
        try await settlePush()

        XCTAssertGreaterThan(registrations, registrationsBeforeEnable)
        let enable = Array(requests[beforeEnable...])
        XCTAssertEqual(enable.filter { $0 == "PUT binding" }.count, 1, "\(enable)")
        XCTAssertEqual(enable.filter { $0 == "PUT registration" }.count, 1, "\(enable)")
        XCTAssertTrue(enable.contains("PATCH provider"), "\(enable)")
        XCTAssertNotNil(subscription["endpoint"] as? String)
        let active = try keychain.listRecords().filter {
            $0.state == .active && $0.accountID == "account-1"
        }
        XCTAssertEqual(active.count, 1)
        XCTAssertFalse(keychain.installation?.providerPatchPending ?? true)
    }

    /// Gives the push coordinator's queued main-actor work (resume,
    /// recovery, a token callback and its reconciliation, all against
    /// fakes) time to finish.
    private func settlePush() async throws {
        for _ in 0..<5 {
            for _ in 0..<50 {
                await Task.yield()
            }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
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

    /// The account page over a runtime reads the session while a push
    /// request the App made on its own is still running, instead of failing
    /// as busy (shown as "can't reach avibe.bot"). Nothing that changes the
    /// account, the store or the selection runs meanwhile.
    func testTheAccountPageReadsTheSessionWhileAPushRequestRuns() async throws {
        let first = FakeMobileAuthTransport()
        first.session = snapshot(accountID: "account-1")
        let management = FakeMobileAuthTransport()
        var coordinator: MobileSessionCoordinator!
        var read: MobileSessionState?
        var refused: [String] = []
        management.pushManagementHandler = { _ in
            read = try await coordinator.bootstrap()
            do {
                _ = try await coordinator.refresh()
            } catch let error as MobileAccountError {
                refused.append("refresh: \(error.code)")
            }
            do {
                _ = try coordinator.selectInstance(id: self.instance.id)
            } catch let error as MobileAccountError {
                refused.append("select: \(error.code)")
            }
            return PushManagementResponse(statusCode: 200, body: [:])
        }
        coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [first, management, FakeMobileAuthTransport()]
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)

        _ = try await coordinator.pushManagementRequest(
            PushManagementRequest(
                method: "POST",
                path: "/api/push/apns/installations",
                headers: [:],
                body: nil
            )
        )

        guard case .signedIn(let session, let selected) = try XCTUnwrap(read) else {
            return XCTFail("The account page did not read the session: \(String(describing: read))")
        }
        XCTAssertEqual(session.account.id, "account-1")
        XCTAssertEqual(selected, instance.id)
        XCTAssertEqual(
            refused,
            ["refresh: \(MobileAccountError.busy.code)", "select: \(MobileAccountError.busy.code)"]
        )
    }

    /// The list's open (the current block after an open timed out, or any
    /// row) made while a push request the App made on its own is running
    /// waits for that request and then opens, instead of failing as busy.
    func testTheListsOpenWaitsForAPushRequestAndOpensOnceItEnds() async throws {
        let first = FakeMobileAuthTransport()
        first.session = snapshot(accountID: "account-1")
        let management = FakeMobileAuthTransport()
        let hold = HeldCall()
        management.pushManagementHandler = { _ in
            await hold.wait()
            return PushManagementResponse(statusCode: 200, body: [:])
        }
        var opened: [MobileRuntimeContext] = []
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [first, management, FakeMobileAuthTransport()],
            onOpen: { opened.append($0) }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)
        XCTAssertEqual(opened.count, 1)

        let request = Task { @MainActor in
            _ = try await coordinator.pushManagementRequest(
                PushManagementRequest(
                    method: "PATCH",
                    path: "/api/push/apns/installations/current",
                    headers: [:],
                    body: nil
                )
            )
        }
        try await hold.waitUntilHeld()
        let reopen = Task { @MainActor in
            try await coordinator.selectInstanceAfterSelectionKeepingOperation(
                id: self.instance.id
            )
        }
        for _ in 0..<50 {
            await Task.yield()
        }
        try await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(opened.count, 1, "It opened while the request still ran.")

        hold.release()
        let reopened = try await reopen.value
        try await request.value
        XCTAssertEqual(reopened.id, instance.id)
        XCTAssertEqual(opened.count, 2)
        XCTAssertEqual(opened.last?.instance.id, instance.id)
    }

    /// Recovery can run push requests back to back, the next one starting
    /// before the waiting open resumes: the open keeps waiting, within its
    /// one bound, and opens once the last of them ends.
    func testTheListsOpenWaitsThroughPushRequestsRunBackToBack() async throws {
        let first = FakeMobileAuthTransport()
        first.session = snapshot(accountID: "account-1")
        let holds = [HeldCall(), HeldCall()]
        let managements = holds.map { hold in
            let management = FakeMobileAuthTransport()
            management.pushManagementHandler = { _ in
                await hold.wait()
                return PushManagementResponse(statusCode: 200, body: [:])
            }
            return management
        }
        var opened = 0
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [first] + managements + [FakeMobileAuthTransport()],
            onOpen: { _ in opened += 1 }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)
        let patch = PushManagementRequest(
            method: "PATCH",
            path: "/api/push/apns/installations/current",
            headers: [:],
            body: nil
        )
        let requests = Task { @MainActor in
            _ = try await coordinator.pushManagementRequest(patch)
            _ = try await coordinator.pushManagementRequest(patch)
        }
        try await holds[0].waitUntilHeld()
        let reopen = Task { @MainActor in
            try await coordinator.selectInstanceAfterSelectionKeepingOperation(
                id: self.instance.id
            )
        }
        for _ in 0..<50 {
            await Task.yield()
        }

        holds[0].release()
        try await holds[1].waitUntilHeld()
        for _ in 0..<50 {
            await Task.yield()
        }
        try await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(opened, 1, "It opened, or gave up, during the second request.")

        holds[1].release()
        let reopened = try await reopen.value
        try await requests.value
        XCTAssertEqual(reopened.id, instance.id)
        XCTAssertEqual(opened, 2)
    }

    /// An open that waited is dropped when the page that asked for it closed
    /// meanwhile (the sheet swiped away over a live runtime): it must not
    /// replace the runtime the user went back to.
    func testTheListsOpenIsDroppedWhenItsPageClosesDuringTheWait() async throws {
        let first = FakeMobileAuthTransport()
        first.session = snapshot(accountID: "account-1")
        let management = FakeMobileAuthTransport()
        let hold = HeldCall()
        management.pushManagementHandler = { _ in
            await hold.wait()
            return PushManagementResponse(statusCode: 200, body: [:])
        }
        var opened = 0
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [first, management, FakeMobileAuthTransport()],
            onOpen: { _ in opened += 1 }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)
        // The sheet over the runtime; the push request uses its transport.
        coordinator.activateAccountSurface()
        let request = Task { @MainActor in
            _ = try await coordinator.pushManagementRequest(
                PushManagementRequest(
                    method: "PATCH",
                    path: "/api/push/apns/installations/current",
                    headers: [:],
                    body: nil
                )
            )
        }
        try await hold.waitUntilHeld()
        let reopen = Task { @MainActor in
            try await coordinator.selectInstanceAfterSelectionKeepingOperation(
                id: self.instance.id
            )
        }
        for _ in 0..<50 {
            await Task.yield()
        }
        coordinator.leaveAccountSurface()

        hold.release()
        do {
            _ = try await reopen.value
            XCTFail("The open went ahead after its page closed.")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error, .busy)
        }
        try await request.value
        XCTAssertEqual(opened, 1)
    }

    /// Only an operation that keeps the selection is waited for: any other
    /// one (here the first session read, before a list exists) still
    /// answers busy at once.
    func testTheListsOpenAnswersBusyAtOnceForAnOperationThatDoesNotKeepTheSelection()
        async throws
    {
        let first = FakeMobileAuthTransport()
        let session = snapshot(accountID: "account-1")
        let hold = HeldCall()
        first.fetchSessionHandler = {
            await hold.wait()
            return .signedIn(session)
        }
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [first, FakeMobileAuthTransport()]
        )
        coordinator.activateAccountSurface()
        let bootstrap = Task { @MainActor in
            _ = try await coordinator.bootstrap()
        }
        try await hold.waitUntilHeld()

        let clock = ContinuousClock()
        let started = clock.now
        do {
            _ = try await coordinator.selectInstanceAfterSelectionKeepingOperation(
                id: instance.id
            )
            XCTFail("The open did not answer busy.")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error, .busy)
        }
        XCTAssertLessThan(started.duration(to: clock.now), .seconds(2))

        hold.release()
        try await bootstrap.value
    }

    /// The wait is bounded: a push request that does not finish in time
    /// leaves the open answering busy, not hanging.
    func testTheListsOpenAnswersBusyWhenThePushRequestOutlastsTheWait()
        async throws
    {
        let first = FakeMobileAuthTransport()
        first.session = snapshot(accountID: "account-1")
        let management = FakeMobileAuthTransport()
        let hold = HeldCall()
        management.pushManagementHandler = { _ in
            await hold.wait()
            return PushManagementResponse(statusCode: 200, body: [:])
        }
        var opened = 0
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [first, management, FakeMobileAuthTransport()],
            onOpen: { _ in opened += 1 }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)
        let request = Task { @MainActor in
            _ = try await coordinator.pushManagementRequest(
                PushManagementRequest(
                    method: "PATCH",
                    path: "/api/push/apns/installations/current",
                    headers: [:],
                    body: nil
                )
            )
        }
        try await hold.waitUntilHeld()

        let clock = ContinuousClock()
        let started = clock.now
        do {
            _ = try await coordinator.selectInstanceAfterSelectionKeepingOperation(
                id: instance.id,
                waitingAtMost: .milliseconds(300)
            )
            XCTFail("The open did not answer busy.")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error, .busy)
        }
        let waited = started.duration(to: clock.now)
        XCTAssertGreaterThanOrEqual(waited, .milliseconds(300))
        XCTAssertLessThan(waited, .seconds(5))
        XCTAssertEqual(opened, 1)

        hold.release()
        try await request.value
        XCTAssertEqual(opened, 1)
    }

    /// The instance list: the current instance first, then the ones opened on
    /// this device, most recent first, then the rest in avibe.bot's order.
    /// Each account keeps its own.
    func testInstancesListTheCurrentThenTheRecentlyOpenedThenAvibeBotsOrder() async throws {
        let instances = ["a", "b", "c", "d"].map { id in
            MobileInstance(
                id: id,
                name: "Instance \(id)",
                host: "\(id).avibe.bot",
                url: URL(string: "https://\(id).avibe.bot")!,
                relationship: "owner",
                ownerEmail: nil,
                available: true
            )
        }
        let defaults = makeDefaults()
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(accountID: "account-1", instances: instances)
        let coordinator = makeCoordinator(
            defaults: defaults,
            transports: [transport, FakeMobileAuthTransport(), FakeMobileAuthTransport()]
        )
        let order = { (state: MobileSessionState) -> [String] in
            guard case .signedIn(let session, _) = state else { return [] }
            return session.instances.map(\.id)
        }
        coordinator.activateAccountSurface()
        let read1 = order(try await coordinator.bootstrap())

        XCTAssertEqual(read1, ["a", "b", "c", "d"])

        _ = try coordinator.selectInstance(id: "c")
        let read2 = order(try await coordinator.bootstrap())

        XCTAssertEqual(read2, ["c", "a", "b", "d"])
        try await Task.sleep(for: .milliseconds(20))
        _ = try coordinator.selectInstance(id: "b")
        let read3 = order(try await coordinator.bootstrap())

        XCTAssertEqual(read3, ["b", "c", "a", "d"])

        // A relaunch keeps it, recorded under this account only.
        let relaunched = makeCoordinator(defaults: defaults, transports: [transport])
        relaunched.activateAccountSurface()
        let read4 = order(try await relaunched.bootstrap())

        XCTAssertEqual(read4, ["b", "c", "a", "d"])
        XCTAssertEqual(
            defaults.dictionary(forKey: "avibe.mobile.instanceUse")?.keys.sorted(),
            ["account-1"]
        )
    }

    /// A swipe in from the left edge presses the page's own Back, where one is
    /// shown, and does nothing where none is.
    func testAnEdgeSwipePressesTheShownBackAndNothingWithoutOne() async throws {
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(accountID: "account-1")
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [transport, FakeMobileAuthTransport()]
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)

        var returned = 0
        let account = MobileOnboardingViewController(
            coordinator: coordinator,
            startScreen: .runtimeAccount,
            returnToRuntime: {
                returned += 1
                return true
            }
        )
        let (accountWindow, accountWebView) = try present(
            account,
            size: CGSize(width: 390, height: 700)
        )
        defer { accountWindow.isHidden = true }
        XCTAssertTrue(
            account.view.gestureRecognizers?.contains {
                ($0 as? UIScreenEdgePanGestureRecognizer)?.edges == .left
            } == true
        )
        let backShown = try await waitForJavaScriptValue(in: accountWebView) {
            "document.querySelector('[data-mobile-back]') !== null"
        }
        XCTAssertTrue(backShown)
        let pressed = await account.pressPageBack()
        XCTAssertTrue(pressed)
        for _ in 0..<50 where returned == 0 {
            try await Task.sleep(for: .milliseconds(100))
        }
        XCTAssertEqual(returned, 1)

        // The instance list with nothing behind it has no Back.
        let list = MobileOnboardingViewController(
            coordinator: coordinator,
            startScreen: .instances
        )
        let (listWindow, listWebView) = try present(
            list,
            size: CGSize(width: 390, height: 700)
        )
        defer { listWindow.isHidden = true }
        let listShown = try await waitForJavaScriptValue(in: listWebView) {
            "document.querySelector('main button') !== null"
        }
        XCTAssertTrue(listShown)
        let pressedOnList = await list.pressPageBack()
        XCTAssertFalse(pressedOnList)
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

    func testRefreshWithoutAnAccountPageReleasesItsTemporaryTransport() async throws {
        let transport = FakeMobileAuthTransport()
        let fresh = snapshot(accountID: "account-1")
        transport.session = fresh
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [transport]
        )

        let state = try await coordinator.refresh()

        XCTAssertEqual(state, .signedIn(snapshot: fresh, selectedInstanceID: nil))
        XCTAssertTrue(transport.invalidated)
        XCTAssertFalse(coordinator.isAccountSurfaceActive)
    }

    func testRefreshSecondFactorRevokesTheLiveSelectionWithoutRotatingTheStore()
        async throws
    {
        let defaults = makeDefaults()
        let store = makeAccountStore(defaults: defaults)
        let storeIdentifier = store.identifier
        let firstTransport = FakeMobileAuthTransport()
        firstTransport.session = snapshot(accountID: "account-1")
        let refreshTransport = FakeMobileAuthTransport()
        let requirement = MobileSecondFactorRequirement(
            email: "account-1@example.com",
            gate: .challenge,
            methods: [.totp]
        )
        refreshTransport.secondFactor = requirement
        var runtime: MobileRuntimeContext?
        var transitions = 0
        let coordinator = makeCoordinator(
            defaults: defaults,
            websiteDataStore: store,
            transports: [firstTransport, refreshTransport],
            onOpen: { runtime = $0 },
            onPrepareAccountTransition: {
                transitions += 1
                return []
            }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)
        let selected = try XCTUnwrap(runtime)
        coordinator.activateAccountSurface()

        let owed = try await coordinator.refresh()
        XCTAssertEqual(owed, .twoFactorRequired(requirement))
        XCTAssertFalse(coordinator.isSelectedRuntime(selected))
        XCTAssertEqual(transitions, 1)
        XCTAssertEqual(store.identifier, storeIdentifier)

        refreshTransport.secondFactor = nil
        refreshTransport.session = snapshot(accountID: "account-1")
        let proved = try await coordinator.refresh()
        XCTAssertEqual(
            proved,
            .signedIn(snapshot: snapshot(accountID: "account-1"), selectedInstanceID: nil)
        )
        XCTAssertEqual(store.identifier, storeIdentifier)
    }

    func testRefreshNetworkFailureKeepsSelectedRuntimeAndSnapshot() async throws {
        let defaults = makeDefaults()
        let firstTransport = FakeMobileAuthTransport()
        firstTransport.session = snapshot(accountID: "account-1")
        let refreshTransport = FakeMobileAuthTransport()
        refreshTransport.fetchSessionError = MobileAccountError.network
        var runtime: MobileRuntimeContext?
        var transitions = 0
        let coordinator = makeCoordinator(
            defaults: defaults,
            transports: [firstTransport, refreshTransport],
            onOpen: { runtime = $0 },
            onPrepareAccountTransition: {
                transitions += 1
                return []
            }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)
        let selected = try XCTUnwrap(runtime)
        coordinator.activateAccountSurface()

        do {
            _ = try await coordinator.refresh()
            XCTFail("A refresh transport failure should be reported")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error.code, MobileAccountError.network.code)
        }
        XCTAssertTrue(coordinator.isCurrentRuntime(selected))
        XCTAssertEqual(coordinator.currentAccount?.id, "account-1")
        XCTAssertEqual(transitions, 0)
    }

    func testRefreshSameAccountKeepsSelectedRuntimeAndAppliesCatalogChanges()
        async throws
    {
        let defaults = makeDefaults()
        let firstTransport = FakeMobileAuthTransport()
        firstTransport.session = snapshot(accountID: "account-1")
        let refreshedInstance = MobileInstance(
            id: instance.id,
            name: "Renamed Avibe",
            host: instance.host,
            url: instance.url,
            relationship: instance.relationship,
            ownerEmail: instance.ownerEmail,
            available: true
        )
        let refreshTransport = FakeMobileAuthTransport()
        refreshTransport.session = snapshot(
            accountID: "account-1",
            instances: [refreshedInstance]
        )
        var runtime: MobileRuntimeContext?
        var transitions = 0
        let coordinator = makeCoordinator(
            defaults: defaults,
            transports: [firstTransport, refreshTransport],
            onOpen: { runtime = $0 },
            onPrepareAccountTransition: {
                transitions += 1
                return []
            }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)
        let selected = try XCTUnwrap(runtime)
        coordinator.activateAccountSurface()

        guard case .signedIn = try await coordinator.refresh() else {
            return XCTFail("Expected a signed-in refresh")
        }
        XCTAssertTrue(coordinator.isCurrentRuntime(selected))
        XCTAssertEqual(coordinator.currentInstances.first?.name, "Renamed Avibe")
        XCTAssertEqual(transitions, 0)
    }

    func testSurfaceRefreshInterleavingsUseOneTransportOwner() async throws {
        enum ResultKind: Equatable {
            case sameAccount
            case networkFailure
        }
        struct Row {
            let name: String
            let result: ResultKind
            let openBeforeRefresh: Bool
            let reopenBeforeCompletion: Bool
            let expectsRuntime: Bool
            let expectsSurface: Bool
        }
        let rows = [
            Row(
                name: "dismiss during same-account refresh",
                result: .sameAccount,
                openBeforeRefresh: true,
                reopenBeforeCompletion: false,
                expectsRuntime: true,
                expectsSurface: false
            ),
            Row(
                name: "dismiss then reopen before same-account refresh completes",
                result: .sameAccount,
                openBeforeRefresh: true,
                reopenBeforeCompletion: true,
                expectsRuntime: true,
                expectsSurface: true
            ),
            Row(
                name: "dismiss during failed refresh",
                result: .networkFailure,
                openBeforeRefresh: true,
                reopenBeforeCompletion: false,
                expectsRuntime: true,
                expectsSurface: false
            ),
            Row(
                name: "native runtime refresh without an account page",
                result: .sameAccount,
                openBeforeRefresh: false,
                reopenBeforeCompletion: false,
                expectsRuntime: true,
                expectsSurface: false
            ),
            Row(
                name: "native runtime refresh fails without an account page",
                result: .networkFailure,
                openBeforeRefresh: false,
                reopenBeforeCompletion: false,
                expectsRuntime: true,
                expectsSurface: false
            ),
        ]

        for row in rows {
            let defaults = makeDefaults()
            let firstTransport = FakeMobileAuthTransport()
            firstTransport.session = snapshot(accountID: "account-1")
            let refreshTransport = FakeMobileAuthTransport()
            let refreshStarted = expectation(description: "refresh started: \(row.name)")
            let allowRefresh = expectation(description: "refresh may finish: \(row.name)")
            refreshTransport.fetchSessionHandler = {
                refreshStarted.fulfill()
                await self.fulfillment(of: [allowRefresh], timeout: 30)
                switch row.result {
                case .sameAccount:
                    return .signedIn(self.snapshot(accountID: "account-1"))
                case .networkFailure:
                    throw MobileAccountError.network
                }
            }
            var runtime: MobileRuntimeContext?
            let coordinator = makeCoordinator(
                defaults: defaults,
                transports: [firstTransport, refreshTransport],
                onOpen: { runtime = $0 }
            )
            coordinator.activateAccountSurface()
            _ = try await coordinator.bootstrap()
            _ = try coordinator.selectInstance(id: instance.id)
            let selected = try XCTUnwrap(runtime, row.name)
            if row.openBeforeRefresh {
                coordinator.activateAccountSurface()
            }

            let refresh = Task { try await coordinator.refresh() }
            await fulfillment(of: [refreshStarted], timeout: 30)
            if row.openBeforeRefresh {
                coordinator.leaveAccountSurface()
            }
            if row.reopenBeforeCompletion {
                coordinator.activateAccountSurface()
            }
            allowRefresh.fulfill()
            do {
                let state = try await refresh.value
                XCTAssertEqual(row.result, .sameAccount, row.name)
                XCTAssertEqual(
                    state,
                    .signedIn(snapshot: snapshot(accountID: "account-1"), selectedInstanceID: instance.id),
                    row.name
                )
            } catch let error as MobileAccountError {
                XCTAssertEqual(row.result, .networkFailure, row.name)
                XCTAssertEqual(error, .network, row.name)
            }
            XCTAssertEqual(refreshTransport.invalidated, !row.expectsSurface, row.name)

            XCTAssertEqual(
                coordinator.isCurrentRuntime(selected),
                row.expectsRuntime,
                row.name
            )
            XCTAssertEqual(
                coordinator.isAccountSurfaceActive,
                row.expectsSurface,
                row.name
            )
            if row.reopenBeforeCompletion {
                coordinator.leaveAccountSurface()
            }
        }
    }

    /// A reset may supersede refresh AFTER discovery while native push cleanup
    /// is suspended. Existing fetch-held tests cover only the earlier await.
    func testRefreshPushCleanupCannotOverwriteLogoutOrANewerAccount() async throws {
        let requirement = MobileSecondFactorRequirement(
            email: "account-1@example.com", gate: .challenge, methods: [.totp]
        )
        let discoveries: [(String, MobileSessionDiscovery?)] = [
            ("removed origin", .signedIn(snapshot(accountID: "account-1", instances: []))),
            ("second factor", .secondFactorRequired(requirement)),
            ("signed out", nil),
            ("other account", .signedIn(snapshot(accountID: "account-2"))),
        ]
        for (kind, discovery) in discoveries {
            for order in ["during logout", "after logout", "after account B login"] {
                let name = "\(kind), \(order)"
                let first = FakeMobileAuthTransport()
                first.session = snapshot(accountID: "account-1")
                let refreshTransport = FakeMobileAuthTransport()
                refreshTransport.fetchSessionHandler = { discovery }
                let replacement = FakeMobileAuthTransport()
                let accountB = snapshot(accountID: "account-3")
                replacement.session = accountB
                let cleanupStarted = expectation(description: "push cleanup: \(name)")
                let allowCleanup = expectation(description: "release push cleanup: \(name)")
                let logoutStarted = expectation(description: "logout: \(name)")
                let allowLogout = expectation(description: "release logout: \(name)")
                refreshTransport.logoutHandler = {
                    logoutStarted.fulfill()
                    await self.fulfillment(of: [allowLogout], timeout: 30)
                }
                var opened: MobileRuntimeContext?
                var created = 0
                let coordinator = makeCoordinator(
                    defaults: makeDefaults(),
                    transports: [first, refreshTransport, replacement]
                        + (0..<4).map { _ in FakeMobileAuthTransport() },
                    onCreateTransport: { created += 1 },
                    onOpen: { opened = $0 },
                    onPreparePushLogout: {
                        cleanupStarted.fulfill()
                        await self.fulfillment(of: [allowCleanup], timeout: 30)
                    }
                )
                coordinator.activateAccountSurface()
                _ = try await coordinator.bootstrap()
                _ = try coordinator.selectInstance(id: instance.id)
                coordinator.activateAccountSurface()
                let refresh = Task { try await coordinator.refresh() }
                await fulfillment(of: [cleanupStarted], timeout: 30)
                let logout = Task { try await coordinator.logout() }
                await fulfillment(of: [logoutStarted], timeout: 30)

                let result: Result<MobileSessionState, Error>
                if order == "during logout" {
                    allowCleanup.fulfill()
                    result = await refresh.result
                    allowLogout.fulfill()
                    _ = try await logout.value
                } else {
                    allowLogout.fulfill()
                    _ = try await logout.value
                    if order == "after account B login" {
                        _ = try await coordinator.verifyOTP(
                            email: "account-3@example.com", code: "123456"
                        )
                        _ = try coordinator.selectInstance(id: instance.id)
                    }
                    allowCleanup.fulfill()
                    result = await refresh.result
                }
                switch result {
                case .success:
                    XCTFail("Superseded refresh must be stale: \(name)")
                case .failure(let error):
                    XCTAssertEqual((error as? MobileAccountError)?.code, "stale_session", name)
                }
                let state = try await coordinator.bootstrap()
                if order == "after account B login" {
                    XCTAssertEqual(coordinator.currentAccount, accountB.account, name)
                    XCTAssertEqual(state, .signedIn(snapshot: accountB, selectedInstanceID: instance.id), name)
                    XCTAssertTrue(coordinator.isCurrentRuntime(try XCTUnwrap(opened)), name)
                } else {
                    XCTAssertNil(coordinator.currentAccount, name)
                    XCTAssertEqual(state, .signedOut, name)
                    XCTAssertFalse(replacement.invalidated, name)
                }
                XCTAssertEqual(created, 3, name)
            }
        }
    }

    /// Reopening during push cleanup is later than the existing fetch-held
    /// cases, but earlier than their post-transition/store-rotation bootstrap.
    func testReplacementBootstrapReadsTheAuthoritativeResultDuringPushCleanup() async throws {
        let requirement = MobileSecondFactorRequirement(
            email: "account-1@example.com", gate: .challenge, methods: [.totp]
        )
        let reduced = snapshot(accountID: "account-1", instances: [])
        let rows: [(String, MobileSessionDiscovery?, MobileSessionState)] = [
            ("signed out", nil, .signedOut),
            ("other account", .signedIn(snapshot(accountID: "account-2")), .signedOut),
            ("removed origin", .signedIn(reduced), .signedIn(snapshot: reduced, selectedInstanceID: nil)),
            ("second factor", .secondFactorRequired(requirement), .twoFactorRequired(requirement)),
        ]
        for (name, discovery, expected) in rows {
            let first = FakeMobileAuthTransport()
            first.session = snapshot(accountID: "account-1")
            let refreshTransport = FakeMobileAuthTransport()
            let fetchStarted = expectation(description: "fetch: \(name)")
            let allowFetch = expectation(description: "release fetch: \(name)")
            let cleanupStarted = expectation(description: "cleanup: \(name)")
            let allowCleanup = expectation(description: "release cleanup: \(name)")
            refreshTransport.fetchSessionHandler = {
                fetchStarted.fulfill()
                await self.fulfillment(of: [allowFetch], timeout: 30)
                return discovery
            }
            let coordinator = makeCoordinator(
                defaults: makeDefaults(),
                transports: [first, refreshTransport, FakeMobileAuthTransport()],
                onPreparePushLogout: {
                    cleanupStarted.fulfill()
                    await self.fulfillment(of: [allowCleanup], timeout: 30)
                }
            )
            coordinator.activateAccountSurface()
            _ = try await coordinator.bootstrap()
            _ = try coordinator.selectInstance(id: instance.id)
            coordinator.activateAccountSurface()
            let refresh = Task { try await coordinator.refresh() }
            await fulfillment(of: [fetchStarted], timeout: 30)
            coordinator.leaveAccountSurface()
            allowFetch.fulfill()
            await fulfillment(of: [cleanupStarted], timeout: 30)
            coordinator.activateAccountSurface()
            let replacement: Result<MobileSessionState, Error>
            do {
                replacement = .success(try await coordinator.bootstrap())
            } catch {
                replacement = .failure(error)
            }
            allowCleanup.fulfill()
            let final = try await refresh.value
            XCTAssertEqual(try replacement.get(), expected, name)
            XCTAssertEqual(final, expected, name)
            XCTAssertTrue(coordinator.isAccountSurfaceActive, name)
            if expected != .signedOut {
                XCTAssertFalse(refreshTransport.invalidated, name)
            }
        }
    }

    func testPushCleanupForBootstrapSecondFactorAndLogoutRejectsSupersededResults() async throws {
        let requirement = MobileSecondFactorRequirement(
            email: "account-1@example.com", gate: .challenge, methods: [.totp]
        )
        for operation in ["bootstrap", "second factor", "logout"] {
            let original = FakeMobileAuthTransport()
            if operation == "second factor" {
                original.secondFactor = requirement
                original.verifySecondFactorHandler = { _ in throw MobileAccountError.signedOut }
            } else if operation == "logout" {
                original.session = snapshot(accountID: "account-1")
            }
            let replacement = FakeMobileAuthTransport()
            replacement.session = snapshot(accountID: "account-2")
            let started = expectation(description: "cleanup \(operation)")
            let release = expectation(description: "release cleanup \(operation)")
            var created = 0
            let coordinator = makeCoordinator(
                defaults: makeDefaults(),
                transports: [original, replacement, FakeMobileAuthTransport()],
                onCreateTransport: { created += 1 },
                onPreparePushLogout: {
                    started.fulfill()
                    await self.fulfillment(of: [release], timeout: 30)
                }
            )
            coordinator.activateAccountSurface()
            if operation != "bootstrap" { _ = try await coordinator.bootstrap() }
            let pending = Task {
                switch operation {
                case "bootstrap": return try await coordinator.bootstrap()
                case "second factor": return try await coordinator.verifySecondFactor(method: .totp, code: "123456")
                default: return try await coordinator.logout()
                }
            }
            await fulfillment(of: [started], timeout: 30)
            if operation == "logout" {
                // Reset now reserves authority before cleanup. A duplicate
                // must refuse without invalidating or stranding that owner.
                do {
                    _ = try await coordinator.logout()
                    XCTFail("Duplicate logout must be busy")
                } catch {
                    XCTAssertEqual(error as? MobileAccountError, .busy)
                }
                release.fulfill()
                let signedOut = try await pending.value
                XCTAssertEqual(signedOut, .signedOut)
                _ = try await coordinator.verifyOTP(email: "account-2@example.com", code: "123456")
                XCTAssertEqual(coordinator.currentAccount?.id, "account-2")
                XCTAssertEqual(created, 2)
                continue
            }
            let signedOut = try await coordinator.logout()
            XCTAssertEqual(signedOut, .signedOut, operation)
            _ = try await coordinator.verifyOTP(email: "account-2@example.com", code: "123456")
            release.fulfill()
            switch await pending.result {
            case .success: XCTFail("Superseded \(operation) must be stale")
            case .failure(let error):
                XCTAssertEqual((error as? MobileAccountError)?.code, "stale_session", operation)
            }
            XCTAssertEqual(coordinator.currentAccount?.id, "account-2", operation)
            XCTAssertFalse(replacement.invalidated, operation)
            XCTAssertEqual(created, 2, operation)
        }
    }

    func testStoreRotationRejectsSupersededAuthOperationsBeforeTheyUseTheNewTransport() async throws {
        for operation in ["bootstrap", "password", "OTP request"] {
            let defaults = makeDefaults()
            let registry = FakeNamedDataStoreRegistry()
            let store = AccountWebsiteDataStore(defaults: defaults, operations: registry.operations)
            let rotating = expectation(description: "old rotation \(operation)")
            let releaseRotation = expectation(description: "release old rotation \(operation)")
            var lists = 0
            registry.listIdentifiersHandler = {
                lists += 1
                if lists == 1 {
                    rotating.fulfill()
                    await self.fulfillment(of: [releaseRotation], timeout: 30)
                }
            }
            let original = FakeMobileAuthTransport()
            original.signInWithPasswordHandler = { _, _ in throw MobileAccountError.signedOut }
            let resetting = FakeMobileAuthTransport()
            let replacement = FakeMobileAuthTransport()
            let accountB = snapshot(accountID: "account-2")
            replacement.session = accountB
            let configuration = WKWebViewConfiguration()
            configuration.websiteDataStore = .nonPersistent()
            let retainedView = WKWebView(frame: .zero, configuration: configuration)
            let lateBarrier = WebsiteDataStoreReleaseBarrier(webViews: [retainedView])
            var transitions = 0
            let transports = [original, resetting, replacement]
                + (0..<3).map { _ in FakeMobileAuthTransport() }
            var otpRequests = 0
            for transport in transports {
                transport.requestOTPHandler = { _ in otpRequests += 1; return 60 }
            }
            var created = 0
            var opened: MobileRuntimeContext?
            let coordinator = makeCoordinator(
                defaults: defaults, websiteDataStore: store, transports: transports,
                onCreateTransport: { created += 1 }, onOpen: { opened = $0 },
                onPrepareAccountTransition: {
                    transitions += 1
                    // The NEW reset records a retained view after the old
                    // rotation has already entered its asynchronous listing.
                    return transitions == 2 ? [lateBarrier] : []
                }
            )
            coordinator.activateAccountSurface()
            let pending = Task {
                switch operation {
                case "bootstrap": _ = try await coordinator.bootstrap()
                case "password":
                    _ = try await coordinator.signInWithPassword(email: "old@example.com", password: "fixture")
                default: _ = try await coordinator.requestOTP(email: "old@example.com")
                }
            }
            await fulfillment(of: [rotating], timeout: 30)
            _ = try await coordinator.logout()
            _ = try await coordinator.verifyOTP(email: "account-2@example.com", code: "123456")
            _ = try coordinator.selectInstance(id: instance.id)
            let runtime = try XCTUnwrap(opened)
            releaseRotation.fulfill()
            switch await pending.result {
            case .success: XCTFail("Old \(operation) must be stale after store rotation")
            case .failure(let error):
                XCTAssertEqual((error as? MobileAccountError)?.code, "stale_session", operation)
            }
            let state = try await coordinator.bootstrap()
            XCTAssertEqual(state, .signedIn(snapshot: accountB, selectedInstanceID: instance.id), operation)
            XCTAssertEqual(otpRequests, 0, operation)
            XCTAssertEqual(created, 3, operation)
            XCTAssertTrue(coordinator.isCurrentRuntime(runtime), operation)
            XCTAssertFalse(coordinator.isAccountSurfaceActive, operation)
            XCTAssertTrue(registry.removalAttempts.isEmpty, operation)
            withExtendedLifetime(retainedView) {}
        }
    }

    func testLogoutReservesAuthorityBeforeAnOlderRefreshReturns() async throws {
        let requirement = MobileSecondFactorRequirement(
            email: "account-1@example.com", gate: .challenge, methods: [.totp]
        )
        let rows: [(String, MobileSessionDiscovery?)] = [
            ("second factor", .secondFactorRequired(requirement)),
            ("removed origin", .signedIn(snapshot(accountID: "account-1", instances: []))),
            ("signed out", nil),
            ("other account", .signedIn(snapshot(accountID: "account-2"))),
        ]
        for (kind, discovery) in rows {
            for refreshFirst in [false, true] {
                let name = "\(kind), refresh completes first: \(refreshFirst)"
                let defaults = makeDefaults()
                let store = makeAccountStore(defaults: defaults)
                let oldIdentifier = store.identifier
                let first = FakeMobileAuthTransport()
                first.session = snapshot(accountID: "account-1")
                let fetch = FakeMobileAuthTransport()
                let fetched = expectation(description: "fetch \(name)")
                let releaseFetch = expectation(description: "release fetch \(name)")
                fetch.fetchSessionHandler = {
                    fetched.fulfill()
                    await self.fulfillment(of: [releaseFetch], timeout: 30)
                    return discovery
                }
                let replacement = FakeMobileAuthTransport()
                let cleanup = expectation(description: "logout owns cleanup \(name)")
                let releaseCleanup = expectation(description: "release cleanup \(name)")
                let coordinator = makeCoordinator(
                    defaults: defaults, websiteDataStore: store,
                    transports: [first, fetch, replacement, FakeMobileAuthTransport()],
                    onPreparePushLogout: {
                        cleanup.fulfill()
                        await self.fulfillment(of: [releaseCleanup], timeout: 30)
                    }
                )
                coordinator.activateAccountSurface()
                _ = try await coordinator.bootstrap()
                _ = try coordinator.selectInstance(id: instance.id)
                coordinator.activateAccountSurface()
                let refresh = Task { try await coordinator.refresh() }
                await fulfillment(of: [fetched], timeout: 30)
                let logout = Task { try await coordinator.logout() }
                await fulfillment(of: [cleanup], timeout: 30)
                let refreshResult: Result<MobileSessionState, Error>
                let logoutResult: Result<MobileSessionState, Error>
                if refreshFirst {
                    releaseFetch.fulfill()
                    refreshResult = await refresh.result
                    releaseCleanup.fulfill()
                    logoutResult = await logout.result
                } else {
                    releaseCleanup.fulfill()
                    logoutResult = await logout.result
                    releaseFetch.fulfill()
                    refreshResult = await refresh.result
                }
                switch refreshResult {
                case .success: XCTFail("Older refresh must lose: \(name)")
                case .failure(let error):
                    XCTAssertEqual((error as? MobileAccountError)?.code, "stale_session", name)
                }
                XCTAssertEqual(try logoutResult.get(), .signedOut, name)
                XCTAssertNil(coordinator.currentAccount, name)
                XCTAssertNotEqual(store.identifier, oldIdentifier, name)
                let bootstrap = try await coordinator.bootstrap()
                XCTAssertEqual(bootstrap, .signedOut, name)
                let cooldown = try await coordinator.requestOTP(email: "next@example.com")
                XCTAssertEqual(cooldown, 60, name)
            }
        }
    }

    func testAnOlderCleanupDoesNotSuppressANewerAccountsCleanup() async throws {
        let requirement = MobileSecondFactorRequirement(
            email: "account-2@example.com", gate: .challenge, methods: [.totp]
        )
        let initial = FakeMobileAuthTransport()
        initial.session = snapshot(accountID: "account-1")
        let oldRefresh = FakeMobileAuthTransport()
        oldRefresh.session = snapshot(accountID: "account-1", instances: [])
        let replacement = FakeMobileAuthTransport()
        replacement.session = snapshot(accountID: "account-2")
        let newRefresh = FakeMobileAuthTransport()
        newRefresh.secondFactor = requirement
        let oldCleanup = expectation(description: "old account cleanup")
        let releaseOld = expectation(description: "release old account cleanup")
        var cleanups = 0
        let coordinator = makeCoordinator(
            defaults: makeDefaults(), transports: [initial, oldRefresh, replacement, newRefresh],
            onPreparePushLogout: {
                cleanups += 1
                if cleanups == 1 {
                    oldCleanup.fulfill()
                    await self.fulfillment(of: [releaseOld], timeout: 30)
                }
            }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)
        coordinator.activateAccountSurface()
        let pending = Task { try await coordinator.refresh() }
        await fulfillment(of: [oldCleanup], timeout: 30)
        _ = try await coordinator.logout()
        _ = try await coordinator.verifyOTP(email: "account-2@example.com", code: "123456")
        _ = try coordinator.selectInstance(id: instance.id)
        let state = try await coordinator.refresh()
        releaseOld.fulfill()
        switch await pending.result {
        case .success: XCTFail("Old account refresh must lose authority")
        case .failure(let error):
            XCTAssertEqual((error as? MobileAccountError)?.code, "stale_session")
        }
        XCTAssertEqual(cleanups, 2)
        XCTAssertEqual(state, .twoFactorRequired(requirement))
        XCTAssertFalse(coordinator.isAccountSurfaceActive)
    }

    func testRefreshPushLogoutKeepsTheOuterTransportOwner() async throws {
        let requirement = MobileSecondFactorRequirement(
            email: "account-1@example.com",
            gate: .challenge,
            methods: [.totp]
        )
        let removed = snapshot(accountID: "account-1", instances: [])
        let rows: [(name: String, discovery: MobileSessionDiscovery?, state: MobileSessionState, transports: Int)] = [
            ("second factor", .secondFactorRequired(requirement), .twoFactorRequired(requirement), 2),
            ("selected origin removed", .signedIn(removed), .signedIn(snapshot: removed, selectedInstanceID: nil), 2),
            ("signed out", nil, .signedOut, 3),
            ("other account", .signedIn(snapshot(accountID: "account-2")), .signedOut, 3),
        ]
        for visible in [false, true] {
            for row in rows {
                let name = "\(row.name), visible surface: \(visible)"
                let first = FakeMobileAuthTransport()
                first.session = snapshot(accountID: "account-1")
                let refresh = FakeMobileAuthTransport()
                refresh.fetchSessionHandler = { row.discovery }
                let replacement = FakeMobileAuthTransport()
                var runtime: MobileRuntimeContext?
                var created = 0
                var pushLogoutCalls = 0
                weak var callbackCoordinator: MobileSessionCoordinator?
                let coordinator = makeCoordinator(
                    defaults: makeDefaults(),
                    // An unexpected intermediate transport must be observable
                    // as a failed count assertion, not crash the test host.
                    transports: [first, refresh, replacement, FakeMobileAuthTransport()],
                    onCreateTransport: { created += 1 },
                    onOpen: { runtime = $0 },
                    onPreparePushLogout: {
                        pushLogoutCalls += 1
                        guard let callbackCoordinator else {
                            return XCTFail("Missing push logout coordinator: \(name)")
                        }
                        do {
                            let response = try await callbackCoordinator.pushManagementRequest(
                                PushManagementRequest(
                                    method: "POST",
                                    path: "/api/push/logout",
                                    headers: [:],
                                    body: nil
                                )
                            )
                            XCTAssertEqual(response.statusCode, 200, name)
                        } catch {
                            XCTFail("Push logout failed: \(error), \(name)")
                        }
                    }
                )
                callbackCoordinator = coordinator
                coordinator.activateAccountSurface()
                _ = try await coordinator.bootstrap()
                _ = try coordinator.selectInstance(id: instance.id)
                let selected = try XCTUnwrap(runtime)
                if visible {
                    coordinator.activateAccountSurface()
                }

                let state = try await coordinator.refresh()

                XCTAssertEqual(state, row.state, name)
                XCTAssertEqual(pushLogoutCalls, 1, name)
                XCTAssertEqual(created, row.transports, name)
                XCTAssertFalse(coordinator.isSelectedRuntime(selected), name)
                XCTAssertEqual(coordinator.isAccountSurfaceActive, visible, name)
                if visible {
                    coordinator.leaveAccountSurface()
                    XCTAssertFalse(coordinator.isAccountSurfaceActive, name)
                }
                XCTAssertTrue(refresh.invalidated, name)
                if row.transports == 3 {
                    XCTAssertTrue(replacement.invalidated, name)
                }
            }
        }
    }

    func testRefreshRemovingSelectedOriginReleasesRuntimeButKeepsAccount()
        async throws
    {
        let defaults = makeDefaults()
        let firstTransport = FakeMobileAuthTransport()
        firstTransport.session = snapshot(accountID: "account-1")
        let removedTransport = FakeMobileAuthTransport()
        let replacement = MobileInstance(
            id: "instance-2",
            name: "Team Avibe",
            host: "team-app.avibe.bot",
            url: URL(string: "https://team-app.avibe.bot")!,
            relationship: "shared",
            ownerEmail: "owner@example.com",
            available: true
        )
        removedTransport.session = snapshot(
            accountID: "account-1",
            instances: [replacement]
        )
        var runtime: MobileRuntimeContext?
        var transitions = 0
        let coordinator = makeCoordinator(
            defaults: defaults,
            transports: [firstTransport, removedTransport],
            onOpen: { runtime = $0 },
            onPrepareAccountTransition: {
                transitions += 1
                return []
            }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)
        let selected = try XCTUnwrap(runtime)
        coordinator.activateAccountSurface()

        let state = try await coordinator.refresh()

        guard case .signedIn(_, let selectedInstanceID) = state else {
            return XCTFail("Expected a signed-in account after catalog removal")
        }
        XCTAssertNil(selectedInstanceID)
        XCTAssertFalse(coordinator.isCurrentRuntime(selected))
        XCTAssertEqual(coordinator.currentAccount?.id, "account-1")
        XCTAssertEqual(transitions, 1)
    }

    func testRefreshAfterSheetDismissReleasesHiddenTransportWhenOperationEnds()
        async throws
    {
        let defaults = makeDefaults()
        let firstTransport = FakeMobileAuthTransport()
        firstTransport.session = snapshot(accountID: "account-1")
        let refreshTransport = FakeMobileAuthTransport()
        refreshTransport.session = snapshot(accountID: "account-1")
        let refreshStarted = expectation(description: "refresh started")
        let allowRefresh = expectation(description: "refresh may finish")
        refreshTransport.fetchSessionHandler = {
            refreshStarted.fulfill()
            await self.fulfillment(of: [allowRefresh], timeout: 30)
            return .signedIn(self.snapshot(accountID: "account-1"))
        }
        let coordinator = makeCoordinator(
            defaults: defaults,
            transports: [firstTransport, refreshTransport]
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)
        coordinator.activateAccountSurface()

        let refresh = Task { try await coordinator.refresh() }
        await fulfillment(of: [refreshStarted], timeout: 30)
        coordinator.leaveAccountSurface()
        XCTAssertFalse(refreshTransport.invalidated)

        allowRefresh.fulfill()
        _ = try await refresh.value
        XCTAssertTrue(refreshTransport.invalidated)
    }

    func testReopeningSheetCancelsDeferredTransportRelease() async throws {
        let defaults = makeDefaults()
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(accountID: "account-1")
        let coordinator = makeCoordinator(
            defaults: defaults,
            transports: [transport]
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        let refreshStarted = expectation(description: "refresh started")
        let allowRefresh = expectation(description: "refresh may finish")
        transport.fetchSessionHandler = {
            refreshStarted.fulfill()
            await self.fulfillment(of: [allowRefresh], timeout: 30)
            return .signedIn(self.snapshot(accountID: "account-1"))
        }

        let refresh = Task { try await coordinator.refresh() }
        await fulfillment(of: [refreshStarted], timeout: 30)
        coordinator.leaveAccountSurface()
        coordinator.activateAccountSurface()
        allowRefresh.fulfill()
        _ = try await refresh.value
        XCTAssertFalse(transport.invalidated)
    }

    func testLogoutAllowsReplacementBootstrapDuringReset() async throws {
        let defaults = makeDefaults()
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(accountID: "account-1")
        let logoutStarted = expectation(description: "logout started")
        let allowLogout = expectation(description: "logout may finish")
        transport.logoutHandler = {
            logoutStarted.fulfill()
            await self.fulfillment(of: [allowLogout], timeout: 30)
        }
        let replacement = FakeMobileAuthTransport()
        let coordinator = makeCoordinator(
            defaults: defaults,
            transports: [transport, replacement]
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()

        let logout = Task { try await coordinator.logout() }
        await fulfillment(of: [logoutStarted], timeout: 30)
        let replacementState = try await coordinator.bootstrap()
        XCTAssertEqual(replacementState, .signedOut)

        allowLogout.fulfill()
        let logoutState = try await logout.value
        XCTAssertEqual(logoutState, .signedOut)
        XCTAssertTrue(transport.invalidated)
        XCTAssertFalse(replacement.invalidated)
        XCTAssertTrue(coordinator.isAccountSurfaceActive)
    }

    func testRefreshAccountChangeStillReleasesSelectedRuntime() async throws {
        let defaults = makeDefaults()
        let firstTransport = FakeMobileAuthTransport()
        firstTransport.session = snapshot(accountID: "account-1")
        let refreshTransport = FakeMobileAuthTransport()
        refreshTransport.session = snapshot(accountID: "account-2")
        var runtime: MobileRuntimeContext?
        var transitions = 0
        let coordinator = makeCoordinator(
            defaults: defaults,
            transports: [firstTransport, refreshTransport, FakeMobileAuthTransport()],
            onOpen: { runtime = $0 },
            onPrepareAccountTransition: {
                transitions += 1
                return []
            }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)
        let selected = try XCTUnwrap(runtime)
        coordinator.activateAccountSurface()

        let refreshState = try await coordinator.refresh()
        XCTAssertEqual(refreshState, .signedOut)
        XCTAssertFalse(coordinator.isCurrentRuntime(selected))
        XCTAssertEqual(transitions, 1)
    }

    func testHiddenTransportStopsBeforeRuntimeAndRestartsOnlyForAccountSurface()
        async throws
    {
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

    func testAccountOverARuntimeKeepsRefreshAndReleasesForLogout() async throws {
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
                // A sheet refresh reuses the selected runtime and activates
                // only the hidden account transport.
                coordinator.activateAccountSurface()
                _ = try await coordinator.refresh()
                XCTAssertEqual(
                    events,
                    ["transport.created"],
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
            XCTAssertEqual(coordinator.isAccountSurfaceActive, operation == "refresh")
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
        for supersedesWaiter in [false, true] {
            let defaults = makeDefaults()
            let registry = FakeNamedDataStoreRegistry()
            let store = AccountWebsiteDataStore(defaults: defaults, operations: registry.operations)
            let oldIdentifier = store.identifier
            let configuration = WKWebViewConfiguration()
            configuration.websiteDataStore = .nonPersistent()
            var retainedWebView: WKWebView? = WKWebView(frame: .zero, configuration: configuration)
            var secondRetainedWebView: WKWebView? = WKWebView(frame: .zero, configuration: configuration)
            let releaseBarrier = WebsiteDataStoreReleaseBarrier(webViews: [try XCTUnwrap(retainedWebView)])
            let secondReleaseBarrier = WebsiteDataStoreReleaseBarrier(webViews: [try XCTUnwrap(secondRetainedWebView)])
            let firstWait = supersedesWaiter
                ? expectation(description: "old transport released before barrier wait") : nil
            let original = FakeMobileAuthTransport()
            if let firstWait { original.onInvalidate = { firstWait.fulfill() } }
            var transitions = 0
            let coordinator = makeCoordinator(
                defaults: defaults, websiteDataStore: store,
                transports: [original, FakeMobileAuthTransport(), FakeMobileAuthTransport()],
                onPrepareAccountTransition: {
                    transitions += 1
                    // AppModel drains the barriers once, not again on reset.
                    return transitions == 1 ? [releaseBarrier, secondReleaseBarrier] : []
                }
            )
            coordinator.activateAccountSurface()
            var older: Task<Int, Error>?
            if let firstWait {
                older = Task { try await coordinator.requestOTP(email: "old@example.com") }
                await fulfillment(of: [firstWait], timeout: 30)
                XCTAssertEqual(store.identifier, oldIdentifier)
            }
            let clock = ContinuousClock()
            let startedAt = clock.now
            let state = try await coordinator.logout()
            let elapsed = startedAt.duration(to: clock.now)
            if let older {
                switch await older.result {
                case .success: XCTFail("Old rotation must lose reset authority")
                case .failure(let error):
                    XCTAssertEqual((error as? MobileAccountError)?.code, "stale_session")
                }
            }
            XCTAssertEqual(state, .signedOut)
            XCTAssertLessThan(elapsed, .milliseconds(1500))
            XCTAssertNotEqual(store.identifier, oldIdentifier)
            XCTAssertTrue(registry.identifiers.contains(oldIdentifier))
            XCTAssertTrue(registry.removalAttempts.isEmpty, "supersedes waiter: \(supersedesWaiter)")

            retainedWebView = nil
            secondRetainedWebView = nil
            let unreleased = await WebsiteDataStoreReleaseBarrier.waitForRelease(of: [releaseBarrier, secondReleaseBarrier])
            XCTAssertTrue(unreleased.isEmpty)
            await store.sweepOrphans()
            XCTAssertFalse(registry.identifiers.contains(oldIdentifier))
            XCTAssertEqual(registry.removalAttempts, [oldIdentifier])
        }
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

    func testLogoutRevokesAndDetachesBeforeAwaitingPushCleanup() async throws {
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(accountID: "account-1")
        let cleanupStarted = expectation(description: "push cleanup started")
        let releaseCleanup = expectation(description: "release push cleanup")
        var transitionPreparations = 0
        var transitionEvents: [String] = []
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [
                transport,
                FakeMobileAuthTransport(),
                FakeMobileAuthTransport(),
            ],
            onPrepareAccountTransition: {
                transitionPreparations += 1
                transitionEvents.append("runtime_revoked")
                return []
            },
            onDetachActiveOwner: {
                transitionEvents.append("detached")
                return true
            },
            onPreparePushLogout: {
                transitionEvents.append("push_cleanup_started")
                cleanupStarted.fulfill()
                await self.fulfillment(of: [releaseCleanup], timeout: 30)
            }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)

        let logout = Task { try await coordinator.logout() }
        await fulfillment(of: [cleanupStarted], timeout: 30)
        XCTAssertEqual(transitionPreparations, 1)
        XCTAssertEqual(
            transitionEvents,
            [
                "runtime_revoked",
                "detached",
                "push_cleanup_started",
            ]
        )
        releaseCleanup.fulfill()
        let logoutRoute = try await logout.value
        XCTAssertEqual(logoutRoute, .signedOut)
        XCTAssertEqual(transitionPreparations, 1)
    }

    // Route B: schedule persistence cannot retain an account's authorization.
    // E1/E5 still require immediate detach notification before awaited cleanup;
    // the file store owns its pending durable revocation, not this transaction.
    func testDetachFailureStillCompletesLogoutAfterImmediateRevocation() async throws {
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(accountID: "account-1")
        var transitionPreparations = 0
        var pushCleanupCalls = 0
        let cleanupStarted = expectation(description: "push cleanup after failed detach")
        let releaseCleanup = expectation(description: "release push cleanup after failed detach")
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [transport, FakeMobileAuthTransport(), FakeMobileAuthTransport()],
            onPrepareAccountTransition: {
                transitionPreparations += 1
                return []
            },
            onDetachActiveOwner: { false },
            onPreparePushLogout: {
                pushCleanupCalls += 1
                cleanupStarted.fulfill()
                await self.fulfillment(of: [releaseCleanup], timeout: 30)
            }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        _ = try coordinator.selectInstance(id: instance.id)

        let logout = Task { try await coordinator.logout() }
        await fulfillment(of: [cleanupStarted], timeout: 30)
        XCTAssertEqual(transitionPreparations, 1)
        XCTAssertThrowsError(try coordinator.selectInstance(id: instance.id))
        let duringCleanup = try await coordinator.bootstrap()
        XCTAssertEqual(duringCleanup, .signedOut)
        releaseCleanup.fulfill()
        let state = try await logout.value
        XCTAssertEqual(state, .signedOut)
        XCTAssertEqual(transitionPreparations, 1)
        XCTAssertEqual(pushCleanupCalls, 1)
        XCTAssertNil(coordinator.currentAccount)
        XCTAssertThrowsError(try coordinator.selectInstance(id: instance.id))
    }

    // I3 review E1: a new authenticated account is not exposed until owner sync commits.
    func testAuthenticatedAccountWaitsWhenScheduleOwnerCommitFails() async throws {
        let transport = FakeMobileAuthTransport()
        transport.session = snapshot(accountID: "account-2")
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [transport],
            onAuthenticatedAccount: { _ in false }
        )
        coordinator.activateAccountSurface()

        do {
            _ = try await coordinator.bootstrap()
            XCTFail("Bootstrap should stop when the schedule owner cannot be committed.")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error.code, "local_state_unavailable")
        }
        XCTAssertNil(coordinator.currentAccount)
    }

    // I3 review D2: server-signed-out bootstrap is not an explicit owner detach.
    func testColdStartSignedOutPreservesThePersistedScheduleOwner() async throws {
        let defaults = makeDefaults()
        let signedIn = FakeMobileAuthTransport()
        signedIn.session = snapshot(accountID: "account-1")
        let first = makeCoordinator(
            defaults: defaults,
            transports: [signedIn]
        )
        first.activateAccountSurface()
        _ = try await first.bootstrap()
        _ = try first.selectInstance(id: instance.id)

        let signedOut = FakeMobileAuthTransport()
        signedOut.session = nil
        var detachedAccounts: [String] = []
        let relaunched = makeCoordinator(
            defaults: defaults,
            transports: [signedOut, FakeMobileAuthTransport()],
            onDetachActiveOwner: {
                detachedAccounts.append("detached")
                return true
            }
        )
        relaunched.activateAccountSurface()

        let relaunchedRoute = try await relaunched.bootstrap()
        XCTAssertEqual(relaunchedRoute, .signedOut)
        XCTAssertEqual(detachedAccounts, [])
    }

    // I3 simplification 2: stale selection cannot prove the persisted schedule owner changed.
    func testColdStartStaleSelectionUsesAuthenticatedAccountOwnerFence() async throws {
        let defaults = makeDefaults()
        let staleTransport = FakeMobileAuthTransport()
        staleTransport.session = snapshot(accountID: "account-2")
        let first = makeCoordinator(
            defaults: defaults,
            transports: [staleTransport]
        )
        first.activateAccountSurface()
        _ = try await first.bootstrap()
        _ = try first.selectInstance(id: instance.id)

        let matchingOwnerTransport = FakeMobileAuthTransport()
        matchingOwnerTransport.session = snapshot(accountID: "account-1")
        let matchingOwnerBootstrapTransport = FakeMobileAuthTransport()
        matchingOwnerBootstrapTransport.session = snapshot(accountID: "account-1")
        var explicitDetachCount = 0
        var authenticatedAccounts: [String] = []
        let relaunched = makeCoordinator(
            defaults: defaults,
            transports: [matchingOwnerTransport, matchingOwnerBootstrapTransport],
            onDetachActiveOwner: {
                explicitDetachCount += 1
                return true
            },
            onDetachActiveOwnerForAuthenticatedAccount: { accountID in
                authenticatedAccounts.append(accountID)
                return true
            }
        )
        relaunched.activateAccountSurface()

        let relaunchedRoute = try await relaunched.bootstrap()
        XCTAssertEqual(relaunchedRoute, .signedOut)
        XCTAssertNil(relaunched.currentAccount)
        XCTAssertEqual(explicitDetachCount, 0)
        XCTAssertEqual(authenticatedAccounts, ["account-1"])
    }

    // I3 simplification 2: a proven different account detaches the persisted owner.
    func testColdStartDifferentAccountDetachesThePersistedDepartingAccount() async throws {
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

        let replacementTransport = FakeMobileAuthTransport()
        replacementTransport.session = snapshot(accountID: "account-2")
        let replacementBootstrapTransport = FakeMobileAuthTransport()
        replacementBootstrapTransport.session = snapshot(accountID: "account-2")
        var explicitDetachCount = 0
        var authenticatedAccounts: [String] = []
        let relaunched = makeCoordinator(
            defaults: defaults,
            transports: [replacementTransport, replacementBootstrapTransport],
            onDetachActiveOwner: {
                explicitDetachCount += 1
                return true
            },
            onDetachActiveOwnerForAuthenticatedAccount: { accountID in
                authenticatedAccounts.append(accountID)
                return true
            }
        )
        relaunched.activateAccountSurface()

        let relaunchedRoute = try await relaunched.bootstrap()
        XCTAssertEqual(relaunchedRoute, .signedOut)
        XCTAssertNil(relaunched.currentAccount)
        XCTAssertEqual(explicitDetachCount, 0)
        XCTAssertEqual(authenticatedAccounts, ["account-2"])
    }

    // I3 review E4: 2FA is not an inferred logout and preserves the owner.
    func testColdStartSecondFactorPreservesThePersistedScheduleOwner() async throws {
        let defaults = makeDefaults()
        let signedIn = FakeMobileAuthTransport()
        signedIn.session = snapshot(accountID: "account-1")
        let first = makeCoordinator(defaults: defaults, transports: [signedIn])
        first.activateAccountSurface()
        _ = try await first.bootstrap()
        _ = try first.selectInstance(id: instance.id)

        let owed = FakeMobileAuthTransport()
        owed.secondFactor = MobileSecondFactorRequirement(
            email: "account-1@example.com",
            gate: .challenge,
            methods: [.totp]
        )
        var detachCount = 0
        let relaunched = makeCoordinator(
            defaults: defaults,
            transports: [owed],
            onDetachActiveOwner: {
                detachCount += 1
                return true
            }
        )
        relaunched.activateAccountSurface()

        let state = try await relaunched.bootstrap()
        guard case .twoFactorRequired = state else {
            return XCTFail("Expected two-factor state")
        }
        XCTAssertEqual(detachCount, 0)
    }

    // I3 review E5: explicit logout detaches the ledger owner without snapshot or selection.
    func testExplicitLogoutDetachesEvenWithoutSnapshotOrSelection() async throws {
        let cleanupStarted = expectation(description: "push cleanup started")
        let releaseCleanup = expectation(description: "release push cleanup")
        var detachCount = 0
        let coordinator = makeCoordinator(
            defaults: makeDefaults(),
            transports: [FakeMobileAuthTransport(), FakeMobileAuthTransport()],
            onDetachActiveOwner: {
                detachCount += 1
                return true
            },
            onPreparePushLogout: {
                cleanupStarted.fulfill()
                await self.fulfillment(of: [releaseCleanup], timeout: 30)
            }
        )
        coordinator.activateAccountSurface()

        let logout = Task { try await coordinator.logout() }
        await fulfillment(of: [cleanupStarted], timeout: 30)
        XCTAssertEqual(detachCount, 1)
        releaseCleanup.fulfill()
        let state = try await logout.value
        XCTAssertEqual(state, .signedOut)
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
        webAuthenticator: MobileWebAuthenticating? = nil,
        appleAuthorizer: MobileAppleAuthorizing? = nil,
        onCreateTransport: @escaping () -> Void = {},
        onOpen: @escaping (MobileRuntimeContext) -> Void = { _ in },
        onPrepareAccountTransition: @escaping () -> [
            WebsiteDataStoreReleaseBarrier
        ] = { [] },
        onDetachActiveOwner: @escaping @MainActor () -> Bool = { true },
        onDetachActiveOwnerForAuthenticatedAccount: @escaping @MainActor (String) -> Bool = { _ in true },
        onAuthenticatedAccount: @escaping @MainActor (String) -> Bool = { _ in true },
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
            webAuthenticator: webAuthenticator ?? FakeWebAuthenticator(),
            appleAuthorizer: appleAuthorizer ?? FakeAppleAuthorizer(),
            onOpenRuntime: onOpen,
            onPrepareAccountTransition: onPrepareAccountTransition,
            onDetachActiveOwner: onDetachActiveOwner,
            onDetachActiveOwnerForAuthenticatedAccount: onDetachActiveOwnerForAuthenticatedAccount,
            onAuthenticatedAccount: onAuthenticatedAccount,
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

    /// Waits for the page's own state. The web content process and the
    /// document are waited for first, on WebKit's bound (`WebKitReadiness`),
    /// so the five seconds left are the page's alone.
    private func waitForJavaScriptValue(
        in webView: WKWebView,
        file: StaticString = #filePath,
        line: UInt = #line,
        script: () -> String
    ) async throws -> Bool {
        try await WebKitReadiness.waitForDocument(
            in: webView,
            file: file,
            line: line
        )
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
    var listIdentifiersHandler: (() async -> Void)?

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
                await listIdentifiersHandler?()
                return identifiers
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

/// A fake transport call that stays in flight until `release()`.
@MainActor
private final class HeldCall {
    private var held = false
    private var released = false
    private var continuation: CheckedContinuation<Void, Never>?

    func wait() async {
        held = true
        guard !released else {
            return
        }
        await withCheckedContinuation { continuation = $0 }
    }

    func waitUntilHeld() async throws {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(10))
        while !held {
            guard clock.now < deadline else {
                throw MobileAccountError.network
            }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
    }

    func release() {
        released = true
        continuation?.resume()
        continuation = nil
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
    var fetchSessionError: Error?
    var fetchSessionHandler: (() async throws -> MobileSessionDiscovery?)?
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
    var providerStartHandler: (
        MobileSignInProvider, String, String
    ) async throws -> MobileProviderStart = { provider, _, _ in
        provider == .apple
            ? .apple(transactionID: "transaction-1", nonce: "server-nonce")
            : .browser(
                transactionID: "transaction-1",
                url: URL(string: "http://127.0.0.1:43127/api/mobile/auth/browser?launch=LLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLLA")!
            )
    }
    private(set) var providerStarts: [(provider: MobileSignInProvider, challenge: String, state: String)] = []
    var providerExchangeHandler: (
        String, String, String
    ) async throws -> MobileProviderExchange = { _, _, _ in
        throw MobileAccountError.invalidOrExpired
    }
    private(set) var providerExchanges: [(transactionID: String, code: String, verifier: String)] = []
    var appleExchangeHandler: (
        String, String, String, String
    ) async throws -> MobileProviderExchange = { _, _, _, _ in
        throw MobileAccountError.invalidOrExpired
    }
    private(set) var appleExchanges: [
        (transactionID: String, identityToken: String, authorizationCode: String, verifier: String)
    ] = []

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
        if let fetchSessionHandler {
            return try await fetchSessionHandler()
        }
        if let fetchSessionError {
            throw fetchSessionError
        }
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

    var advertised: [MobileSignInProvider]? = []
    var advertisedDelay: UInt64 = 0

    func advertisedProviders() async -> [MobileSignInProvider]? {
        if advertisedDelay > 0 {
            try? await Task.sleep(nanoseconds: advertisedDelay)
        }
        return advertised
    }

    func providerStart(
        provider: MobileSignInProvider,
        challenge: String,
        state: String
    ) async throws -> MobileProviderStart {
        providerStarts.append((provider, challenge, state))
        return try await providerStartHandler(provider, challenge, state)
    }

    func providerExchange(
        transactionID: String,
        code: String,
        verifier: String
    ) async throws -> MobileProviderExchange {
        providerExchanges.append((transactionID, code, verifier))
        return try await providerExchangeHandler(transactionID, code, verifier)
    }

    func appleExchange(
        transactionID: String,
        identityToken: String,
        authorizationCode: String,
        verifier: String
    ) async throws -> MobileProviderExchange {
        appleExchanges.append((transactionID, identityToken, authorizationCode, verifier))
        return try await appleExchangeHandler(transactionID, identityToken, authorizationCode, verifier)
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

@MainActor
private final class QuietPushNotificationCenter: PushNotificationCentering {
    func deliveredNotifications() async -> [PushNotificationCenterEntry] {
        []
    }

    func pendingNotificationRequests() async -> [PushNotificationCenterEntry] {
        []
    }

    func removeDeliveredNotifications(withIdentifiers identifiers: [String]) {}

    func removePendingNotificationRequests(withIdentifiers identifiers: [String]) {}
}

@MainActor
private final class FakeWebAuthenticator: MobileWebAuthenticating {
    var handler: (URL, String, String) async throws -> URL = { _, _, _ in
        throw MobileAccountError.providerCancelled
    }
    var onCancel: () -> Void = {}
    private(set) var opened: [URL] = []
    private(set) var cancels = 0

    func authenticate(
        url: URL,
        callbackHost: String,
        callbackPath: String,
        presentationAnchor: ASPresentationAnchor
    ) async throws -> URL {
        opened.append(url)
        return try await handler(url, callbackHost, callbackPath)
    }

    func cancel() {
        cancels += 1
        onCancel()
    }
}

@MainActor
private final class FakeAppleAuthorizer: MobileAppleAuthorizing {
    var handler: (String, String) async throws -> MobileAppleCredential = { _, _ in
        throw MobileAccountError.providerCancelled
    }
    private(set) var cancels = 0

    func authorize(
        nonce: String,
        state: String,
        presentationAnchor: ASPresentationAnchor
    ) async throws -> MobileAppleCredential {
        try await handler(nonce, state)
    }

    func cancel() {
        cancels += 1
    }
}
