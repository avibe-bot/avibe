import XCTest
@testable import Avibe

/// An instance's Session notifications leave Notification Center once its
/// Web UI reports nothing unread (`badge.set({count: 0})`).
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
