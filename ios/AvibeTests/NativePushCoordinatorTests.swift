import CryptoKit
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

    func testAccountActivationReregistersForRemoteNotifications() async throws {
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: PushProviderState(
                deviceToken: Data(repeating: 30, count: 32),
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 1
            ),
            providerPatchPending: false
        )
        try store.saveRecord(
            makeRecord(
                kid: "active-registration",
                state: .active,
                pendingRevoke: false,
                bindGeneration: 1
            )
        )
        var registrationCalls = 0
        let coordinator = makeCoordinator(
            store: store,
            management: ManagementRecorder(),
            registerForRemoteNotifications: {
                registrationCalls += 1
            }
        )

        try await Task.sleep(for: .milliseconds(20))
        registrationCalls = 0
        coordinator.resumeAfterAccountActivation()

        try await waitUntil { registrationCalls == 1 }
    }

    func testEqualProviderTupleAfterRelayInvalidationAllocatesHigherGenerationAndPatches()
        async throws
    {
        let token = Data(repeating: 31, count: 32)
        let store = makeInstallationStore(
            providerGeneration: 7,
            provider: PushProviderState(
                deviceToken: token,
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 7
            ),
            providerPatchPending: false
        )
        try store.saveRecord(
            makeRecord(
                kid: "active-registration",
                state: .active,
                pendingRevoke: false,
                bindGeneration: 1
            )
        )
        let recorder = ManagementRecorder()
        recorder.responseHandler = { request in
            XCTAssertEqual(request.method, "PATCH")
            return Self.successResponse
        }
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        coordinator.didRegister(deviceToken: token, environment: "sandbox")
        try await recorder.waitForRequestCount(1)

        XCTAssertEqual(recorder.requests.count, 1)
        XCTAssertEqual(recorder.requests.first.flatMap(Self.generation), 8)
        XCTAssertEqual(store.installation?.providerGeneration, 8)
        XCTAssertEqual(store.installation?.provider?.generation, 8)
        XCTAssertFalse(store.installation?.providerPatchPending ?? true)
    }

    func testRegistrationFailurePreservesStateAndRetriesOnActivation()
        async throws
    {
        let token = Data(repeating: 32, count: 32)
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: PushProviderState(
                deviceToken: token,
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 1
            ),
            providerPatchPending: false
        )
        try store.saveRecord(
            makeRecord(
                kid: "active-registration",
                state: .active,
                pendingRevoke: false,
                bindGeneration: 1
            )
        )
        let authorization = Locked(UNAuthorizationStatus.notDetermined)
        var registrationCalls = 0
        let coordinator = makeCoordinator(
            store: store,
            management: ManagementRecorder(),
            notificationAuthorizationStatus: { authorization.value },
            registerForRemoteNotifications: {
                registrationCalls += 1
            }
        )
        try await Task.sleep(for: .milliseconds(20))

        coordinator.didFailToRegister()
        XCTAssertEqual(store.installation?.provider?.generation, 1)
        XCTAssertEqual(store.installation?.provider?.deviceToken, token)
        XCTAssertFalse(store.installation?.providerPatchPending ?? true)

        authorization.value = .authorized
        coordinator.resumeAfterAccountActivation()
        try await waitUntil { registrationCalls == 1 }
    }

    func testDeniedOrUndeterminedAuthorizationMakesStatusUnavailable()
        async throws
    {
        let authorization = Locked(UNAuthorizationStatus.denied)
        let coordinator = makeCoordinator(
            store: InMemoryPushKeychainStore(),
            management: ManagementRecorder(),
            notificationAuthorizationStatus: { authorization.value }
        )

        let deniedStatus = try await coordinator.status(
            vapidPublicKey: Self.vapidPublicKey,
            instanceOrigin: Self.instanceOrigin
        )["state"] as? String
        XCTAssertEqual(deniedStatus, "unavailable")

        authorization.value = .notDetermined
        let undeterminedStatus = try await coordinator.status(
            vapidPublicKey: Self.vapidPublicKey,
            instanceOrigin: Self.instanceOrigin
        )["state"] as? String
        XCTAssertEqual(undeterminedStatus, "unavailable")
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

    func testProviderConflictDiscardsObsoleteTupleWithoutRetry() async throws {
        let localToken = Data(repeating: 21, count: 32)
        let serverToken = Data(repeating: 99, count: 32)
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: PushProviderState(
                deviceToken: localToken,
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 1
            ),
            providerPatchPending: true
        )
        let recorder = ManagementRecorder()
        recorder.responseHandler = { request in
            if request.method == "PATCH" {
                return PushManagementResponse(
                    statusCode: 409,
                    body: [
                        "error": ["code": "stale_provider_generation"],
                        "provider_generation": 10,
                        "device_token_sha256": Self.sha256(serverToken),
                        "environment": "production",
                        "topic": "wrong.topic",
                    ]
                )
            }
            return Self.successResponse
        }
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        coordinator.resumeAfterAccountActivation()
        try await recorder.waitForRequestCount(1)
        try await waitUntil {
            store.installation?.providerPatchPending == false
        }

        let patches = recorder.requests.filter { $0.method == "PATCH" }
        XCTAssertEqual(patches.count, 1)
        let body = try XCTUnwrap(jsonBody(try XCTUnwrap(patches.first)))
        XCTAssertEqual((body["provider_generation"] as? NSNumber)?.int64Value, 1)
        XCTAssertEqual(body["device_token"] as? String, PushBase64URL.encode(localToken))
        XCTAssertNil(store.installation?.provider)
        XCTAssertEqual(store.installation?.providerGeneration, 10)
    }

    func testProviderConflictUsesServerGenerationOnlyForNewerCallback()
        async throws
    {
        let oldToken = Data(repeating: 21, count: 32)
        let serverToken = Data(repeating: 99, count: 32)
        let newToken = Data(repeating: 42, count: 32)
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: PushProviderState(
                deviceToken: oldToken,
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
                    await recorder.waitForReleaseIfNeeded()
                    return PushManagementResponse(
                        statusCode: 409,
                        body: [
                            "error": ["code": "stale_provider_generation"],
                            "provider_generation": 10,
                            "device_token_sha256": Self.sha256(serverToken),
                            "environment": "production",
                            "topic": "bot.avibe.ios",
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

        coordinator.resumeAfterAccountActivation()
        try await recorder.waitForRequestCount(1)
        coordinator.didRegister(
            deviceToken: newToken,
            environment: "sandbox"
        )
        try await waitUntil {
            store.installation?.provider?.deviceToken == newToken
                && store.installation?.provider?.generation == 2
        }
        recorder.release()
        try await recorder.waitForRequestCount(2)
        try await waitUntil {
            store.installation?.providerPatchPending == false
        }

        let patches = recorder.requests.filter { $0.method == "PATCH" }
        XCTAssertEqual(patches.count, 2)
        let first = try XCTUnwrap(jsonBody(patches[0]))
        let second = try XCTUnwrap(jsonBody(patches[1]))
        XCTAssertEqual(first["device_token"] as? String, PushBase64URL.encode(oldToken))
        XCTAssertEqual(second["device_token"] as? String, PushBase64URL.encode(newToken))
        XCTAssertEqual(
            (second["provider_generation"] as? NSNumber)?.int64Value,
            11
        )
        XCTAssertNotEqual(second["device_token"] as? String, first["device_token"] as? String)
        XCTAssertFalse(store.installation?.providerPatchPending ?? true)
    }

    func testProviderConflictMatchingTupleAdoptsServerGenerationWithoutRetry()
        async throws
    {
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
        recorder.responseHandler = { request in
            if request.method == "PATCH" {
                return PushManagementResponse(
                    statusCode: 409,
                    body: [
                        "error": ["code": "stale_provider_generation"],
                        "provider_generation": 10,
                        "device_token_sha256": Self.sha256(token),
                        "environment": "sandbox",
                        "topic": "bot.avibe.ios",
                    ]
                )
            }
            return Self.successResponse
        }
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        coordinator.resumeAfterAccountActivation()
        try await recorder.waitForRequestCount(1)
        try await waitUntil {
            store.installation?.providerPatchPending == false
        }

        XCTAssertEqual(recorder.requests.filter { $0.method == "PATCH" }.count, 1)
        XCTAssertEqual(store.installation?.provider?.generation, 10)
        XCTAssertEqual(store.installation?.provider?.deviceToken, token)
        XCTAssertFalse(store.installation?.providerPatchPending ?? true)
    }

    func testProviderConflictMatchesLatestQueuedTupleWithoutSecondPatch()
        async throws
    {
        let oldToken = Data(repeating: 33, count: 32)
        let queuedToken = Data(repeating: 34, count: 32)
        let store = makeInstallationStore(
            providerGeneration: 2,
            provider: PushProviderState(
                deviceToken: oldToken,
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 1
            ),
            providerPatchPending: true
        )
        let recorder = ManagementRecorder()
        recorder.responseHandler = { request in
            if request.method == "PATCH" {
                await recorder.waitForReleaseIfNeeded()
                return PushManagementResponse(
                    statusCode: 409,
                    body: [
                        "error": ["code": "stale_provider_generation"],
                        "provider_generation": 10,
                        "device_token_sha256": Self.sha256(queuedToken),
                        "environment": "production",
                        "topic": "bot.avibe.ios",
                    ]
                )
            }
            return Self.successResponse
        }
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        coordinator.resumeAfterAccountActivation()
        try await recorder.waitForRequestCount(1)
        let firstBody = try XCTUnwrap(jsonBody(recorder.requests[0]))
        XCTAssertEqual(
            firstBody["device_token"] as? String,
            PushBase64URL.encode(oldToken)
        )
        coordinator.didRegister(
            deviceToken: queuedToken,
            environment: "production"
        )
        try await waitUntil {
            store.installation?.provider?.deviceToken == queuedToken
        }
        recorder.release()
        try await waitUntil {
            store.installation?.providerPatchPending == false
        }

        let patches = recorder.requests.filter { $0.method == "PATCH" }
        XCTAssertEqual(patches.count, 1)
        XCTAssertEqual(store.installation?.provider?.deviceToken, queuedToken)
        XCTAssertEqual(store.installation?.provider?.generation, 10)
        XCTAssertEqual(store.installation?.providerGeneration, 10)
        XCTAssertFalse(store.installation?.providerPatchPending ?? true)
    }

    func testRebuiltCoordinatorResumes503WithSameProviderTuple()
        async throws
    {
        let token = Data(repeating: 35, count: 32)
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

        let first = makeCoordinator(store: store, management: recorder)
        first.resumeAfterAccountActivation()
        try await recorder.waitForRequestCount(1)
        try await waitUntil { store.installation?.providerPatchPending == true }
        let firstBody = try XCTUnwrap(jsonBody(recorder.requests[0]))

        let second = makeCoordinator(store: store, management: recorder)
        second.resumeAfterAccountActivation()
        try await recorder.waitForRequestCount(2)
        let secondBody = try XCTUnwrap(jsonBody(recorder.requests[1]))

        XCTAssertEqual(
            (firstBody["provider_generation"] as? NSNumber)?.int64Value,
            1
        )
        XCTAssertEqual(
            (secondBody["provider_generation"] as? NSNumber)?.int64Value,
            1
        )
        XCTAssertEqual(
            firstBody["device_token"] as? String,
            PushBase64URL.encode(token)
        )
        XCTAssertEqual(
            secondBody["device_token"] as? String,
            PushBase64URL.encode(token)
        )
        try await waitUntil { store.installation?.providerPatchPending == false }
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
        let pendingGeneration = store.installation?.provider?.generation
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
        XCTAssertEqual(
            (body["provider_generation"] as? NSNumber)?.int64Value,
            pendingGeneration
        )
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

    func testReplacementKeyRetiresForTheMaximumAcceptedPushTTL() async throws {
        let currentTime = Date(timeIntervalSince1970: 1_794_096_000)
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
        let coordinator = makeCoordinator(
            store: store,
            management: ManagementRecorder(),
            now: { currentTime }
        )

        _ = try await coordinator.subscribe(
            vapidPublicKey: Self.vapidPublicKey,
            instanceOrigin: Self.instanceOrigin
        )

        let retired = try XCTUnwrap(
            try store.loadRecord(kid: "old-registration")
        )
        XCTAssertEqual(retired.state, .retiring)
        XCTAssertFalse(retired.pendingRevoke)
        XCTAssertEqual(
            retired.retirementDeadline,
            currentTime.addingTimeInterval(86_400)
        )
    }

    func testRecoveryDeletesOnlyExpiredConfirmedRetiringKeys() async throws {
        let currentTime = Date(timeIntervalSince1970: 1_794_096_000)
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: nil,
            providerPatchPending: false
        )
        try store.saveRecord(
            makeRecord(
                kid: "expired",
                state: .retiring,
                pendingRevoke: false,
                bindGeneration: 1,
                retirementDeadline: currentTime.addingTimeInterval(-1)
            )
        )
        try store.saveRecord(
            makeRecord(
                kid: "unexpired",
                state: .retiring,
                pendingRevoke: false,
                bindGeneration: 1,
                retirementDeadline: currentTime.addingTimeInterval(1)
            )
        )
        try store.saveRecord(
            makeRecord(
                kid: "pending-revoke",
                state: .retiring,
                pendingRevoke: true,
                bindGeneration: 1,
                retirementDeadline: currentTime.addingTimeInterval(-1)
            )
        )
        let recorder = ManagementRecorder()
        recorder.responseHandler = { request in
            XCTAssertEqual(request.method, "DELETE")
            return PushManagementResponse(
                statusCode: 503,
                body: ["error": ["code": "relay_unavailable"]]
            )
        }
        let coordinator = makeCoordinator(
            store: store,
            management: recorder,
            now: { currentTime }
        )

        coordinator.resumeAfterAccountActivation()
        try await recorder.waitForRequestCount(1)

        XCTAssertNil(try store.loadRecord(kid: "expired"))
        XCTAssertNotNil(try store.loadRecord(kid: "unexpired"))
        XCTAssertNotNil(try store.loadRecord(kid: "pending-revoke"))
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
        let notificationCenter = FakePushNotificationCenter(
            delivered: [
                PushNotificationCenterEntry(
                    identifier: "top-level",
                    userInfo: ["kid": "kid-a"]
                ),
                PushNotificationCenterEntry(
                    identifier: "fallback",
                    userInfo: [
                        "avibe": ["kid": "kid-b"],
                    ]
                ),
                PushNotificationCenterEntry(
                    identifier: "both-fields",
                    userInfo: [
                        "kid": "kid-other",
                        "avibe": ["kid": "kid-b"],
                    ]
                ),
                PushNotificationCenterEntry(
                    identifier: "other",
                    userInfo: ["kid": "kid-other"]
                ),
            ],
            pending: [
                PushNotificationCenterEntry(
                    identifier: "pending-fallback",
                    userInfo: [
                        "avibe": ["kid": "kid-a"],
                    ]
                )
            ]
        )
        try store.saveRecord(
            makeRecord(
                kid: "kid-a",
                state: .active,
                pendingRevoke: false,
                bindGeneration: 1
            )
        )
        try store.saveRecord(
            makeRecord(
                kid: "kid-b",
                state: .retiring,
                pendingRevoke: true,
                bindGeneration: 1
            )
        )
        let coordinator = makeCoordinator(
            store: store,
            management: ManagementRecorder(),
            notificationCenter: notificationCenter
        )

        await coordinator.prepareForLogout(throughBindGeneration: 1)

        XCTAssertEqual(
            notificationCenter.removedDelivered,
            ["top-level", "fallback", "both-fields"]
        )
        XCTAssertEqual(notificationCenter.removedPending, ["pending-fallback"])
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
        notificationCenter: PushNotificationCentering? = nil,
        notificationAuthorizationStatus: @escaping @MainActor () async
            -> UNAuthorizationStatus = { .authorized },
        registerForRemoteNotifications: @escaping @MainActor () -> Void = {},
        now: @escaping () -> Date = Date.init,
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
            registerForRemoteNotifications: registerForRemoteNotifications,
            requestNotificationAuthorization: { true },
            notificationAuthorizationStatus: notificationAuthorizationStatus,
            installationMarker: installationMarker,
            notificationCenter: notificationCenter
                ?? FakePushNotificationCenter(),
            tokenWaitSeconds: tokenWaitSeconds,
            now: now,
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
        bindGeneration: Int64,
        retirementDeadline: Date? = nil
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
            replacementRegistrationIDs: [],
            retirementDeadline: retirementDeadline
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

    private static func sha256(_ data: Data) -> String {
        SHA256.hash(data: data)
            .map { String(format: "%02x", $0) }
            .joined()
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
private final class FakePushNotificationCenter: PushNotificationCentering {
    var delivered: [PushNotificationCenterEntry]
    var pending: [PushNotificationCenterEntry]
    private(set) var removedDelivered: [String] = []
    private(set) var removedPending: [String] = []

    init(
        delivered: [PushNotificationCenterEntry] = [],
        pending: [PushNotificationCenterEntry] = []
    ) {
        self.delivered = delivered
        self.pending = pending
    }

    func deliveredNotifications() async -> [PushNotificationCenterEntry] {
        delivered
    }

    func pendingNotificationRequests() async -> [PushNotificationCenterEntry] {
        pending
    }

    func removeDeliveredNotifications(withIdentifiers identifiers: [String]) {
        removedDelivered.append(contentsOf: identifiers)
    }

    func removePendingNotificationRequests(withIdentifiers identifiers: [String]) {
        removedPending.append(contentsOf: identifiers)
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
