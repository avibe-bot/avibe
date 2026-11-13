import WebKit
import XCTest
@testable import Avibe

/// An instance's Session notifications leave Notification Center once they
/// are read: named as read by `notifications.reconcile`, or, from a page that
/// does not send it, when the badge it sets is 0.
final class SessionNotificationTests: XCTestCase {
    private let one = "https://instance-one.example"
    private let two = "https://instance-two.example"

    @MainActor
    func testNothingUnreadClearsEverySessionOfThatInstanceButNotNotices() async throws {
        let center = RecordingNotificationCenter(entries: notificationsAcrossInstances())
        let cleaner = SessionNotificationCleaner(center: center)

        cleaner.clearAllSessions(instanceOrigin: one, accountID: "account-1", readAt: Date())
        try await waitUntil { center.passes == 1 }

        // Another instance, another account, a workspace notice, the test
        // notification, one the extension could not decrypt, and a Session
        // notification delivered after the zero was computed all stay.
        XCTAssertEqual(center.removedDelivered, ["one-a", "one-a-2", "one-b"])
        XCTAssertEqual(center.removedPending, [])
    }

    @MainActor
    func testTheSelectedInstancesZeroBadgeClearsItsSessions() async throws {
        let center = RecordingNotificationCenter(entries: notificationsAcrossInstances())
        let appModel = try await makeAppModel(center: center)
        let originOne = try XCTUnwrap(WebOrigin.parseConfigured(one))
        let originTwo = try XCTUnwrap(WebOrigin.parseConfigured(two))
        // Let the push coordinator finish its own startup work first.
        try await Task.sleep(for: .milliseconds(200))
        let startupPasses = center.passes

        // No instance is open: its zero is not accepted.
        XCTAssertFalse(appModel.setBadgeCount(0, origin: originOne))

        _ = try appModel.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await waitUntil {
            appModel.route.runtime?.instance.id == "instance-1"
        }

        // A count above zero, and another instance's zero, clear nothing.
        XCTAssertTrue(appModel.setBadgeCount(2, origin: originOne))
        XCTAssertFalse(appModel.setBadgeCount(0, origin: originTwo))
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(center.passes, startupPasses)

        // The selected instance's zero retires its Session notifications
        // delivered before the call; one landing after it stays.
        XCTAssertTrue(appModel.setBadgeCount(0, origin: originOne))
        try await waitUntil { center.passes == startupPasses + 1 }
        XCTAssertEqual(center.removedDelivered, ["one-a", "one-a-2", "one-b"])
    }

    func testReconcileTakesExactlyBoundedUnreadSessionsAndAnAsOfTime() {
        let reconcile = NotificationReconcile.parse(
            ["unreadSessions": ["ses_a", "ses_b"], "asOf": 1_794_096_000_000]
        )
        XCTAssertEqual(reconcile?.unreadSessions, ["ses_a", "ses_b"])
        XCTAssertEqual(reconcile?.asOf, Date(timeIntervalSince1970: 1_794_096_000))
        XCTAssertEqual(
            NotificationReconcile.parse(["unreadSessions": [], "asOf": 0])?.unreadSessions,
            []
        )
        let atLimit = (0..<NotificationReconcile.maximumSessions).map { "ses_\($0)" }
        XCTAssertNotNil(NotificationReconcile.parse(["unreadSessions": atLimit, "asOf": 1]))
        let longest = String(repeating: "s", count: NotificationReconcile.maximumSessionIDLength)
        XCTAssertNotNil(NotificationReconcile.parse(["unreadSessions": [longest], "asOf": 1]))

        // Anything that could leave an unread Session unnamed is refused whole.
        for params: [String: Any] in [
            [:],
            ["unreadSessions": ["ses_a"]],
            ["asOf": 1],
            ["unreadSessions": atLimit + ["ses_more"], "asOf": 1],
            ["unreadSessions": [longest + "s"], "asOf": 1],
            ["unreadSessions": [""], "asOf": 1],
            ["unreadSessions": ["ses_a", "ses_a"], "asOf": 1],
            ["unreadSessions": ["ses_a", 3], "asOf": 1],
            ["unreadSessions": "ses_a", "asOf": 1],
            ["unreadSessions": ["ses_a"], "asOf": "1"],
            ["unreadSessions": ["ses_a"], "asOf": true],
            ["unreadSessions": ["ses_a"], "asOf": Double.nan],
            ["unreadSessions": ["ses_a"], "asOf": Double.infinity],
            ["unreadSessions": ["ses_a"], "asOf": -1],
            ["unreadSessions": ["ses_a"], "asOf": 1, "badge": 0],
        ] {
            XCTAssertNil(NotificationReconcile.parse(params), "\(params)")
        }
    }

    @MainActor
    func testReconcileRetiresOnlySessionsItDoesNotNameDeliveredByItsTime() async throws {
        let center = RecordingNotificationCenter(entries: notificationsAcrossInstances())
        let cleaner = SessionNotificationCleaner(center: center)

        // Read before anything was delivered: nothing is covered.
        cleaner.reconcile(
            NotificationReconcile(unreadSessions: [], asOf: Date(timeIntervalSinceNow: -120)),
            instanceOrigin: one,
            accountID: "account-1"
        )
        try await waitUntil { center.passes == 1 }
        XCTAssertEqual(center.removedDelivered, [])

        // `ses_b` is still unread; `ses_a` was read. A future time counts as
        // now, so the `ses_a` result landing after the call stays, as do
        // another instance, another account, the non-Session threads, the
        // fallback, and pending requests.
        cleaner.reconcile(
            NotificationReconcile(unreadSessions: ["ses_b"], asOf: Date(timeIntervalSinceNow: 3600)),
            instanceOrigin: one,
            accountID: "account-1"
        )
        try await waitUntil { center.passes == 2 }
        XCTAssertEqual(center.removedDelivered, ["one-a", "one-a-2"])
        XCTAssertEqual(center.removedPending, [])
    }

    @MainActor
    func testTheSelectedInstancesUnreadSetRetiresAndThenOutranksItsZeroBadge() async throws {
        let center = RecordingNotificationCenter(entries: notificationsAcrossInstances())
        let appModel = try await makeAppModel(center: center)
        let originOne = try XCTUnwrap(WebOrigin.parseConfigured(one))
        let originTwo = try XCTUnwrap(WebOrigin.parseConfigured(two))
        let readAll = NotificationReconcile(unreadSessions: [], asOf: Date())
        try await Task.sleep(for: .milliseconds(200))
        let startupPasses = center.passes

        // No instance is open, then another instance's page: not accepted.
        XCTAssertFalse(appModel.reconcileNotifications(readAll, origin: originOne))
        _ = try appModel.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await waitUntil {
            appModel.route.runtime?.instance.id == "instance-1"
        }
        XCTAssertFalse(appModel.reconcileNotifications(readAll, origin: originTwo))

        XCTAssertTrue(
            appModel.reconcileNotifications(
                NotificationReconcile(unreadSessions: ["ses_a"], asOf: Date()),
                origin: originOne
            )
        )
        try await waitUntil { center.passes == startupPasses + 1 }
        XCTAssertEqual(center.removedDelivered, ["one-b"])

        // This page names its unread Sessions now: a zero from it (which a
        // muted category can cause) retires nothing more.
        XCTAssertTrue(appModel.setBadgeCount(0, origin: originOne))
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(center.passes, startupPasses + 1)
    }

    @MainActor
    func testReconcileReachesTheAppOnlyFromTheInstanceMainFrame() async throws {
        let instance = try XCTUnwrap(WebOrigin.parseConfigured(one))
        var requests: [(unread: Set<String>, origin: String)] = []
        let webView = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: instance,
                metadata: NativeShellMetadata(
                    shellVersion: "1.2.3 (45)",
                    userAgentVersion: "1.2.3"
                ),
                setOuterScrollLocked: { _, _ in false },
                reconcileNotifications: { reconcile, origin in
                    requests.append((reconcile.unreadSessions, origin.serialized))
                    return true
                }
            )
        )
        let probe = SessionWebViewLoadProbe()
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
            const done = await window.avibeNative.call(
              "notifications.reconcile", {unreadSessions: ["ses_a"], asOf: Date.now()}
            );
            let invalid;
            try {
              await window.avibeNative.call("notifications.reconcile", {unreadSessions: ["ses_a"]});
              invalid = "ok";
            } catch (error) {
              invalid = error.code;
            }
            const subframe = await document.querySelector("#child").contentWindow.webkit
              .messageHandlers.avibeNative.postMessage({
                method: "notifications.reconcile",
                params: {unreadSessions: [], asOf: Date.now()}
              });
            return JSON.stringify({
              capability: info.capabilities.notifications,
              done,
              invalid,
              subframe: subframe?.error?.code ?? "ok"
            });
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        let data = try XCTUnwrap((result as? String)?.data(using: .utf8))
        let payload = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(payload["capability"] as? Int, 1)
        XCTAssertTrue(payload["done"] is NSNull)
        XCTAssertEqual(payload["invalid"] as? String, "invalid_argument")
        XCTAssertEqual(payload["subframe"] as? String, "forbidden")
        XCTAssertEqual(requests.map(\.unread), [["ses_a"]])
        XCTAssertEqual(requests.map(\.origin), [one])
    }

    /// An App signed in to `account-1` with instances one and two, its
    /// notifications in `center`.
    @MainActor
    private func makeAppModel(center: RecordingNotificationCenter) async throws -> AppModel {
        let server = try LocalHTTPServer { request in
            switch (request.method, request.path) {
            case ("GET", "/api/config/auth"):
                return .json("{}")
            case ("GET", "/api/mobile/session"):
                return .json(
                    """
                    {
                      "account": {"id": "account-1", "email": "max@example.com"},
                      "instances": [
                        {"id": "instance-1", "name": "One", "host": "instance-one.example",
                         "url": "https://instance-one.example", "relationship": "owner",
                         "owner_email": "max@example.com", "available": true},
                        {"id": "instance-2", "name": "Two", "host": "instance-two.example",
                         "url": "https://instance-two.example", "relationship": "owner",
                         "owner_email": "max@example.com", "available": true}
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
        let suiteName = "SessionNotificationTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        addTeardownBlock {
            defaults.removePersistentDomain(forName: suiteName)
        }
        let appModel = AppModel(
            defaults: defaults,
            controlPlane: MobileControlPlaneConfiguration(testBaseURL: baseURL),
            pushKeychain: InMemoryPushKeychainStore(),
            pushGenerationStore: InMemoryPushGenerationStore(),
            pushInstallationMarker: InMemoryPushInstallationMarkerStore(),
            pushNotificationCenter: center,
            badgeCenter: SessionTestBadgeCenter()
        )
        try await waitUntil {
            if case .onboarding(.instances) = appModel.route {
                return true
            }
            return false
        }
        return appModel
    }

    /// Delivered notifications across two instances, two accounts, the
    /// non-Session threads, and an undecrypted fallback, plus pending ones.
    private func notificationsAcrossInstances() -> NotificationFixture {
        func entry(
            _ id: String,
            thread: String,
            origin: String?,
            account: String? = "account-1",
            deliveredAt: Date? = Date(timeIntervalSinceNow: -60)
        ) -> PushNotificationCenterEntry {
            var userInfo: [AnyHashable: Any] = [:]
            if let origin {
                userInfo["instance_origin"] = origin
                userInfo["kid"] = "kid"
            }
            if let account, origin != nil {
                userInfo["account_id"] = account
            }
            return PushNotificationCenterEntry(
                identifier: id,
                userInfo: userInfo,
                threadIdentifier: thread,
                deliveredAt: deliveredAt
            )
        }
        return NotificationFixture(
            delivered: [
                entry("one-a", thread: "session:ses_a", origin: one),
                entry("one-a-2", thread: "session:ses_a", origin: one),
                entry("one-b", thread: "session:ses_b", origin: one),
                entry("two-a", thread: "session:ses_a", origin: two),
                entry("other-account-a", thread: "session:ses_a", origin: one, account: "account-2"),
                entry("one-harness", thread: "harness:failure", origin: one),
                entry("one-test", thread: "web-push-test", origin: one),
                entry("fallback", thread: "", origin: nil),
            ],
            pending: [
                entry("one-a-pending", thread: "session:ses_a", origin: one, deliveredAt: nil),
                entry("two-a-pending", thread: "session:ses_a", origin: two, deliveredAt: nil),
            ],
            // Delivered after the read was observed, by the time the
            // delivered list is read: dated when that read happens.
            arrivingLate: { [one] in
                entry("one-a-late", thread: "session:ses_a", origin: one, deliveredAt: Date())
            }
        )
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
}

private struct NotificationFixture {
    let delivered: [PushNotificationCenterEntry]
    let pending: [PushNotificationCenterEntry]
    /// A notification that lands between a read and the list being read.
    let arrivingLate: () -> PushNotificationCenterEntry
}

/// A notification center holding fixed notifications, plus one that lands
/// just before each read of the delivered list, recording what is removed.
/// `passes` counts finished removals (each pass removes once, maybe nothing).
@MainActor
private final class RecordingNotificationCenter: PushNotificationCentering {
    private let fixture: NotificationFixture
    private(set) var passes = 0
    var removedDelivered: [String] = []
    var removedPending: [String] = []

    init(entries: NotificationFixture) {
        fixture = entries
    }

    func deliveredNotifications() async -> [PushNotificationCenterEntry] {
        fixture.delivered + [fixture.arrivingLate()]
    }

    func pendingNotificationRequests() async -> [PushNotificationCenterEntry] {
        fixture.pending
    }

    func removeDeliveredNotifications(withIdentifiers identifiers: [String]) {
        removedDelivered += identifiers
        passes += 1
    }

    func removePendingNotificationRequests(withIdentifiers identifiers: [String]) {
        removedPending += identifiers
    }
}

private final class SessionTestBadgeCenter: AppBadgeCentering {
    func badgeAllowed() async -> Bool {
        true
    }

    func setBadgeCount(_ count: Int) async throws {}
}

private final class SessionWebViewLoadProbe: NSObject, WKNavigationDelegate {
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
