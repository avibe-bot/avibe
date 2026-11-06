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

    func testBundledPageFillsItsContainerAndStillTracksTheKeyboard() throws {
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
                    && $0.secondItem === controller.view.keyboardLayoutGuide
                    && $0.secondAttribute == .top
            }
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
        let enteredCode = try await replaceText(
            "123456",
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
              const target = document.querySelector("button.shadow-mint-card-sm");
              if (!viewport || !target || target.disabled) return false;
              const viewportRect = viewport.getBoundingClientRect();
              const targetRect = target.getBoundingClientRect();
              return viewport.scrollTop > 0
                && targetRect.top >= viewportRect.top - 1
                && targetRect.bottom <= viewportRect.bottom + 1;
            })()
            """
        }
        XCTAssertTrue(verifyVisible)

        _ = try await webView.evaluateJavaScript(
            """
            document.querySelector("button.shadow-mint-card-sm")?.click();
            true;
            """
        )
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

    func testMobileBridgeMethodSurfaceIsClosed() {
        XCTAssertEqual(
            Set(MobileBridgeMethod.allCases.map(\.rawValue)),
            [
                "session.bootstrap",
                "auth.requestOtp",
                "auth.verifyOtp",
                "session.refresh",
                "session.selectInstance",
                "session.logout",
                "app.openExternal",
            ]
        )
        XCTAssertThrowsError(
            try MobileBridgeRequest.parse(["method": "auth.fetch"])
        )
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

    func testRuntimeBridgeAddsOnlyTheAccountPresentationMethod() throws {
        let request = try XCTUnwrap(
            NativeBridgeRequest.parse(["method": "app.showAccount"]).success
        )
        XCTAssertEqual(request.method, .appShowAccount)
        XCTAssertEqual(
            Set(NativeBridgeMethod.allCases.map(\.rawValue)),
            [
                "app.info",
                "app.openExternal",
                "app.showAccount",
                "haptics.impact",
                "haptics.notify",
                "haptics.selection",
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
        onCreateTransport: @escaping () -> Void = {},
        onOpen: @escaping (MobileRuntimeContext) -> Void = { _ in },
        onPrepareAccountTransition: @escaping () -> [
            WebsiteDataStoreReleaseBarrier
        ] = { [] }
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
            onOpenRuntime: onOpen,
            onPrepareAccountTransition: onPrepareAccountTransition
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
                identifiers.remove(identifier)
                return nil
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
    var logoutError: Error?
    var logoutHandler: () async throws -> Void = {}
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
    ) async throws -> MobileSessionSnapshot {
        if let session {
            return session
        }
        return try await verifyOTPHandler(email, code)
    }

    func fetchSession() async throws -> MobileSessionSnapshot? {
        session
    }

    func logout() async throws {
        try await logoutHandler()
        if let logoutError {
            throw logoutError
        }
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

private extension Result where Success == NativeBridgeRequest, Failure == NativeBridgeError {
    var success: NativeBridgeRequest? {
        guard case .success(let value) = self else {
            return nil
        }
        return value
    }
}
