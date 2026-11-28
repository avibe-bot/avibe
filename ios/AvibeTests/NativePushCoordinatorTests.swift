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

    func testLiveActivityRegistrationIsBoundToActiveAccountOriginAndGeneration() throws {
        let store = makeInstallationStore(providerGeneration: 1, provider: nil, providerPatchPending: false)
        for (id, state, pending, generation) in [
            ("old-generation", PushKeyRecord.State.active, false, Int64(2)),
            ("revoking", .active, true, 1), ("retiring", .retiring, false, 1),
            ("current", .active, false, 1)
        ] {
            try store.saveRecord(makeRecord(kid: id, state: state, pendingRevoke: pending, bindGeneration: generation))
        }
        let account = Locked<MobileAccount?>(Self.accountA)
        let coordinator = makeCoordinator(store: store, management: ManagementRecorder(), account: account)
        XCTAssertEqual(coordinator.liveActivityRegistrationID(accountID: Self.accountA.id, instanceOrigin: Self.instanceOrigin), "current")
        XCTAssertNil(coordinator.liveActivityRegistrationID(accountID: Self.accountA.id, instanceOrigin: "https://other.example"))
        XCTAssertNil(coordinator.liveActivityRegistrationID(accountID: Self.accountB.id, instanceOrigin: Self.instanceOrigin))
        account.value = Self.accountB
        XCTAssertNil(coordinator.liveActivityRegistrationID(accountID: Self.accountA.id, instanceOrigin: Self.instanceOrigin))
    }

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

    func testAuthorizationStatusDistinguishesOptInFromDenied()
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
        XCTAssertEqual(undeterminedStatus, "off")

        authorization.value = .authorized
        let authorizedWithoutRegistration = try await coordinator.status(
            vapidPublicKey: Self.vapidPublicKey,
            instanceOrigin: Self.instanceOrigin
        )["state"] as? String
        XCTAssertEqual(authorizedWithoutRegistration, "off")
    }

    func testStatusExposesActivePendingAndAbsentRevokeStates() async throws {
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: nil,
            providerPatchPending: false
        )
        let active = makeRecord(
            kid: "cleanup-target",
            state: .active,
            pendingRevoke: false,
            bindGeneration: 1
        )
        try store.saveRecord(active)
        let coordinator = makeCoordinator(
            store: store,
            management: ManagementRecorder()
        )

        let activeStatus = try await coordinator.status(
            vapidPublicKey: nil,
            instanceOrigin: Self.instanceOrigin
        )
        XCTAssertEqual(activeStatus["state"] as? String, "off")
        XCTAssertEqual(activeStatus["revokeState"] as? String, "active")
        XCTAssertEqual(
            activeStatus["endpoint"] as? String,
            "https://avibe.bot/api/push/apns/cleanup-target"
        )

        var pending = active
        pending.state = .retiring
        pending.pendingRevoke = true
        try store.saveRecord(pending)
        let pendingStatus = try await coordinator.status(
            vapidPublicKey: nil,
            instanceOrigin: Self.instanceOrigin
        )
        XCTAssertEqual(pendingStatus["state"] as? String, "off")
        XCTAssertEqual(pendingStatus["revokeState"] as? String, "pending")
        XCTAssertEqual(
            pendingStatus["endpoint"] as? String,
            "https://avibe.bot/api/push/apns/cleanup-target"
        )

        try store.deleteRecord(kid: pending.kid)
        let absentStatus = try await coordinator.status(
            vapidPublicKey: nil,
            instanceOrigin: Self.instanceOrigin
        )
        XCTAssertEqual(absentStatus["state"] as? String, "off")
        XCTAssertEqual(absentStatus["revokeState"] as? String, "absent")
        XCTAssertNil(absentStatus["endpoint"])
    }

    /// The status names iOS's notification permission, so the page can turn
    /// notifications back on after a sign-in only when iOS already allows
    /// them, never prompting on its own. A fresh install's account (nothing
    /// bound yet) and a signed-out App say it too.
    func testStatusNamesTheSystemPermission() async throws {
        let cases: [(UNAuthorizationStatus, String)] = [
            (.authorized, "granted"),
            (.provisional, "granted"),
            (.ephemeral, "granted"),
            (.notDetermined, "notDetermined"),
            (.denied, "denied"),
        ]
        for (authorization, expected) in cases {
            let store = makeInstallationStore(
                providerGeneration: 1,
                provider: nil,
                providerPatchPending: false
            )
            let bound = makeCoordinator(
                store: store,
                management: ManagementRecorder(),
                notificationAuthorizationStatus: { authorization }
            )
            let status = try await bound.status(
                vapidPublicKey: nil,
                instanceOrigin: Self.instanceOrigin
            )
            XCTAssertEqual(status["permission"] as? String, expected, "\(authorization)")

            let unbound = makeCoordinator(
                store: InMemoryPushKeychainStore(),
                management: ManagementRecorder(),
                notificationAuthorizationStatus: { authorization }
            )
            let fresh = try await unbound.status(
                vapidPublicKey: nil,
                instanceOrigin: Self.instanceOrigin
            )
            XCTAssertEqual(fresh["state"] as? String, expected == "denied" ? "unavailable" : "off")
            XCTAssertEqual(fresh["permission"] as? String, expected, "\(authorization)")

            let signedOut = makeCoordinator(
                store: InMemoryPushKeychainStore(),
                management: ManagementRecorder(),
                account: Locked<MobileAccount?>(nil),
                notificationAuthorizationStatus: { authorization }
            )
            let none = try await signedOut.status(
                vapidPublicKey: nil,
                instanceOrigin: Self.instanceOrigin
            )
            XCTAssertEqual(none["permission"] as? String, expected, "\(authorization)")
        }
    }

    func testRevokeOutcomeBridgePayloadRemainsLegacyCompatible() {
        XCTAssertEqual(
            NativePushRevokeOutcome.confirmed.jsonObject["revoked"] as? Bool,
            true
        )
        XCTAssertEqual(
            NativePushRevokeOutcome.confirmed.jsonObject["revoke"] as? String,
            "confirmed"
        )
        XCTAssertEqual(
            NativePushRevokeOutcome.pending.jsonObject["revoked"] as? Bool,
            false
        )
        XCTAssertEqual(
            NativePushRevokeOutcome.pending.jsonObject["revoke"] as? String,
            "pending"
        )
        XCTAssertEqual(
            NativePushRevokeOutcome.notNeeded.jsonObject["revoked"] as? Bool,
            true
        )
        XCTAssertEqual(
            NativePushRevokeOutcome.notNeeded.jsonObject["revoke"] as? String,
            "not_needed"
        )
    }

    func testStatusExposesPendingRevokeAcrossVapidRotation() async throws {
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: nil,
            providerPatchPending: false
        )
        let pending = makeRecord(
            kid: "old-vapid-pending-revoke",
            state: .retiring,
            pendingRevoke: true,
            bindGeneration: 1,
            vapidKeyHash: String(repeating: "b", count: 64)
        )
        try store.saveRecord(pending)
        let coordinator = makeCoordinator(
            store: store,
            management: ManagementRecorder()
        )

        let status = try await coordinator.status(
            vapidPublicKey: Self.vapidPublicKey,
            instanceOrigin: Self.instanceOrigin
        )

        XCTAssertEqual(status["state"] as? String, "off")
        XCTAssertEqual(status["revokeState"] as? String, "pending")
        XCTAssertEqual(
            status["endpoint"] as? String,
            "https://avibe.bot/api/push/apns/old-vapid-pending-revoke"
        )
    }

    func testStatusExposesActiveRevokeTargetAcrossVapidRotation() async throws {
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: nil,
            providerPatchPending: false
        )
        let active = makeRecord(
            kid: "old-vapid-active",
            state: .active,
            pendingRevoke: false,
            bindGeneration: 1,
            vapidKeyHash: String(repeating: "b", count: 64)
        )
        try store.saveRecord(active)
        let coordinator = makeCoordinator(
            store: store,
            management: ManagementRecorder()
        )

        let status = try await coordinator.status(
            vapidPublicKey: Self.vapidPublicKey,
            instanceOrigin: Self.instanceOrigin
        )

        XCTAssertEqual(status["state"] as? String, "off")
        XCTAssertEqual(status["revokeState"] as? String, "active")
        XCTAssertEqual(
            status["endpoint"] as? String,
            "https://avibe.bot/api/push/apns/old-vapid-active"
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

        await coordinator.prepareForLogout(throughBindGeneration: 1, reason: .signedOut)

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
        // The create carries no reason; the bulk revoke names the sign-out.
        XCTAssertEqual(Self.jsonBody(recorder.requests[1])?["reason"] as? String, "signed_out")
        XCTAssertNil(store.installation?.pendingBulkRevokeReason)
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

        await coordinator.prepareForLogout(throughBindGeneration: 1, reason: .signedOut)

        XCTAssertEqual(recorder.requests.map(\.method), ["PUT"])
        XCTAssertEqual(
            store.installation?.pendingBulkRevokeThroughGeneration,
            1
        )
        XCTAssertNotNil(try store.loadRecord(kid: pending.kid))
    }

    /// A bulk revoke that waited for recovery is sent with the reason it was
    /// staged with; one staged before reasons existed is sent as `unspecified`.
    func testAPersistedBulkRevokeIsRetriedWithItsStagedReason() async throws {
        for (staged, sent) in [("session_expired", "session_expired"), (nil, "unspecified")] as [(String?, String)] {
            let store = makeInstallationStore(
                providerGeneration: 1,
                provider: nil,
                providerPatchPending: false
            )
            store.installation?.pendingBulkRevokeThroughGeneration = 1
            store.installation?.pendingBulkRevokeReason = staged
            let recorder = ManagementRecorder()
            let coordinator = makeCoordinator(store: store, management: recorder)

            coordinator.resumeAfterAccountActivation()
            try await recorder.waitForRequestCount(1)

            let bulk = try XCTUnwrap(recorder.requests.first { $0.method == "POST" })
            XCTAssertEqual(Self.jsonBody(bulk)?["reason"] as? String, sent)
            try await waitUntil { store.installation?.pendingBulkRevokeThroughGeneration == nil }
            XCTAssertNil(store.installation?.pendingBulkRevokeReason)
        }
    }

    /// Logout stages its reason on the records it newly retires; a revoke
    /// already pending keeps the reason it was staged with.
    func testLogoutKeepsAPendingRevokesFirstReason() async throws {
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: nil,
            providerPatchPending: false
        )
        var earlier = makeRecord(kid: "removed-earlier", state: .retiring, pendingRevoke: true, bindGeneration: 1)
        earlier.revokeReason = PushRevokeReason.instanceRemoved.rawValue
        try store.saveRecord(earlier)
        try store.saveRecord(makeRecord(kid: "active", state: .active, pendingRevoke: false, bindGeneration: 1))
        let recorder = ManagementRecorder()
        recorder.responseHandler = { _ in
            PushManagementResponse(statusCode: 503, body: ["error": ["code": "relay_unavailable"]])
        }
        let coordinator = makeCoordinator(store: store, management: recorder)

        await coordinator.prepareForLogout(throughBindGeneration: 1, reason: .sessionExpired)

        XCTAssertEqual(try store.loadRecord(kid: "removed-earlier")?.revokeReason, "instance_removed")
        XCTAssertEqual(try store.loadRecord(kid: "active")?.revokeReason, "session_expired")
        XCTAssertEqual(store.installation?.pendingBulkRevokeReason, "session_expired")
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

        await coordinator.prepareForLogout(throughBindGeneration: 1, reason: .signedOut)
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

    func testForcedReplacementCreatesAConcreteRegistrationInsteadOfReusingActive()
        async throws
    {
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
        let recorder = ManagementRecorder()
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        let first = try await coordinator.subscribe(
            vapidPublicKey: Self.vapidPublicKey,
            instanceOrigin: Self.instanceOrigin
        )
        let firstRecord = try XCTUnwrap(try store.listRecords().first)

        let replacement = try await coordinator.subscribe(
            vapidPublicKey: Self.vapidPublicKey,
            instanceOrigin: Self.instanceOrigin,
            forceReplacement: true
        )

        let creates = recorder.requests.filter {
            $0.method == "PUT" && $0.path.contains("/registrations/")
        }
        XCTAssertEqual(creates.count, 2)
        XCTAssertNotEqual(
            first["endpoint"] as? String,
            replacement["endpoint"] as? String
        )
        let replacementBody = try XCTUnwrap(jsonBody(creates[1]))
        XCTAssertEqual(
            replacementBody["replaces_registration_ids"] as? [String],
            [firstRecord.kid]
        )
        let records = try store.listRecords()
        XCTAssertEqual(records.count, 2)
        XCTAssertTrue(records.contains { $0.state == .active })
        XCTAssertTrue(
            records.contains {
                $0.kid == firstRecord.kid && $0.state == .retiring
            }
        )
    }

    func testFirstSubscriptionCreatesOneActiveRegistration() async throws {
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
        let recorder = ManagementRecorder()
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        let subscription = try await coordinator.subscribe(
            vapidPublicKey: Self.vapidPublicKey,
            instanceOrigin: Self.instanceOrigin
        )

        let registrationRequests = recorder.requests.filter {
            $0.method == "PUT"
                && $0.path.contains("/registrations/")
        }
        XCTAssertEqual(registrationRequests.count, 1)
        let record = try XCTUnwrap(try store.listRecords().first)
        XCTAssertEqual(try store.listRecords().count, 1)
        XCTAssertEqual(record.state, .active)
        XCTAssertFalse(record.pendingRevoke)
        XCTAssertEqual(
            subscription["endpoint"] as? String,
            record.publicSubscription["endpoint"] as? String
        )
    }

    /// A revoke the relay keeps refusing for one origin (an instance that left
    /// the account) stays queued for retry, and does not stop another
    /// origin from subscribing (turning notifications back on after a
    /// sign-in). One the new registration depends on still does: for the
    /// subscribing origin itself, or with the same VAPID key (the relay
    /// refuses a create while another registration with that key is active,
    /// as when the same instance moved to a new address).
    func testAStuckRevokeForAnotherOriginDoesNotBlockSubscribing() async throws {
        let subscribingHash = SHA256.hash(
            data: try XCTUnwrap(PushBase64URL.decode(Self.vapidPublicKey))
        ).map { String(format: "%02x", $0) }.joined()
        let rows: [(name: String, origin: String, vapidKeyHash: String, blocks: Bool)] = [
            ("another origin", "https://gone.example", String(repeating: "a", count: 64), false),
            ("own origin", Self.instanceOrigin, String(repeating: "a", count: 64), true),
            ("another origin, same VAPID key", "https://moved-from.example", subscribingHash, true),
        ]
        for row in rows {
            let ownOrigin = row.blocks
            let stuckOrigin = row.origin
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
            try store.saveRecord(PushKeyRecord(
                kid: "stuck-revoke",
                instanceOrigin: stuckOrigin,
                accountID: Self.accountA.id,
                accountGeneration: 1,
                bindGeneration: 1,
                privateKey: Data(),
                authSecret: Data(),
                vapidKeyHash: row.vapidKeyHash,
                state: .retiring,
                pendingRevoke: true,
                replacementRegistrationIDs: []
            ))
            let recorder = ManagementRecorder()
            recorder.responseHandler = { request in
                request.method == "DELETE" && Self.pathname(request).hasSuffix("/stuck-revoke")
                    ? PushManagementResponse(
                        statusCode: 503,
                        body: ["error": ["code": "relay_unavailable"]]
                    )
                    : Self.successResponse
            }
            let coordinator = makeCoordinator(store: store, management: recorder)

            let name = row.name
            do {
                let subscription = try await coordinator.subscribe(
                    vapidPublicKey: Self.vapidPublicKey,
                    instanceOrigin: Self.instanceOrigin
                )
                XCTAssertFalse(ownOrigin, "A revoke it depends on must stop it: \(name)")
                XCTAssertNotNil(subscription["endpoint"] as? String, name)
            } catch {
                XCTAssertTrue(ownOrigin, "Blocked by an unrelated revoke: \(error), \(name)")
            }

            let creates = recorder.requests.filter {
                $0.method == "PUT" && $0.path.contains("/registrations/")
            }
            XCTAssertEqual(creates.count, ownOrigin ? 0 : 1, name)
            XCTAssertTrue(
                recorder.requests.contains {
                    $0.method == "DELETE" && Self.pathname($0).hasSuffix("/stuck-revoke")
                },
                "The stuck revoke was tried: \(name)"
            )
            // Still queued for retry either way.
            let stuck = try XCTUnwrap(try store.loadRecord(kid: "stuck-revoke"), name)
            XCTAssertTrue(stuck.pendingRevoke, name)
            XCTAssertEqual(stuck.state, .retiring, name)
        }
    }

    /// UIKit hands the device token back as a main-queue block. When it is
    /// queued while the subscription asks for it again, it runs after the
    /// subscription's provider reconciliation has found nothing pending and
    /// before the subscription checks the result: the tuple it leaves pending
    /// is reconciled again and the registration is still created.
    func testTokenCallbackQueuedAheadOfReconciliationStillRegisters()
        async throws
    {
        let token = Data(repeating: 41, count: 32)
        let store = makeInstallationStore(
            providerGeneration: 5,
            provider: PushProviderState(
                deviceToken: token,
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 5
            ),
            providerPatchPending: false
        )
        let recorder = ManagementRecorder()
        let coordinatorBox = Locked<NativePushCoordinator?>(nil)
        var registrationCalls = 0
        let coordinator = makeCoordinator(
            store: store,
            management: recorder,
            registerForRemoteNotifications: {
                registrationCalls += 1
                DispatchQueue.main.async {
                    coordinatorBox.value?.didRegister(
                        deviceToken: token,
                        environment: "sandbox"
                    )
                }
            }
        )
        coordinatorBox.value = coordinator

        let subscription = try await coordinator.subscribe(
            vapidPublicKey: Self.vapidPublicKey,
            instanceOrigin: Self.instanceOrigin
        )
        // The callback's own queued reconciliation finds nothing left to do.
        try await Task.sleep(nanoseconds: 50_000_000)

        XCTAssertEqual(registrationCalls, 1)
        XCTAssertEqual(
            recorder.requests.map(Self.requestKind),
            ["PUT binding", "PATCH provider", "PUT registration"]
        )
        XCTAssertEqual(
            recorder.requests.compactMap(Self.generation),
            [6]
        )
        XCTAssertEqual(store.installation?.provider?.generation, 6)
        XCTAssertFalse(store.installation?.providerPatchPending ?? true)
        let record = try XCTUnwrap(try store.listRecords().first)
        XCTAssertEqual(try store.listRecords().count, 1)
        XCTAssertEqual(record.state, .active)
        XCTAssertEqual(
            subscription["endpoint"] as? String,
            record.publicSubscription["endpoint"] as? String
        )
    }

    func testTokenCallbackDuringProviderPatchStillRegisters() async throws {
        let store = makeInstallationStore(
            providerGeneration: 5,
            provider: PushProviderState(
                deviceToken: Data(repeating: 42, count: 32),
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 5
            ),
            providerPatchPending: true
        )
        let recorder = ManagementRecorder()
        var patches = 0
        recorder.responseHandler = { request in
            if request.method == "PATCH" {
                patches += 1
                if patches == 1 {
                    await recorder.waitForReleaseIfNeeded()
                }
            }
            return Self.successResponse
        }
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        let subscription = Task { @MainActor in
            try await coordinator.subscribe(
                vapidPublicKey: Self.vapidPublicKey,
                instanceOrigin: Self.instanceOrigin
            )
        }
        try await recorder.waitForRequestCount(2)
        let newerToken = Data(repeating: 43, count: 32)
        coordinator.didRegister(deviceToken: newerToken, environment: "sandbox")
        try await waitUntil {
            store.installation?.provider?.generation == 6
        }
        try await Task.sleep(nanoseconds: 30_000_000)
        recorder.release()
        _ = try await subscription.value
        try await Task.sleep(nanoseconds: 50_000_000)

        XCTAssertEqual(
            recorder.requests.map(Self.requestKind),
            [
                "PUT binding",
                "PATCH provider",
                "PATCH provider",
                "PUT registration",
            ]
        )
        XCTAssertEqual(recorder.requests.compactMap(Self.generation), [5, 6])
        XCTAssertEqual(
            recorder.requests.last(where: { $0.method == "PATCH" })
                .flatMap { $0.body.flatMap(Self.deviceToken) },
            PushBase64URL.encode(newerToken)
        )
        XCTAssertFalse(store.installation?.providerPatchPending ?? true)
        XCTAssertEqual(try store.listRecords().first?.state, .active)
    }

    /// A tuple that changes again after every reconciliation ends the
    /// subscription as unavailable after a bounded number of rounds, with no
    /// registration created.
    func testEndlessTokenCallbacksEndUnavailableAfterBoundedRounds()
        async throws
    {
        let base = makeInstallationStore(
            providerGeneration: 5,
            provider: PushProviderState(
                deviceToken: Data(repeating: 44, count: 32),
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 5
            ),
            providerPatchPending: true
        )
        let store = ProviderSettledHookStore(base: base)
        let recorder = ManagementRecorder()
        let coordinator = makeCoordinator(
            store: base,
            management: recorder,
            keychain: store
        )
        var callbacks: UInt8 = 0
        store.onProviderSettled = {
            // Every settled tuple is replaced by a new callback before the
            // subscription reads it.
            MainActor.assumeIsolated {
                callbacks += 1
                coordinator.didRegister(
                    deviceToken: Data(repeating: 44 &+ callbacks, count: 32),
                    environment: "sandbox"
                )
            }
        }

        do {
            _ = try await coordinator.subscribe(
                vapidPublicKey: Self.vapidPublicKey,
                instanceOrigin: Self.instanceOrigin
            )
            XCTFail("A tuple that never settles must not subscribe")
        } catch {
            XCTAssertEqual(error as? NativePushError, .unavailable)
        }
        store.onProviderSettled = nil
        try await Task.sleep(nanoseconds: 50_000_000)

        let rounds = NativePushCoordinator.providerReconciliationRounds
        XCTAssertEqual(rounds, 3)
        XCTAssertEqual(
            Array(recorder.requests.compactMap(Self.generation).prefix(rounds)),
            [5, 6, 7]
        )
        XCTAssertFalse(
            recorder.requests.contains {
                $0.method == "PUT" && $0.path.contains("/registrations/")
            }
        )
        XCTAssertEqual(try base.listRecords().count, 0)
    }

    func testLostCreateResponseReturnsTheRecoveredRegistration()
        async throws
    {
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
        let recorder = ManagementRecorder()
        var acceptedRegistrationPaths: Set<String> = []
        recorder.responseHandler = { request in
            guard request.path.contains("/registrations/") else {
                return Self.successResponse
            }
            let wasInserted = acceptedRegistrationPaths.insert(
                request.path
            ).inserted
            if wasInserted {
                throw URLError(.networkConnectionLost)
            }
            return Self.successResponse
        }
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        do {
            _ = try await coordinator.subscribe(
                vapidPublicKey: Self.vapidPublicKey,
                instanceOrigin: Self.instanceOrigin
            )
            XCTFail("Expected the first registration response to be lost")
        } catch {
            XCTAssertEqual(
                (error as? URLError)?.code,
                .networkConnectionLost
            )
        }
        let pending = try XCTUnwrap(try store.listRecords().first)
        XCTAssertEqual(pending.state, .pending)

        let subscription = try await coordinator.subscribe(
            vapidPublicKey: Self.vapidPublicKey,
            instanceOrigin: Self.instanceOrigin
        )

        let registrationRequests = recorder.requests.filter {
            $0.method == "PUT"
                && $0.path.contains("/registrations/")
        }
        XCTAssertEqual(registrationRequests.count, 2)
        XCTAssertEqual(
            Set(registrationRequests.map(\.path)),
            acceptedRegistrationPaths
        )
        XCTAssertEqual(acceptedRegistrationPaths.count, 1)
        XCTAssertEqual(
            subscription["endpoint"] as? String,
            pending.publicSubscription["endpoint"] as? String
        )
        let subscriptionKeys = try XCTUnwrap(
            subscription["keys"] as? [String: Any]
        )
        let pendingKeys = try XCTUnwrap(
            pending.publicSubscription["keys"] as? [String: Any]
        )
        XCTAssertEqual(
            subscriptionKeys["p256dh"] as? String,
            pendingKeys["p256dh"] as? String
        )
        XCTAssertEqual(
            subscriptionKeys["auth"] as? String,
            pendingKeys["auth"] as? String
        )
        let recovered = try XCTUnwrap(
            try store.loadRecord(kid: pending.kid)
        )
        XCTAssertEqual(try store.listRecords().count, 1)
        XCTAssertEqual(recovered.state, .active)
        XCTAssertFalse(recovered.pendingRevoke)
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
        XCTAssertEqual(revoked, .confirmed)

        XCTAssertEqual(
            recorder.requests.map(\.path),
            ["/api/push/apns/registrations/\(target.kid)?reason=notifications_off"]
        )
        XCTAssertNil(try store.loadRecord(kid: target.kid))
        XCTAssertNotNil(try store.loadRecord(kid: "a-other-account"))
        XCTAssertNotNil(try store.loadRecord(kid: "b-stale-generation"))
    }

    func testTheDeviceIdentifierIsDerivedFromThisInstallationOnly() async throws {
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: nil,
            providerPatchPending: false
        )
        let installation = try XCTUnwrap(store.installation)
        let coordinator = makeCoordinator(store: store, management: ManagementRecorder())

        // HMAC-SHA256 of the instance id under this installation's secret.
        XCTAssertEqual(
            coordinator.deviceIdentifier(forInstanceID: "instance-1"),
            "9e24a60986550cf6eb110b6137bfe1a8"
        )
        XCTAssertNotEqual(
            coordinator.deviceIdentifier(forInstanceID: "instance-2"),
            coordinator.deviceIdentifier(forInstanceID: "instance-1")
        )
        // Neither the relay's installation id nor its secret is the answer.
        XCTAssertNotEqual(
            coordinator.deviceIdentifier(forInstanceID: "instance-1"),
            installation.installationID.uuidString.lowercased()
        )

        // A Keychain installation from before a reinstall is not this one.
        let reinstalled = InMemoryPushInstallationMarkerStore()
        reinstalled.installationID = UUID()
        let afterReinstall = makeCoordinator(
            store: store, management: ManagementRecorder(), marker: reinstalled
        )
        XCTAssertNil(afterReinstall.deviceIdentifier(forInstanceID: "instance-1"))

        // No installation yet.
        let empty = makeCoordinator(store: InMemoryPushKeychainStore(), management: ManagementRecorder())
        XCTAssertNil(empty.deviceIdentifier(forInstanceID: "instance-1"))
    }

    /// The account's list no longer has some instances: their registrations
    /// are staged as pending revokes (keys zeroed) and revoked; a listed
    /// origin, another account's record and a stale generation stay. A
    /// failed request leaves the revoke pending for recovery, and a call for
    /// an account that is not the one signed in does nothing.
    func testRevokingOutsideTheListedOriginsKeepsListedOnes() async throws {
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: nil,
            providerPatchPending: false
        )
        func record(_ kid: String, _ origin: String, account: String = Self.accountA.id, generation: Int64 = 1, state: PushKeyRecord.State = .active) -> PushKeyRecord {
            PushKeyRecord(
                kid: kid,
                instanceOrigin: origin,
                accountID: account,
                accountGeneration: generation,
                bindGeneration: 1,
                privateKey: Data(repeating: 13, count: 32),
                authSecret: Data(repeating: 14, count: 16),
                vapidKeyHash: String(repeating: "a", count: 64),
                state: state,
                pendingRevoke: false,
                replacementRegistrationIDs: []
            )
        }
        try store.saveRecord(record("listed", Self.instanceOrigin))
        // A create whose answer was lost, for a removed origin.
        try store.saveRecord(record("removed-3", "https://removed-three.example", state: .pending))
        try store.saveRecord(record("removed-1", "https://removed-one.example"))
        try store.saveRecord(record("removed-2", "https://removed-two.example"))
        try store.saveRecord(record("other-account", "https://removed-one.example", account: Self.accountB.id))
        try store.saveRecord(record("stale-generation", "https://removed-one.example", generation: 0))
        let recorder = ManagementRecorder()
        recorder.responseHandler = { request in
            Self.pathname(request).hasSuffix("/removed-1")
                ? PushManagementResponse(statusCode: 503, body: ["error": ["code": "relay_unavailable"]])
                : Self.successResponse
        }
        let coordinator = makeCoordinator(store: store, management: recorder)

        // Not the account signed in: nothing.
        await coordinator.revokeRegistrations(outside: [Self.instanceOrigin], accountID: Self.accountB.id)
        XCTAssertTrue(recorder.requests.isEmpty)

        await coordinator.revokeRegistrations(outside: [Self.instanceOrigin], accountID: Self.accountA.id)

        XCTAssertEqual(
            recorder.requests.map(\.path),
            [
                "/api/push/apns/registrations/removed-1?reason=instance_removed",
                "/api/push/apns/registrations/removed-2?reason=instance_removed",
                "/api/push/apns/registrations/removed-3?reason=instance_removed",
            ]
        )
        // The first one failing does not hold back the others.
        XCTAssertNil(try store.loadRecord(kid: "removed-2"))
        XCTAssertNil(try store.loadRecord(kid: "removed-3"))
        let pending = try XCTUnwrap(try store.loadRecord(kid: "removed-1"))
        XCTAssertEqual(pending.state, .retiring)
        XCTAssertTrue(pending.pendingRevoke)
        XCTAssertTrue(pending.privateKey.isEmpty)
        XCTAssertEqual(try store.loadRecord(kid: "listed")?.state, .active)
        XCTAssertEqual(try store.loadRecord(kid: "other-account")?.state, .active)
        XCTAssertEqual(try store.loadRecord(kid: "stale-generation")?.state, .active)

        XCTAssertEqual(pending.revokeReason, PushRevokeReason.instanceRemoved.rawValue)

        // Called again with the same list: the pending one is retried, with its first reason.
        recorder.responseHandler = { _ in Self.successResponse }
        await coordinator.revokeRegistrations(outside: [Self.instanceOrigin], accountID: Self.accountA.id)
        XCTAssertEqual(recorder.requests.last?.path, "/api/push/apns/registrations/removed-1?reason=instance_removed")
        XCTAssertNil(try store.loadRecord(kid: "removed-1"))
        XCTAssertEqual(try store.loadRecord(kid: "listed")?.state, .active)
    }

    /// A revoke staged by an App from before reasons is retried as
    /// `unspecified`, never guessed (docs/plans/ios-push-relay-contract.md).
    func testARevokeStagedBeforeReasonsIsSentAsUnspecified() async throws {
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: nil,
            providerPatchPending: false
        )
        try store.saveRecord(makeRecord(
            kid: "legacy-pending",
            state: .retiring,
            pendingRevoke: true,
            bindGeneration: 1
        ))
        let recorder = ManagementRecorder()
        let coordinator = makeCoordinator(store: store, management: recorder)

        await coordinator.revokeRegistrations(outside: [Self.instanceOrigin], accountID: Self.accountA.id)

        XCTAssertEqual(
            recorder.requests.map(\.path),
            ["/api/push/apns/registrations/legacy-pending?reason=unspecified"]
        )
        XCTAssertNil(try store.loadRecord(kid: "legacy-pending"))
    }

    /// Turning notifications off for an origin whose revoke is already
    /// pending retries it with the reason it was staged with.
    func testUnsubscribeRetryingAPendingRevokeKeepsItsReason() async throws {
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: nil,
            providerPatchPending: false
        )
        var pending = makeRecord(kid: "pending-target", state: .retiring, pendingRevoke: true, bindGeneration: 1)
        pending.revokeReason = PushRevokeReason.instanceRemoved.rawValue
        try store.saveRecord(pending)
        let recorder = ManagementRecorder()
        let coordinator = makeCoordinator(store: store, management: recorder)

        let outcome = try await coordinator.unsubscribe(instanceOrigin: Self.instanceOrigin)

        XCTAssertEqual(outcome, .confirmed)
        XCTAssertEqual(
            recorder.requests.map(\.path),
            ["/api/push/apns/registrations/pending-target?reason=instance_removed"]
        )
    }

    func testUnsubscribeRetriesThePersistedConcretePendingRevoke()
        async throws
    {
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: nil,
            providerPatchPending: false
        )
        let target = makeRecord(
            kid: "cleanup-target",
            state: .active,
            pendingRevoke: false,
            bindGeneration: 1
        )
        try store.saveRecord(target)
        let recorder = ManagementRecorder()
        var attempts = 0
        recorder.responseHandler = { request in
            XCTAssertEqual(
                request.path,
                "/api/push/apns/registrations/cleanup-target?reason=notifications_off"
            )
            attempts += 1
            return attempts == 1
                ? PushManagementResponse(
                    statusCode: 503,
                    body: ["error": ["code": "relay_unavailable"]]
                )
                : Self.successResponse
        }
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        let firstOutcome = try await coordinator.unsubscribe(
            instanceOrigin: Self.instanceOrigin
        )
        XCTAssertEqual(firstOutcome, .pending)
        let persisted = try XCTUnwrap(
            try store.loadRecord(kid: target.kid)
        )
        XCTAssertEqual(persisted.state, .retiring)
        XCTAssertTrue(persisted.pendingRevoke)
        let pendingStatus = try await coordinator.status(
            vapidPublicKey: nil,
            instanceOrigin: Self.instanceOrigin
        )
        XCTAssertEqual(pendingStatus["revokeState"] as? String, "pending")

        let secondOutcome = try await coordinator.unsubscribe(
            instanceOrigin: Self.instanceOrigin
        )
        XCTAssertEqual(secondOutcome, .confirmed)
        XCTAssertNil(try store.loadRecord(kid: target.kid))
        let thirdOutcome = try await coordinator.unsubscribe(
            instanceOrigin: Self.instanceOrigin
        )
        XCTAssertEqual(thirdOutcome, .notNeeded)
        XCTAssertEqual(recorder.requests.count, 2)
    }

    func testLogoutAndAPNsCallbackPreserveTheNewestProviderTuple() async throws {
        let oldToken = Data(repeating: 17, count: 32)
        let newToken = Data(repeating: 18, count: 32)
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: PushProviderState(
                deviceToken: oldToken,
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 1
            ),
            providerPatchPending: false
        )
        let recorder = ManagementRecorder()
        recorder.responseHandler = { request in
            if request.method == "POST" {
                await recorder.waitForReleaseIfNeeded()
            }
            return Self.successResponse
        }
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        let logout = Task {
            await coordinator.prepareForLogout(throughBindGeneration: 1, reason: .signedOut)
        }
        try await recorder.waitForRequestCount(1)
        XCTAssertEqual(recorder.requests.first?.method, "POST")

        coordinator.didRegister(
            deviceToken: newToken,
            environment: "production"
        )
        recorder.release()
        await logout.value

        try await waitUntil {
            store.installation?.provider?.deviceToken == newToken
        }
        XCTAssertEqual(store.installation?.provider?.deviceToken, newToken)
        XCTAssertEqual(store.installation?.provider?.environment, "production")
        XCTAssertEqual(store.installation?.provider?.generation, 2)
        XCTAssertEqual(store.installation?.providerGeneration, 2)
    }

    /// An APNs callback that lands while logout waits on a create's answer
    /// is persisted before logout writes its own state: logout keeps it.
    func testAPNsCallbackDuringLogoutCreateReconciliationKeepsTheNewestTuple()
        async throws
    {
        let oldToken = Data(repeating: 21, count: 32)
        let newToken = Data(repeating: 22, count: 32)
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: PushProviderState(
                deviceToken: oldToken,
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 1
            ),
            providerPatchPending: false
        )
        try store.saveRecord(
            makeRecord(
                kid: "pending-registration",
                state: .pending,
                pendingRevoke: false,
                bindGeneration: 1
            )
        )
        let recorder = ManagementRecorder()
        recorder.responseHandler = { request in
            if request.method == "PUT" {
                await recorder.waitForReleaseIfNeeded()
            }
            return Self.successResponse
        }
        let coordinator = makeCoordinator(
            store: store,
            management: recorder
        )

        let logout = Task {
            await coordinator.prepareForLogout(throughBindGeneration: 1, reason: .signedOut)
        }
        try await recorder.waitForRequestCount(1)
        XCTAssertEqual(recorder.requests.first?.method, "PUT")
        coordinator.didRegister(
            deviceToken: newToken,
            environment: "production"
        )
        try await waitUntil {
            store.installation?.provider?.deviceToken == newToken
        }
        recorder.release()
        await logout.value

        XCTAssertEqual(recorder.requests.map(\.method), ["PUT", "POST"])
        XCTAssertNil(store.installation?.accountID)
        XCTAssertNil(store.installation?.pendingBulkRevokeThroughGeneration)
        XCTAssertEqual(store.installation?.provider?.deviceToken, newToken)
        XCTAssertEqual(store.installation?.provider?.environment, "production")
        XCTAssertEqual(store.installation?.provider?.generation, 2)
        XCTAssertEqual(store.installation?.providerGeneration, 2)
        // Patched at the next sign-in, not while signed out.
        XCTAssertEqual(store.installation?.providerPatchPending, true)
    }

    /// A logout whose revoke could not reach the relay is retried when the
    /// signed-out App returns to the foreground, with the installation's own
    /// credential. Nothing is turned back on.
    func testSignedOutForegroundRetriesThePendingLogoutRevoke() async throws {
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: PushProviderState(
                deviceToken: Data(repeating: 23, count: 32),
                environment: "sandbox",
                topic: "bot.avibe.ios",
                generation: 1
            ),
            providerPatchPending: true
        )
        store.installation?.accountID = nil
        store.installation?.pendingBulkRevokeThroughGeneration = 1
        store.installation?.pendingBulkRevokeReason = PushRevokeReason.signedOut.rawValue
        try store.saveRecord(
            makeRecord(
                kid: "signed-out",
                state: .retiring,
                pendingRevoke: true,
                bindGeneration: 1
            )
        )
        let recorder = ManagementRecorder()
        var registrationCalls = 0
        let coordinator = makeCoordinator(
            store: store,
            management: recorder,
            account: Locked<MobileAccount?>(nil),
            registerForRemoteNotifications: { registrationCalls += 1 }
        )

        NotificationCenter.default.post(
            name: UIApplication.didBecomeActiveNotification,
            object: nil
        )
        try await waitUntil {
            store.installation?.pendingBulkRevokeThroughGeneration == nil
        }
        try await waitUntil { !coordinator.hasWorkInFlight }

        XCTAssertEqual(recorder.requests.map(Self.requestKind), ["POST revoke"])
        let bulk = try XCTUnwrap(recorder.requests.first)
        XCTAssertEqual(Self.jsonBody(bulk)?["reason"] as? String, "signed_out")
        XCTAssertEqual(Self.jsonBody(bulk)?["through_bind_generation"] as? Int, 1)
        XCTAssertTrue(try store.listRecords().isEmpty)
        XCTAssertNil(store.installation?.accountID)
        XCTAssertEqual(store.installation?.providerPatchPending, true)
        XCTAssertEqual(registrationCalls, 0)

        // Nothing is owed any more: another foreground sends nothing.
        NotificationCenter.default.post(
            name: UIApplication.didBecomeActiveNotification,
            object: nil
        )
        try await Task.sleep(for: .milliseconds(50))
        XCTAssertEqual(recorder.requests.count, 1)
        XCTAssertEqual(registrationCalls, 0)
    }

    /// Recovery deletes a retired key once its deadline passes, while an
    /// alert it decrypted can still be in Notification Center. Logout still
    /// removes that alert: it names the account it was decrypted for.
    func testLogoutRemovesAnAlertWhoseRetiredKeyAlreadyExpired() async throws {
        let currentTime = Date(timeIntervalSince1970: 1_794_096_000)
        let store = makeInstallationStore(
            providerGeneration: 1,
            provider: nil,
            providerPatchPending: false
        )
        try store.saveRecord(
            makeRecord(
                kid: "replacement",
                state: .active,
                pendingRevoke: false,
                bindGeneration: 1
            )
        )
        try store.saveRecord(
            makeRecord(
                kid: "retired",
                state: .retiring,
                pendingRevoke: false,
                bindGeneration: 1,
                retirementDeadline: currentTime.addingTimeInterval(-1)
            )
        )
        let notificationCenter = FakePushNotificationCenter(
            delivered: [
                PushNotificationCenterEntry(
                    identifier: "decrypted-by-retired",
                    userInfo: [
                        "kid": "retired",
                        "account_id": Self.accountA.id,
                        "instance_origin": Self.instanceOrigin,
                        "url": "/chat/session",
                    ]
                ),
                PushNotificationCenterEntry(
                    identifier: "another-account",
                    userInfo: [
                        "kid": "elsewhere",
                        "account_id": Self.accountB.id,
                    ]
                ),
                PushNotificationCenterEntry(
                    identifier: "undecrypted",
                    userInfo: ["avibe": ["kid": "elsewhere"]]
                ),
            ]
        )
        let coordinator = makeCoordinator(
            store: store,
            management: ManagementRecorder(),
            notificationCenter: notificationCenter,
            now: { currentTime }
        )
        coordinator.resumeAfterAccountActivation()
        try await waitUntil { store.records["retired"] == nil }
        try await waitUntil { !coordinator.hasWorkInFlight }

        await coordinator.prepareForLogout(throughBindGeneration: 1, reason: .signedOut)

        XCTAssertEqual(notificationCenter.removedDelivered, ["decrypted-by-retired"])
        XCTAssertEqual(notificationCenter.removedPending, [])
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

        await coordinator.prepareForLogout(throughBindGeneration: 1, reason: .signedOut)

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
        XCTAssertEqual(Self.jsonBody(request)?["reason"] as? String, "reinstalled")
        XCTAssertEqual(store.orphanedInstallations.count, 0)
        XCTAssertNotEqual(store.installation?.installationID, oldID)
        XCTAssertEqual(store.installation?.installationSecret.count, 32)
    }

    func testHealthRequestNotificationOpensOnlyItsStatusLandingPath() {
        var taps: [PushNotificationTap] = []
        var recoveries = 0
        let coordinator = makeCoordinator(
            store: InMemoryPushKeychainStore(), management: ManagementRecorder(),
            onTap: { taps.append($0) }, onTapRecovery: { recoveries += 1 }
        )
        coordinator.handleNotificationTap(userInfo: [
            "kind": "health.request", "account_id": Self.accountA.id,
            "instance_origin": Self.instanceOrigin,
            "url": "/mobile/health/request-1",
        ])
        XCTAssertEqual(taps, [PushNotificationTap(
            accountID: Self.accountA.id, instanceOrigin: Self.instanceOrigin,
            url: "/mobile/health/request-1"
        )])
        XCTAssertEqual(recoveries, 0)
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
        keychain: PushKeychainStoring? = nil,
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
            keychain: keychain ?? store,
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
        retirementDeadline: Date? = nil,
        vapidKeyHash: String = String(repeating: "a", count: 64)
    ) -> PushKeyRecord {
        PushKeyRecord(
            kid: kid,
            instanceOrigin: Self.instanceOrigin,
            accountID: Self.accountA.id,
            accountGeneration: 1,
            bindGeneration: bindGeneration,
            privateKey: Data(repeating: 13, count: 32),
            authSecret: Data(repeating: 14, count: 16),
            vapidKeyHash: vapidKeyHash,
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

    private static func requestKind(
        _ request: ManagementRecorder.RecordedRequest
    ) -> String {
        if request.path.contains("/registrations/") {
            return "\(request.method) registration"
        }
        return "\(request.method) \(request.path.split(separator: "/").last ?? "")"
    }

    /// The request's path without its query (revokes carry `?reason=`).
    private static func pathname(_ request: ManagementRecorder.RecordedRequest) -> Substring {
        request.path.split(separator: "?", maxSplits: 1).first ?? ""
    }

    private static func jsonBody(
        _ request: ManagementRecorder.RecordedRequest
    ) -> [String: Any]? {
        request.body
            .flatMap { try? JSONSerialization.jsonObject(with: $0) }
            .flatMap { $0 as? [String: Any] }
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

/// Calls `onProviderSettled` after every write that leaves a provider tuple
/// with no PATCH pending, before the writer goes on.
private final class ProviderSettledHookStore: PushKeychainStoring {
    let base: InMemoryPushKeychainStore
    var onProviderSettled: (() -> Void)?

    init(base: InMemoryPushKeychainStore) {
        self.base = base
    }

    func loadInstallation() throws -> PushInstallationState? {
        try base.loadInstallation()
    }

    func saveInstallation(_ state: PushInstallationState) throws {
        try base.saveInstallation(state)
        if state.provider != nil, !state.providerPatchPending {
            onProviderSettled?()
        }
    }

    func loadOrphanedInstallations() throws -> [PushOrphanedInstallation] {
        try base.loadOrphanedInstallations()
    }

    func saveOrphanedInstallations(
        _ installations: [PushOrphanedInstallation]
    ) throws {
        try base.saveOrphanedInstallations(installations)
    }

    func loadRecord(kid: String) throws -> PushKeyRecord? {
        try base.loadRecord(kid: kid)
    }

    func saveRecord(_ record: PushKeyRecord) throws {
        try base.saveRecord(record)
    }

    func deleteRecord(kid: String) throws {
        try base.deleteRecord(kid: kid)
    }

    func listRecords() throws -> [PushKeyRecord] {
        try base.listRecords()
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
