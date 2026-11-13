import UIKit
import WebKit
import XCTest
@testable import Avibe

/// A Session's notifications leave Notification Center once it is read in the
/// App: when its page is shown, and when the instance has nothing unread.
final class SessionNotificationTests: XCTestCase {
    private let one = "https://instance-one.example"
    private let two = "https://instance-two.example"

    func testTheShownSessionIsReadFromTheChatPath() throws {
        func shown(_ string: String) throws -> String? {
            SessionNotificationThread.sessionID(shownAt: try XCTUnwrap(URL(string: string)))
        }
        XCTAssertEqual(
            try shown("\(one)/chat/ses_123?msg=msg_456&from=notification"),
            "ses_123"
        )
        XCTAssertEqual(try shown("\(one)/chat/ses%20a#top"), "ses a")
        XCTAssertNil(try shown("\(one)/chat"))
        XCTAssertNil(try shown("\(one)/chat/"))
        XCTAssertNil(try shown("\(one)/chat/ses_123/files"))
        XCTAssertNil(try shown("\(one)/inbox"))
        XCTAssertNil(try shown("\(one)/projects/chat/ses_123"))
    }

    @MainActor
    func testOpeningASessionClearsOnlyItsNotificationsOnThatInstanceAndAccount() async throws {
        let center = RecordingNotificationCenter(entries: notificationsAcrossInstances())
        let cleaner = SessionNotificationCleaner(center: center)

        cleaner.clearSession("ses_a", instanceOrigin: one, accountID: "account-1")
        try await waitUntil { center.reads == 1 }

        XCTAssertEqual(center.removedDelivered, ["one-a", "one-a-2"])
        XCTAssertEqual(center.removedPending, ["one-a-pending"])
    }

    @MainActor
    func testNothingUnreadClearsEverySessionOfThatInstanceButNotNotices() async throws {
        let center = RecordingNotificationCenter(entries: notificationsAcrossInstances())
        let cleaner = SessionNotificationCleaner(center: center)

        cleaner.clearAllSessions(instanceOrigin: one, accountID: "account-1")
        try await waitUntil { center.reads == 1 }

        // Another instance, another account, a workspace notice, the test
        // notification, and one the extension could not decrypt all stay.
        XCTAssertEqual(center.removedDelivered, ["one-a", "one-a-2", "one-b"])
        XCTAssertEqual(center.removedPending, ["one-a-pending"])
    }

    @MainActor
    func testTheSelectedInstancesPageAndItsZeroBadgeClearItsSessions() async throws {
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
        let center = RecordingNotificationCenter(entries: notificationsAcrossInstances())
        let appModel = AppModel(
            defaults: defaults,
            controlPlane: MobileControlPlaneConfiguration(testBaseURL: baseURL),
            pushKeychain: InMemoryPushKeychainStore(),
            pushGenerationStore: InMemoryPushGenerationStore(),
            pushInstallationMarker: InMemoryPushInstallationMarkerStore(),
            pushNotificationCenter: center,
            badgeCenter: SessionTestBadgeCenter()
        )
        let originOne = try XCTUnwrap(WebOrigin.parseConfigured(one))
        let originTwo = try XCTUnwrap(WebOrigin.parseConfigured(two))
        try await waitUntil {
            if case .onboarding(.instances) = appModel.route {
                return true
            }
            return false
        }
        // The coordinator's own startup reads (push recovery) are not ours.
        try await Task.sleep(for: .milliseconds(200))
        let startupReads = center.reads

        // No instance is open: a Session page clears nothing.
        appModel.primaryLocationDidChange(try XCTUnwrap(URL(string: "\(one)/chat/ses_a")))

        _ = try appModel.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await waitUntil {
            appModel.route.runtime?.instance.id == "instance-1"
        }

        // Another instance's page, and a page that is not a Session, clear
        // nothing; the selected instance's Session page clears that Session.
        appModel.primaryLocationDidChange(try XCTUnwrap(URL(string: "\(two)/chat/ses_a")))
        appModel.primaryLocationDidChange(try XCTUnwrap(URL(string: "\(one)/inbox")))
        appModel.primaryLocationDidChange(
            try XCTUnwrap(URL(string: "\(one)/chat/ses_a?msg=m1&from=notification"))
        )
        try await waitUntil { center.reads == startupReads + 1 }
        XCTAssertEqual(center.removedDelivered, ["one-a", "one-a-2"])
        center.removedDelivered = []
        center.removedPending = []

        // A count above zero, or a zero from another instance, clears
        // nothing; the selected instance's zero clears all its Sessions.
        XCTAssertTrue(appModel.setBadgeCount(2, origin: originOne))
        XCTAssertFalse(appModel.setBadgeCount(0, origin: originTwo))
        XCTAssertTrue(appModel.setBadgeCount(0, origin: originOne))
        try await waitUntil { center.reads == startupReads + 2 }
        XCTAssertEqual(center.removedDelivered, ["one-a", "one-a-2", "one-b"])
    }

    @MainActor
    func testTheRuntimeReportsWhereItsPageIsAndAgainOnForeground() async throws {
        let instance = try XCTUnwrap(WebOrigin.parseConfigured(one))
        var reported: [String] = []
        let controller = WebViewController(
            instanceOrigin: instance,
            websiteDataStore: .nonPersistent(),
            primaryLocationChanged: { reported.append($0.absoluteString) },
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 700))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        defer {
            window.isHidden = true
        }
        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        let probe = SessionWebViewLoadProbe()
        webView.navigationDelegate = probe
        try await probe.loadHTML(
            "<!doctype html><title>Workbench</title>",
            baseURL: instance.url,
            in: webView
        )

        // The Web UI routes without a load: `pushState`.
        _ = try await webView.evaluateJavaScript(
            "history.pushState({}, '', '/chat/ses_a'); location.href"
        )
        try await waitUntil { reported.last == "\(one)/chat/ses_a" }

        let before = reported.count
        NotificationCenter.default.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        XCTAssertEqual(reported.count, before + 1)
        XCTAssertEqual(reported.last, "\(one)/chat/ses_a")

        // Released for an account transition: it reports nothing more.
        _ = controller.invalidateForAccountTransition()
        NotificationCenter.default.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        XCTAssertEqual(reported.count, before + 1)
    }

    /// Delivered notifications across two instances, two accounts, the
    /// non-Session threads, and an undecrypted fallback, plus pending ones.
    private func notificationsAcrossInstances() -> (
        delivered: [PushNotificationCenterEntry],
        pending: [PushNotificationCenterEntry]
    ) {
        func entry(
            _ id: String,
            thread: String,
            origin: String?,
            account: String? = "account-1"
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
                threadIdentifier: thread
            )
        }
        return (
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
                entry("one-a-pending", thread: "session:ses_a", origin: one),
                entry("two-a-pending", thread: "session:ses_a", origin: two),
            ]
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

/// A notification center holding fixed notifications that records what is
/// removed. `reads` counts completed reads of the delivered list.
@MainActor
private final class RecordingNotificationCenter: PushNotificationCentering {
    private let delivered: [PushNotificationCenterEntry]
    private let pending: [PushNotificationCenterEntry]
    private(set) var reads = 0
    var removedDelivered: [String] = []
    var removedPending: [String] = []

    init(
        entries: (
            delivered: [PushNotificationCenterEntry],
            pending: [PushNotificationCenterEntry]
        )
    ) {
        delivered = entries.delivered
        pending = entries.pending
    }

    func deliveredNotifications() async -> [PushNotificationCenterEntry] {
        delivered
    }

    func pendingNotificationRequests() async -> [PushNotificationCenterEntry] {
        // Read second, so a finished read means both lists were read.
        defer { reads += 1 }
        return pending
    }

    func removeDeliveredNotifications(withIdentifiers identifiers: [String]) {
        removedDelivered += identifiers
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
