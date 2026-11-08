import Foundation
import UserNotifications
import XCTest
@testable import Avibe

@MainActor
final class NativePushCoordinatorTests: XCTestCase {
    private static let accountA = MobileAccount(
        id: "account-a",
        email: "a@example.com"
    )
    private static let accountB = MobileAccount(
        id: "account-b",
        email: "b@example.com"
    )
    private static let instanceOrigin = "https://instance.example"
    private static let vapidPublicKey =
        "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"

    func testProviderPatchesAreSerializedAndUseLatestGeneration() async throws {
        let store = makeInstallationStore(
            providerGeneration: 0,
            provider: nil,
            providerPatchPending: false
        )
        let recorder = ManagementRecorder()
        recorder.responseHandler = { request in
            if request.method == "PATCH" {
                await recorder.waitForReleaseIfNeeded()
            }
            return Self.successResponse
        }
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        coordinator.didRegister(
            deviceToken: Data(repeating: 1, count: 32),
            environment: "sandbox"
        )
        try await recorder.waitForRequestCount(1)
        coordinator.didRegister(
            deviceToken: Data(repeating: 2, count: 32),
            environment: "sandbox"
        )

        try await Task.sleep(nanoseconds: 30_000_000)
        XCTAssertEqual(recorder.maximumInFlight, 1)
        XCTAssertEqual(recorder.requests.count, 1)

        recorder.release()
        try await recorder.waitForRequestCount(2)
        XCTAssertEqual(recorder.maximumInFlight, 1)
        let generations = try recorder.requests.compactMap(Self.generation)
        XCTAssertEqual(generations, [1, 2])
        XCTAssertEqual(
            recorder.requests.last.flatMap {
                $0.body.flatMap(Self.deviceToken)
            },
            PushBase64URL.encode(Data(repeating: 2, count: 32))
        )
    }

    func testProviderPatchFailuresKeepTheCompleteTuplePending() async throws {
        for statusCode in [401, 502, 503] {
            let store = makeInstallationStore(
                providerGeneration: 0,
                provider: nil,
                providerPatchPending: false
            )
            let recorder = ManagementRecorder()
            recorder.responseHandler = { request in
                if request.method == "PATCH" {
                    return PushManagementResponse(
                        statusCode: statusCode,
                        body: ["error": ["code": "temporary"]]
                    )
                }
                return Self.successResponse
            }
            let coordinator = makeCoordinator(
                store: store,
                management: recorder
            )

            coordinator.didRegister(
                deviceToken: Data(repeating: UInt8(statusCode % 255), count: 32),
                environment: "sandbox"
            )
            try await recorder.waitForRequestCount(1)
            try await waitUntil {
                store.installation?.providerPatchPending == true
            }
            XCTAssertEqual(store.installation?.provider?.generation, 1)
            XCTAssertTrue(store.installation?.providerPatchPending == true)
        }
    }

    func testProviderConflictKeepsTuplePendingAndRetriesWithHigherGeneration() async throws {
        let token = Data(repeating: 21, count: 32)
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: PushProviderState(
                deviceToken: token,
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 1
            ),
            providerPatchPending: true
        )
        let recorder = ManagementRecorder()
        var patchCount = 0
        recorder.responseHandler = { request in
            if request.method == "PATCH" {
                patchCount += 1
                if patchCount == 1 {
                    return PushManagementResponse(
                        statusCode: 409,
                        body: [
                            "error": ["code": "provider_generation_conflict"],
                            "provider_generation": 10,
                            "device_token": PushBase64URL.encode(
                                Data(repeating: 99, count: 32)
                            ),
                            "environment": "production",
                            "topic": "wrong.topic",
                        ]
                    )
                }
            }
            return Self.successResponse
        }
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        coordinator.didRegister(
            deviceToken: token,
            environment: "sandbox"
        )
        try await recorder.waitForRequestCount(1)
        try await waitUntil {
            store.installation?.providerPatchPending == true
                && store.installation?.provider?.generation == 11
        }

        _ = try await coordinator.subscribe(
            vapidPublicKey: Self.vapidPublicKey,
            instanceOrigin: Self.instanceOrigin
        )

        let patches = recorder.requests.filter { $0.method == "PATCH" }
        XCTAssertEqual(patches.count, 2)
        let retried = try XCTUnwrap(patches.last)
        let body = try XCTUnwrap(jsonBody(retried))
        XCTAssertEqual(
            (body["provider_generation"] as? NSNumber)?.int64Value,
            11
        )
        XCTAssertEqual(
            body["device_token"] as? String,
            PushBase64URL.encode(token)
        )
        XCTAssertEqual(body["environment"] as? String, "sandbox")
        XCTAssertEqual(body["topic"] as? String, "bot.avibe.ios")
        XCTAssertFalse(store.installation?.providerPatchPending ?? true)
    }

    func testRelayUnavailableKeepsTuplePendingUntilLaterReconciliation() async throws {
        let token = Data(repeating: 22, count: 32)
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: PushProviderState(
                deviceToken: token,
                environment: "production",
                topic: "bot.avibe.ios",
                generation: 1
            ),
            providerPatchPending: true
        )
        let recorder = ManagementRecorder()
        var patchCount = 0
        recorder.responseHandler = { request in
            if request.method == "PATCH" {
                patchCount += 1
                if patchCount == 1 {
                    return PushManagementResponse(
                        statusCode: 503,
                        body: ["error": ["code": "relay_unavailable"]]
                    )
                }
            }
            return Self.successResponse
        }
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        coordinator.didRegister(
            deviceToken: token,
            environment: "production"
        )
        try await recorder.waitForRequestCount(1)
        try await waitUntil {
            store.installation?.providerPatchPending == true
        }
        XCTAssertEqual(store.installation?.provider?.deviceToken, token)
        XCTAssertEqual(store.installation?.provider?.environment, "production")
        XCTAssertEqual(store.installation?.provider?.topic, "bot.avibe.ios")

        _ = try await coordinator.subscribe(
            vapidPublicKey: Self.vapidPublicKey,
            instanceOrigin: Self.instanceOrigin
        )

        let patches = recorder.requests.filter { $0.method == "PATCH" }
        XCTAssertEqual(patches.count, 2)
        let body = try XCTUnwrap(jsonBody(try XCTUnwrap(patches.last)))
        XCTAssertEqual(body["device_token"] as? String, PushBase64URL.encode(token))
        XCTAssertEqual(body["environment"] as? String, "production")
        XCTAssertEqual(body["topic"] as? String, "bot.avibe.ios")
        XCTAssertFalse(store.installation?.providerPatchPending ?? true)
    }

    func testProviderGenerationUsesInt64WireIntegersAndStopsAtMaximum() async throws {
        let aboveJavaScriptSafeInteger = Int64(9_007_199_254_740_992)
        let store = makeInstallationStore(
            providerGeneration: aboveJavaScriptSafeInteger,
            provider: nil,
            providerPatchPending: false
        )
        let recorder = ManagementRecorder()
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        coordinator.didRegister(
            deviceToken: Data(repeating: 3, count: 32),
            environment: "sandbox"
        )
        try await recorder.waitForRequestCount(1)
        XCTAssertEqual(
            try XCTUnwrap(Self.generation(recorder.requests[0])),
            aboveJavaScriptSafeInteger + 1
        )
        XCTAssertEqual(
            store.installation?.provider?.generation,
            aboveJavaScriptSafeInteger + 1
        )

        let maximumStore = makeInstallationStore(
            providerGeneration: Int64.max - 1,
            provider: nil,
            providerPatchPending: false
        )
        let maximumRecorder = ManagementRecorder()
        let maximumCoordinator = makeCoordinator(
            store: maximumStore,
            management: maximumRecorder
        )
        maximumCoordinator.didRegister(
            deviceToken: Data(repeating: 4, count: 32),
            environment: "production"
        )
        try await maximumRecorder.waitForRequestCount(1)
        XCTAssertEqual(
            try XCTUnwrap(Self.generation(maximumRecorder.requests[0])),
            Int64.max
        )
        XCTAssertEqual(
            maximumStore.installation?.provider?.generation,
            Int64.max
        )

        maximumCoordinator.didRegister(
            deviceToken: Data(repeating: 5, count: 32),
            environment: "production"
        )
        try await Task.sleep(nanoseconds: 30_000_000)
        XCTAssertEqual(maximumRecorder.requests.count, 1)
        XCTAssertEqual(
            maximumStore.installation?.provider?.generation,
            Int64.max
        )
    }

    func testTokenWaitTimeoutAndCancellationCanFinishOnlyOnce() async throws {
        let timeoutStore = makeInstallationStore(
            providerGeneration: 0,
            provider: nil,
            providerPatchPending: false
        )
        let timeoutCoordinator = makeCoordinator(
            store: timeoutStore,
            management: ManagementRecorder(),
            tokenWaitSeconds: 0.01
        )
        do {
            _ = try await timeoutCoordinator.subscribe(
                vapidPublicKey: Self.vapidPublicKey,
                instanceOrigin: Self.instanceOrigin
            )
            XCTFail("Expected provider token wait to time out")
        } catch {
            // The timeout is the expected result.
        }
        timeoutCoordinator.didRegister(
            deviceToken: Data(repeating: 6, count: 32),
            environment: "sandbox"
        )
        try await Task.sleep(nanoseconds: 30_000_000)

        let cancellationStore = makeInstallationStore(
            providerGeneration: 0,
            provider: nil,
            providerPatchPending: false
        )
        let cancellationCoordinator = makeCoordinator(
            store: cancellationStore,
            management: ManagementRecorder(),
            tokenWaitSeconds: 5
        )
        let task = Task {
            try await cancellationCoordinator.subscribe(
                vapidPublicKey: Self.vapidPublicKey,
                instanceOrigin: Self.instanceOrigin
            )
        }
        try await Task.sleep(nanoseconds: 30_000_000)
        task.cancel()
        _ = try? await task.value
        cancellationCoordinator.didRegister(
            deviceToken: Data(repeating: 7, count: 32),
            environment: "sandbox"
        )
        try await Task.sleep(nanoseconds: 30_000_000)
    }

    func testLogoutReconcilesPendingCreatesBeforeBulkRevoke() async throws {
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: PushProviderState(
                deviceToken: Data(repeating: 8, count: 32),
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 1
            ),
            providerPatchPending: false
        )
        let pending = makeRecord(
            kid: "pending-registration",
            state: .pending,
            pendingRevoke: false,
            bindGeneration: 1
        )
        try store.saveRecord(pending)
        let recorder = ManagementRecorder()
        recorder.responseHandler = { _ in Self.successResponse }
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        await coordinator.prepareForLogout(throughBindGeneration: 1)

        XCTAssertEqual(
            recorder.requests.map(\.method),
            ["PUT", "POST"]
        )
        XCTAssertEqual(
            recorder.requests.map(\.path),
            [
                "/api/push/apns/registrations/pending-registration",
                "/api/push/apns/installations/\(store.installation!.installationID.uuidString)/revoke",
            ]
        )
        XCTAssertNil(try store.loadRecord(kid: pending.kid))
        XCTAssertNil(store.installation?.pendingBulkRevokeThroughGeneration)
    }

    func testLogoutLeavesBulkRevokePendingWhenCreateCannotBeReconciled() async throws {
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: PushProviderState(
                deviceToken: Data(repeating: 9, count: 32),
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 1
            ),
            providerPatchPending: false
        )
        let pending = makeRecord(
            kid: "pending-registration",
            state: .pending,
            pendingRevoke: false,
            bindGeneration: 1
        )
        try store.saveRecord(pending)
        let recorder = ManagementRecorder()
        recorder.responseHandler = { request in
            if request.method == "PUT" {
                return PushManagementResponse(
                    statusCode: 502,
                    body: ["error": ["code": "temporary"]]
                )
            }
            XCTFail("Bulk revoke must wait for create reconciliation")
            return Self.successResponse
        }
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        await coordinator.prepareForLogout(throughBindGeneration: 1)

        XCTAssertEqual(recorder.requests.map(\.method), ["PUT"])
        XCTAssertEqual(
            store.installation?.pendingBulkRevokeThroughGeneration,
            1
        )
        XCTAssertNotNil(try store.loadRecord(kid: pending.kid))
    }

    func testRebindUsesHigherGenerationAndConcreteReplacementIDs() async throws {
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: PushProviderState(
                deviceToken: Data(repeating: 10, count: 32),
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 1
            ),
            providerPatchPending: false
        )
        try store.saveRecord(
            makeRecord(
                kid: "old-registration",
                state: .active,
                pendingRevoke: false,
                bindGeneration: 1
            )
        )
        let recorder = ManagementRecorder()
        recorder.responseHandler = { _ in Self.successResponse }
        let account = Locked<MobileAccount?>(Self.accountA)
        let coordinator = makeCoordinator(
            store: store,
            management: recorder,
            account: account
        )

        await coordinator.prepareForLogout(throughBindGeneration: 1)
        coordinator.resumeAfterAccountActivation()
        account.value = Self.accountB
        _ = try await coordinator.subscribe(
            vapidPublicKey: Self.vapidPublicKey,
            instanceOrigin: Self.instanceOrigin
        )

        let bind = try XCTUnwrap(
            recorder.requests.first {
                $0.path.hasSuffix("/binding")
            }
        )
        let bindBody = try XCTUnwrap(jsonBody(bind))
        XCTAssertEqual(
            (bindBody["bind_generation"] as? NSNumber)?.int64Value,
            2
        )
        let create = try XCTUnwrap(
            recorder.requests.first {
                $0.method == "PUT"
                    && $0.path.contains("/registrations/")
            }
        )
        let createBody = try XCTUnwrap(jsonBody(create))
        XCTAssertEqual(createBody["replaces_registration_ids"] as? [String], [])
    }

    func testSubscriptionUsesConcreteReplacementIDs() async throws {
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: PushProviderState(
                deviceToken: Data(repeating: 15, count: 32),
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 1
            ),
            providerPatchPending: false
        )
        try store.saveRecord(
            makeRecord(
                kid: "old-registration",
                state: .active,
                pendingRevoke: false,
                bindGeneration: 1
            )
        )
        let recorder = ManagementRecorder()
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        _ = try await coordinator.subscribe(
            vapidPublicKey: Self.vapidPublicKey,
            instanceOrigin: Self.instanceOrigin
        )

        let create = try XCTUnwrap(
            recorder.requests.first {
                $0.method == "PUT"
                    && $0.path.contains("/registrations/")
            }
        )
        let createBody = try XCTUnwrap(jsonBody(create))
        XCTAssertEqual(
            createBody["replaces_registration_ids"] as? [String],
            ["old-registration"]
        )
    }

    func testUnsubscribeScopesTheRecordToAccountAndInstanceOrigin() async throws {
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: PushProviderState(
                deviceToken: Data(repeating: 16, count: 32),
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 1
            ),
            providerPatchPending: false
        )
        try store.saveRecord(
            PushKeyRecord(
                kid: "a-other-account",
                instanceOrigin: "https://other-instance.example",
                accountID: Self.accountB.id,
                accountGeneration: 1,
                bindGeneration: 1,
                privateKey: Data(repeating: 17, count: 32),
                authSecret: Data(repeating: 18, count: 16),
                vapidKeyHash: String(repeating: "b", count: 64),
                state: .active,
                pendingRevoke: false,
                replacementRegistrationIDs: []
            )
        )
        try store.saveRecord(
            PushKeyRecord(
                kid: "b-stale-generation",
                instanceOrigin: Self.instanceOrigin,
                accountID: Self.accountA.id,
                accountGeneration: 0,
                bindGeneration: 1,
                privateKey: Data(repeating: 17, count: 32),
                authSecret: Data(repeating: 18, count: 16),
                vapidKeyHash: String(repeating: "b", count: 64),
                state: .active,
                pendingRevoke: false,
                replacementRegistrationIDs: []
            )
        )
        let target = makeRecord(
            kid: "z-current-account",
            state: .active,
            pendingRevoke: false,
            bindGeneration: 1
        )
        try store.saveRecord(target)
        let recorder = ManagementRecorder()
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        let revoked = try await coordinator.unsubscribe(
            instanceOrigin: Self.instanceOrigin
        )
        XCTAssertTrue(revoked)

        XCTAssertEqual(
            recorder.requests.map(\.path),
            ["/api/push/apns/registrations/\(target.kid)"]
        )
        XCTAssertNil(try store.loadRecord(kid: target.kid))
        XCTAssertNotNil(try store.loadRecord(kid: "a-other-account"))
        XCTAssertNotNil(try store.loadRecord(kid: "b-stale-generation"))
    }

    func testLogoutRemovesNotificationsForTheFormerAccount() async throws {
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: PushProviderState(
                deviceToken: Data(repeating: 19, count: 32),
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 1
            ),
            providerPatchPending: false
        )
        var cleanedAccountIDs: [String] = []
        let coordinator = makeCoordinator(
            store: store,
            management: ManagementRecorder(),
            notificationCleanup: { accountID in
                cleanedAccountIDs.append(accountID)
            }
        )

        await coordinator.prepareForLogout(throughBindGeneration: 1)

        XCTAssertEqual(cleanedAccountIDs, [Self.accountA.id])
    }

    func testReinstallMovesCredentialToBoundedOrphanRevoke() async throws {
        let store = InMemoryPushKeychainStore()
        let oldID = UUID()
        let newMarkerID = UUID()
        let secret = Data(repeating: 11, count: 32)
        store.installation = PushInstallationState(
            installationID: oldID,
            installationSecret: secret,
            accountID: Self.accountA.id,
            bindGeneration: 4,
            providerGeneration: 1,
            provider: nil,
            pendingBulkRevokeThroughGeneration: nil
        )
        let marker = InMemoryPushInstallationMarkerStore()
        marker.installationID = newMarkerID
        let recorder = ManagementRecorder()
        recorder.responseHandler = { _ in Self.successResponse }
        let coordinator = makeCoordinator(
            store: store,
            management: recorder,
            marker: marker,
            account: Locked<MobileAccount?>(nil)
        )

        coordinator.resumeAfterAccountActivation()
        try await waitUntil {
            store.installation?.installationID != oldID
        }
        try await waitUntil {
            recorder.requests.count == 1
        }

        let request = try XCTUnwrap(recorder.requests.first)
        XCTAssertEqual(
            request.path,
            "/api/push/apns/installations/\(oldID.uuidString)/revoke"
        )
        XCTAssertEqual(store.orphanedInstallations.count, 0)
        XCTAssertNotEqual(store.installation?.installationID, oldID)
        XCTAssertEqual(store.installation?.installationSecret.count, 32)
    }

    func testInvalidNotificationTapUsesInstanceRecovery() {
        var taps = 0
        var recoveries = 0
        let coordinator = makeCoordinator(
            store: InMemoryPushKeychainStore(),
            management: ManagementRecorder(),
            onTap: { _ in taps += 1 },
            onTapRecovery: { recoveries += 1 }
        )

        coordinator.handleNotificationTap(
            userInfo: [
                "account_id": Self.accountA.id,
                "instance_origin": Self.instanceOrigin,
                "url": "https://evil.example/phish",
            ]
        )
        coordinator.handleNotificationTap(
            userInfo: [
                "account_id": Self.accountA.id,
                "instance_origin": Self.instanceOrigin,
                "url": "/chat/\u{1b}[31m",
            ]
        )
        coordinator.handleNotificationTap(
            userInfo: [
                "account_id": Self.accountA.id,
                "instance_origin": Self.instanceOrigin,
                "url": "/chat\\evil",
            ]
        )

        XCTAssertEqual(taps, 0)
        XCTAssertEqual(recoveries, 3)
    }

    private static let successResponse = PushManagementResponse(
        statusCode: 200,
        body: [:]
    )

    private func makeCoordinator(
        store: InMemoryPushKeychainStore,
        management: ManagementRecorder,
        tokenWaitSeconds: Double = 0.5,
        marker: InMemoryPushInstallationMarkerStore? = nil,
        account: Locked<MobileAccount?>? = nil,
        notificationCleanup: @escaping @MainActor (String) async -> Void = { _ in },
        onTap: @escaping @MainActor (PushNotificationTap) -> Void = { _ in },
        onTapRecovery: @escaping @MainActor () -> Void = {}
    ) -> NativePushCoordinator {
        let account = account ?? Locked<MobileAccount?>(Self.accountA)
        let installationMarker =
            marker ?? InMemoryPushInstallationMarkerStore()
        if installationMarker.installationID == nil {
            installationMarker.installationID = store.installation?.installationID
        }
        let generationStore = InMemoryPushGenerationStore()
        generationStore.generation = account.value == nil ? 0 : 1
        return NativePushCoordinator(
            keychain: store,
            generationStore: generationStore,
            managementRequest: { request in
                management.record(request)
                return try await management.respond(to: request)
            },
            accountProvider: { account.value },
            registerForRemoteNotifications: {},
            requestNotificationAuthorization: { true },
            installationMarker: installationMarker,
            removeAccountNotifications: notificationCleanup,
            tokenWaitSeconds: tokenWaitSeconds,
            onTap: onTap,
            onTapRecovery: onTapRecovery
        )
    }

    private func makeInstallationStore(
        providerGeneration: Int64,
        provider: PushProviderState?,
        providerPatchPending: Bool
    ) -> InMemoryPushKeychainStore {
        let store = InMemoryPushKeychainStore()
        store.installation = PushInstallationState(
            installationID: UUID(),
            installationSecret: Data(repeating: 12, count: 32),
            accountID: Self.accountA.id,
            bindGeneration: 1,
            providerGeneration: providerGeneration,
            provider: provider,
            providerPatchPending: providerPatchPending,
            pendingBulkRevokeThroughGeneration: nil
        )
        return store
    }

    private func makeRecord(
        kid: String,
        state: PushKeyRecord.State,
        pendingRevoke: Bool,
        bindGeneration: Int64
    ) -> PushKeyRecord {
        PushKeyRecord(
            kid: kid,
            instanceOrigin: Self.instanceOrigin,
            accountID: Self.accountA.id,
            accountGeneration: 1,
            bindGeneration: bindGeneration,
            privateKey: Data(repeating: 13, count: 32),
            authSecret: Data(repeating: 14, count: 16),
            vapidKeyHash: String(repeating: "a", count: 64),
            state: state,
            pendingRevoke: pendingRevoke,
            replacementRegistrationIDs: []
        )
    }

    private static func generation(
        _ request: ManagementRecorder.RecordedRequest
    ) -> Int64? {
        guard
            let body = request.body,
            let object = try? JSONSerialization.jsonObject(with: body),
            let json = object as? [String: Any]
        else {
            return nil
        }
        return (json["provider_generation"] as? NSNumber)?.int64Value
    }

    private static func deviceToken(
        _ body: Data
    ) -> String? {
        guard
            let object = try? JSONSerialization.jsonObject(with: body),
            let json = object as? [String: Any]
        else {
            return nil
        }
        return json["device_token"] as? String
    }

    private func jsonBody(
        _ request: ManagementRecorder.RecordedRequest
    ) throws -> [String: Any]? {
        try XCTUnwrap(
            request.body.flatMap {
                try? JSONSerialization.jsonObject(with: $0)
            } as? [String: Any]
        )
    }

    private func waitUntil(
        _ condition: @escaping @MainActor () -> Bool
    ) async throws {
        for _ in 0..<100 {
            if condition() {
                return
            }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTFail("Timed out waiting for test condition")
    }
}

@MainActor
private final class Locked<Value> {
    var value: Value

    init(_ value: Value) {
        self.value = value
    }
}

@MainActor
private final class ManagementRecorder {
    struct RecordedRequest {
        let method: String
        let path: String
        let body: Data?
    }

    var requests: [RecordedRequest] = []
    var maximumInFlight = 0
    var responseHandler: (RecordedRequest) async throws -> PushManagementResponse = { _ in
        PushManagementResponse(statusCode: 200, body: [:])
    }

    private var inFlight = 0
    private var releaseContinuation: CheckedContinuation<Void, Never>?

    func record(_ request: PushManagementRequest) {
        inFlight += 1
        maximumInFlight = max(maximumInFlight, inFlight)
        requests.append(
            RecordedRequest(
                method: request.method,
                path: request.path,
                body: request.body
            )
        )
    }

    func respond(to request: PushManagementRequest) async throws
        -> PushManagementResponse
    {
        defer { inFlight -= 1 }
        return try await responseHandler(
            RecordedRequest(
                method: request.method,
                path: request.path,
                body: request.body
            )
        )
    }

    func waitForRequestCount(_ count: Int) async throws {
        for _ in 0..<100 {
            if requests.count >= count {
                return
            }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTFail("Timed out waiting for \(count) management requests")
    }

    func waitForReleaseIfNeeded() async {
        await withCheckedContinuation { continuation in
            releaseContinuation = continuation
        }
    }

    func release() {
        releaseContinuation?.resume()
        releaseContinuation = nil
    }
}
