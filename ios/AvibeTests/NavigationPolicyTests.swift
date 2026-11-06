import SwiftUI
import XCTest
import WebKit
@testable import Avibe

final class NavigationPolicyTests: XCTestCase {
    private let instance = WebOrigin.parseConfigured("https://alice-app.avibe.bot")!

    func testConfiguredOriginRequiresHTTPSAndCanonicalizesToOrigin() {
        XCTAssertEqual(
            WebOrigin.parseConfigured("  https://Example.COM:443/chat/ses_1?source=phone  "),
            WebOrigin.parseConfigured("https://example.com/")
        )
        XCTAssertEqual(
            WebOrigin.parseConfigured("https://Example.COM:8443/chat")?.serialized,
            "https://example.com:8443"
        )
        XCTAssertNil(WebOrigin.parseConfigured("http://example.com"))
        XCTAssertNil(WebOrigin.parseConfigured("example.com"))
        XCTAssertNil(WebOrigin.parseConfigured("https://user:password@example.com"))
    }

    func testConfiguredInstanceOriginStaysInWebView() {
        let policy = NavigationPolicy(instanceOrigins: [instance])

        XCTAssertEqual(
            policy.destination(for: URL(string: "https://alice-app.avibe.bot/chat/ses_1")!),
            .instance
        )
    }

    func testAuthenticationOriginsStayInWebViewWithoutBroadSubdomainMatching() {
        let policy = NavigationPolicy(instanceOrigins: [instance])

        XCTAssertEqual(policy.destination(for: URL(string: "https://avibe.bot/oauth/authorize")!), .authentication)
        XCTAssertEqual(
            policy.destination(for: URL(string: "https://mctppyrqmrjotgkrwdse.supabase.co/auth/v1/authorize")!),
            .authentication
        )
        XCTAssertEqual(policy.destination(for: URL(string: "https://github.com/login/oauth/authorize")!), .authentication)
        XCTAssertEqual(policy.destination(for: URL(string: "https://evil.avibe.bot/login")!), .external)
    }

    func testPrimaryExternalNavigationUsesSafariWhileChildNavigationStaysInWebView() {
        let policy = NavigationPolicy(instanceOrigins: [instance])
        let sandbox = URL(string: "https://sandbox.avibe.bot/v/pinned/index.html")!
        let external = URL(string: "https://example.com/docs")!

        XCTAssertEqual(policy.destination(for: sandbox), .external)
        XCTAssertEqual(policy.disposition(for: sandbox, context: .primaryMainFrame), .external)
        XCTAssertEqual(policy.disposition(for: sandbox, context: .popupMainFrame), .allow)
        XCTAssertEqual(policy.disposition(for: external, context: .primaryMainFrame), .external)
        XCTAssertEqual(policy.disposition(for: external, context: .popupMainFrame), .allow)
    }

    func testPrimaryBackForwardNavigationStaysInsideTheSelectedInstance() {
        let policy = NavigationPolicy(instanceOrigins: [instance])

        XCTAssertTrue(
            policy.allowsBackForwardNavigation(
                to: URL(string: "https://alice-app.avibe.bot/chat/ses_1")!,
                context: .primaryMainFrame
            )
        )
        for url in [
            URL(string: "https://avibe.bot/oauth/authorize")!,
            URL(string: "https://github.com/login/oauth/authorize")!,
            URL(string: "https://example.com/docs")!,
        ] {
            XCTAssertFalse(
                policy.allowsBackForwardNavigation(
                    to: url,
                    context: .primaryMainFrame
                )
            )
            XCTAssertTrue(
                policy.allowsBackForwardNavigation(
                    to: url,
                    context: .popupMainFrame
                )
            )
        }
    }

    func testRuntimeAccountAvatarUsesTheAccountLocalPartInitial() {
        XCTAssertEqual(
            runtimeAccountInitial(for: "max@example.com"),
            "M"
        )
        XCTAssertEqual(
            runtimeAccountInitial(for: "  alex@example.com"),
            "A"
        )
        XCTAssertEqual(runtimeAccountInitial(for: nil), "A")
        XCTAssertEqual(runtimeAccountInitial(for: " @example.com"), "A")
    }

    @MainActor
    func testLegacyRuntimeToolbarMatchesTheMobileBrandHeaderHeight() throws {
        for colorScheme in [ColorScheme.light, .dark] {
            let toolbarController = UIHostingController(
                rootView: LegacyRuntimeToolbar(
                    instanceName: "Personal Avibe",
                    accountEmail: "max@example.com",
                    showAccount: {},
                    showInstancePicker: {}
                )
                .environment(\.colorScheme, colorScheme)
                .environment(\.dynamicTypeSize, .large)
            )
            let toolbarSize = toolbarController.sizeThatFits(
                in: CGSize(width: 390, height: 180)
            )
            XCTAssertEqual(toolbarSize.height, 56, accuracy: 0.5)

            let controller = UIHostingController(
                rootView: LegacyRuntimeToolbarScreenshotFixture(
                    instanceName: "Personal Avibe",
                    accountEmail: "max@example.com"
                )
                .environment(\.colorScheme, colorScheme)
                .environment(\.dynamicTypeSize, .large)
            )
            controller.loadViewIfNeeded()
            controller.view.frame = CGRect(
                x: 0,
                y: 0,
                width: 390,
                height: 180
            )
            controller.view.layoutIfNeeded()

            let image = snapshot(of: controller.view)

            let attachment = XCTAttachment(
                data: try XCTUnwrap(image.pngData()),
                uniformTypeIdentifier: "public.png"
            )
            attachment.name = "legacy-runtime-fallback-\(colorScheme)"
            attachment.lifetime = .keepAlways
            add(attachment)
        }
    }

    func testSubframesUseWebKitDefaultsExceptForExternalApplicationSchemes() {
        let policy = NavigationPolicy(instanceOrigins: [instance])

        for url in [
            URL(string: "about:srcdoc")!,
            URL(string: "about:blank")!,
            URL(string: "blob:https://alice-app.avibe.bot/preview-id")!,
            URL(string: "data:text/html,hello")!,
            URL(string: "https://example.com/embed")!,
        ] {
            XCTAssertEqual(policy.subframeDisposition(for: url), .allow)
        }

        for url in [
            URL(string: "tel:+15555550123")!,
            URL(string: "mailto:support@avibe.bot")!,
            URL(string: "avibe://settings")!,
        ] {
            XCTAssertEqual(policy.subframeDisposition(for: url), .cancel)
        }
    }

    func testBrowserInternalURLsUseWebKitDefaultsAtTopLevel() {
        let policy = NavigationPolicy(instanceOrigins: [instance])

        for url in [
            URL(string: "about:blank")!,
            URL(string: "about:srcdoc")!,
            URL(string: "blob:https://alice-app.avibe.bot/export-id")!,
            URL(string: "data:text/plain,hello")!,
            URL(string: "javascript:void(0)")!,
            URL(fileURLWithPath: "/tmp/example.txt"),
        ] {
            XCTAssertEqual(policy.destination(for: url), .browserInternal)
            XCTAssertEqual(policy.disposition(for: url, context: .primaryMainFrame), .allow)
            XCTAssertEqual(policy.disposition(for: url, context: .popupMainFrame), .allow)
        }
    }

    func testGoogleAuthorizationOriginIsIntercepted() {
        let policy = NavigationPolicy(instanceOrigins: [instance])

        XCTAssertEqual(
            policy.destination(for: URL(string: "https://accounts.google.com/o/oauth2/v2/auth")!),
            .googleAuthentication
        )
    }

    func testOtherWebOriginsOpenExternallyAndApplicationSchemesLeaveWebKit() {
        let policy = NavigationPolicy(instanceOrigins: [instance])

        XCTAssertEqual(policy.destination(for: URL(string: "https://example.com/docs")!), .external)
        XCTAssertEqual(policy.destination(for: URL(string: "http://example.com/docs")!), .external)
        XCTAssertEqual(policy.destination(for: URL(string: "avibe://settings")!), .browserInternal)
        XCTAssertEqual(
            policy.disposition(
                for: URL(string: "mailto:support@avibe.bot")!,
                context: .primaryMainFrame
            ),
            .externalApplication
        )
    }

    func testInternalSchemesAreNotTreatedAsExternalApplications() {
        let policy = NavigationPolicy(instanceOrigins: [instance])

        for url in [
            URL(string: "about:blank")!,
            URL(string: "blob:https://alice-app.avibe.bot/export-id")!,
            URL(string: "data:text/plain,hello")!,
            URL(string: "javascript:void(0)")!,
            URL(fileURLWithPath: "/tmp/example.txt"),
        ] {
            XCTAssertNotEqual(
                policy.disposition(for: url, context: .primaryMainFrame),
                .externalApplication
            )
        }
    }

    func testDownloadsAreLimitedToTheConfiguredInstanceOrigin() {
        let policy = NavigationPolicy(instanceOrigins: [instance])

        XCTAssertEqual(
            policy.downloadDisposition(
                for: URL(string: "https://alice-app.avibe.bot/api/files/content?download=1")!,
                sourceOrigin: instance,
                trustedMainFrame: true
            ),
            .download
        )
        XCTAssertEqual(
            policy.downloadDisposition(
                for: URL(string: "https://sandbox.avibe.bot/v/pinned/index.html")!,
                sourceOrigin: instance,
                trustedMainFrame: true
            ),
            .externalBrowser
        )
        XCTAssertEqual(
            policy.downloadDisposition(
                for: URL(string: "https://example.com/archive.zip")!,
                sourceOrigin: instance,
                trustedMainFrame: true
            ),
            .externalBrowser
        )
    }

    func testNewWindowsUseChildWebViewsForEveryWebOrigin() {
        let policy = NavigationPolicy(instanceOrigins: [instance])

        for url in [
            URL(string: "https://alice-app.avibe.bot/show/ses_1/")!,
            URL(string: "https://github.com/login/oauth/authorize")!,
            URL(string: "https://sandbox.avibe.bot/v/pinned/index.html")!,
            URL(string: "https://example.com/docs")!,
        ] {
            XCTAssertEqual(policy.newWindowDisposition(for: url), .childWebView)
        }
    }

    func testNewWindowsKeepBrowserInternalURLsAndRouteClosedListExceptions() {
        let policy = NavigationPolicy(instanceOrigins: [instance])

        XCTAssertEqual(
            policy.newWindowDisposition(for: URL(string: "about:blank")!),
            .childWebView
        )
        XCTAssertEqual(
            policy.newWindowDisposition(for: URL(string: "mailto:support@avibe.bot")!),
            .externalApplication
        )
        XCTAssertEqual(
            policy.newWindowDisposition(
                for: URL(string: "https://accounts.google.com/o/oauth2/v2/auth")!
            ),
            .googleAuthentication
        )
    }

    func testChildBrowsingContextsCanOpenFurtherChildBrowsingContexts() {
        let policy = NavigationPolicy(instanceOrigins: [instance])

        for url in [
            URL(string: "https://alice-app.avibe.bot/show/ses_1/")!,
            URL(string: "https://github.com/login/oauth/authorize")!,
            URL(string: "about:blank")!,
            URL(string: "https://example.com/docs")!,
        ] {
            XCTAssertEqual(policy.newWindowDisposition(for: url), .childWebView)
        }
    }

    func testBlobDownloadsRequireTheEmbeddedInstanceOrigin() {
        let policy = NavigationPolicy(instanceOrigins: [instance])

        XCTAssertEqual(
            policy.downloadDisposition(
                for: URL(string: "blob:https://alice-app.avibe.bot/export-id")!,
                sourceOrigin: nil,
                trustedMainFrame: true
            ),
            .download
        )
        XCTAssertEqual(
            policy.downloadDisposition(
                for: URL(string: "blob:https://example.com/export-id")!,
                sourceOrigin: instance,
                trustedMainFrame: true
            ),
            .blocked
        )
    }

    func testDataDownloadsRequireAnInstanceSourceFrame() {
        let policy = NavigationPolicy(instanceOrigins: [instance])
        let authenticationOrigin = WebOrigin.parseConfigured("https://avibe.bot")!
        let dataURL = URL(string: "data:text/plain,hello")!

        XCTAssertEqual(
            policy.downloadDisposition(
                for: dataURL,
                sourceOrigin: instance,
                trustedMainFrame: true
            ),
            .download
        )
        XCTAssertEqual(
            policy.downloadDisposition(
                for: dataURL,
                sourceOrigin: authenticationOrigin,
                trustedMainFrame: true
            ),
            .blocked
        )
        XCTAssertEqual(
            policy.downloadDisposition(
                for: dataURL,
                sourceOrigin: nil,
                trustedMainFrame: true
            ),
            .blocked
        )
    }

    func testDownloadRequiresATrustedContainingMainFrame() {
        let policy = NavigationPolicy(instanceOrigins: [instance])

        XCTAssertEqual(
            policy.downloadDisposition(
                for: URL(string: "https://alice-app.avibe.bot/api/files/content?download=1")!,
                sourceOrigin: instance,
                trustedMainFrame: false
            ),
            .blocked
        )
        XCTAssertEqual(
            policy.downloadDisposition(
                for: URL(string: "blob:https://alice-app.avibe.bot/export-id")!,
                sourceOrigin: instance,
                trustedMainFrame: false
            ),
            .blocked
        )
        XCTAssertEqual(
            policy.downloadDisposition(
                for: URL(string: "data:text/plain,hello")!,
                sourceOrigin: instance,
                trustedMainFrame: false
            ),
            .blocked
        )
    }

    func testNativeBridgePolicyRequiresTheInstanceMainFrame() {
        let policy = NativeBridgePolicy(instanceOrigin: instance)
        let external = WebOrigin.parseConfigured("https://example.com")!

        XCTAssertTrue(policy.allows(isMainFrame: true, origin: instance))
        XCTAssertFalse(policy.allows(isMainFrame: false, origin: instance))
        XCTAssertFalse(policy.allows(isMainFrame: true, origin: external))
        XCTAssertFalse(policy.allows(isMainFrame: true, origin: nil))
    }

    func testNativeBridgeRequestParserKeepsTheMethodSurfaceClosed() throws {
        let request = try NativeBridgeRequest.parse(
            [
                "method": "haptics.impact",
                "params": ["style": "medium"],
            ]
        ).get()

        XCTAssertEqual(request.method, .hapticsImpact)
        XCTAssertEqual(request.params["style"] as? String, "medium")
        XCTAssertEqual(
            try NativeBridgeRequest.parse(
                [
                    "method": "viewport.setOuterScrollLocked",
                    "params": ["locked": true],
                ]
            ).get().method,
            .viewportSetOuterScrollLocked
        )
        XCTAssertEqual(
            try NativeBridgeRequest.parse(
                [
                    "method": "app.setAccountActionAvailable",
                    "params": ["available": true],
                ]
            ).get().method,
            .appSetAccountActionAvailable
        )
        XCTAssertEqual(
            NativeBridgeRequest.parse(["method": "app.instances"]).failure,
            .unsupported
        )
        XCTAssertEqual(
            NativeBridgeRequest.parse(["method": "app.info", "params": "bad"]).failure,
            .invalidRequest
        )
    }

    func testNativeExternalURLRequiresAnAbsoluteSupportedTarget() {
        XCTAssertEqual(
            NativeExternalURL.parse(" https://example.com/path?q=1 ")?.absoluteString,
            "https://example.com/path?q=1"
        )
        XCTAssertEqual(
            NativeExternalURL.parse("mailto:hello@example.com")?.absoluteString,
            "mailto:hello@example.com"
        )
        XCTAssertEqual(
            NativeExternalURL.parse("tel:+15551234567")?.absoluteString,
            "tel:+15551234567"
        )

        XCTAssertNil(NativeExternalURL.parse("http:relative"))
        XCTAssertNil(NativeExternalURL.parse("https:///missing-host"))
        XCTAssertNil(NativeExternalURL.parse("mailto:"))
        XCTAssertNil(NativeExternalURL.parse("tel:"))
        XCTAssertNil(NativeExternalURL.parse("javascript:alert(1)"))
    }

    func testNativeBridgeConfigurationInjectsOnlyAtMainFrameDocumentStart() {
        let metadata = NativeShellMetadata(
            shellVersion: "1.2.3 (45)",
            userAgentVersion: "1.2.3"
        )
        let configuration = NativeBridgeConfiguration.make(
            instanceOrigin: instance,
            metadata: metadata,
            setOuterScrollLocked: { _, _ in false }
        )

        XCTAssertEqual(configuration.applicationNameForUserAgent, "AvibeiOS/1.2.3")
        XCTAssertEqual(configuration.userContentController.userScripts.count, 1)
        let script = configuration.userContentController.userScripts[0]
        XCTAssertEqual(script.injectionTime, .atDocumentStart)
        XCTAssertTrue(script.isForMainFrameOnly)
        XCTAssertTrue(script.source.contains(#""instanceOrigin""#))
        XCTAssertTrue(script.source.contains(instance.host))
        XCTAssertTrue(script.source.contains(#""contract":1"#))
        XCTAssertTrue(script.source.contains(#""keyboardResizesViewport":1"#))
    }

    @MainActor
    func testWebViewFactoryUsesTheSameKeyboardLayoutForPrimaryAndChild() {
        let factory = WebViewFactory(
            instanceOrigin: instance,
            websiteDataStore: .nonPersistent()
        )
        let delegate = WebViewDelegateProbe()
        let primary = factory.makeWebView(delegate: delegate)
        let child = factory.makeWebView(
            configuration: primary.configuration,
            delegate: delegate
        )
        let primaryContainer = UIView()
        let childContainer = UIView()

        let primaryConstraints = factory.install(primary, in: primaryContainer)
        let childConstraints = factory.install(child, in: childContainer)

        XCTAssertTrue(factory.isManaging(primary))
        XCTAssertTrue(factory.isManaging(child))
        assertKeyboardBottomConstraint(
            primaryConstraints,
            webView: primary,
            containerView: primaryContainer
        )
        assertKeyboardBottomConstraint(
            childConstraints,
            webView: child,
            containerView: childContainer
        )
        XCTAssertTrue(
            primary.configuration.userContentController
                === child.configuration.userContentController
        )
        #if DEBUG
        XCTAssertTrue(primary.isInspectable)
        XCTAssertTrue(child.isInspectable)
        #else
        XCTAssertFalse(primary.isInspectable)
        XCTAssertFalse(child.isInspectable)
        #endif
    }

    @MainActor
    func testOuterScrollLockWorksPerWebViewAndResetsOnItsCommit() {
        let factory = WebViewFactory(
            instanceOrigin: instance,
            websiteDataStore: .nonPersistent()
        )
        let delegate = WebViewDelegateProbe()
        let primary = factory.makeWebView(delegate: delegate)
        let child = factory.makeWebView(
            configuration: primary.configuration,
            delegate: delegate
        )
        primary.scrollView.isScrollEnabled = true
        primary.scrollView.bounces = true
        primary.scrollView.contentInsetAdjustmentBehavior = .automatic
        child.scrollView.isScrollEnabled = true
        child.scrollView.bounces = true
        child.scrollView.contentInsetAdjustmentBehavior = .automatic

        XCTAssertTrue(factory.setOuterScrollLocked(true, in: primary))
        XCTAssertFalse(primary.scrollView.isScrollEnabled)
        XCTAssertFalse(primary.scrollView.bounces)
        XCTAssertEqual(primary.scrollView.contentInsetAdjustmentBehavior, .never)
        XCTAssertEqual(primary.scrollView.contentOffset, .zero)
        XCTAssertTrue(child.scrollView.isScrollEnabled)

        primary.scrollView.setContentOffset(CGPoint(x: 0, y: 40), animated: false)
        factory.resetLockedContentOffsetsForKeyboardChange()
        XCTAssertEqual(primary.scrollView.contentOffset, .zero)

        XCTAssertTrue(factory.setOuterScrollLocked(true, in: child))
        XCTAssertFalse(child.scrollView.isScrollEnabled)
        XCTAssertFalse(child.scrollView.bounces)
        XCTAssertEqual(child.scrollView.contentInsetAdjustmentBehavior, .never)

        factory.resetForNavigationCommit(in: child)
        XCTAssertFalse(primary.scrollView.isScrollEnabled)
        XCTAssertTrue(child.scrollView.isScrollEnabled)
        XCTAssertTrue(child.scrollView.bounces)
        XCTAssertEqual(child.scrollView.contentInsetAdjustmentBehavior, .automatic)

        factory.resetForNavigationCommit(in: primary)
        XCTAssertTrue(primary.scrollView.isScrollEnabled)
        XCTAssertTrue(primary.scrollView.bounces)
        XCTAssertEqual(primary.scrollView.contentInsetAdjustmentBehavior, .automatic)

        XCTAssertFalse(
            factory.setOuterScrollLocked(true, in: WKWebView(frame: .zero))
        )
    }

    @MainActor
    func testNativeBridgeIsDiscoverableOnlyInTheInstanceMainFrame() async throws {
        let metadata = NativeShellMetadata(
            shellVersion: "1.2.3 (45)",
            userAgentVersion: "1.2.3"
        )
        var outerScrollRequests: [Bool] = []
        var accountActionRequests: [Bool] = []
        weak var primaryWebView: WKWebView?
        let accountShown = expectation(description: "runtime bridge opens account surface")
        let webView = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: instance,
                metadata: metadata,
                setOuterScrollLocked: { _, locked in
                    outerScrollRequests.append(locked)
                    return true
                },
                setAccountActionAvailable: { sourceWebView, available in
                    guard sourceWebView === primaryWebView else {
                        return false
                    }
                    accountActionRequests.append(available)
                    return true
                },
                showAccount: {
                    accountShown.fulfill()
                }
            )
        )
        primaryWebView = webView
        let probe = WebViewLoadProbe()
        webView.navigationDelegate = probe

        try await probe.loadHTML(
            """
            <!doctype html>
            <iframe id="child" srcdoc="<p>subframe</p>"></iframe>
            """,
            baseURL: instance.url,
            in: webView
        )

        let instanceResult = try await webView.callAsyncJavaScript(
            """
            const info = await window.avibeNative.call("app.info");
            await window.avibeNative.call(
              "viewport.setOuterScrollLocked",
              {locked: true}
            );
            await window.avibeNative.call(
              "app.setAccountActionAvailable",
              {available: true}
            );
            await window.avibeNative.call("app.showAccount");
            let unsupported = null;
            try {
              await window.avibeNative.call("app.instances");
            } catch (error) {
              unsupported = error.code;
            }
            return JSON.stringify({
              bridge: typeof window.avibeNative,
              contract: info.contract,
              platform: info.platform,
              shellVersion: info.shellVersion,
              accountPresentationCapability: info.capabilities.accountPresentation,
              appCapability: info.capabilities.app,
              keyboardResizesViewport: info.capabilities.keyboardResizesViewport,
              viewportCapability: info.capabilities.viewport,
              subframeBridge: typeof document.querySelector("#child").contentWindow.avibeNative,
              subframeTransport: await document.querySelector("#child").contentWindow.webkit
                .messageHandlers.avibeNative.postMessage({method: "app.info", params: {}}),
              unsupported
            });
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        let instancePayload = try jsonObject(from: instanceResult)
        XCTAssertEqual(instancePayload["bridge"] as? String, "object")
        XCTAssertEqual(instancePayload["contract"] as? Int, 1)
        XCTAssertEqual(instancePayload["platform"] as? String, "ios")
        XCTAssertEqual(instancePayload["shellVersion"] as? String, "1.2.3 (45)")
        XCTAssertEqual(
            instancePayload["accountPresentationCapability"] as? Int,
            2
        )
        XCTAssertEqual(instancePayload["appCapability"] as? Int, 1)
        XCTAssertEqual(instancePayload["keyboardResizesViewport"] as? Int, 1)
        XCTAssertEqual(instancePayload["viewportCapability"] as? Int, 1)
        XCTAssertEqual(outerScrollRequests, [true])
        XCTAssertEqual(accountActionRequests, [true])
        XCTAssertEqual(instancePayload["subframeBridge"] as? String, "undefined")
        XCTAssertEqual(instancePayload["unsupported"] as? String, "unsupported")
        assertForbiddenReply(instancePayload["subframeTransport"])
        await fulfillment(of: [accountShown], timeout: 2)

        try await probe.loadHTML(
            "<!doctype html><title>External</title>",
            baseURL: URL(string: "https://example.com/")!,
            in: webView
        )
        let externalResult = try await webView.callAsyncJavaScript(
            """
            return JSON.stringify({
              bridge: typeof window.avibeNative,
              transport: await window.webkit.messageHandlers.avibeNative.postMessage({
                method: "app.info",
                params: {}
              })
            });
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        let externalPayload = try jsonObject(from: externalResult)
        XCTAssertEqual(externalPayload["bridge"] as? String, "undefined")
        assertForbiddenReply(externalPayload["transport"])

        let childWebView = WKWebView(
            frame: .zero,
            configuration: webView.configuration
        )
        let childProbe = WebViewLoadProbe()
        childWebView.navigationDelegate = childProbe
        try await childProbe.loadHTML(
            "<!doctype html><title>Child instance</title>",
            baseURL: instance.url,
            in: childWebView
        )
        let childResult = try await childWebView.callAsyncJavaScript(
            """
            const info = await window.avibeNative.call("app.info");
            await window.avibeNative.call(
              "viewport.setOuterScrollLocked",
              {locked: false}
            );
            let registration = null;
            try {
              await window.avibeNative.call(
                "app.setAccountActionAvailable",
                {available: true}
              );
            } catch (error) {
              registration = error.code;
            }
            return JSON.stringify({
              bridge: typeof window.avibeNative,
              keyboardResizesViewport: info.capabilities.keyboardResizesViewport,
              registration
            });
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        let childPayload = try jsonObject(from: childResult)
        XCTAssertEqual(childPayload["bridge"] as? String, "object")
        XCTAssertEqual(childPayload["keyboardResizesViewport"] as? Int, 1)
        XCTAssertEqual(childPayload["registration"] as? String, "forbidden")
        XCTAssertEqual(outerScrollRequests, [true, false])
        XCTAssertEqual(accountActionRequests, [true])
    }

    @MainActor
    func testPrimaryWebViewTraversesSPAHistoryWithTheInteractiveGestureEnabled() async throws {
        let server = try LocalHTTPServer { request in
            LocalHTTPResponse.html(
                """
                <!doctype html>
                <title>Workbench</title>
                <p>\(request.path)</p>
                """
            )
        }
        let baseURL = try await server.start()
        defer {
            server.stop()
        }
        let instanceOrigin = try XCTUnwrap(
            WebOrigin(url: baseURL, requiresHTTPS: false)
        )
        let controller = makeRuntimeController(instanceOrigin: instanceOrigin)
        let window = host(controller)
        defer {
            window.isHidden = true
        }
        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        try await waitForTitle("Workbench", in: webView)

        _ = try await webView.evaluateJavaScript(
            """
            history.pushState({idx: 1}, "", "/inbox");
            history.pushState({idx: 2}, "", "/chat/ses_1");
            """
        )
        try await waitUntil {
            webView.allowsBackForwardNavigationGestures
                && webView.backForwardList.backItem?.url.path == "/inbox"
        }

        XCTAssertNotNil(webView.goBack())
        try await waitUntil {
            webView.url?.path == "/inbox"
        }
        XCTAssertEqual(webView.url?.path, "/inbox")
    }

    @MainActor
    func testPrimaryBackGestureAndForcedBackCannotEnterAuthenticationHistory() async throws {
        let instanceServer = try LocalHTTPServer { request in
            LocalHTTPResponse.html(
                """
                <!doctype html>
                <title>\(request.path == "/returned" ? "Instance returned" : "Instance start")</title>
                """
            )
        }
        let authServer = try LocalHTTPServer { _ in
            LocalHTTPResponse.html(
                "<!doctype html><title>Consent</title>"
            )
        }
        let instanceBaseURL = try await instanceServer.start()
        let authBaseURL = try await authServer.start()
        defer {
            instanceServer.stop()
            authServer.stop()
        }
        let instanceOrigin = try XCTUnwrap(
            WebOrigin(url: instanceBaseURL, requiresHTTPS: false)
        )
        let authOrigin = try XCTUnwrap(
            WebOrigin(url: authBaseURL, requiresHTTPS: false)
        )
        let policy = NavigationPolicy(
            instanceOrigins: [instanceOrigin],
            authenticationOrigins: [authOrigin]
        )
        let controller = makeRuntimeController(
            instanceOrigin: instanceOrigin,
            navigationPolicy: policy
        )
        let window = host(controller)
        defer {
            window.isHidden = true
        }
        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        try await waitForTitle("Instance start", in: webView)
        webView.load(
            URLRequest(
                url: authBaseURL.appendingPathComponent("consent")
            )
        )
        try await waitForTitle("Consent", in: webView)
        webView.load(
            URLRequest(
                url: instanceBaseURL.appendingPathComponent("returned")
            )
        )
        try await waitForTitle("Instance returned", in: webView)
        try await waitUntil {
            WebOrigin(
                url: webView.backForwardList.backItem?.url
                    ?? URL(fileURLWithPath: "/"),
                requiresHTTPS: false
            ) == authOrigin
        }

        XCTAssertFalse(webView.allowsBackForwardNavigationGestures)
        XCTAssertNotNil(webView.goBack())
        try await Task.sleep(for: .milliseconds(400))
        XCTAssertEqual(
            WebOrigin(
                url: try XCTUnwrap(webView.url),
                requiresHTTPS: false
            ),
            instanceOrigin
        )
        let returnedTitle = try await webView.evaluateJavaScript(
            "document.title"
        ) as? String
        XCTAssertEqual(
            returnedTitle,
            "Instance returned"
        )
        let remainingBackOrigin = webView.backForwardList.backItem.flatMap {
            WebOrigin(url: $0.url, requiresHTTPS: false)
        }
        XCTAssertEqual(remainingBackOrigin, authOrigin)
        XCTAssertFalse(webView.allowsBackForwardNavigationGestures)

        _ = try await webView.evaluateJavaScript(
            "history.pushState({idx: 3}, '', '/chat/ses_2')"
        )
        try await waitUntil {
            webView.url?.path == "/chat/ses_2"
                && webView.allowsBackForwardNavigationGestures
                && WebOrigin(
                    url: webView.backForwardList.backItem?.url
                        ?? URL(fileURLWithPath: "/"),
                    requiresHTTPS: false
                ) == instanceOrigin
        }
    }

    @MainActor
    func testAccountFallbackTracksRegistrationAndPrimaryNavigationLifecycle() async throws {
        var visibility: [Bool] = []
        let controller = WebViewController(
            instanceOrigin: instance,
            websiteDataStore: .nonPersistent(),
            accountFallbackVisibilityChanged: {
                visibility.append($0)
            },
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        let window = host(controller)
        defer {
            window.isHidden = true
        }
        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        webView.stopLoading()
        webView.loadHTMLString(
            """
            <!doctype html>
            <meta name="viewport" content="width=device-width">
            <style>
              * { box-sizing: border-box; }
              body {
                margin: 0;
                color: #111827;
                background: #ffffff;
                font: 15px -apple-system, sans-serif;
              }
              header {
                height: 56px;
                display: flex;
                align-items: center;
                gap: 12px;
                padding: 0 16px;
                border-bottom: 1px solid #dfe3eb;
                background: #f4f6fb;
              }
              .avatar {
                width: 28px;
                height: 28px;
                display: grid;
                place-items: center;
                margin-left: auto;
                border: 1px solid rgba(8, 145, 178, 0.35);
                border-radius: 50%;
                color: #0891b2;
                background: rgba(8, 145, 178, 0.08);
                font-size: 11px;
                font-weight: 600;
              }
              main { padding: 24px 16px; }
            </style>
            <title>Current Workbench</title>
            <header>
              <strong>Avibe</strong>
              <span>Chat</span>
              <span class="avatar">M</span>
            </header>
            <main>Current Workbench account action</main>
            """,
            baseURL: instance.url
        )
        try await waitForTitle("Current Workbench", in: webView)
        _ = try await webView.callAsyncJavaScript(
            """
            await window.avibeNative.call(
              "app.setAccountActionAvailable",
              {available: true}
            );
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        visibility.removeAll()
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertFalse(visibility.contains(true))
        let attachment = XCTAttachment(
            data: try XCTUnwrap(snapshot(of: controller.view).pngData()),
            uniformTypeIdentifier: "public.png"
        )
        attachment.name = "current-runtime-native-fallback-hidden"
        attachment.lifetime = .keepAlways
        add(attachment)

        _ = try await webView.callAsyncJavaScript(
            """
            await window.avibeNative.call(
              "app.setAccountActionAvailable",
              {available: false}
            );
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        visibility.removeAll()
        try await waitUntil {
            visibility.last == true
        }

        _ = try await webView.callAsyncJavaScript(
            """
            await window.avibeNative.call(
              "app.setAccountActionAvailable",
              {available: true}
            );
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        visibility.removeAll()
        controller.webView(
            webView,
            didStartProvisionalNavigation: nil
        )
        _ = try await webView.callAsyncJavaScript(
            """
            await window.avibeNative.call(
              "app.setAccountActionAvailable",
              {available: false}
            );
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertFalse(visibility.contains(true))

        webView.loadHTMLString(
            "<!doctype html><title>Legacy Workbench</title>",
            baseURL: instance.url
        )
        try await waitForTitle("Legacy Workbench", in: webView)
        try await waitUntil {
            visibility.last == true
        }

        let child = WKWebView(
            frame: .zero,
            configuration: webView.configuration
        )
        let childProbe = WebViewLoadProbe()
        child.navigationDelegate = childProbe
        try await childProbe.loadHTML(
            "<!doctype html><title>Child</title>",
            baseURL: instance.url,
            in: child
        )
        let childResult = try await child.callAsyncJavaScript(
            """
            try {
              await window.avibeNative.call(
                "app.setAccountActionAvailable",
                {available: true}
              );
              return "allowed";
            } catch (error) {
              return error.code;
            }
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        XCTAssertEqual(childResult as? String, "forbidden")
        XCTAssertFalse(child.allowsBackForwardNavigationGestures)
    }

    @MainActor
    func testNavigatorShareIsAvailableInTheSimulatorWebView() async throws {
        let webView = WKWebView(frame: .zero)
        let probe = WebViewLoadProbe()
        webView.navigationDelegate = probe

        try await probe.loadHTML(
            "<!doctype html><title>Capability probe</title>",
            baseURL: instance.url,
            in: webView
        )

        let result = try await webView.evaluateJavaScript(
            """
            JSON.stringify({
              secureContext: window.isSecureContext,
              navigatorShare: typeof navigator.share
            })
            """
        )
        let output = result as? String
        print("AVIBE_CAPABILITY navigator.share=\(output ?? "unavailable")")
        XCTAssertEqual(
            output,
            #"{"secureContext":true,"navigatorShare":"function"}"#
        )
    }

    private func jsonObject(from value: Any?) throws -> [String: Any] {
        let string = try XCTUnwrap(value as? String)
        let data = try XCTUnwrap(string.data(using: .utf8))
        return try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any]
        )
    }

    private func assertForbiddenReply(
        _ value: Any?,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let reply = value as? [String: Any]
        XCTAssertEqual(reply?["ok"] as? Bool, false, file: file, line: line)
        let error = reply?["error"] as? [String: Any]
        XCTAssertEqual(error?["code"] as? String, "forbidden", file: file, line: line)
    }

    @MainActor
    private func makeRuntimeController(
        instanceOrigin: WebOrigin? = nil,
        navigationPolicy: NavigationPolicy? = nil
    ) -> WebViewController {
        WebViewController(
            instanceOrigin: instanceOrigin ?? instance,
            websiteDataStore: .nonPersistent(),
            navigationPolicy: navigationPolicy,
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
    }

    @MainActor
    private func host(_ controller: UIViewController) -> UIWindow {
        let window = UIWindow(
            frame: CGRect(x: 0, y: 0, width: 390, height: 700)
        )
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        controller.view.frame = window.bounds
        controller.view.layoutIfNeeded()
        return window
    }

    @MainActor
    private func waitForTitle(
        _ title: String,
        in webView: WKWebView
    ) async throws {
        try await waitUntil {
            let value = try? await webView.evaluateJavaScript("document.title")
            return value as? String == title
        }
    }

    @MainActor
    private func waitUntil(
        timeout: Duration = .seconds(5),
        condition: @escaping @MainActor () async -> Bool
    ) async throws {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: timeout)
        while clock.now < deadline {
            if await condition() {
                return
            }
            try await Task.sleep(for: .milliseconds(50))
        }
        XCTFail("Condition did not become true before the deadline.")
        throw TestWaitError.timedOut
    }

    @MainActor
    private func snapshot(of view: UIView) -> UIImage {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let renderer = UIGraphicsImageRenderer(
            bounds: view.bounds,
            format: format
        )
        return renderer.image { _ in
            view.drawHierarchy(in: view.bounds, afterScreenUpdates: true)
        }
    }

    @MainActor
    private func assertKeyboardBottomConstraint(
        _ constraints: [NSLayoutConstraint],
        webView: WKWebView,
        containerView: UIView,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let hasKeyboardBottomConstraint = constraints.contains { constraint in
            guard
                let firstItem = constraint.firstItem as? WKWebView,
                let secondItem = constraint.secondItem as? UILayoutGuide
            else {
                return false
            }
            return firstItem === webView
                && constraint.firstAttribute == .bottom
                && secondItem === containerView.keyboardLayoutGuide
                && constraint.secondAttribute == .top
        }
        XCTAssertTrue(hasKeyboardBottomConstraint, file: file, line: line)
    }
}

private enum TestWaitError: Error {
    case timedOut
}

private struct LegacyRuntimeToolbarScreenshotFixture: View {
    @Environment(\.colorScheme) private var colorScheme

    let instanceName: String
    let accountEmail: String?

    var body: some View {
        VStack(spacing: 0) {
            LegacyRuntimeToolbar(
                instanceName: instanceName,
                accountEmail: accountEmail,
                showAccount: {},
                showInstancePicker: {}
            )

            HStack(spacing: 10) {
                Image(systemName: "chevron.left")
                Text("Chat")
                    .font(.subheadline.weight(.semibold))
                Spacer()
                Image(systemName: "rectangle.on.rectangle")
            }
            .padding(.horizontal, 16)
            .frame(height: 48)
            .background(webFixtureBackground)

            Spacer()
        }
        .background(webFixtureBackground)
    }

    private var webFixtureBackground: Color {
        colorScheme == .dark
            ? Color(red: 8 / 255, green: 8 / 255, blue: 18 / 255)
            : .white
    }
}

private extension Result where Failure == NativeBridgeError {
    var failure: NativeBridgeError? {
        guard case .failure(let error) = self else {
            return nil
        }
        return error
    }
}

@MainActor
private final class WebViewLoadProbe: NSObject, WKNavigationDelegate {
    private var continuation: CheckedContinuation<Void, Error>?

    func loadHTML(
        _ html: String,
        baseURL: URL,
        in webView: WKWebView
    ) async throws {
        try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            webView.loadHTMLString(html, baseURL: baseURL)
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation?) {
        continuation?.resume()
        continuation = nil
    }

    func webView(
        _ webView: WKWebView,
        didFail navigation: WKNavigation?,
        withError error: Error
    ) {
        continuation?.resume(throwing: error)
        continuation = nil
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation?,
        withError error: Error
    ) {
        continuation?.resume(throwing: error)
        continuation = nil
    }
}

@MainActor
private final class WebViewDelegateProbe: NSObject, WKNavigationDelegate, WKUIDelegate {}
