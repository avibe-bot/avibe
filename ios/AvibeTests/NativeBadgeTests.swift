import WebKit
import XCTest
@testable import Avibe

/// The App icon badge follows the Inbox total the Web UI sets through
/// `badge.set({count})` (`docs/plans/ios-app.md` section 4).
final class NativeBadgeTests: XCTestCase {
    private let instance = WebOrigin.parseConfigured("https://alice-app.avibe.bot")!

    func testTheShellAdvertisesBadgeSet() throws {
        XCTAssertEqual(NativeShellMetadata.capabilities["badge"], 1)
        XCTAssertEqual(
            try NativeBridgeRequest.parse(
                ["method": "badge.set", "params": ["count": 3]]
            ).get().method,
            .badgeSet
        )
    }

    func testBadgeSetTakesExactlyOneCountNumber() {
        XCTAssertEqual(NativeBadgeCount.parse(["count": 3]), 3)
        XCTAssertEqual(NativeBadgeCount.parse(["count": 0]), 0)
        // Out of range numbers are clamped: below zero clears, a fraction is
        // rounded down, and a huge count shows the largest one.
        XCTAssertEqual(NativeBadgeCount.parse(["count": -4]), 0)
        XCTAssertEqual(NativeBadgeCount.parse(["count": 2.9]), 2)
        XCTAssertEqual(
            NativeBadgeCount.parse(["count": 1e12]),
            NativeBadgeCount.maximum
        )
        // Anything else is not a count.
        XCTAssertNil(NativeBadgeCount.parse([:]))
        XCTAssertNil(NativeBadgeCount.parse(["count": "3"]))
        XCTAssertNil(NativeBadgeCount.parse(["count": true]))
        XCTAssertNil(NativeBadgeCount.parse(["count": NSNull()]))
        XCTAssertNil(NativeBadgeCount.parse(["count": Double.nan]))
        XCTAssertNil(NativeBadgeCount.parse(["count": 3, "total": 3]))
    }

    @MainActor
    func testTheBadgeTakesTheLatestCountWhileTheUserAllowsBadges() async throws {
        let center = FakeAppBadgeCenter(allowed: true)
        let badge = NativeBadge(center: center)

        // A burst ends on the badge the last request asked for.
        badge.set(3)
        badge.set(5)
        try await waitUntil { center.counts.count == 1 }
        XCTAssertEqual(center.counts, [5])

        badge.set(0)
        try await waitUntil { center.counts.count == 2 }
        badge.clear()
        try await waitUntil { center.counts.count == 3 }
        XCTAssertEqual(center.counts, [5, 0, 0])
        XCTAssertEqual(center.checks, 3)
    }

    @MainActor
    func testNothingIsSetWhileTheUserDoesNotAllowBadges() async throws {
        let center = FakeAppBadgeCenter(allowed: false)
        let badge = NativeBadge(center: center)

        badge.set(4)
        try await waitUntil { center.checks == 1 }
        badge.clear()
        try await waitUntil { center.checks == 2 }

        XCTAssertEqual(center.counts, [])
    }

    @MainActor
    func testBadgeSetReachesTheAppOnlyFromTheInstanceMainFrame() async throws {
        var requests: [(count: Int, origin: String)] = []
        var selected = true
        let webView = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: instance,
                metadata: NativeShellMetadata(
                    shellVersion: "1.2.3 (45)",
                    userAgentVersion: "1.2.3"
                ),
                setOuterScrollLocked: { _, _ in false },
                setBadgeCount: { count, origin in
                    guard selected else {
                        return false
                    }
                    requests.append((count, origin.serialized))
                    return true
                }
            )
        )
        let probe = BadgeWebViewLoadProbe()
        webView.navigationDelegate = probe
        try await probe.loadHTML(
            """
            <!doctype html>
            <iframe id="child" srcdoc="<p>subframe</p>"></iframe>
            """,
            baseURL: instance.url,
            in: webView
        )

        let result = try await webView.callAsyncJavaScript(
            """
            const info = await window.avibeNative.call("app.info");
            const set = await window.avibeNative.call("badge.set", {count: 4});
            await window.avibeNative.call("badge.set", {count: 0});
            await window.avibeNative.call("badge.set", {count: -2});
            const invalid = [];
            for (const params of [{}, {count: "4"}, {count: true}, {count: null}, {count: 4, total: 4}]) {
              try {
                await window.avibeNative.call("badge.set", params);
                invalid.push("ok");
              } catch (error) {
                invalid.push(error.code);
              }
            }
            const subframe = await document.querySelector("#child").contentWindow.webkit
              .messageHandlers.avibeNative.postMessage({method: "badge.set", params: {count: 9}});
            return JSON.stringify({badge: info.capabilities.badge, set, invalid, subframe});
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        let payload = try jsonObject(from: result)
        XCTAssertEqual(payload["badge"] as? Int, 1)
        XCTAssertTrue(payload["set"] is NSNull)
        XCTAssertEqual(
            payload["invalid"] as? [String],
            Array(repeating: "invalid_argument", count: 5)
        )
        let subframe = payload["subframe"] as? [String: Any]
        XCTAssertEqual(subframe?["ok"] as? Bool, false)
        XCTAssertEqual(
            (subframe?["error"] as? [String: Any])?["code"] as? String,
            "forbidden"
        )
        XCTAssertEqual(requests.map { $0.count }, [4, 0, 0])
        XCTAssertEqual(Set(requests.map { $0.origin }), [instance.serialized])

        // Once the App no longer has this instance selected, it refuses.
        selected = false
        let refused = try await webView.callAsyncJavaScript(
            """
            try {
              await window.avibeNative.call("badge.set", {count: 2});
              return "ok";
            } catch (error) {
              return error.code;
            }
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        XCTAssertEqual(refused as? String, "forbidden")
        XCTAssertEqual(requests.map { $0.count }, [4, 0, 0])
    }

    @MainActor
    func testTheSelectedInstanceSetsTheBadgeAndOnlySigningOutClearsIt() async throws {
        // A push-management answer larger than what the server sends before
        // `held` is signaled, so the request stays in flight until then.
        let held = DispatchSemaphore(value: 0)
        let inFlight = FixtureEvent(description: "push management request in flight")
        let heldBody = Data(("{\"pad\":\"" + String(repeating: "x", count: 100_000) + "\"}").utf8)
        let server = try LocalHTTPServer { request in
            switch (request.method, request.path) {
            case ("POST", "/api/push/hold"):
                return LocalHTTPResponse(
                    status: 200,
                    headers: [("Content-Type", "application/json")],
                    body: heldBody,
                    headersSentEvent: inFlight,
                    bodyRelease: held
                )
            case ("GET", "/api/config/auth"):
                return .json("{}")
            case ("GET", "/api/mobile/session"):
                return .json(
                    """
                    {
                      "account": {"id": "account-1", "email": "max@example.com"},
                      "instances": [
                        {
                          "id": "instance-1",
                          "name": "One",
                          "host": "instance-one.example",
                          "url": "https://instance-one.example",
                          "relationship": "owner",
                          "owner_email": "max@example.com",
                          "available": true
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
            default:
                return .status(404)
            }
        }
        let baseURL = try await server.start()
        addTeardownBlock {
            server.stop()
        }
        let suiteName = "NativeBadgeTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        addTeardownBlock {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let center = FakeAppBadgeCenter(allowed: true)
        let appModel = AppModel(
            defaults: defaults,
            controlPlane: MobileControlPlaneConfiguration(testBaseURL: baseURL),
            pushKeychain: InMemoryPushKeychainStore(),
            pushGenerationStore: InMemoryPushGenerationStore(),
            pushInstallationMarker: InMemoryPushInstallationMarkerStore(),
            pushNotificationCenter: BadgeTestPushNotificationCenter(),
            badgeCenter: center
        )
        let one = try XCTUnwrap(
            WebOrigin.parseConfigured("https://instance-one.example")
        )
        let two = try XCTUnwrap(
            WebOrigin.parseConfigured("https://instance-two.example")
        )
        try await waitUntil {
            if case .onboarding(.instances) = appModel.route {
                return true
            }
            return false
        }

        // No instance is open: nothing sets the badge.
        XCTAssertFalse(appModel.setBadgeCount(1, origin: one))

        _ = try appModel.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await waitUntil {
            appModel.route.runtime?.instance.id == "instance-1"
        }
        XCTAssertTrue(appModel.setBadgeCount(7, origin: one))
        XCTAssertFalse(appModel.setBadgeCount(9, origin: two))
        try await waitUntil { center.counts.count == 1 }
        XCTAssertEqual(center.counts, [7])

        // A push-management request in flight (APNs registration, provider
        // reconciliation) keeps the account and its selection: the badge
        // still follows the Inbox, a clear included.
        let management = Task {
            try await appModel.mobileSessionCoordinator.pushManagementRequest(
                PushManagementRequest(
                    method: "POST",
                    path: "/api/push/hold",
                    headers: [:],
                    body: nil
                )
            )
        }
        await fulfillment(of: [inFlight.expectation], timeout: 20)
        XCTAssertTrue(appModel.setBadgeCount(0, origin: one))
        try await waitUntil { center.counts.count == 2 }
        XCTAssertEqual(center.counts, [7, 0])
        held.signal()
        _ = try await management.value
        XCTAssertTrue(appModel.setBadgeCount(7, origin: one))
        try await waitUntil { center.counts.count == 3 }
        XCTAssertEqual(center.counts, [7, 0, 7])

        // Refreshing the same account's session keeps the badge: the account's
        // Inbox has not changed hands.
        _ = try await appModel.mobileSessionCoordinator.refresh()
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(center.counts, [7, 0, 7])

        // Signing out clears the account's badge.
        _ = try await appModel.mobileSessionCoordinator.logout()
        try await waitUntil { center.counts.count == 4 }
        XCTAssertEqual(center.counts, [7, 0, 7, 0])
        XCTAssertFalse(appModel.setBadgeCount(3, origin: one))
    }

    @MainActor
    private func waitUntil(
        timeout: TimeInterval = 20,
        _ condition: () -> Bool
    ) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            guard Date() < deadline else {
                XCTFail("Timed out waiting for the condition.")
                return
            }
            try await Task.sleep(for: .milliseconds(20))
        }
    }

    private func jsonObject(from value: Any?) throws -> [String: Any] {
        let string = try XCTUnwrap(value as? String)
        let data = try XCTUnwrap(string.data(using: .utf8))
        return try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any]
        )
    }
}

private final class FakeAppBadgeCenter: AppBadgeCentering {
    let allowed: Bool
    /// How often it was asked whether badges are allowed: once per request applied or skipped.
    private(set) var checks = 0
    private(set) var counts: [Int] = []

    init(allowed: Bool) {
        self.allowed = allowed
    }

    func badgeAllowed() async -> Bool {
        checks += 1
        return allowed
    }

    func setBadgeCount(_ count: Int) async throws {
        counts.append(count)
    }
}

private final class BadgeTestPushNotificationCenter: PushNotificationCentering {
    func deliveredNotifications() async -> [PushNotificationCenterEntry] {
        []
    }

    func pendingNotificationRequests() async -> [PushNotificationCenterEntry] {
        []
    }

    func removeDeliveredNotifications(withIdentifiers identifiers: [String]) {}

    func removePendingNotificationRequests(withIdentifiers identifiers: [String]) {}
}

private final class BadgeWebViewLoadProbe: NSObject, WKNavigationDelegate {
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
