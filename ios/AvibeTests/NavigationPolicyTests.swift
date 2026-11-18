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
        XCTAssertEqual(
            policy.destination(for: URL(string: "https://auth.avibe.bot/auth/v1/callback?code=1")!),
            .authentication
        )
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
        let url = URL(string: "https://accounts.google.com/o/oauth2/v2/auth")!

        XCTAssertEqual(policy.destination(for: url), .thirdPartySignIn(.google))
        XCTAssertEqual(
            policy.disposition(for: url, context: .primaryMainFrame),
            .thirdPartySignIn(.google)
        )
        XCTAssertEqual(
            policy.disposition(for: url, context: .popupMainFrame),
            .thirdPartySignIn(.google)
        )
    }

    // The App signs in only with avibe.bot's own accounts (App Review 4.8),
    // so GitHub authorization for avibe.bot's sign-in is intercepted in every
    // browsing context, as Google's is. GitHub sign-in for anything else and
    // the rest of github.com are an ordinary site.
    // avibe.bot's login card also offers Sign in with Apple (owner, 2026-11-18). Inside the App,
    // the App's own sign-in screen offers it natively, so the web authorization for avibe.bot's
    // sign-in is intercepted like Google's and GitHub's; Apple's other pages are ordinary sites.
    func testAppleWebAuthorizationForAvibeIsInterceptedAndTheRestOfAppleIsExternal() {
        let policy = NavigationPolicy(instanceOrigins: [instance])
        let intercepted = URL(string: "https://appleid.apple.com/auth/authorize?client_id=bot.avibe.web&redirect_uri=https%3A%2F%2Fauth.avibe.bot%2Fauth%2Fv1%2Fcallback&response_mode=form_post")!
        XCTAssertEqual(policy.destination(for: intercepted), .thirdPartySignIn(.apple))
        XCTAssertEqual(policy.disposition(for: intercepted, context: .primaryMainFrame), .thirdPartySignIn(.apple))
        XCTAssertEqual(policy.disposition(for: intercepted, context: .popupMainFrame), .thirdPartySignIn(.apple))
        XCTAssertEqual(policy.newWindowDisposition(for: intercepted), .thirdPartySignIn(.apple))
        for raw in [
            "https://appleid.apple.com/auth/authorize?client_id=other&redirect_uri=https%3A%2F%2Fexample.com%2Fcallback",
            "https://appleid.apple.com/auth/authorize?client_id=other",
            "https://appleid.apple.com/account/manage",
            "https://support.apple.com/en-us/105078",
        ] {
            let url = URL(string: raw)!
            XCTAssertEqual(policy.destination(for: url), .external, raw)
            XCTAssertEqual(policy.newWindowDisposition(for: url), .childWebView, raw)
        }
    }

    func testGitHubAuthorizationIsInterceptedAndTheRestOfGitHubIsExternal() {
        let policy = NavigationPolicy(instanceOrigins: [instance])

        for raw in [
            "https://github.com/login/oauth/authorize?client_id=abc&redirect_uri=https%3A%2F%2Fauth.avibe.bot%2Fauth%2Fv1%2Fcallback&scope=user%3Aemail",
            "https://github.com/login/oauth/authorize?client_id=abc&redirect_uri=https%3A%2F%2Fmctppyrqmrjotgkrwdse.supabase.co%2Fauth%2Fv1%2Fcallback",
            "https://github.com/login/oauth/select_account?redirect_uri=https%3A%2F%2Fauth.avibe.bot%2Fauth%2Fv1%2Fcallback",
            "https://GitHub.com/Login/OAuth/authorize?redirect_uri=https%3A%2F%2Fauth.avibe.bot%2Fauth%2Fv1%2Fcallback",
        ] {
            let url = URL(string: raw)!
            XCTAssertEqual(policy.destination(for: url), .thirdPartySignIn(.github), raw)
            XCTAssertEqual(
                policy.disposition(for: url, context: .primaryMainFrame),
                .thirdPartySignIn(.github),
                raw
            )
            XCTAssertEqual(
                policy.disposition(for: url, context: .popupMainFrame),
                .thirdPartySignIn(.github),
                raw
            )
        }

        for raw in [
            // The device-code page an instance's Copilot sign-in opens.
            "https://github.com/login/device",
            // GitHub sign-in for something else, such as an MCP server an
            // instance connects to, or one that names no callback.
            "https://github.com/login/oauth/authorize?client_id=mcp&redirect_uri=https%3A%2F%2Fmcp.example.com%2Fcallback",
            "https://github.com/login/oauth/authorize?client_id=mcp&redirect_uri=https%3A%2F%2Falice-app.avibe.bot%2Foauth%2Fcallback",
            "https://github.com/login/oauth/authorize?client_id=mcp&redirect_uri=https%3A%2F%2Fevil.avibe.bot%2Fauth%2Fv1%2Fcallback",
            "https://github.com/login/oauth/authorize?client_id=abc",
            "https://github.com/login",
            "https://github.com/login/oauthx",
            "https://github.com/avibe-bot/avibe",
            "https://gist.github.com/login/oauth/authorize",
        ] {
            let url = URL(string: raw)!
            XCTAssertEqual(policy.destination(for: url), .external, raw)
            XCTAssertEqual(policy.disposition(for: url, context: .primaryMainFrame), .external, raw)
            XCTAssertEqual(policy.disposition(for: url, context: .popupMainFrame), .allow, raw)
            XCTAssertEqual(policy.newWindowDisposition(for: url), .childWebView, raw)
        }
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
            URL(string: "https://github.com/login/device")!,
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
            .thirdPartySignIn(.google)
        )
        XCTAssertEqual(
            policy.newWindowDisposition(
                for: URL(string: "https://github.com/login/oauth/authorize?client_id=abc&redirect_uri=https%3A%2F%2Fauth.avibe.bot%2Fauth%2Fv1%2Fcallback")!
            ),
            .thirdPartySignIn(.github)
        )
    }

    func testChildBrowsingContextsCanOpenFurtherChildBrowsingContexts() {
        let policy = NavigationPolicy(instanceOrigins: [instance])

        for url in [
            URL(string: "https://alice-app.avibe.bot/show/ses_1/")!,
            URL(string: "https://github.com/login/device")!,
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
            try NativeBridgeRequest.parse(
                ["method": "navigation.historyChanged"]
            ).get().method,
            .navigationHistoryChanged
        )
        XCTAssertEqual(
            try NativeBridgeRequest.parse(
                [
                    "method": "appearance.colorSchemeChanged",
                    "params": ["scheme": "light"],
                ]
            ).get().method,
            .appearanceColorSchemeChanged
        )
        XCTAssertNil(NativeShellMetadata.capabilities["appearance"])
        XCTAssertEqual(
            NativeBridgeRequest.parse(["method": "app.instances"]).failure,
            .unsupported
        )
        XCTAssertEqual(
            NativeBridgeRequest.parse(["method": "app.info", "params": "bad"]).failure,
            .invalidRequest
        )
    }

    func testAccountRootArgumentsRequireTheDeclaredCapabilityAndExactShape() {
        for capability in [nil, 0, 1] as [Int?] {
            XCTAssertEqual(
                NativeAccountPresentation.parse([:], instancePresentation: capability), .instances
            )
            XCTAssertEqual(
                NativeAccountPresentation.parse(["start": "account"], instancePresentation: capability),
                capability == 1 ? .account : nil
            )
        }
        let invalid: [[String: Any]] = [
            ["start": "instances"], ["start": "Account"], ["start": ""],
            ["start": NSNull()], ["start": true], ["start": 1],
            ["start": ["account"]], ["start": ["page": "account"]],
            ["page": "account"], ["start": "account", "extra": true],
        ]
        for params in invalid {
            XCTAssertNil(NativeAccountPresentation.parse(params, instancePresentation: 1))
        }
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
    func testNativeBridgeQueuesPushOpenedUntilTheWebListenerSubscribes()
        async throws
    {
        var acknowledgedID: String?
        let configuration = NativeBridgeConfiguration.make(
            instanceOrigin: instance,
            setOuterScrollLocked: { _, _ in false },
            pushOpenedAck: { id, origin in
                XCTAssertEqual(origin, self.instance)
                acknowledgedID = id
                return true
            }
        )
        let webView = WKWebView(
            frame: .zero,
            configuration: configuration
        )
        let probe = WebViewLoadProbe()
        webView.navigationDelegate = probe

        try await probe.loadHTML(
            """
            <!doctype html>
            <script>
              window.dispatchEvent(new CustomEvent(
                "avibe:native:push.opened",
                { detail: { id: "tap-1", url: "/chat/queued" } }
              ));
            </script>
            """,
            baseURL: instance.url,
            in: webView
        )

        let result = try await webView.callAsyncJavaScript(
            """
            return new Promise((resolve) => {
              window.avibeNative.on("push.opened", (payload) => {
                resolve(payload.url);
              });
            });
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )

        XCTAssertEqual(result as? String, "/chat/queued")
        try await waitUntil { acknowledgedID == "tap-1" }
    }

    @MainActor
    func testNativeBridgeKeepsOnlyTheLatestQueuedTapAndDeduplicatesItsID()
        async throws
    {
        var acknowledgedIDs: [String] = []
        let configuration = NativeBridgeConfiguration.make(
            instanceOrigin: instance,
            setOuterScrollLocked: { _, _ in false },
            pushOpenedAck: { id, _ in
                acknowledgedIDs.append(id)
                return true
            }
        )
        let webView = WKWebView(
            frame: .zero,
            configuration: configuration
        )
        let probe = WebViewLoadProbe()
        webView.navigationDelegate = probe

        try await probe.loadHTML(
            """
            <!doctype html>
            <script>
              for (const detail of [
                { id: "tap-old", url: "/chat/old" },
                { id: "tap-new", url: "/chat/new" }
              ]) {
                window.dispatchEvent(new CustomEvent(
                  "avibe:native:push.opened",
                  { detail }
                ));
              }
            </script>
            """,
            baseURL: instance.url,
            in: webView
        )

        let result = try await webView.callAsyncJavaScript(
            """
            return new Promise((resolve) => {
              const received = [];
              window.avibeNative.on("push.opened", (payload) => {
                received.push(payload.url);
                window.dispatchEvent(new CustomEvent(
                  "avibe:native:push.opened",
                  { detail: payload }
                ));
                queueMicrotask(() => resolve(received));
              });
            });
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )

        XCTAssertEqual(result as? [String], ["/chat/new"])
        try await waitUntil { acknowledgedIDs == ["tap-new"] }
    }

    @MainActor
    func testATapPendingDuringBootstrapOpensBeforeHealthRestorationFinishes()
        async throws
    {
        // Health restoration is held at its manifest for the whole test.
        let healthDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("NavigationHealth.\(UUID().uuidString)", isDirectory: true)
        let health = HealthCoordinatorFixture(directory: healthDirectory, savedIdentity: true)
        health.client.claims = []
        health.client.holdManifest = true
        // Once released, restoration never waits on an unacknowledged upload.
        health.uploader.autoAcknowledge = true
        let (appModel, _) = try await makeFixtureAppModel(healthSync: health.coordinator) { appModel in
            health.credentials.identity?.websiteDataStoreID = appModel.websiteDataStore.identifier
            // Arrives before bootstrap completes, so it is held as pending.
            appModel.pushCoordinator.handleNotificationTap(
                userInfo: [
                    "account_id": "account-1",
                    "instance_origin": "https://instance-one.example",
                    "url": "/chat/pending",
                ]
            )
        }
        // Registered after the fixture so it runs first: the held restoration keeps
        // bootstrap (which holds the App) running until Health is released.
        addTeardownBlock { @MainActor in
            health.client.holdManifest = false
            health.client.releasePending()
            await health.coordinator.logout()
            try? FileManager.default.removeItem(at: healthDirectory)
        }
        _ = try await waitForPushRuntime(appModel, instanceID: "instance-1")
        try await waitUntil("Health restoration holds its manifest") {
            health.client.manifestContinuation != nil
        }
    }

    @MainActor
    func testPendingPushTapRedeliversAcrossNavigationUntilMatchingAck()
        async throws
    {
        let appModel = try await makePushTapAppModel()
        appModel.pushCoordinator.handleNotificationTap(
            userInfo: [
                "account_id": "account-1",
                "instance_origin": "https://instance-one.example",
                "url": "/chat/redelivered",
            ]
        )
        let runtime = try await waitForPushRuntime(
            appModel,
            instanceID: "instance-1"
        )
        var acknowledgements: [Bool] = []
        let (controller, webView, window) = try makePushRuntimeController(
            appModel: appModel,
            runtime: runtime,
            origin: WebOrigin.parseConfigured(
                "https://instance-one.example"
            )!
        ) { id, origin in
            let accepted = appModel.acknowledgePushOpened(
                id: id,
                origin: origin
            )
            acknowledgements.append(accepted)
            return accepted
        }
        defer {
            window.rootViewController = nil
            window.isHidden = true
        }

        // The first attempt has no document yet. The next document has no
        // listener, and navigation replaces its document-start queue.
        appModel.didFinishRuntimeLoading()
        let probe = WebViewLoadProbe()
        webView.navigationDelegate = probe
        try await probe.loadHTML(
            "<!doctype html><title>no listener</title>",
            baseURL: runtime.instance.url,
            in: webView
        )
        appModel.didFinishRuntimeLoading()
        XCTAssertTrue(acknowledgements.isEmpty)

        try await probe.loadHTML(
            """
            <!doctype html>
            <script>
              window.receivedPushOpened = [];
              window.avibeNative.on("push.opened", (payload) => {
                window.receivedPushOpened.push(payload.url);
              });
            </script>
            """,
            baseURL: runtime.instance.url,
            in: webView
        )
        appModel.didFinishRuntimeLoading()
        try await waitUntil { acknowledgements == [true] }
        let received = try await webView.evaluateJavaScript(
            "window.receivedPushOpened"
        )
        XCTAssertEqual(received as? [String], ["/chat/redelivered"])

        // A fresh document has an empty JS dedupe set. No second ack proves
        // the matching first ack cleared the native pending tap.
        try await probe.loadHTML(
            """
            <!doctype html>
            <script>
              window.avibeNative.on("push.opened", () => {});
            </script>
            """,
            baseURL: runtime.instance.url,
            in: webView
        )
        appModel.didFinishRuntimeLoading()
        try await Task.sleep(for: .milliseconds(50))
        XCTAssertEqual(acknowledgements, [true])
        _ = controller
    }

    @MainActor
    func testPendingPushTapKeepsOnlyLatestAndDropsOnInstanceSwitch()
        async throws
    {
        let appModel = try await makePushTapAppModel()
        for path in ["/chat/old", "/chat/new"] {
            appModel.pushCoordinator.handleNotificationTap(
                userInfo: [
                    "account_id": "account-1",
                    "instance_origin": "https://instance-one.example",
                    "url": path,
                ]
            )
        }
        let latestRuntime = try await waitForPushRuntime(
            appModel,
            instanceID: "instance-1"
        )
        var receivedURLs: [String] = []
        let (_, latestWebView, latestWindow) = try makePushRuntimeController(
            appModel: appModel,
            runtime: latestRuntime,
            origin: WebOrigin.parseConfigured(
                "https://instance-one.example"
            )!
        ) { _, _ in true }
        defer {
            latestWindow.rootViewController = nil
            latestWindow.isHidden = true
        }
        let latestProbe = WebViewLoadProbe()
        latestWebView.navigationDelegate = latestProbe
        try await latestProbe.loadHTML(
            """
            <!doctype html>
            <script>
              window.avibeNative.on("push.opened", (payload) => {
                window.webkit.messageHandlers.testTap.postMessage(payload.url);
              });
            </script>
            """,
            baseURL: latestRuntime.instance.url,
            in: latestWebView
        )
        let tapHandler = ScriptMessageHandler { value in
            if let path = value as? String {
                receivedURLs.append(path)
            }
        }
        latestWebView.configuration.userContentController.add(
            tapHandler,
            name: "testTap"
        )
        appModel.didFinishRuntimeLoading()
        try await waitUntil { receivedURLs == ["/chat/new"] }

        appModel.pushCoordinator.handleNotificationTap(
            userInfo: [
                "account_id": "account-1",
                "instance_origin": "https://instance-one.example",
                "url": "/chat/drop-on-switch",
            ]
        )
        _ = try appModel.mobileSessionCoordinator.selectInstance(
            id: "instance-2"
        )
        let switched = try await waitForPushRuntime(
            appModel,
            instanceID: "instance-2"
        )
        var switchedAckCount = 0
        let (_, switchedWebView, switchedWindow) = try makePushRuntimeController(
            appModel: appModel,
            runtime: switched,
            origin: WebOrigin.parseConfigured(
                "https://instance-two.example"
            )!
        ) { _, _ in
            switchedAckCount += 1
            return true
        }
        defer {
            switchedWindow.rootViewController = nil
            switchedWindow.isHidden = true
        }
        let switchedProbe = WebViewLoadProbe()
        switchedWebView.navigationDelegate = switchedProbe
        try await switchedProbe.loadHTML(
            """
            <!doctype html>
            <script>
              window.avibeNative.on("push.opened", () => {});
            </script>
            """,
            baseURL: switched.instance.url,
            in: switchedWebView
        )
        appModel.didFinishRuntimeLoading()
        try await Task.sleep(for: .milliseconds(50))
        XCTAssertEqual(switchedAckCount, 0)
    }

    @MainActor
    func testSignedInFixtureRejectsBootstrapFailureOnThePickerRoute() async throws {
        do {
            _ = try await makePushTapAppModel(sessionResponse: .status(503))
            XCTFail("A failed bootstrap must not admit a signed-in fixture")
        } catch let error as FixtureReadinessError {
            // A readiness timeout must not satisfy this regression: prove the
            // fixture reached the deliberately failed session response.
            XCTAssertTrue(error.description.contains("bootstrap settled"))
            XCTAssertTrue(error.description.contains("account=nil"))
            XCTAssertTrue(error.description.contains("GET /api/mobile/session -> 503"))
        }
    }

    @MainActor
    func testPendingPushTapExpiresAfterOneHundredTwentySeconds()
        async throws
    {
        let clock = MutableDate(
            Date(timeIntervalSince1970: 1_794_096_000)
        )
        let appModel = try await makePushTapAppModel(
            pushOpenedNow: { clock.value }
        )
        appModel.pushCoordinator.handleNotificationTap(
            userInfo: [
                "account_id": "account-1",
                "instance_origin": "https://instance-one.example",
                "url": "/chat/expired",
            ]
        )
        let runtime = try await waitForPushRuntime(
            appModel,
            instanceID: "instance-1"
        )
        clock.value = clock.value.addingTimeInterval(121)
        var ackCount = 0
        let (_, webView, window) = try makePushRuntimeController(
            appModel: appModel,
            runtime: runtime,
            origin: WebOrigin.parseConfigured(
                "https://instance-one.example"
            )!
        ) { _, _ in
            ackCount += 1
            return true
        }
        defer {
            window.rootViewController = nil
            window.isHidden = true
        }
        let probe = WebViewLoadProbe()
        webView.navigationDelegate = probe
        try await probe.loadHTML(
            """
            <!doctype html>
            <script>
              window.avibeNative.on("push.opened", () => {});
            </script>
            """,
            baseURL: runtime.instance.url,
            in: webView
        )

        appModel.didFinishRuntimeLoading()
        try await Task.sleep(for: .milliseconds(50))
        XCTAssertEqual(ackCount, 0)
    }

    /// Settings > Action Button > Shortcut > Avibe runs `OpenMainBotIntent`.
    /// Launched by it, the App gets the request before its bootstrap
    /// restores the selected instance; that instance then opens again, and
    /// its page receives the main Bot's conversation through `push.opened`.
    @MainActor
    func testMainBotShortcutOpensTheSelectedInstanceAtTheMainBotConversation()
        async throws
    {
        let (appModel, _) = try await makeFixtureAppModel(
            storedSelection: Self.instanceOneSelection
        ) { appModel in
            // What `AvibeApp` attaches for its own model. This replaces the
            // test host's handler for the rest of the process; no other test
            // runs a shortcut.
            InstancePathRequests.shared.attach { [weak appModel] path in
                appModel?.openSelectedInstance(path: path)
            }
            _ = try await OpenMainBotIntent().perform()
        }
        // The restored open, then the shortcut's.
        try await waitUntil("The shortcut's open") {
            appModel.runtimeIdentity == 2
        }
        let runtime = try await waitForPushRuntime(
            appModel,
            instanceID: "instance-1"
        )
        var acknowledgements: [Bool] = []
        let (controller, webView, window) = try makePushRuntimeController(
            appModel: appModel,
            runtime: runtime,
            origin: WebOrigin.parseConfigured(
                "https://instance-one.example"
            )!
        ) { id, origin in
            let accepted = appModel.acknowledgePushOpened(
                id: id,
                origin: origin
            )
            acknowledgements.append(accepted)
            return accepted
        }
        defer {
            window.rootViewController = nil
            window.isHidden = true
        }
        let probe = WebViewLoadProbe()
        webView.navigationDelegate = probe
        try await probe.loadHTML(
            """
            <!doctype html>
            <script>
              window.receivedPushOpened = [];
              window.avibeNative.on("push.opened", (payload) => {
                window.receivedPushOpened.push(payload.url);
              });
            </script>
            """,
            baseURL: runtime.instance.url,
            in: webView
        )
        appModel.didFinishRuntimeLoading()
        try await waitUntil { acknowledgements == [true] }
        let received = try await webView.evaluateJavaScript(
            "window.receivedPushOpened"
        )
        // `from=notification`: a reader who is not the instance Owner has
        // no Bots, and the Web UI sends that 404 to the Inbox.
        XCTAssertEqual(
            received as? [String],
            ["/chat/ses_main?from=notification"]
        )
        XCTAssertTrue(OpenMainBotIntent.openAppWhenRun)
        _ = controller
    }

    /// Signed out, with no instance selected, or with the selected instance
    /// unavailable, the shortcut leaves the App on the screen it shows
    /// anyway: no runtime opens and nothing is presented, whether the
    /// request arrives before the bootstrap settles or after.
    @MainActor
    func testMainBotShortcutKeepsTheCurrentScreenWhenNothingCanOpen()
        async throws
    {
        let cases: [(
            name: String,
            session: LocalHTTPResponse?,
            instanceOneAvailable: Bool,
            selection: String?
        )] = [
            ("signed out", .status(401), true, nil),
            ("no instance selected", nil, true, nil),
            ("selected instance offline", nil, false, Self.instanceOneSelection),
        ]
        for item in cases {
            var launchStore: UUID?
            let (appModel, _) = try await makeFixtureAppModel(
                sessionResponse: item.session,
                instanceOneAvailable: item.instanceOneAvailable,
                storedSelection: item.selection
            ) { appModel in
                launchStore = appModel.websiteDataStore.identifier
                appModel.openSelectedInstance(path: OpenMainBotIntent.path)
            }
            try await waitUntil("\(item.name): bootstrap settles") {
                appModel.route != .loading
            }
            // A signed-out launch moves to a fresh store; remove that one
            // too, once the account transport has let it go.
            let currentStore = appModel.websiteDataStore.identifier
            if currentStore != launchStore {
                let cleanup = NamedDataStoreCleanup()
                cleanup.identifier = currentStore
                // Weak: the App holds its current store, so the store can
                // go only once the App has.
                addTeardownBlock { @MainActor [weak appModel] in
                    appModel?.mobileSessionCoordinator.deactivateAccountSurface()
                    let error = await cleanup.remove()
                    XCTAssertNil(error, "Signed-out store cleanup failed")
                }
            }
            XCTAssertEqual(
                appModel.mobileSessionCoordinator.currentAccount == nil,
                item.name == "signed out",
                item.name
            )

            appModel.openSelectedInstance(path: OpenMainBotIntent.path)
            try await Task.sleep(for: .milliseconds(100))

            XCTAssertEqual(appModel.route, .onboarding(.instances), item.name)
            XCTAssertEqual(appModel.runtimeIdentity, 0, item.name)
            XCTAssertNil(appModel.runtimeOpenAttempt, item.name)
            if item.selection != nil {
                // Still selected, and unavailable: the reason nothing opened.
                XCTAssertEqual(
                    appModel.mobileSessionCoordinator.selectedInstance?.available,
                    false,
                    item.name
                )
            }
        }
    }

    /// On a cold launch the intent can run before the scene attaches the
    /// App's model: the request waits, is delivered once on attach, and a
    /// request after that goes straight through.
    @MainActor
    func testShortcutRequestsWaitForTheAppToAttach() {
        let requests = InstancePathRequests()
        var delivered: [String] = []
        requests.open(path: "/chat/early")
        XCTAssertEqual(delivered, [])

        requests.attach { delivered.append($0) }
        XCTAssertEqual(delivered, ["/chat/early"])

        var reattached: [String] = []
        requests.attach { reattached.append($0) }
        XCTAssertEqual(reattached, [], "A delivered request came back.")

        requests.open(path: "/chat/later")
        XCTAssertEqual(delivered, ["/chat/early"])
        XCTAssertEqual(reattached, ["/chat/later"])
    }

    /// A shortcut usually arrives as the App returns to the foreground, when
    /// the App may be refreshing the account or updating push on its own.
    /// Those keep the selection, so the open waits for them and then opens
    /// the main Bot's conversation instead of being dropped as busy, unless
    /// the user moved on meanwhile (here: opened Account).
    @MainActor
    func testMainBotShortcutWaitsForASelectionKeepingRefresh() async throws {
        for userMovesOn in [false, true] {
            let held = HeldSessionReads()
            addTeardownBlock { held.release.signal() }
            let (appModel, _) = try await makeFixtureAppModel(
                storedSelection: Self.instanceOneSelection,
                heldSessionReads: held
            )
            try await waitUntil("The restored open") {
                appModel.runtimeIdentity == 1
            }
            let refresh = Task { @MainActor in
                try await appModel.mobileSessionCoordinator.refresh()
            }
            try await waitUntil("The refresh's session read") {
                held.count.current == 2
            }
            XCTAssertTrue(
                appModel.mobileSessionCoordinator.isHeldBySelectionKeepingOperation
            )

            appModel.openSelectedInstance(path: OpenMainBotIntent.path)
            try await Task.sleep(for: .milliseconds(200))
            XCTAssertEqual(
                appModel.runtimeIdentity, 1, "It opened during the refresh."
            )
            if userMovesOn {
                appModel.showAccount()
                guard case .accountOverBrowser = appModel.route else {
                    XCTFail("Account did not cover the runtime")
                    return
                }
            }

            held.release.signal()
            _ = try await refresh.value
            if userMovesOn {
                try await Task.sleep(for: .milliseconds(200))
                XCTAssertEqual(appModel.runtimeIdentity, 1)
                guard case .accountOverBrowser = appModel.route else {
                    XCTFail("The waited open replaced Account")
                    return
                }
            } else {
                try await waitUntil("The shortcut's open after the refresh") {
                    appModel.runtimeIdentity == 2
                }
                _ = try await waitForPushRuntime(
                    appModel,
                    instanceID: "instance-1"
                )
            }
        }
    }

    /// The shortcut reaches Settings > Action Button only through the App
    /// Intents metadata Xcode extracts into the App bundle, with its
    /// localized title and Siri phrases beside it.
    @MainActor
    func testMainBotShortcutIsInTheAppIntentsMetadata() throws {
        let metadataURL = try XCTUnwrap(
            Bundle.main.url(
                forResource: "Metadata",
                withExtension: "appintents"
            ),
            "The App bundle has no Metadata.appintents"
        ).appendingPathComponent("extract.actionsdata")
        let metadata = try XCTUnwrap(
            JSONSerialization.jsonObject(
                with: Data(contentsOf: metadataURL)
            ) as? [String: Any]
        )
        let actions = try XCTUnwrap(metadata["actions"] as? [String: Any])
        let action = try XCTUnwrap(
            actions["OpenMainBotIntent"] as? [String: Any]
        )
        XCTAssertEqual(action["openAppWhenRun"] as? Bool, true)
        XCTAssertEqual(
            (action["title"] as? [String: Any])?["key"] as? String,
            "Open Main Bot"
        )
        let shortcuts = try XCTUnwrap(
            metadata["autoShortcuts"] as? [[String: Any]]
        )
        let shortcut = try XCTUnwrap(
            shortcuts.first {
                $0["actionIdentifier"] as? String == "OpenMainBotIntent"
            }
        )
        XCTAssertEqual(
            (shortcut["shortTitle"] as? [String: Any])?["key"] as? String,
            "Open Main Bot"
        )
        XCTAssertEqual(
            shortcut["systemImageName"] as? String,
            "bubble.left.and.bubble.right"
        )
        // Talking to the main Bot is the background voice shortcut.
        let voice = try XCTUnwrap(
            shortcuts.first {
                $0["actionIdentifier"] as? String == "SpeakToMainBotIntent"
            }
        )
        XCTAssertEqual(
            (voice["shortTitle"] as? [String: Any])?["key"] as? String,
            "Talk to Main Bot"
        )
        XCTAssertEqual(
            Set((voice["phraseTemplates"] as? [[String: Any]] ?? []).compactMap { $0["key"] as? String }),
            ["Talk to Main Bot in ${applicationName}"]
        )
        let phrases = Set(
            (shortcut["phraseTemplates"] as? [[String: Any]] ?? []).compactMap {
                $0["key"] as? String
            }
        )
        XCTAssertEqual(phrases, ["Open Main Bot in ${applicationName}"])

        let chinese = try XCTUnwrap(
            Bundle.main.path(forResource: "zh-Hans", ofType: "lproj")
                .flatMap(Bundle.init(path:))
        )
        XCTAssertEqual(
            chinese.localizedString(
                forKey: "Open Main Bot",
                value: nil,
                table: nil
            ),
            "打开主伙伴"
        )
        XCTAssertEqual(
            chinese.localizedString(forKey: "Talk to Main Bot", value: nil, table: nil),
            "和主伙伴说话"
        )
        for phrase in phrases.union(["Talk to Main Bot in ${applicationName}"]) {
            let localized = chinese.localizedString(
                forKey: phrase,
                value: nil,
                table: "AppShortcuts"
            )
            XCTAssertNotEqual(localized, phrase)
            XCTAssertTrue(localized.contains("${applicationName}"), localized)
            XCTAssertTrue(localized.contains("主伙伴"), localized)
        }
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

        let primaryConstraints = factory.install(
            primary,
            in: primaryContainer,
            extent: .container
        )
        let childConstraints = factory.install(
            child,
            in: childContainer,
            extent: .safeArea
        )

        XCTAssertTrue(factory.isManaging(primary))
        XCTAssertTrue(factory.isManaging(child))
        assertVerticalConstraints(
            primaryConstraints,
            webView: primary,
            top: primaryContainer,
            bottom: primaryContainer
        )
        assertVerticalConstraints(
            childConstraints,
            webView: child,
            top: childContainer.safeAreaLayoutGuide,
            bottom: childContainer.safeAreaLayoutGuide
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
    func testKeyboardViewportUsesDockedFramesAndWillHideRestoresTheBaseline() {
        for bottomEdge in [
            KeyboardViewportLayoutController.BottomEdge.container,
            .safeArea,
        ] {
            assertKeyboardViewportRows(bottomEdge: bottomEdge)
        }
    }

    @MainActor
    private func assertKeyboardViewportRows(
        bottomEdge: KeyboardViewportLayoutController.BottomEdge
    ) {
        let fixture = KeyboardViewportFixture(bottomEdge: bottomEdge)
        let safeAreaBottom = fixture.safeAreaBottom
        let restingBottom = fixture.restingBottom
        let keyboardTop = safeAreaBottom - 278
        let shortcutBarTop = safeAreaBottom - 48
        let rows: [
            (
                name: String,
                notification: Notification.Name,
                frame: CGRect,
                expectedBottom: CGFloat
            )
        ] = [
            (
                "software keyboard",
                UIResponder.keyboardWillChangeFrameNotification,
                fixture.dockedKeyboardFrame(top: keyboardTop),
                keyboardTop
            ),
            (
                "external keyboard shortcut bar",
                UIResponder.keyboardWillChangeFrameNotification,
                fixture.dockedKeyboardFrame(top: shortcutBarTop),
                shortcutBarTop
            ),
            (
                "floating frame above the bottom",
                UIResponder.keyboardWillChangeFrameNotification,
                CGRect(
                    x: 80,
                    y: keyboardTop,
                    width: 220,
                    height: 200
                ),
                restingBottom
            ),
            (
                "frame outside the horizontal safe area",
                UIResponder.keyboardWillChangeFrameNotification,
                CGRect(
                    x: fixture.containerView.bounds.maxX + 10,
                    y: keyboardTop,
                    width: 200,
                    height: fixture.containerView.bounds.maxY - keyboardTop
                ),
                restingBottom
            ),
            (
                "keyboard completely offscreen",
                UIResponder.keyboardWillChangeFrameNotification,
                fixture.offscreenKeyboardFrame(),
                restingBottom
            ),
            (
                "willHide without didHide",
                UIResponder.keyboardWillHideNotification,
                fixture.offscreenKeyboardFrame(),
                restingBottom
            ),
        ]

        for row in rows {
            fixture.postKeyboardFrame(
                row.frame,
                notification: row.notification
            )
            XCTAssertEqual(
                fixture.contentBottom,
                row.expectedBottom,
                accuracy: 0.5,
                "\(row.name) (\(bottomEdge))"
            )
        }
    }

    @MainActor
    func testKeyboardViewportUsesTheUIKitAnimationPath() {
        let fixture = KeyboardViewportFixture(bottomEdge: .container)
        let keyboardTop = fixture.safeAreaBottom - 278

        let animationsWereEnabled = UIView.areAnimationsEnabled
        UIView.setAnimationsEnabled(false)
        defer {
            UIView.setAnimationsEnabled(animationsWereEnabled)
        }
        fixture.postKeyboardFrame(
            fixture.dockedKeyboardFrame(top: keyboardTop),
            duration: 0.31,
            curve: .easeOut
        )

        XCTAssertEqual(fixture.contentBottom, keyboardTop, accuracy: 0.5)
    }

    @MainActor
    func testKeyboardViewportRecalculatesAfterRotationAndBackgrounding() {
        for bottomEdge in [
            KeyboardViewportLayoutController.BottomEdge.container,
            .safeArea,
        ] {
            assertKeyboardViewportRecalculation(bottomEdge: bottomEdge)
        }
    }

    @MainActor
    private func assertKeyboardViewportRecalculation(
        bottomEdge: KeyboardViewportLayoutController.BottomEdge
    ) {
        let fixture = KeyboardViewportFixture(bottomEdge: bottomEdge)
        let portraitKeyboardTop = fixture.safeAreaBottom - 278
        fixture.postKeyboardFrame(
            fixture.dockedKeyboardFrame(top: portraitKeyboardTop)
        )
        XCTAssertEqual(
            fixture.contentBottom,
            portraitKeyboardTop,
            accuracy: 0.5
        )

        fixture.window.bounds = CGRect(
            x: 0,
            y: 0,
            width: fixture.window.bounds.height,
            height: fixture.window.bounds.width
        )
        fixture.containerView.frame = fixture.window.bounds.insetBy(
            dx: 8,
            dy: 20
        )
        fixture.layoutController.refreshForContainerGeometryChange()
        XCTAssertEqual(
            fixture.contentBottom,
            fixture.restingBottom,
            accuracy: 0.5
        )

        let landscapeKeyboardTop = fixture.safeAreaBottom - 119
        fixture.postKeyboardFrame(
            fixture.dockedKeyboardFrame(top: landscapeKeyboardTop)
        )
        XCTAssertEqual(
            fixture.contentBottom,
            landscapeKeyboardTop,
            accuracy: 0.5
        )

        NotificationCenter.default.post(
            name: UIApplication.didEnterBackgroundNotification,
            object: nil
        )
        XCTAssertEqual(
            fixture.contentBottom,
            landscapeKeyboardTop,
            accuracy: 0.5
        )

        fixture.postKeyboardFrame(
            fixture.offscreenKeyboardFrame(),
            notification: UIResponder.keyboardWillHideNotification
        )
        XCTAssertEqual(
            fixture.contentBottom,
            fixture.restingBottom,
            accuracy: 0.5
        )

        NotificationCenter.default.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        XCTAssertEqual(
            fixture.contentBottom,
            fixture.restingBottom,
            accuracy: 0.5
        )
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
        child.scrollView.isScrollEnabled = true
        child.scrollView.bounces = true
        // WebKit keeps deriving the inset policy from viewport-fit only while
        // nothing outside it sets the behavior.
        let webKitInsetBehavior = primary.scrollView.contentInsetAdjustmentBehavior

        XCTAssertTrue(factory.setOuterScrollLocked(true, in: primary))
        XCTAssertFalse(primary.scrollView.isScrollEnabled)
        XCTAssertFalse(primary.scrollView.bounces)
        XCTAssertEqual(
            primary.scrollView.contentInsetAdjustmentBehavior,
            webKitInsetBehavior
        )
        XCTAssertEqual(primary.scrollView.contentOffset, .zero)
        XCTAssertTrue(child.scrollView.isScrollEnabled)

        primary.scrollView.setContentOffset(CGPoint(x: 0, y: 40), animated: false)
        factory.resetLockedContentOffsetsForKeyboardChange()
        XCTAssertEqual(primary.scrollView.contentOffset, .zero)

        XCTAssertTrue(factory.setOuterScrollLocked(true, in: child))
        XCTAssertFalse(child.scrollView.isScrollEnabled)
        XCTAssertFalse(child.scrollView.bounces)

        factory.resetForNavigationCommit(in: child)
        XCTAssertFalse(primary.scrollView.isScrollEnabled)
        XCTAssertTrue(child.scrollView.isScrollEnabled)
        XCTAssertTrue(child.scrollView.bounces)

        factory.resetForNavigationCommit(in: primary)
        XCTAssertTrue(primary.scrollView.isScrollEnabled)
        XCTAssertTrue(primary.scrollView.bounces)
        XCTAssertEqual(
            primary.scrollView.contentInsetAdjustmentBehavior,
            webKitInsetBehavior
        )

        XCTAssertFalse(
            factory.setOuterScrollLocked(true, in: WKWebView(frame: .zero))
        )
    }

    @MainActor
    func testFactoryPinsThePrimaryToTheWindowEdgesAndAChildToTheSafeArea() async throws {
        let rootViewController = UIViewController()
        let window = try await makeHomeIndicatorWindow(
            rootViewController: rootViewController
        )
        defer {
            window.rootViewController = nil
            window.isHidden = true
        }
        let safeAreaTop = window.safeAreaInsets.top
        XCTAssertGreaterThan(
            safeAreaTop,
            0,
            "Top-edge checks need a device with a status bar inset."
        )
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
        let installations: [(WKWebView, WebViewExtent)] = [
            (primary, .container),
            (child, .safeArea),
        ]
        for (webView, extent) in installations {
            let containerView = UIView(frame: rootViewController.view.bounds)
            containerView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            rootViewController.view.addSubview(containerView)
            factory.install(webView, in: containerView, extent: extent)
        }
        let windowBottom = window.bounds.maxY
        let safeAreaBottom = windowBottom - window.safeAreaInsets.bottom

        XCTAssertEqual(top(of: primary, in: window), window.bounds.minY, accuracy: 0.5)
        XCTAssertEqual(top(of: child, in: window), safeAreaTop, accuracy: 0.5)
        XCTAssertEqual(bottom(of: primary, in: window), windowBottom, accuracy: 0.5)
        XCTAssertEqual(bottom(of: child, in: window), safeAreaBottom, accuracy: 0.5)

        let keyboardTop = safeAreaBottom - 291
        postKeyboardFrame(
            CGRect(
                x: 0,
                y: keyboardTop,
                width: window.bounds.width,
                height: windowBottom - keyboardTop
            ),
            in: window
        )
        XCTAssertEqual(bottom(of: primary, in: window), keyboardTop, accuracy: 0.5)
        XCTAssertEqual(bottom(of: child, in: window), keyboardTop, accuracy: 0.5)

        postKeyboardFrame(
            CGRect(
                x: 0,
                y: windowBottom,
                width: window.bounds.width,
                height: 291
            ),
            in: window,
            notification: UIResponder.keyboardWillHideNotification
        )
        XCTAssertEqual(bottom(of: primary, in: window), windowBottom, accuracy: 0.5)
        XCTAssertEqual(bottom(of: child, in: window), safeAreaBottom, accuracy: 0.5)
    }

    @MainActor
    func testWorkbenchPadsTheHomeIndicatorOnceAndOrdinaryDocumentsKeepTheAutomaticInset() async throws {
        let instanceServer = try LocalHTTPServer { request in
            switch request.path {
            case "/ordinary":
                return LocalHTTPResponse.html(
                    FullBleedFixture.ordinaryDocument(title: "Instance page")
                )
            default:
                return LocalHTTPResponse.html(FullBleedFixture.workbenchDocument)
            }
        }
        let authServer = try LocalHTTPServer { _ in
            LocalHTTPResponse.html(FullBleedFixture.ordinaryDocument(title: "Consent"))
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
        let controller = makeRuntimeController(
            instanceOrigin: instanceOrigin,
            navigationPolicy: NavigationPolicy(
                instanceOrigins: [instanceOrigin],
                authenticationOrigins: [authOrigin]
            )
        )
        let window = try await makeHomeIndicatorWindow(
            rootViewController: controller
        )
        defer {
            window.rootViewController = nil
            window.isHidden = true
        }
        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        let homeIndicatorInset = window.safeAreaInsets.bottom
        XCTAssertEqual(
            bottom(of: webView, in: window),
            window.bounds.maxY,
            accuracy: 0.5
        )

        // The document locks its outer scroll as the Workbench AppShell does.
        try await waitForTitle("Workbench", in: webView)
        // WebKit applies the cover declaration across separate updates: the
        // inset behavior, the initial containing block, the env() value, and
        // the fixed-position layout viewport. Wait for all of them together;
        // a layout viewport that never reaches the bottom still fails here.
        try await waitUntil {
            let padding = await self.paddingBottom(of: "#tab-bar", in: webView)
            let tabBarBottom = try? await webView.evaluateJavaScript(
                "document.querySelector('#tab-bar').getBoundingClientRect().bottom"
            ) as? Double
            return abs((padding ?? -1) - homeIndicatorInset) < 0.5
                && webView.scrollView.adjustedContentInset.bottom == 0
                && abs((tabBarBottom ?? -1) - Double(webView.bounds.height)) < 0.5
        }
        XCTAssertEqual(webView.scrollView.contentInsetAdjustmentBehavior, .never)
        let lockedResult = try await webView.evaluateJavaScript(
            """
            JSON.stringify({
              tabBarBottom: document.querySelector("#tab-bar")
                .getBoundingClientRect().bottom,
              viewport: document.querySelector('meta[name="viewport"]')
                .getAttribute("content")
            })
            """
        )
        let locked = try jsonObject(from: lockedResult)
        // The tab bar's own background reaches the screen bottom.
        XCTAssertEqual(
            try XCTUnwrap(locked["tabBarBottom"] as? Double),
            Double(webView.bounds.height),
            accuracy: 0.5
        )
        XCTAssertEqual(
            locked["viewport"] as? String,
            FullBleedFixture.workbenchViewport + ", viewport-fit=cover"
        )

        // Unmounting the AppShell returns the same document to its declaration.
        let unlockedViewport = try await webView.callAsyncJavaScript(
            """
            await window.avibeNative.call(
              "viewport.setOuterScrollLocked",
              {locked: false}
            );
            return document.querySelector('meta[name="viewport"]')
              .getAttribute("content");
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        ) as? String
        XCTAssertEqual(unlockedViewport, FullBleedFixture.workbenchViewport)
        try await waitForAutomaticBottomInset(
            homeIndicatorInset,
            probe: "#tab-bar",
            in: webView
        )

        // A remounted AppShell locks again; leaving the document releases it.
        _ = try await webView.callAsyncJavaScript(
            """
            await window.avibeNative.call(
              "viewport.setOuterScrollLocked",
              {locked: true}
            );
            return true;
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        try await waitUntil {
            webView.scrollView.adjustedContentInset.bottom == 0
        }
        webView.load(
            URLRequest(url: authBaseURL.appendingPathComponent("consent"))
        )
        try await waitForTitle("Consent", in: webView)
        try await waitForAutomaticBottomInset(
            homeIndicatorInset,
            probe: "#last",
            in: webView
        )

        webView.load(
            URLRequest(url: instanceBaseURL.appendingPathComponent("ordinary"))
        )
        try await waitForTitle("Instance page", in: webView)
        try await waitForAutomaticBottomInset(
            homeIndicatorInset,
            probe: "#last",
            in: webView
        )
    }

    @MainActor
    func testWorkbenchDrawsUnderTheStatusBarAndChildWindowsKeepTheSafeArea() async throws {
        let instanceServer = try LocalHTTPServer { request in
            switch request.path {
            case "/child":
                return LocalHTTPResponse.html(
                    FullBleedFixture.ordinaryDocument(title: "Child")
                )
            default:
                return LocalHTTPResponse.html(
                    FullBleedFixture.topEdgeWorkbenchDocument
                )
            }
        }
        let authServer = try LocalHTTPServer { _ in
            LocalHTTPResponse.html(FullBleedFixture.ordinaryDocument(title: "Consent"))
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
        let controller = makeRuntimeController(
            instanceOrigin: instanceOrigin,
            navigationPolicy: NavigationPolicy(
                instanceOrigins: [instanceOrigin],
                authenticationOrigins: [authOrigin]
            )
        )
        let window = try await makeHomeIndicatorWindow(
            rootViewController: controller
        )
        defer {
            window.rootViewController = nil
            window.isHidden = true
        }
        let statusBarInset = window.safeAreaInsets.top
        XCTAssertGreaterThan(
            statusBarInset,
            0,
            "Top-edge checks need a device with a status bar inset."
        )
        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        XCTAssertEqual(top(of: webView, in: window), window.bounds.minY, accuracy: 0.5)

        // The locked AppShell pads the status bar once, through env().
        try await waitForTitle("Workbench", in: webView)
        try await waitUntil {
            let padding = await self.padding(.top, of: "#app-header", in: webView)
            let headerTop = try? await webView.evaluateJavaScript(
                "document.querySelector('#app-header').getBoundingClientRect().top"
            ) as? Double
            return abs((padding ?? -1) - statusBarInset) < 0.5
                && webView.scrollView.adjustedContentInset.top == 0
                && headerTop.map { abs($0) < 0.5 } == true
        }

        // A child window keeps the safe area, here below its navigation bar,
        // and cannot drive the status bar.
        _ = try await webView.evaluateJavaScript(
            "document.querySelector('#open-child').click()"
        )
        var popupNavigation: UINavigationController?
        try await waitUntil {
            popupNavigation = controller.presentedViewController
                as? UINavigationController
            return popupNavigation != nil
        }
        let popupController = try XCTUnwrap(popupNavigation?.topViewController)
        var childWebView: WKWebView?
        try await waitUntil {
            guard
                let popupView = popupController.viewIfLoaded,
                popupView.window != nil,
                let child = popupView.subviews.compactMap({ $0 as? WKWebView }).first
            else {
                return false
            }
            popupView.layoutIfNeeded()
            childWebView = child
            let safeAreaTop = popupView.safeAreaLayoutGuide.layoutFrame.minY
            return safeAreaTop > 0 && abs(child.frame.minY - safeAreaTop) < 0.5
        }
        let child = try XCTUnwrap(childWebView)
        XCTAssertEqual(
            child.frame.minY,
            popupController.view.safeAreaLayoutGuide.layoutFrame.minY,
            accuracy: 0.5
        )
        try await waitForTitle("Child", in: child)
        let childSchemeResult = try await child.callAsyncJavaScript(
            """
            try {
              await window.avibeNative.call(
                "appearance.colorSchemeChanged",
                {scheme: "dark"}
              );
              return "accepted";
            } catch (error) {
              return error.code;
            }
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        ) as? String
        XCTAssertEqual(childSchemeResult, "forbidden")
        XCTAssertNil(controller.pageColorScheme)
        popupNavigation?.dismiss(animated: false)
        try await waitUntil {
            controller.presentedViewController == nil
        }

        // An unlocked document keeps WebKit's automatic top inset.
        webView.load(
            URLRequest(url: authBaseURL.appendingPathComponent("consent"))
        )
        try await waitForTitle("Consent", in: webView)
        try await waitUntil {
            let padding = await self.padding(.top, of: "#first", in: webView)
            return padding == 0
                && abs(
                    webView.scrollView.adjustedContentInset.top - statusBarInset
                ) < 0.5
        }
    }

    /// The owner's report: an explicit Web theme that differs from the
    /// system. The status bar text follows what the page draws under it,
    /// live, while the WebView keeps the system appearance, so
    /// `prefers-color-scheme` stays the system's and a page that follows the
    /// system can still follow it.
    @MainActor
    func testPageSchemeDrivesTheStatusBarWhileTheWebViewKeepsTheSystemAppearance() async throws {
        let server = try LocalHTTPServer { _ in
            LocalHTTPResponse.html(FullBleedFixture.themedDocument)
        }
        let baseURL = try await server.start()
        defer {
            server.stop()
        }
        let instanceOrigin = try XCTUnwrap(
            WebOrigin(url: baseURL, requiresHTTPS: false)
        )
        let scene = try XCTUnwrap(
            UIApplication.shared.connectedScenes
                .compactMap { $0 as? UIWindowScene }
                .first
        )
        // The scene's own appearance stands in for the system setting.
        scene.traitOverrides.userInterfaceStyle = .dark
        defer {
            scene.traitOverrides.remove(UITraitUserInterfaceStyle.self)
        }
        var runtimeController: WebViewController?
        var reportedSchemes: [PageColorScheme?] = []
        let hostingController = UIHostingController(
            rootView: WebViewContainer(
                instanceOrigin: instanceOrigin,
                websiteDataStore: .nonPersistent(),
                showAccount: {},
                accountFallbackVisibilityChanged: { _ in },
                pageColorSchemeChanged: { reportedSchemes.append($0) },
                didCreateController: { runtimeController = $0 },
                didReleaseWebViews: { _ in },
                didStartLoading: {},
                didFinishLoading: {},
                didFailLoading: {}
            )
            .ignoresSafeArea()
        )
        let window = try await makeHomeIndicatorWindow(
            rootViewController: hostingController
        )
        defer {
            _ = runtimeController?.invalidateForAccountTransition()
            window.rootViewController = nil
            window.isHidden = true
        }
        let controller = try XCTUnwrap(runtimeController)
        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        try await waitForTitle("Themed", in: webView)

        func expectStatusBar(
            page scheme: PageColorScheme,
            style: UIStatusBarStyle,
            systemDark: Bool,
            line: UInt = #line
        ) async throws {
            let systemStyle: UIUserInterfaceStyle = systemDark ? .dark : .light
            try await waitUntil {
                let prefersDark = try? await webView.evaluateJavaScript(
                    "matchMedia('(prefers-color-scheme: dark)').matches"
                ) as? Bool
                return controller.pageColorScheme == scheme
                    && scene.statusBarManager?.statusBarStyle == style
                    && webView.traitCollection.userInterfaceStyle == systemStyle
                    && prefersDark == systemDark
            }
            XCTAssertTrue(hostingController.childForStatusBarStyle === controller, line: line)
            XCTAssertEqual(controller.preferredStatusBarStyle, style, line: line)
            XCTAssertEqual(reportedSchemes.last ?? nil, scheme, line: line)
            // Nothing overrides the WebView's appearance.
            XCTAssertEqual(controller.overrideUserInterfaceStyle, .unspecified, line: line)
            XCTAssertEqual(webView.overrideUserInterfaceStyle, .unspecified, line: line)
            XCTAssertFalse(
                controller.traitOverrides.contains(UITraitUserInterfaceStyle.self),
                line: line
            )
        }
        func setTheme(_ theme: String?) async throws {
            _ = try await webView.callAsyncJavaScript(
                """
                if (theme === null) {
                  document.documentElement.removeAttribute("data-theme");
                } else {
                  document.documentElement.setAttribute("data-theme", theme);
                }
                return true;
                """,
                arguments: ["theme": theme ?? NSNull()],
                in: nil,
                contentWorld: .page
            )
        }

        // Explicit light theme while the system is dark.
        try await setTheme("light")
        try await expectStatusBar(page: .light, style: .darkContent, systemDark: true)
        // A live switch to the dark theme.
        try await setTheme("dark")
        try await expectStatusBar(page: .dark, style: .lightContent, systemDark: true)
        // The system turns light under an explicit dark theme.
        scene.traitOverrides.userInterfaceStyle = .light
        try await expectStatusBar(page: .dark, style: .lightContent, systemDark: false)
        // A live switch to the light theme.
        try await setTheme("light")
        try await expectStatusBar(page: .light, style: .darkContent, systemDark: false)
        // The system theme follows live system changes both ways.
        try await setTheme(nil)
        try await expectStatusBar(page: .light, style: .darkContent, systemDark: false)
        scene.traitOverrides.userInterfaceStyle = .dark
        try await expectStatusBar(page: .dark, style: .lightContent, systemDark: true)
        scene.traitOverrides.userInterfaceStyle = .light
        try await expectStatusBar(page: .light, style: .darkContent, systemDark: false)

        // A layer that is dark in every theme, such as the image viewer,
        // declares its scheme; while it covers the top the text turns light,
        // and it returns to the theme's when the layer leaves.
        try await setTheme("light")
        _ = try await webView.evaluateJavaScript(
            """
            (() => {
              const layer = document.createElement("div");
              layer.id = "dark-layer";
              layer.style.cssText =
                "position: fixed; inset: 0; color-scheme: dark; background: rgb(0 0 0 / 0.8)";
              document.body.append(layer);
              return true;
            })()
            """
        )
        try await expectStatusBar(page: .dark, style: .lightContent, systemDark: false)
        _ = try await webView.evaluateJavaScript(
            "document.querySelector('#dark-layer').remove(); true"
        )
        try await expectStatusBar(page: .light, style: .darkContent, systemDark: false)
    }

    @MainActor
    func testBrowserRouteLetsThePrimaryWebViewReachTheWindowTopAndBottom() async throws {
        let server = try LocalHTTPServer { request in
            switch (request.method, request.path) {
            case ("GET", "/api/config/auth"):
                return .json("{}")
            case ("GET", "/api/mobile/session"):
                return .json(
                    """
                    {
                      "account": {
                        "id": "account",
                        "email": "max@example.com"
                      },
                      "instances": [
                        {
                          "id": "instance",
                          "name": "Unreachable",
                          "host": "full-bleed.invalid",
                          "url": "https://full-bleed.invalid",
                          "relationship": "owner",
                          "owner_email": "max@example.com",
                          "available": true
                        }
                      ]
                    }
                    """
                )
            default:
                return .status(404)
            }
        }
        let baseURL = try await server.start()
        addTeardownBlock {
            server.stop()
        }
        let suiteName = "NavigationPolicyTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        addTeardownBlock {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let storeCleanup = NamedDataStoreCleanup()
        addTeardownBlock {
            _ = await storeCleanup.remove()
        }

        let appModel = makeTestAppModel(
            defaults: defaults,
            controlPlane: MobileControlPlaneConfiguration(
                testBaseURL: baseURL
            )
        )
        storeCleanup.identifier = appModel.websiteDataStore.identifier
        try await waitUntil {
            (
                try? appModel.mobileSessionCoordinator.selectInstance(
                    id: "instance"
                )
            ) != nil
        }
        let hostingController = UIHostingController(
            rootView: RootView().environmentObject(appModel)
        )
        let window = try await makeHomeIndicatorWindow(
            rootViewController: hostingController
        )
        defer {
            // The app never destroys its root hosting view, so release the
            // runtime as an account transition does first. Dismantling a live
            // runtime inside SwiftUI's graph teardown writes BrowserScreen
            // state during that teardown.
            if let runtimeController = primaryRuntimeWebView(
                in: hostingController.view
            )?.superview?.next as? WebViewController {
                appModel.recordRuntimeReleaseBarrier(
                    runtimeController.invalidateForAccountTransition()
                )
            }
            window.rootViewController = nil
            window.isHidden = true
        }

        var runtimeWebView: WKWebView?
        try await waitUntil {
            runtimeWebView = self.primaryRuntimeWebView(in: hostingController.view)
            return runtimeWebView != nil
        }
        let webView = try XCTUnwrap(runtimeWebView)
        try await waitUntil {
            abs(self.top(of: webView, in: window) - window.bounds.minY) < 0.5
                && abs(self.bottom(of: webView, in: window) - window.bounds.maxY) < 0.5
        }
        XCTAssertGreaterThan(window.safeAreaInsets.top, 0)
        XCTAssertEqual(
            top(of: webView, in: window),
            window.bounds.minY,
            accuracy: 0.5
        )
        XCTAssertEqual(
            bottom(of: webView, in: window),
            window.bounds.maxY,
            accuracy: 0.5
        )
    }

    @MainActor
    func testColdLaunchPushTapWaitsForAccountBootstrap() async throws {
        let sessionHeaders = FixtureEvent(description: "session response headers")
        let sessionRelease = DispatchSemaphore(value: 0)
        let server = try LocalHTTPServer { request in
            switch (request.method, request.path) {
            case ("GET", "/api/config/auth"):
                return .json("{}")
            case ("GET", "/api/mobile/session"):
                let body = """
                {
                  "account": {
                    "id": "account-1",
                    "email": "max@example.com"
                  },
                  "instances": [
                    {
                      "id": "instance-1",
                      "name": "Push target",
                      "host": "instance.example",
                      "url": "https://instance.example",
                      "relationship": "owner",
                      "owner_email": "max@example.com",
                      "available": true
                    }
                  ]
                }
                """
                return LocalHTTPResponse(
                    status: 200,
                    headers: [("Content-Type", "application/json")],
                    body: Data(body.utf8),
                    headersSentEvent: sessionHeaders,
                    bodyRelease: sessionRelease
                )
            default:
                return .status(404)
            }
        }
        let baseURL = try await server.start()
        addTeardownBlock {
            sessionRelease.signal()
            server.stop()
        }
        let suiteName = "NavigationPolicyTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        addTeardownBlock {
            defaults.removePersistentDomain(forName: suiteName)
        }

        let appModel = makeTestAppModel(
            defaults: defaults,
            controlPlane: MobileControlPlaneConfiguration(
                testBaseURL: baseURL
            )
        )
        await fulfillment(of: [sessionHeaders.expectation], timeout: 15)

        NotificationCenter.default.post(
            name: .avibePushNotificationTapped,
            object: nil,
            userInfo: [
                "payload": [
                    "account_id": "account-1",
                    "instance_origin": "https://instance.example",
                    "url": "/chat/ses_1?from=notification",
                ],
            ]
        )
        sessionRelease.signal()

        try await waitUntil {
            guard case .browser(let runtime) = appModel.route else {
                return false
            }
            return runtime.instance.id == "instance-1"
        }
    }

    /// SwiftUI's root hosting controller hands the status bar to its newest
    /// representable controller. The bundled sign-in page follows the system
    /// theme, and so does its status bar; over a runtime, the runtime
    /// controller owns it until Account covers that runtime.
    @MainActor
    func testRootViewHandsTheStatusBarToTheSurfaceOnTop() async throws {
        let server = try LocalHTTPServer { request in
            switch (request.method, request.path) {
            case ("GET", "/api/config/auth"):
                return .json("{}")
            case ("GET", "/api/mobile/session"):
                return .json(
                    """
                    {
                      "account": {
                        "id": "account",
                        "email": "max@example.com"
                      },
                      "instances": [
                        {
                          "id": "instance",
                          "name": "Unreachable",
                          "host": "status-bar.invalid",
                          "url": "https://status-bar.invalid",
                          "relationship": "owner",
                          "owner_email": "max@example.com",
                          "available": true
                        }
                      ]
                    }
                    """
                )
            default:
                return .status(404)
            }
        }
        let baseURL = try await server.start()
        addTeardownBlock {
            server.stop()
        }
        let suiteName = "NavigationPolicyTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        addTeardownBlock {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let storeCleanup = NamedDataStoreCleanup()
        addTeardownBlock {
            _ = await storeCleanup.remove()
        }
        let appModel = makeTestAppModel(
            defaults: defaults,
            controlPlane: MobileControlPlaneConfiguration(
                testBaseURL: baseURL
            )
        )
        storeCleanup.identifier = appModel.websiteDataStore.identifier
        let hostingController = UIHostingController(
            rootView: RootView().environmentObject(appModel)
        )
        let window = try await makeHomeIndicatorWindow(
            rootViewController: hostingController
        )
        let scene = try XCTUnwrap(window.windowScene)
        defer {
            scene.traitOverrides.remove(UITraitUserInterfaceStyle.self)
            if let runtimeController = primaryRuntimeWebView(
                in: hostingController.view
            )?.superview?.next as? WebViewController {
                appModel.recordRuntimeReleaseBarrier(
                    runtimeController.invalidateForAccountTransition()
                )
            }
            window.rootViewController = nil
            window.isHidden = true
        }

        // Sign-in: the bundled page and the status bar follow the system.
        for style in [UIUserInterfaceStyle.dark, .light] {
            scene.traitOverrides.userInterfaceStyle = style
            try await waitUntil {
                guard
                    let onboarding = hostingController.childForStatusBarStyle
                        as? MobileOnboardingViewController,
                    let page = onboarding.viewIfLoaded?.subviews
                        .compactMap({ $0 as? WKWebView }).first
                else {
                    return false
                }
                return onboarding.preferredStatusBarStyle == .default
                    && scene.statusBarManager?.statusBarStyle == .default
                    && page.traitCollection.userInterfaceStyle == style
                    && onboarding.overrideUserInterfaceStyle == .unspecified
            }
        }

        // A runtime owns the status bar; its page has not reported a scheme.
        try await waitUntil {
            (
                try? appModel.mobileSessionCoordinator.selectInstance(
                    id: "instance"
                )
            ) != nil
        }
        var runtime: WebViewController?
        try await waitUntil {
            runtime = hostingController.childForStatusBarStyle
                as? WebViewController
            return runtime != nil
        }
        let runtimeController = try XCTUnwrap(runtime)
        XCTAssertNil(runtimeController.pageColorScheme)
        XCTAssertEqual(runtimeController.preferredStatusBarStyle, .default)

        // Account covers the runtime in a native sheet and takes the status
        // bar, then hands it back to the same runtime.
        appModel.showAccount()
        var coveredOnboarding: MobileOnboardingViewController?
        try await waitUntil {
            coveredOnboarding = self.mobileOnboardingController(
                in: hostingController
            )
            return coveredOnboarding != nil
        }
        XCTAssertEqual(coveredOnboarding?.preferredStatusBarStyle, .default)
        XCTAssertTrue(appModel.route.coversRuntime)
        XCTAssertTrue(appModel.returnToRuntime())
        try await waitUntil {
            hostingController.presentedViewController == nil
                && hostingController.childForStatusBarStyle === runtimeController
        }

        // Consume B through the actual SwiftUI root, not just AppModel's mode.
        appModel.showInstances()
        var namedSheet: MobileOnboardingViewController?
        try await waitUntil {
            namedSheet = self.mobileOnboardingController(in: hostingController)
            let page = namedSheet?.viewIfLoaded?.subviews.compactMap { $0 as? WKWebView }.first
            return page?.url?.fragment == "sheet-instances"
        }
        appModel.showInstances()
        appModel.showAccount()
        XCTAssertTrue(self.mobileOnboardingController(in: hostingController) === namedSheet)
        XCTAssertTrue(appModel.returnToRuntime())
        try await waitUntil {
            hostingController.presentedViewController == nil
                && hostingController.childForStatusBarStyle === runtimeController
        }
        // Consume the parameterized call through RootView's real Runtime
        // bridge wiring, including factory/container callbacks.
        let runtimePage = try XCTUnwrap(runtimeController.livePrimaryWebView)
        let rootProbe = WebViewLoadProbe()
        runtimePage.navigationDelegate = rootProbe
        try await rootProbe.loadHTML(
            "<!doctype html><title>Selected runtime</title>",
            baseURL: URL(string: "https://status-bar.invalid")!, in: runtimePage
        )
        runtimePage.navigationDelegate = runtimeController
        let accountRootResult = try await runtimePage.callAsyncJavaScript(
            "return JSON.stringify(await window.avibeNative.call('app.showAccount', {start: 'account'}));",
            arguments: [:], in: nil, contentWorld: .page
        )
        XCTAssertEqual(accountRootResult as? String, "null")
        var accountRoot: MobileOnboardingViewController?
        try await waitUntil {
            accountRoot = self.mobileOnboardingController(in: hostingController)
            let page = accountRoot?.viewIfLoaded?.subviews.compactMap { $0 as? WKWebView }.first
            return page?.url?.fragment == "sheet-account"
        }
        appModel.showAccount()
        appModel.showInstances()
        appModel.showAccountRoot()
        XCTAssertTrue(self.mobileOnboardingController(in: hostingController) === accountRoot)
        XCTAssertTrue(appModel.returnToRuntime())
        try await waitUntil {
            hostingController.presentedViewController == nil
                && hostingController.childForStatusBarStyle === runtimeController
        }
    }

    @MainActor
    func testAppInfoWithholdsIdentityUntilTheEnvelopeCommits() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("I3AppInfo.\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        var failing = true
        let store = FileSystemSystemScheduleStore(fileURL: directory.appendingPathComponent("ledger.json"),
            fileIO: FaultingSystemScheduleFileIO(checkpoint: { if failing && $0 == .beforeDirectorySync { throw CocoaError(.fileWriteUnknown) } }))
        let webView = WKWebView(frame: .zero, configuration: NativeBridgeConfiguration.make(
            instanceOrigin: instance, setOuterScrollLocked: { _, _ in false },
            deviceID: { store.installationID }))
        let loader = WebViewLoadProbe()
        webView.navigationDelegate = loader
        try await loader.loadHTML("<!doctype html><title>Identity</title>", baseURL: instance.url, in: webView)
        func readID() async throws -> String? {
            try await webView.callAsyncJavaScript(
                "return (await window.avibeNative.call('app.info')).device?.id ?? null;",
                arguments: [:], in: nil, contentWorld: .page) as? String
        }
        let uncommitted = try await readID()
        XCTAssertNil(uncommitted)
        failing = false
        XCTAssertTrue(store.retryCommit())
        let committed = try await readID()
        XCTAssertEqual(committed, store.installationID)
        XCTAssertNotNil(committed)
    }

    @MainActor
    func testNativeBridgeIsDiscoverableOnlyInTheInstanceMainFrame() async throws {
        let metadata = NativeShellMetadata(
            shellVersion: "1.2.3 (45)",
            userAgentVersion: "1.2.3"
        )
        var outerScrollRequests: [Bool] = []
        var accountActionRequests: [Bool] = []
        var historyChangeRequests = 0
        weak var primaryWebView: WKWebView?
        let accountShown = expectation(description: "runtime bridge opens account surface")
        let instancesShown = expectation(description: "runtime bridge opens named instances surface")
        let accountRootShown = expectation(description: "runtime bridge opens Account root")
        var accountRootRequestCount = 0
        var nativeName: String? = "Studio"
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
                historyDidChange: { sourceWebView in
                    guard sourceWebView === primaryWebView else {
                        return false
                    }
                    historyChangeRequests += 1
                    return true
                },
                showAccount: {
                    accountShown.fulfill()
                },
                showAccountRoot: {
                    accountRootRequestCount += 1
                    accountRootShown.fulfill()
                },
                showInstances: {
                    instancesShown.fulfill()
                },
                instanceName: { nativeName },
                instanceID: { "instance-1" },
                deviceID: { "device-1" }
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
            await window.avibeNative.call("navigation.historyChanged");
            let invalidHistory = null;
            try {
              await window.avibeNative.call(
                "navigation.historyChanged",
                {url: window.location.href}
              );
            } catch (error) {
              invalidHistory = error.code;
            }
            const legacyAccountResult = await window.avibeNative.call("app.showAccount");
            const invalidAccountArguments = [];
            for (const params of [
              {start: "instances"}, {start: "Account"}, {start: ""},
              {start: null}, {start: true}, {start: 1},
              {start: ["account"]}, {start: {page: "account"}},
              {page: "account"}, {start: "account", extra: true}
            ]) {
              try {
                await window.avibeNative.call("app.showAccount", params);
                invalidAccountArguments.push("accepted");
              } catch (error) {
                invalidAccountArguments.push(error.code);
              }
            }
            const accountRootResult = await window.avibeNative.call("app.showAccount", {start: "account"});
            let invalidInstances = null;
            try {
              await window.avibeNative.call("app.showInstances", {url: "https://example.com"});
            } catch (error) {
              invalidInstances = error.code;
            }
            const instancesResult = await window.avibeNative.call("app.showInstances");
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
              instancePresentationCapability: info.capabilities.instancePresentation,
              instanceInfo: info.instance,
              deviceInfo: info.device,
              legacyAccountResult,
              accountRootResult,
              invalidAccountArguments,
              instancesResult,
              invalidInstances,
              appCapability: info.capabilities.app,
              keyboardResizesViewport: info.capabilities.keyboardResizesViewport,
              viewportCapability: info.capabilities.viewport,
              subframeBridge: typeof document.querySelector("#child").contentWindow.avibeNative,
              subframeTransport: await document.querySelector("#child").contentWindow.webkit
                .messageHandlers.avibeNative.postMessage({method: "app.info", params: {}}),
              invalidHistory,
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
        XCTAssertEqual(instancePayload["instancePresentationCapability"] as? Int, 1)
        XCTAssertEqual(
            instancePayload["instanceInfo"] as? [String: String],
            ["id": "instance-1", "name": "Studio"]
        )
        XCTAssertEqual(
            instancePayload["deviceInfo"] as? [String: String],
            ["id": "device-1"]
        )
        XCTAssertTrue(instancePayload["legacyAccountResult"] is NSNull)
        XCTAssertTrue(instancePayload["accountRootResult"] is NSNull)
        XCTAssertEqual(instancePayload["invalidAccountArguments"] as? [String],
                       Array(repeating: "invalid_argument", count: 10))
        XCTAssertTrue(instancePayload["instancesResult"] is NSNull)
        XCTAssertEqual(instancePayload["invalidInstances"] as? String, "invalid_argument")
        XCTAssertEqual(instancePayload["appCapability"] as? Int, 1)
        XCTAssertEqual(instancePayload["keyboardResizesViewport"] as? Int, 1)
        XCTAssertEqual(instancePayload["viewportCapability"] as? Int, 1)
        XCTAssertEqual(outerScrollRequests, [true])
        XCTAssertEqual(accountActionRequests, [true])
        XCTAssertEqual(historyChangeRequests, 1)
        XCTAssertEqual(instancePayload["subframeBridge"] as? String, "undefined")
        XCTAssertEqual(instancePayload["invalidHistory"] as? String, "invalid_argument")
        XCTAssertEqual(instancePayload["unsupported"] as? String, "unsupported")
        assertForbiddenReply(instancePayload["subframeTransport"])
        await fulfillment(of: [accountShown, accountRootShown, instancesShown], timeout: 2)
        XCTAssertEqual(accountRootRequestCount, 1)

        // The Runtime reads app.info on document load; no rename event or
        // caller-supplied identity is involved. Empty/missing names are safe.
        for (name, expected) in [("Renamed Studio" as String?, "Renamed Studio"), (nil, "Avibe"), (" \n", "Avibe")] {
            nativeName = name
            try await probe.loadHTML("<!doctype html><title>Reloaded</title>", baseURL: instance.url, in: webView)
            let info = try await webView.callAsyncJavaScript(
                "return JSON.stringify((await window.avibeNative.call('app.info')).instance);",
                arguments: [:], in: nil, contentWorld: .page
            )
            XCTAssertEqual(
                try jsonObject(from: info) as? [String: String],
                ["id": "instance-1", "name": expected]
            )
        }

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
            let history = null;
            try {
              await window.avibeNative.call("navigation.historyChanged");
            } catch (error) {
              history = error.code;
            }
            return JSON.stringify({
              bridge: typeof window.avibeNative,
              keyboardResizesViewport: info.capabilities.keyboardResizesViewport,
              registration,
              history
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
        XCTAssertEqual(childPayload["history"] as? String, "forbidden")
        XCTAssertEqual(historyChangeRequests, 1)
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

        let returnedURL = try XCTUnwrap(webView.url)
        _ = try await webView.evaluateJavaScript(
            "history.pushState({idx: 3}, '', window.location.href)"
        )
        try await waitUntil {
            webView.allowsBackForwardNavigationGestures
                && WebOrigin(
                    url: webView.backForwardList.backItem?.url
                        ?? URL(fileURLWithPath: "/"),
                    requiresHTTPS: false
                ) == instanceOrigin
        }
        XCTAssertEqual(webView.url, returnedURL)
    }

    @MainActor
    func testAccountBridgeTransitionDoesNotRecreateReleasedRuntimeWebView() async throws {
        for (method, params) in [
            ("app.showAccount", "{}"),
            ("app.showAccount", #"{"start":"account"}"#),
            ("app.showInstances", "{}"),
        ] {
            try await exerciseReleasedRuntimeSurfaceCallback(method, params: params)
        }
    }

    @MainActor
    private func exerciseReleasedRuntimeSurfaceCallback(_ method: String, params: String) async throws {
        let accountShown = expectation(description: "runtime surface bridge requested")
        var accountRequestCount = 0
        var loadingStarted = 0
        var loadingFinished = 0
        var loadingFailed = 0
        var controller: WebViewController!
        let showSurface: () -> Void = {
            accountRequestCount += 1
            accountShown.fulfill()
            _ = controller.invalidateForAccountTransition()
        }
        controller = WebViewController(
            instanceOrigin: instance,
            websiteDataStore: .nonPersistent(),
            showAccount: showSurface,
            showAccountRoot: showSurface,
            showInstances: showSurface,
            didStartLoading: {
                loadingStarted += 1
            },
            didFinishLoading: {
                loadingFinished += 1
            },
            didFailLoading: {
                loadingFailed += 1
            }
        )
        let window = host(controller)
        defer {
            window.isHidden = true
        }
        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        let probe = WebViewLoadProbe()
        webView.navigationDelegate = probe
        try await probe.loadHTML(
            """
            <!doctype html>
            <button id="account">Account</button>
            <script>
              document.querySelector('#account').addEventListener('click', () => {
                window.avibeNative.call('\(method)', \(params));
              });
            </script>
            """,
            baseURL: instance.url,
            in: webView
        )

        _ = try await webView.callAsyncJavaScript(
            "document.querySelector('#account').click()",
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        await fulfillment(of: [accountShown], timeout: 2)

        let lateBridgeResult = try await webView.callAsyncJavaScript(
            """
            const result = {};
            await window.avibeNative.call("\(method)", \(params));
            try {
              await window.avibeNative.call(
                "app.setAccountActionAvailable",
                {available: true}
              );
            } catch (error) {
              result.registration = error.code;
            }
            try {
              await window.avibeNative.call("navigation.historyChanged");
            } catch (error) {
              result.history = error.code;
            }
            return JSON.stringify(result);
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        let lateBridgePayload = try jsonObject(from: lateBridgeResult)
        XCTAssertEqual(lateBridgePayload["registration"] as? String, "forbidden")
        XCTAssertEqual(lateBridgePayload["history"] as? String, "forbidden")
        XCTAssertEqual(accountRequestCount, 1)

        let cancelled = NSError(
            domain: NSURLErrorDomain,
            code: NSURLErrorCancelled
        )
        controller.webView(
            webView,
            didStartProvisionalNavigation: nil
        )
        controller.webView(
            webView,
            didCommit: nil
        )
        controller.webView(
            webView,
            didFinish: nil
        )
        controller.webView(
            webView,
            didFailProvisionalNavigation: nil,
            withError: cancelled
        )
        controller.webView(
            webView,
            didFail: nil,
            withError: cancelled
        )
        controller.webViewWebContentProcessDidTerminate(webView)
        controller.webViewDidClose(webView)
        XCTAssertEqual(loadingStarted, 0)
        XCTAssertEqual(loadingFinished, 0)
        XCTAssertEqual(loadingFailed, 0)
    }

    @MainActor
    func testAccountCoversTheRuntimeUntilASwitchOrLogoutReleasesIt() async throws {
        let server = try LocalHTTPServer { request in
            switch (request.method, request.path) {
            case ("GET", "/api/config/auth"):
                return .json("{}")
            case ("GET", "/api/mobile/session"):
                return .json(
                    """
                    {
                      "account": {
                        "id": "account",
                        "email": "max@example.com"
                      },
                      "instances": [
                        {
                          "id": "instance",
                          "name": "Alice",
                          "host": "alice-app.avibe.bot",
                          "url": "https://alice-app.avibe.bot",
                          "relationship": "owner",
                          "owner_email": "max@example.com",
                          "available": true
                        },
                        {
                          "id": "instance-2",
                          "name": "Bob",
                          "host": "bob-app.avibe.bot",
                          "url": "https://bob-app.avibe.bot",
                          "relationship": "owner",
                          "owner_email": "max@example.com",
                          "available": true
                        }
                      ]
                    }
                    """
                )
            default:
                return .status(404)
            }
        }
        let baseURL = try await server.start()
        addTeardownBlock {
            server.stop()
        }
        let suiteName = "NavigationPolicyTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        addTeardownBlock {
            defaults.removePersistentDomain(forName: suiteName)
        }

        // The launch store, and the fresh store sign-out rotates to.
        let storeCleanups = [NamedDataStoreCleanup(), NamedDataStoreCleanup()]
        addTeardownBlock {
            for storeCleanup in storeCleanups {
                _ = await storeCleanup.remove()
            }
        }

        try await exerciseAppModelAccountTransition(
            defaults: defaults,
            controlPlaneURL: baseURL,
            storeCleanups: storeCleanups,
            requests: server.requests
        )
        for storeCleanup in storeCleanups {
            let removalError = await storeCleanup.remove()
            XCTAssertNil(removalError)
        }
    }

    @MainActor
    func testInvalidatedRuntimeCancelsAccountFallbackWork() async throws {
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
        let probe = WebViewLoadProbe()
        webView.navigationDelegate = probe
        try await probe.loadHTML(
            "<!doctype html><title>Workbench</title>",
            baseURL: instance.url,
            in: webView
        )

        controller.webView(webView, didFinish: nil)
        _ = controller.invalidateForAccountTransition()
        try await Task.sleep(for: .milliseconds(500))

        XCTAssertFalse(visibility.contains(true))
    }

    @MainActor
    func testInvalidatedRuntimeIgnoresRealLateWebKitUIDelegateCallbacks() async throws {
        let controller = WebViewController(
            instanceOrigin: instance,
            websiteDataStore: .nonPersistent(),
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
        _ = controller.invalidateForAccountTransition()

        let probe = WebViewLoadProbe()
        webView.navigationDelegate = probe
        webView.uiDelegate = controller
        try await probe.loadHTML(
            """
            <!doctype html>
            <button id="callbacks" onclick="runCallbacks()">Run</button>
            <script>
              function runCallbacks() {
                alert("late");
                confirm("late");
                prompt("late", "default");
                window.open("about:blank", "_blank");
              }
            </script>
            """,
            baseURL: instance.url,
            in: webView
        )

        _ = try await webView.callAsyncJavaScript(
            "document.querySelector('#callbacks').click()",
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
    }

    @MainActor
    func testInvalidatedRuntimeCancelsDownloadsAndRemovesTemporaryDestinations() async throws {
        let headersSent = FixtureEvent(
            description: "download response prefix sent"
        )
        let releaseBody = DispatchSemaphore(value: 0)
        let server = try LocalHTTPServer { request in
            if request.path != "/account-export.txt" {
                return .html(
                    "<!doctype html><title>Download fixture</title>"
                )
            }
            return LocalHTTPResponse(
                status: 200,
                headers: [
                    ("Content-Type", "application/octet-stream"),
                    (
                        "Content-Disposition",
                        "attachment; filename=\"account-export.txt\""
                    ),
                ],
                body: Data(repeating: 0x41, count: 1_048_576),
                headersSentEvent: headersSent,
                bodyRelease: releaseBody
            )
        }
        let baseURL = try await server.start()
        defer {
            releaseBody.signal()
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
        let responseProbe = NavigationResponseProbe()
        webView.navigationDelegate = responseProbe
        webView.load(URLRequest(url: baseURL))
        await fulfillment(
            of: [responseProbe.finishedExpectation],
            timeout: 30
        )
        let navigationResponse = try XCTUnwrap(
            responseProbe.navigationResponse
        )
        webView.navigationDelegate = controller
        let directoriesBeforeDownload = temporaryDownloadDirectories()
        // The controller takes the download inside WebKit's start callback, as
        // `beginDownload` does in production. WebKit asks for a destination in
        // a later message; a delegate registered after an `await` can miss it,
        // and a download with no delegate gets no destination and is cancelled.
        let download = await withCheckedContinuation { continuation in
            webView.startDownload(
                using: URLRequest(
                    url: baseURL.appending(path: "account-export.txt")
                )
            ) { download in
                controller.webView(
                    webView,
                    navigationResponse: navigationResponse,
                    didBecome: download
                )
                continuation.resume(returning: download)
            }
        }
        await fulfillment(
            of: [headersSent.expectation],
            timeout: 30
        )
        var createdDirectories: Set<URL> = []
        try await waitUntil(
            "A temporary download directory exists",
            timeout: .seconds(30)
        ) {
            createdDirectories =
                self.temporaryDownloadDirectories().subtracting(
                    directoriesBeforeDownload
                )
            return !createdDirectories.isEmpty
        }

        _ = controller.invalidateForAccountTransition()
        releaseBody.signal()
        try await waitUntil(
            "Every temporary download directory is removed",
            timeout: .seconds(30)
        ) {
            createdDirectories.allSatisfy {
                !FileManager.default.fileExists(atPath: $0.path)
            }
        }
        controller.downloadDidFinish(download)
        controller.download(
            download,
            didFailWithError: URLError(.cancelled),
            resumeData: nil
        )
        XCTAssertNil(controller.presentedViewController)
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

    private func temporaryDownloadDirectories() -> Set<URL> {
        let temporaryDirectory = FileManager.default.temporaryDirectory
        let contents = (
            try? FileManager.default.contentsOfDirectory(
                at: temporaryDirectory,
                includingPropertiesForKeys: nil
            )
        ) ?? []
        return Set(
            contents.filter {
                $0.lastPathComponent.hasPrefix("Avibe-")
            }
        )
    }

    @MainActor
    private func exerciseAppModelAccountTransition(
        defaults: UserDefaults,
        controlPlaneURL: URL,
        storeCleanups: [NamedDataStoreCleanup],
        requests: FixtureRequestLog
    ) async throws {
        var appModel: AppModel? = makeTestAppModel(
            defaults: defaults,
            controlPlane: MobileControlPlaneConfiguration(
                testBaseURL: controlPlaneURL
            )
        )
        storeCleanups[0].identifier = try XCTUnwrap(
            appModel?.websiteDataStore.identifier
        )
        try await exerciseRuntimeAccountTransition(
            appModel: try XCTUnwrap(appModel),
            storeCleanups: storeCleanups,
            requests: requests
        )
        appModel = nil
        try? await Task.sleep(for: .milliseconds(100))
    }

    @MainActor
    private func exerciseRuntimeAccountTransition(
        appModel: AppModel,
        storeCleanups: [NamedDataStoreCleanup],
        requests: FixtureRequestLog
    ) async throws {
        try await waitForSignedInInstances(
            appModel, accountID: "account", instanceIDs: ["instance", "instance-2"],
            requests: requests
        )
        _ = try appModel.mobileSessionCoordinator.selectInstance(id: "instance")

        guard case .browser(let runtime) = appModel.route else {
            XCTFail("Selecting an instance did not open the runtime.")
            return
        }
        let (controller, webView, window) = try await openRuntimeDocument(
            for: runtime,
            origin: instance,
            in: appModel
        )
        defer {
            window.rootViewController = nil
            window.isHidden = true
        }

        // Account covers the runtime and leaves its document as it was.
        _ = try await webView.callAsyncJavaScript(
            """
            window.avibeRuntimeState = "kept";
            document.querySelector('#account').click();
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        try await waitUntil {
            appModel.route == .accountOverBrowser(runtime)
        }
        XCTAssertNotNil(webView.superview)
        appModel.showAccount()
        XCTAssertEqual(appModel.route, .accountOverBrowser(runtime))

        XCTAssertTrue(appModel.returnToRuntime())
        XCTAssertEqual(appModel.route, .browser(runtime))
        XCTAssertFalse(appModel.returnToRuntime())
        let returnedState = try await webView.callAsyncJavaScript(
            """
            await window.avibeNative.call(
              "app.setAccountActionAvailable",
              {available: true}
            );
            return window.avibeRuntimeState;
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        XCTAssertEqual(returnedState as? String, "kept")
        XCTAssertNotNil(webView.superview)
        let runtimeIdentity = appModel.runtimeIdentity

        // Switching instance from that account page releases the runtime.
        appModel.showAccount()
        XCTAssertEqual(appModel.route, .accountOverBrowser(runtime))
        _ = try appModel.mobileSessionCoordinator.selectInstance(
            id: "instance-2"
        )
        guard case .browser(let switched) = appModel.route else {
            XCTFail("Switching instance did not open the new runtime.")
            return
        }
        XCTAssertEqual(switched.instance.id, "instance-2")
        XCTAssertEqual(appModel.runtimeIdentity, runtimeIdentity + 1)
        XCTAssertNil(webView.superview)

        let lateBridgeResult = try await webView.callAsyncJavaScript(
            """
            const result = {};
            await window.avibeNative.call("app.showAccount");
            try {
              await window.avibeNative.call(
                "app.setAccountActionAvailable",
                {available: true}
              );
            } catch (error) {
              result.registration = error.code;
            }
            try {
              await window.avibeNative.call("navigation.historyChanged");
            } catch (error) {
              result.history = error.code;
            }
            return JSON.stringify(result);
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        let lateBridgePayload = try jsonObject(from: lateBridgeResult)
        XCTAssertEqual(lateBridgePayload["registration"] as? String, "forbidden")
        XCTAssertEqual(lateBridgePayload["history"] as? String, "forbidden")
        XCTAssertEqual(appModel.route, .browser(switched))

        controller.webView(
            webView,
            didStartProvisionalNavigation: nil
        )
        controller.webView(
            webView,
            didCommit: nil
        )
        controller.webView(
            webView,
            didFinish: nil
        )
        controller.webViewWebContentProcessDidTerminate(webView)
        controller.webViewDidClose(webView)

        // Signing out from the account page over a runtime releases it too.
        let switchedOrigin = try XCTUnwrap(
            WebOrigin(url: switched.instance.url)
        )
        let (_, switchedWebView, switchedWindow) = try await openRuntimeDocument(
            for: switched,
            origin: switchedOrigin,
            in: appModel
        )
        defer {
            switchedWindow.rootViewController = nil
            switchedWindow.isHidden = true
        }
        appModel.showAccount()
        XCTAssertEqual(appModel.route, .accountOverBrowser(switched))

        let signedOut = try await appModel.mobileSessionCoordinator.logout()
        storeCleanups[1].identifier = appModel.websiteDataStore.identifier
        XCTAssertEqual(signedOut, .signedOut)
        XCTAssertEqual(appModel.route, .onboarding(.account))
        XCTAssertNil(switchedWebView.superview)
        XCTAssertFalse(appModel.returnToRuntime())
        appModel.mobileSessionCoordinator.deactivateAccountSurface()
    }

    @MainActor
    private func openRuntimeDocument(
        for runtime: MobileRuntimeContext,
        origin: WebOrigin,
        in appModel: AppModel
    ) async throws -> (WebViewController, WKWebView, UIWindow) {
        let controller = WebViewController(
            instanceOrigin: origin,
            websiteDataStore: appModel.runtimeWebsiteDataStore,
            showAccount: appModel.showAccount,
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        appModel.registerRuntimeController(controller, for: runtime)
        let window = host(controller)
        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        let probe = WebViewLoadProbe()
        webView.navigationDelegate = probe
        try await probe.loadHTML(
            """
            <!doctype html>
            <button id="account" onclick="window.avibeNative.call('app.showAccount')">
              Account
            </button>
            """,
            baseURL: origin.url,
            in: webView
        )
        return (controller, webView, window)
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
    private func makeTestAppModel(
        defaults: UserDefaults,
        controlPlane: MobileControlPlaneConfiguration,
        pushOpenedNow: @escaping () -> Date = Date.init,
        healthSync: HealthSyncCoordinator? = nil
    ) -> AppModel {
        let scheduleDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("I3Fixture.\(UUID().uuidString)", isDirectory: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: scheduleDirectory) }
        let scheduleStore = FileSystemSystemScheduleStore(
            fileURL: scheduleDirectory.appendingPathComponent("ledger.json")
        )
        return AppModel(
            defaults: defaults,
            controlPlane: controlPlane,
            pushKeychain: InMemoryPushKeychainStore(),
            pushGenerationStore: InMemoryPushGenerationStore(),
            pushInstallationMarker: InMemoryPushInstallationMarkerStore(),
            pushNotificationCenter: NavigationTestPushNotificationCenter(),
            pushOpenedNow: pushOpenedNow,
            // Never the test host's own badge.
            systemScheduleStore: scheduleStore,
            badgeCenter: NavigationTestBadgeCenter(),
            healthSync: healthSync
        )
    }

    @MainActor
    private func makePushTapAppModel(
        pushOpenedNow: @escaping () -> Date = Date.init,
        sessionResponse: LocalHTTPResponse? = nil
    ) async throws -> AppModel {
        let (appModel, requests) = try await makeFixtureAppModel(
            pushOpenedNow: pushOpenedNow,
            sessionResponse: sessionResponse
        )
        try await waitForSignedInInstances(appModel, requests: requests)
        return appModel
    }

    /// The selection the App keeps for instance-1 of `account-1`.
    private static let instanceOneSelection =
        #"{"accountID":"account-1","instanceID":"instance-1","url":"https://instance-one.example"}"#

    /// An `AppModel` on a local control plane whose session lists
    /// `account-1` with instances one and two, returned before its
    /// bootstrap settles. `storedSelection` launches it with that stored
    /// selection; `whenTheAppExists` runs right after it is made.
    @MainActor
    private func makeFixtureAppModel(
        pushOpenedNow: @escaping () -> Date = Date.init,
        sessionResponse: LocalHTTPResponse? = nil,
        instanceOneAvailable: Bool = true,
        storedSelection: String? = nil,
        heldSessionReads: HeldSessionReads? = nil,
        healthSync: HealthSyncCoordinator? = nil,
        whenTheAppExists: @MainActor (AppModel) async throws -> Void = { _ in }
    ) async throws -> (AppModel, FixtureRequestLog) {
        let server = try LocalHTTPServer { request in
            switch (request.method, request.path) {
            case ("GET", "/api/config/auth"):
                return .json("{}")
            case ("GET", "/api/mobile/session"):
                let read = heldSessionReads?.count.next() ?? 1
                let response = sessionResponse ?? .json(
                    """
                    {
                      "account": {
                        "id": "account-1",
                        "email": "max@example.com"
                      },
                      "instances": [
                        {
                          "id": "instance-1",
                          "name": "One",
                          "host": "instance-one.example",
                          "url": "https://instance-one.example",
                          "relationship": "owner",
                          "owner_email": "max@example.com",
                          "available": \(instanceOneAvailable)
                        },
                        {
                          "id": "instance-2",
                          "name": "Two",
                          "host": "instance-two.example",
                          "url": "https://instance-two.example",
                          "relationship": "owner",
                          "owner_email": "max@example.com",
                          "available": true
                        }
                      ]
                    }
                    """
                )
                guard let heldSessionReads, read > 1 else {
                    return response
                }
                // The server sends the first 64 KiB at once and holds the
                // rest: pad the JSON past that so the read really waits.
                var body = response.body
                body.append(Data(repeating: UInt8(ascii: " "), count: 70_000))
                return LocalHTTPResponse(
                    status: response.status,
                    headers: response.headers,
                    body: body,
                    bodyRelease: heldSessionReads.release
                )
            default:
                return .status(404)
            }
        }
        let baseURL = try await server.start()
        addTeardownBlock {
            server.stop()
        }
        let suiteName = "NavigationPolicyTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        addTeardownBlock {
            defaults.removePersistentDomain(forName: suiteName)
        }
        if let storedSelection {
            defaults.set(
                Data(storedSelection.utf8),
                forKey: "avibe.mobile.selection"
            )
        }
        let appModel = makeTestAppModel(
            defaults: defaults,
            controlPlane: MobileControlPlaneConfiguration(
                testBaseURL: baseURL
            ),
            pushOpenedNow: pushOpenedNow,
            healthSync: healthSync
        )
        let storeCleanup = NamedDataStoreCleanup()
        storeCleanup.identifier = appModel.websiteDataStore.identifier
        addTeardownBlock { @MainActor [weak appModel, self] in
            appModel?.showInstancePicker()
            appModel?.mobileSessionCoordinator.deactivateAccountSurface()
            print("[iOS fixture] stage=push App views invalidated before store removal")
            let error = await storeCleanup.remove()
            XCTAssertNil(error, "Push fixture named-store cleanup failed")
            server.requests.attach(to: self, name: "push App bootstrap requests")
        }
        try await whenTheAppExists(appModel)
        return (appModel, server.requests)
    }

    /// A settled picker is not enough: bootstrap errors also land there.
    @MainActor
    private func waitForSignedInInstances(
        _ appModel: AppModel,
        accountID: String = "account-1",
        instanceIDs: Set<String> = ["instance-1", "instance-2"],
        requests: FixtureRequestLog
    ) async throws {
        try await WebKitReadiness.waitForSignedInFixture(
            appModel, accountID: accountID, instanceIDs: instanceIDs, requests: requests
        )
    }

    @MainActor
    private func waitForPushRuntime(
        _ appModel: AppModel,
        instanceID: String
    ) async throws -> MobileRuntimeContext {
        try await waitUntil {
            guard case .browser(let runtime) = appModel.route else {
                return false
            }
            return runtime.instance.id == instanceID
        }
        guard case .browser(let runtime) = appModel.route else {
            throw TestWaitError.timedOut
        }
        return runtime
    }

    @MainActor
    private func makePushRuntimeController(
        appModel: AppModel,
        runtime: MobileRuntimeContext,
        origin: WebOrigin,
        pushOpenedAck: @escaping (String, WebOrigin) -> Bool
    ) throws -> (WebViewController, WKWebView, UIWindow) {
        let controller = WebViewController(
            instanceOrigin: origin,
            websiteDataStore: appModel.runtimeWebsiteDataStore,
            pushOpenedAck: pushOpenedAck,
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        appModel.registerRuntimeController(controller, for: runtime)
        let window = host(controller)
        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        return (controller, webView, window)
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
    private func mobileOnboardingController(
        in controller: UIViewController
    ) -> MobileOnboardingViewController? {
        if let onboarding = controller as? MobileOnboardingViewController {
            return onboarding
        }
        if let presented = controller.presentedViewController,
           let onboarding = mobileOnboardingController(in: presented) {
            return onboarding
        }
        for child in controller.children {
            if let onboarding = mobileOnboardingController(in: child) {
                return onboarding
            }
        }
        return nil
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
        in webView: WKWebView,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async throws {
        try await WebKitReadiness.waitForDocument(
            in: webView,
            file: file,
            line: line
        )
        try await waitUntil("The title \(title)", file: file, line: line) {
            let value = try? await webView.evaluateJavaScript("document.title")
            return value as? String == title
        }
    }

    /// Polls `condition` until it holds. A timeout is reported at the caller's
    /// line with `description`, so a failure names the wait that expired rather
    /// than this helper.
    @MainActor
    private func waitUntil(
        _ description: String = "Condition",
        timeout: Duration = .seconds(20),
        file: StaticString = #filePath,
        line: UInt = #line,
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
        XCTFail(
            "\(description) did not become true within \(timeout).",
            file: file,
            line: line
        )
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
    private func assertVerticalConstraints(
        _ constraints: [NSLayoutConstraint],
        webView: WKWebView,
        top topOwner: AnyObject,
        bottom bottomOwner: AnyObject,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        for (attribute, owner) in [(NSLayoutConstraint.Attribute.top, topOwner), (.bottom, bottomOwner)] {
            let pinned = constraints.contains { constraint in
                constraint.firstItem === webView
                    && constraint.firstAttribute == attribute
                    && constraint.secondItem === owner
                    && constraint.secondAttribute == attribute
            }
            XCTAssertTrue(pinned, "\(attribute.rawValue)", file: file, line: line)
        }
    }

    @MainActor
    private func top(of view: UIView, in window: UIWindow) -> CGFloat {
        window.layoutIfNeeded()
        return view.convert(view.bounds, to: window).minY
    }

    @MainActor
    private func bottom(of view: UIView, in window: UIWindow) -> CGFloat {
        window.layoutIfNeeded()
        return view.convert(view.bounds, to: window).maxY
    }

    @MainActor
    private func postKeyboardFrame(
        _ frame: CGRect,
        in window: UIWindow,
        notification: Notification.Name =
            UIResponder.keyboardWillChangeFrameNotification
    ) {
        postKeyboardNotification(
            notification,
            frameInScreenCoordinates: window.convert(
                frame,
                to: window.screen.coordinateSpace
            )
        )
    }

    @MainActor
    private func paddingBottom(
        of selector: String,
        in webView: WKWebView
    ) async -> CGFloat? {
        await padding(.bottom, of: selector, in: webView)
    }

    private enum PaddingSide: String {
        case top = "paddingTop"
        case bottom = "paddingBottom"
    }

    @MainActor
    private func padding(
        _ side: PaddingSide,
        of selector: String,
        in webView: WKWebView
    ) async -> CGFloat? {
        let value = try? await webView.callAsyncJavaScript(
            """
            const element = document.querySelector(selector);
            return element
              ? parseFloat(getComputedStyle(element)[side])
              : -1;
            """,
            arguments: ["selector": selector, "side": side.rawValue],
            in: nil,
            contentWorld: .page
        )
        guard let number = value as? NSNumber, number.doubleValue >= 0 else {
            return nil
        }
        return CGFloat(number.doubleValue)
    }

    /// An ordinary document keeps WebKit's automatic inset above the Home
    /// indicator and reads no safe-area padding of its own.
    @MainActor
    private func waitForAutomaticBottomInset(
        _ homeIndicatorInset: CGFloat,
        probe selector: String,
        in webView: WKWebView
    ) async throws {
        try await waitUntil {
            let padding = await self.paddingBottom(of: selector, in: webView)
            return padding == 0
                && abs(
                    webView.scrollView.adjustedContentInset.bottom
                        - homeIndicatorInset
                ) < 0.5
        }
    }

    @MainActor
    private func primaryRuntimeWebView(in view: UIView) -> WKWebView? {
        for subview in view.subviews {
            if let webView = subview as? WKWebView,
               webView.superview?.next is WebViewController {
                return webView
            }
            if let webView = primaryRuntimeWebView(in: subview) {
                return webView
            }
        }
        return nil
    }
}

@MainActor
private final class NavigationTestBadgeCenter: AppBadgeCentering {
    func badgeAllowed() async -> Bool {
        true
    }

    func setBadgeCount(_ count: Int) async throws {}
}

private final class NavigationTestPushNotificationCenter:
    PushNotificationCentering
{
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
private final class MutableDate {
    var value: Date

    init(_ value: Date) {
        self.value = value
    }
}

private final class ScriptMessageHandler: NSObject, WKScriptMessageHandler {
    private let handler: (Any) -> Void

    init(_ handler: @escaping (Any) -> Void) {
        self.handler = handler
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        handler(message.body)
    }
}

private enum FullBleedFixture {
    /// The instance `ui/index.html` viewport declaration.
    static let workbenchViewport =
        "width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no"

    static let workbenchDocument = """
        <!doctype html>
        <html>
          <head>
            <meta name="viewport" content="\(workbenchViewport)">
            <title>Workbench</title>
            <style>
              html, body { margin: 0; height: 100%; overflow: hidden; }
              #tab-bar {
                position: fixed;
                left: 0;
                right: 0;
                bottom: 0;
                height: 56px;
                padding-bottom: env(safe-area-inset-bottom);
                background: #0891b2;
              }
            </style>
          </head>
          <body>
            <nav id="tab-bar"></nav>
            <script>
              window.avibeNative.call("viewport.setOuterScrollLocked", {locked: true});
            </script>
          </body>
        </html>
        """

    /// A locked AppShell whose header pads the status bar, as
    /// `MobileBrandHeader` does, and which opens a child window.
    static let topEdgeWorkbenchDocument = """
        <!doctype html>
        <html>
          <head>
            <meta name="viewport" content="\(workbenchViewport)">
            <title>Workbench</title>
            <style>
              html, body { margin: 0; height: 100%; overflow: hidden; }
              #app-header {
                position: fixed;
                left: 0;
                right: 0;
                top: 0;
                height: 44px;
                padding-top: env(safe-area-inset-top);
                background: #f4f6fb;
              }
              #open-child { position: fixed; top: 200px; left: 16px; }
            </style>
          </head>
          <body>
            <header id="app-header"></header>
            <button id="open-child" onclick="window.open('/child', '_blank')">Open</button>
            <script>
              window.avibeNative.call("viewport.setOuterScrollLocked", {locked: true});
            </script>
          </body>
        </html>
        """

    /// The theme rules of `ui/src/index.css`: dark by default, light under a
    /// light system unless the dark theme is picked, and an explicit light
    /// theme.
    static let themedDocument = """
        <!doctype html>
        <html>
          <head>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Themed</title>
            <style>
              :root, [data-theme="dark"] { color-scheme: dark; background: #080812; }
              @media (prefers-color-scheme: light) {
                :root:not([data-theme="dark"]) { color-scheme: light; background: #f4f6fb; }
              }
              [data-theme="light"] { color-scheme: light; background: #f4f6fb; }
            </style>
          </head>
          <body></body>
        </html>
        """

    static func ordinaryDocument(title: String) -> String {
        """
        <!doctype html>
        <html>
          <head>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>\(title)</title>
          </head>
          <body style="margin: 0">
            <p id="first" style="margin: 0; padding-top: env(safe-area-inset-top)">Start</p>
            <div style="height: 3000px"></div>
            <p id="last" style="padding-bottom: env(safe-area-inset-bottom)">End</p>
          </body>
        </html>
        """
    }
}

private enum HomeIndicatorWindowError: Error {
    case missingBottomInset
}

/// Hosts `rootViewController` in a key window that fills the app's scene, so
/// its safe area is the simulated device's. A device without a bottom inset
/// fails here instead of letting full-bleed checks pass vacuously.
@MainActor
func makeHomeIndicatorWindow(
    rootViewController: UIViewController,
    file: StaticString = #filePath,
    line: UInt = #line
) async throws -> UIWindow {
    let windowScene = try XCTUnwrap(
        UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .first,
        "Full-bleed checks need the app's window scene.",
        file: file,
        line: line
    )
    let window = UIWindow(windowScene: windowScene)
    window.frame = windowScene.screen.bounds
    window.rootViewController = rootViewController
    window.makeKeyAndVisible()
    rootViewController.view.frame = window.bounds
    window.layoutIfNeeded()
    let clock = ContinuousClock()
    let deadline = clock.now.advanced(by: .seconds(5))
    while window.safeAreaInsets.bottom <= 0, clock.now < deadline {
        try await Task.sleep(for: .milliseconds(50))
        window.layoutIfNeeded()
    }
    guard window.safeAreaInsets.bottom > 0 else {
        window.rootViewController = nil
        window.isHidden = true
        XCTFail(
            """
            The test device reports no bottom safe-area inset, so full-bleed \
            behavior cannot be verified. Run on an iPhone with a Home indicator.
            """,
            file: file,
            line: line
        )
        throw HomeIndicatorWindowError.missingBottomInset
    }
    return window
}

func postKeyboardNotification(
    _ notification: Notification.Name,
    frameInScreenCoordinates: CGRect,
    duration: TimeInterval = 0,
    curve: UIView.AnimationCurve = .easeInOut
) {
    NotificationCenter.default.post(
        name: notification,
        object: nil,
        userInfo: [
            UIResponder.keyboardFrameEndUserInfoKey: NSValue(
                cgRect: frameInScreenCoordinates
            ),
            UIResponder.keyboardAnimationDurationUserInfoKey: duration,
            UIResponder.keyboardAnimationCurveUserInfoKey: curve.rawValue,
        ]
    )
}

@MainActor
private final class KeyboardViewportFixture {
    let window: UIWindow
    let containerView: UIView
    let contentView = UIView()
    let layoutController: KeyboardViewportLayoutController
    private let bottomEdge: KeyboardViewportLayoutController.BottomEdge

    var safeAreaBottom: CGFloat {
        containerView.layoutIfNeeded()
        return containerView.safeAreaLayoutGuide.layoutFrame.maxY
    }

    /// Where the content ends while no docked keyboard covers it.
    var restingBottom: CGFloat {
        switch bottomEdge {
        case .container:
            return containerView.bounds.maxY
        case .safeArea:
            return safeAreaBottom
        }
    }

    var contentBottom: CGFloat {
        containerView.layoutIfNeeded()
        return contentView.frame.maxY
    }

    init(bottomEdge: KeyboardViewportLayoutController.BottomEdge) {
        self.bottomEdge = bottomEdge
        guard let windowScene = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene })
            .first
        else {
            preconditionFailure("Keyboard viewport tests require a window scene.")
        }
        let screenBounds = windowScene.screen.bounds
        window = UIWindow(windowScene: windowScene)
        window.frame = screenBounds
        containerView = UIView(
            frame: screenBounds.insetBy(dx: 8, dy: 20)
        )
        let rootViewController = UIViewController()
        rootViewController.view = UIView(frame: screenBounds)
        window.rootViewController = rootViewController
        window.isHidden = false
        rootViewController.view.addSubview(containerView)
        contentView.translatesAutoresizingMaskIntoConstraints = false
        containerView.addSubview(contentView)
        layoutController = KeyboardViewportLayoutController(
            contentView: contentView,
            containerView: containerView,
            bottomEdge: bottomEdge
        )
        NSLayoutConstraint.activate([
            contentView.topAnchor.constraint(equalTo: containerView.topAnchor),
            contentView.leadingAnchor.constraint(
                equalTo: containerView.leadingAnchor
            ),
            contentView.trailingAnchor.constraint(
                equalTo: containerView.trailingAnchor
            ),
            layoutController.bottomConstraint,
        ])
        containerView.layoutIfNeeded()
    }

    deinit {
        window.isHidden = true
    }

    func dockedKeyboardFrame(top: CGFloat) -> CGRect {
        CGRect(
            x: containerView.bounds.minX,
            y: top,
            width: containerView.bounds.width,
            height: containerView.bounds.maxY - top
        )
    }

    func offscreenKeyboardFrame() -> CGRect {
        CGRect(
            x: containerView.bounds.minX,
            y: containerView.bounds.maxY,
            width: containerView.bounds.width,
            height: max(containerView.bounds.height / 3, 1)
        )
    }

    func postKeyboardFrame(
        _ frame: CGRect,
        notification: Notification.Name =
            UIResponder.keyboardWillChangeFrameNotification,
        duration: TimeInterval = 0,
        curve: UIView.AnimationCurve = .easeInOut
    ) {
        guard let screen = containerView.window?.screen else {
            XCTFail("Keyboard viewport fixture is not attached to a window.")
            return
        }
        postKeyboardNotification(
            notification,
            frameInScreenCoordinates: containerView.convert(
                frame,
                to: screen.coordinateSpace
            ),
            duration: duration,
            curve: curve
        )
    }
}

private enum TestWaitError: Error {
    case timedOut
}

/// Holds the body of every account session read after the bootstrap's
/// until `release` is signalled, counting the reads.
private final class HeldSessionReads: @unchecked Sendable {
    final class Counter: @unchecked Sendable {
        private let lock = NSLock()
        private var value = 0

        func next() -> Int {
            lock.lock()
            defer { lock.unlock() }
            value += 1
            return value
        }

        var current: Int {
            lock.lock()
            defer { lock.unlock() }
            return value
        }
    }

    let count = Counter()
    let release = DispatchSemaphore(value: 0)
}

@MainActor
private final class NamedDataStoreCleanup {
    var identifier: UUID?

    func remove() async -> Error? {
        guard let identifier else {
            return nil
        }
        var lastError: Error?
        for attempt in 0..<50 {
            lastError = await AccountWebsiteDataStoreOperations.live
                .removeIdentifier(identifier)
            let registered = await withCheckedContinuation { continuation in
                WKWebsiteDataStore.fetchAllDataStoreIdentifiers {
                    continuation.resume(returning: Set($0))
                }
            }
            if !registered.contains(identifier) {
                self.identifier = nil
                return nil
            }
            if attempt < 49 {
                try? await Task.sleep(for: .milliseconds(100))
            }
        }
        return lastError ?? NamedDataStoreCleanupError.stillRegistered(identifier)
    }
}

private enum NamedDataStoreCleanupError: Error {
    case stillRegistered(UUID)
}

private final class NavigationResponseProbe: NSObject, WKNavigationDelegate {
    let finishedExpectation = XCTestExpectation(
        description: "fixture navigation finished"
    )
    private(set) var navigationResponse: WKNavigationResponse?

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationResponse: WKNavigationResponse,
        decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void
    ) {
        self.navigationResponse = navigationResponse
        decisionHandler(.allow)
    }

    func webView(
        _ webView: WKWebView,
        didFinish navigation: WKNavigation?
    ) {
        finishedExpectation.fulfill()
    }
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

final class FormAccessoryBarTests: XCTestCase {
    private let instance = WebOrigin.parseConfigured("https://alice-app.avibe.bot")!

    @MainActor
    func testInstanceWebViewsShowNoFormAccessoryBar() throws {
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

        let primaryContent = try XCTUnwrap(FormAccessoryBar.contentView(of: primary))
        let childContent = try XCTUnwrap(FormAccessoryBar.contentView(of: child))
        for content in [primaryContent, childContent] {
            XCTAssertTrue(
                NSStringFromClass(type(of: content)).hasSuffix(FormAccessoryBar.subclassSuffix)
            )
            XCTAssertNil(content.inputAccessoryView)
        }
        // Both content views share one runtime subclass.
        XCTAssertEqual(
            NSStringFromClass(type(of: primaryContent)),
            NSStringFromClass(type(of: childContent))
        )
    }

    @MainActor
    func testHidingTwiceKeepsTheSameSubclass() throws {
        let webView = WKWebView(frame: .zero)
        XCTAssertTrue(FormAccessoryBar.hide(in: webView))
        let first = NSStringFromClass(
            type(of: try XCTUnwrap(FormAccessoryBar.contentView(of: webView)))
        )
        XCTAssertTrue(FormAccessoryBar.hide(in: webView))
        let second = NSStringFromClass(
            type(of: try XCTUnwrap(FormAccessoryBar.contentView(of: webView)))
        )
        XCTAssertEqual(first, second)
        XCTAssertTrue(second.hasSuffix(FormAccessoryBar.subclassSuffix))
    }

    @MainActor
    func testTheSignInPageKeepsWebKitsDefaultAccessory() throws {
        // The bundled sign-in WebView is not made by the instance factory, so
        // its keyboard keeps WebKit's default accessory.
        let webView = WKWebView(frame: .zero)
        let content = try XCTUnwrap(FormAccessoryBar.contentView(of: webView))
        XCTAssertFalse(
            NSStringFromClass(type(of: content)).hasSuffix(FormAccessoryBar.subclassSuffix)
        )
    }

    // App Store 5.1.2(i): a Workbench that asks before voice goes to a third-party
    // AI service carries the marker and gets the microphone with no second
    // question; an older Workbench is told to update; any other page is denied
    // without that message.
    @MainActor
    func testVoiceConsentMarkerIsReadFromTheInstancePage() async throws {
        let workbench = #"<link rel="manifest" href="/manifest.webmanifest">"#
        let cases: [(String, String, String, VoiceConsentMarker)] = [
            ("/chat", #"<meta name="avibe-voice-consent" content="1">"# + workbench, "<div id=\"root\"></div>", .marked),
            ("/chat", workbench, "<div id=\"root\"></div>", .unmarkedWorkbench),
            ("/chat", #"<meta name="avibe-voice-consent" content="0">"# + workbench, "<div id=\"root\"></div>", .unmarkedWorkbench),
            ("/show/ses_1/", workbench, "<div id=\"root\"></div>", .other),
            ("/chat", "", "<div id=\"root\"></div>", .other),
        ]
        for (path, head, body, expected) in cases {
            let webView = WKWebView(frame: .zero)
            webView.loadHTMLString(
                "<!doctype html><html><head>\(head)</head><body>\(body)</body></html>",
                baseURL: URL(string: "https://alice-app.avibe.bot\(path)")
            )
            try await WebKitReadiness.waitForDocument(in: webView)
            let result = try? await webView.evaluateJavaScript(VoiceConsentMarker.script)
            XCTAssertEqual(VoiceConsentMarker(result: result), expected, "\(path) \(head)")
        }
        XCTAssertEqual(VoiceConsentMarker(result: nil), .other)
        XCTAssertEqual(VoiceConsentMarker(result: true), .other)
    }
}
