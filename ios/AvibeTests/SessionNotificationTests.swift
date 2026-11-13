import UIKit
import WebKit
import XCTest
@testable import Avibe

/// A Session's notifications leave Notification Center once it is read in the
/// App: when its page has shown it and then read its Inbox, and when the
/// instance has nothing unread.
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

        cleaner.clearSession("ses_a", instanceOrigin: one, accountID: "account-1", readAt: Date())
        try await waitUntil { center.passes == 1 }

        // A result for the same Session delivered after it was on screen
        // ("one-a-late") is news the read did not cover; a pending request
        // has not been delivered at all.
        XCTAssertEqual(center.removedDelivered, ["one-a", "one-a-2"])
        XCTAssertEqual(center.removedPending, [])
    }

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
        // Let the push coordinator finish its own startup work first.
        try await Task.sleep(for: .milliseconds(200))
        let startupPasses = center.passes

        // No instance is open: a Session page is not the selected one's.
        appModel.primaryLocationDidChange(try XCTUnwrap(URL(string: "\(one)/chat/ses_a")))

        _ = try appModel.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await waitUntil {
            appModel.route.runtime?.instance.id == "instance-1"
        }
        XCTAssertTrue(appModel.setBadgeCount(2, origin: originOne))

        // A Session covered by a child window (whose page may send the
        // badge), one left before the page read its Inbox, and another
        // instance's page, are not read here.
        appModel.primaryLocationDidChange(try XCTUnwrap(URL(string: "\(one)/chat/ses_a")))
        appModel.primaryLocationDidChange(nil)
        XCTAssertTrue(appModel.setBadgeCount(2, origin: originOne))
        appModel.primaryLocationDidChange(try XCTUnwrap(URL(string: "\(one)/chat/ses_a")))
        appModel.primaryLocationDidChange(try XCTUnwrap(URL(string: "\(one)/inbox")))
        appModel.primaryLocationDidChange(try XCTUnwrap(URL(string: "\(two)/chat/ses_a")))
        XCTAssertTrue(appModel.setBadgeCount(2, origin: originOne))

        // Shown alone, a Session is not yet read: the page may not have
        // loaded it (offline after a return to the foreground).
        appModel.primaryLocationDidChange(
            try XCTUnwrap(URL(string: "\(one)/chat/ses_a?msg=m1&from=notification"))
        )
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(center.passes, startupPasses)

        // The page's next badge, read after showing it, retires that
        // Session's notifications delivered by then; another instance's
        // badge does not.
        XCTAssertFalse(appModel.setBadgeCount(2, origin: originTwo))
        XCTAssertTrue(appModel.setBadgeCount(2, origin: originOne))
        try await waitUntil { center.passes == startupPasses + 1 }
        XCTAssertEqual(center.removedDelivered, ["one-a", "one-a-2"])
        center.removedDelivered = []

        // Nothing unread retires every Session of the instance; another
        // instance's zero does not.
        appModel.primaryLocationDidChange(try XCTUnwrap(URL(string: "\(one)/inbox")))
        XCTAssertFalse(appModel.setBadgeCount(0, origin: originTwo))
        XCTAssertTrue(appModel.setBadgeCount(0, origin: originOne))
        try await waitUntil { center.passes == startupPasses + 2 }
        XCTAssertEqual(center.removedDelivered, ["one-a", "one-a-2", "one-b"])
    }

    @MainActor
    func testTheRuntimeReportsWhereItsPageIsAgainOnForegroundAndWhileCovered() async throws {
        let instance = try XCTUnwrap(WebOrigin.parseConfigured(one))
        var reported: [String] = []
        let controller = WebViewController(
            instanceOrigin: instance,
            websiteDataStore: .nonPersistent(),
            primaryLocationChanged: { reported.append($0?.absoluteString ?? "covered") },
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 700))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        defer {
            window.rootViewController = nil
            window.isHidden = true
        }
        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        let probe = SessionWebViewLoadProbe()
        webView.navigationDelegate = probe
        try await probe.loadHTML(
            """
            <!doctype html><title>Workbench</title>
            <button id="open-child" onclick="window.open('/chat/ses_b', '_blank')">Open</button>
            """,
            baseURL: instance.url,
            in: webView
        )

        // The Web UI routes without a load: `pushState`.
        _ = try await webView.evaluateJavaScript(
            "history.pushState({}, '', '/chat/ses_a'); location.href"
        )
        try await waitUntil { reported.last == "\(one)/chat/ses_a" }

        var before = reported.count
        NotificationCenter.default.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        XCTAssertEqual(reported.count, before + 1)
        XCTAssertEqual(reported.last, "\(one)/chat/ses_a")

        // A child window covers the page, and its own page may send the
        // badge: no Session of the primary is on screen until it closes.
        _ = try await webView.evaluateJavaScript(
            "document.querySelector('#open-child').click()"
        )
        try await waitUntil { reported.last == "covered" }
        var popupNavigation: UINavigationController?
        try await waitUntil {
            popupNavigation = controller.presentedViewController as? UINavigationController
            return popupNavigation != nil
        }
        var child: WKWebView?
        try await waitUntil {
            child = popupNavigation?.topViewController?.viewIfLoaded?.subviews
                .compactMap { $0 as? WKWebView }.first
            return child != nil
        }
        _ = try await child?.evaluateJavaScript("window.close(); true")
        try await waitUntil { reported.last == "\(one)/chat/ses_a" }

        // Released for an account transition: it reports nothing more.
        _ = controller.invalidateForAccountTransition()
        before = reported.count
        NotificationCenter.default.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        XCTAssertEqual(reported.count, before)
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
