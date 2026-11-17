import EventKit
import Foundation
import UIKit
import XCTest
@testable import Avibe

final class SystemScheduleTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_800_000_000)

    func testRequestParserRequiresTheClosedVersionOneShape() throws {
        let request = try XCTUnwrap(SystemScheduleRequest.parse(validParams()))
        XCTAssertEqual(request.requestID, "request-1")
        XCTAssertEqual(request.operationID, "operation-1")
        XCTAssertEqual(request.kind, .alarm)
        XCTAssertEqual(request.title, "Take medicine")
        XCTAssertEqual(request.timeZoneIdentifier, "Asia/Shanghai")
        XCTAssertEqual(request.instanceID, "instance-1")
        XCTAssertEqual(request.sessionID, "session-1")

        var missingRecurrence = validParams()
        missingRecurrence.removeValue(forKey: "recurrence")
        XCTAssertNil(SystemScheduleRequest.parse(missingRecurrence))

        var recurring = validParams()
        recurring["recurrence"] = ["frequency": "daily"]
        XCTAssertNil(SystemScheduleRequest.parse(recurring))

        var extra = validParams()
        extra["platform"] = "AlarmKit"
        XCTAssertNil(SystemScheduleRequest.parse(extra))

        var invalidZone = validParams()
        invalidZone["timeZone"] = "Not/AZone"
        XCTAssertNil(SystemScheduleRequest.parse(invalidZone))

        for invalidIdentifier in ["hidden\u{7f}control", "hidden\u{200e}format"] {
            var invalidOperation = validParams()
            invalidOperation["operationId"] = invalidIdentifier
            XCTAssertNil(SystemScheduleRequest.parse(invalidOperation))
        }
    }

    func testCancelParserRequiresPurposeAndKeepsTheProductSurfaceClosed() throws {
        let params: [String: Any] = [
            "requestId": "cancel-1",
            "operationId": "operation-1",
            "kind": "reminder",
            "purpose": "user",
        ]
        XCTAssertEqual(
            SystemScheduleCancelRequest.parse(params),
            SystemScheduleCancelRequest(
                requestID: "cancel-1",
                operationID: "operation-1",
                kind: .reminder,
                purpose: .user
            )
        )

        var compensation = params
        compensation["purpose"] = "compensation"
        XCTAssertNil(SystemScheduleCancelRequest.parse(compensation))

        var missingPurpose = params
        missingPurpose.removeValue(forKey: "purpose")
        XCTAssertNil(SystemScheduleCancelRequest.parse(missingPurpose))

        var extra = params
        extra["systemIdentifier"] = "raw-platform-id"
        XCTAssertNil(SystemScheduleCancelRequest.parse(extra))
    }

    func testSharedContractVectorsCoverSwiftParsersAndEveryTerminalReceipt() throws {
        let vectors = try contractVectors()
        let createRequests = try XCTUnwrap(
            vectors["createRequests"] as? [[String: Any]]
        )
        XCTAssertEqual(createRequests.count, 2)
        XCTAssertEqual(
            createRequests.compactMap {
                SystemScheduleRequest.parse($0)?.title
            },
            ["Take medicine", "吃药 💊"]
        )

        let cancelRequests = try XCTUnwrap(
            vectors["cancelRequests"] as? [[String: Any]]
        )
        XCTAssertEqual(
            cancelRequests.compactMap {
                SystemScheduleCancelRequest.parse($0)?.purpose
            },
            [.user, .user]
        )

        let receiptVectors = try XCTUnwrap(
            vectors["receipts"] as? [[String: Any]]
        )
        var states: Set<SystemScheduleState> = []
        for vector in receiptVectors {
            let state = try XCTUnwrap(
                (vector["state"] as? String).flatMap(SystemScheduleState.init)
            )
            let kind = try XCTUnwrap(
                (vector["kind"] as? String).flatMap(SystemScheduleKind.init)
            )
            let authorization = try XCTUnwrap(
                (vector["authorization"] as? String)
                    .flatMap(SystemScheduleAuthorization.init)
            )
            let errorObject = vector["error"] as? [String: Any]
            let receipt = SystemScheduleReceipt(
                requestID: try XCTUnwrap(vector["requestId"] as? String),
                operationID: try XCTUnwrap(vector["operationId"] as? String),
                kind: kind,
                state: state,
                systemIdentifier: vector["systemIdentifier"] as? String,
                fireAt: try (vector["fireAt"] as? String).map(parseVectorDate),
                timeZoneIdentifier: vector["timeZone"] as? String,
                authorization: authorization,
                error: errorObject.map {
                    SystemScheduleErrorDetail(
                        code: $0["code"] as! String,
                        message: $0["message"] as! String
                    )
                }
            )
            XCTAssertEqual(
                try canonicalJSON(receipt.jsonObject),
                try canonicalJSON(vector)
            )
            states.insert(state)
        }
        XCTAssertEqual(
            states,
            [
                .scheduled,
                .userDenied,
                .permissionDenied,
                .unsupported,
                .failed,
                .expired,
                .staleContext,
                .cancelled,
            ]
        )
    }

    func testDeviceIdentityIsStableForOneInstallationAndRepairsInvalidState() throws {
        let suiteName = "SystemScheduleDeviceIdentityTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let first = SystemScheduleDeviceIdentity.resolve(defaults: defaults)
        XCTAssertEqual(SystemScheduleDeviceIdentity.resolve(defaults: defaults), first)
        XCTAssertNotNil(UUID(uuidString: first))

        defaults.set("not-a-uuid", forKey: "avibe.systemSchedule.deviceID")
        let repaired = SystemScheduleDeviceIdentity.resolve(defaults: defaults)
        XCTAssertNotEqual(repaired, first)
        XCTAssertNotNil(UUID(uuidString: repaired))
        XCTAssertEqual(SystemScheduleDeviceIdentity.resolve(defaults: defaults), repaired)
    }

    @MainActor
    func testLiveCapabilitiesExposeOnlyThePlatformPathForThisOS() {
        let capabilities = LiveSystemSchedulePlatform().capabilities()
        if #available(iOS 26.0, *) {
            XCTAssertTrue(capabilities.alarmKitAvailable)
            XCTAssertFalse(capabilities.remindersAvailable)
            XCTAssertEqual(capabilities.remindersAuthorization, .unsupported)
            XCTAssertEqual(capabilities.supportedKinds, [.alarm])
        } else {
            XCTAssertFalse(capabilities.alarmKitAvailable)
            XCTAssertTrue(capabilities.remindersAvailable)
            XCTAssertEqual(capabilities.alarmAuthorization, .unsupported)
            XCTAssertEqual(capabilities.supportedKinds, [.reminder])
        }
    }

    @MainActor
    func testCorruptAndFutureLocalStateFailClosedWithoutReplacement() async throws {
        for data in [
            Data("not-json".utf8),
            try JSONSerialization.data(
                withJSONObject: ["version": 2, "activeOwner": NSNull(), "records": []]
            ),
        ] {
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent(
                "SystemScheduleCorruptTests.\(UUID().uuidString)",
                isDirectory: true
            )
            defer { try? FileManager.default.removeItem(at: directory) }
            try FileManager.default.createDirectory(
                at: directory,
                withIntermediateDirectories: true
            )
            let fileURL = directory.appendingPathComponent("ledger.json")
            try data.write(to: fileURL)
            let platform = SystemSchedulePlatformSpy()
            let coordinator = SystemScheduleCoordinator(
                platform: platform,
                store: FileSystemSystemScheduleStore(fileURL: fileURL)
            )

            let receipt = await coordinator.schedule(
                makeRequest(),
                context: makeContext(),
                creationFence: { nil },
                postWriteFence: { nil }
            )

            XCTAssertEqual(receipt.state, .failed)
            XCTAssertEqual(receipt.error?.code, "local_state_unavailable")
            XCTAssertEqual(platform.scheduleCalls, 0)
            XCTAssertEqual(try Data(contentsOf: fileURL), data)
        }
    }

    @MainActor
    func testSameOperationIDIsScopedByInstance() async throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        let platform = SystemSchedulePlatformSpy()
        let coordinator = SystemScheduleCoordinator(platform: platform, store: store)
        let firstRequest = makeRequest(instanceID: "instance-1")
        let secondRequest = makeRequest(
            requestID: "request-2",
            instanceID: "instance-2"
        )

        let first = await coordinator.schedule(
            firstRequest,
            context: makeContext(owner: owner, instanceID: "instance-1"),
            creationFence: { nil },
            postWriteFence: { nil }
        )
        let second = await coordinator.schedule(
            secondRequest,
            context: makeContext(owner: owner, instanceID: "instance-2"),
            creationFence: { nil },
            postWriteFence: { nil }
        )

        XCTAssertEqual(first.state, .scheduled)
        XCTAssertEqual(second.state, .scheduled)
        XCTAssertNotEqual(first.systemIdentifier, second.systemIdentifier)
        XCTAssertEqual(platform.scheduleWriteCalls, 2)
        XCTAssertEqual(
            store.record(
                operationID: firstRequest.operationID,
                instanceID: firstRequest.instanceID,
                owner: owner
            )?.systemIdentifier,
            first.systemIdentifier
        )
        XCTAssertEqual(
            store.record(
                operationID: secondRequest.operationID,
                instanceID: secondRequest.instanceID,
                owner: owner
            )?.systemIdentifier,
            second.systemIdentifier
        )
    }

    @MainActor
    func testUserCancelTargetsTheExactInstanceRecord() async throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        let firstRequest = makeRequest(instanceID: "instance-1")
        let secondRequest = makeRequest(
            requestID: "request-2",
            instanceID: "instance-2"
        )
        let platform = SystemSchedulePlatformSpy()
        let firstIdentifier = platform.systemIdentifier(
            for: firstRequest,
            owner: owner
        )
        let secondIdentifier = platform.systemIdentifier(
            for: secondRequest,
            owner: owner
        )
        XCTAssertTrue(
            store.save(
                cleanupPendingRecord(
                    request: firstRequest,
                    owner: owner,
                    systemIdentifier: firstIdentifier,
                    updatedAt: now
                )
            )
        )
        XCTAssertTrue(
            store.save(
                cleanupPendingRecord(
                    request: secondRequest,
                    owner: owner,
                    systemIdentifier: secondIdentifier,
                    updatedAt: now.addingTimeInterval(1)
                )
            )
        )
        let coordinator = SystemScheduleCoordinator(platform: platform, store: store)

        let receipt = await coordinator.cancel(
            makeCancelRequest(),
            context: makeContext(owner: owner, instanceID: "instance-1"),
            stillCurrent: { true }
        )

        XCTAssertEqual(receipt.state, .cancelled)
        XCTAssertEqual(platform.cancelSystemIdentifiers.last, firstIdentifier)
        XCTAssertEqual(
            store.record(
                operationID: firstRequest.operationID,
                instanceID: firstRequest.instanceID,
                owner: owner
            )?.lifecycle,
            .closedRemoved
        )
        XCTAssertEqual(
            store.record(
                operationID: secondRequest.operationID,
                instanceID: secondRequest.instanceID,
                owner: owner
            )?.lifecycle,
            .managedCleanupPending
        )
    }

    // I3 review E1: the reservation crosses a real file durability barrier before OS work.
    @MainActor
    func testReservationIsDurableBeforeThePlatformWrite() async throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        let context = makeContext(owner: owner)
        let request = makeRequest()
        let platform = SystemSchedulePlatformSpy()
        platform.onScheduleWrite = {
            let reopened = FileSystemSystemScheduleStore(
                fileURL: directory.appendingPathComponent("ledger.json")
            )
            let record = reopened.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )
            XCTAssertEqual(record?.lifecycle, .creatingUnknown)
            XCTAssertTrue(record?.effectMayExist == true)
            XCTAssertEqual(
                record?.systemIdentifier,
                platform.systemIdentifier(for: request, owner: owner)
            )
        }
        let coordinator = SystemScheduleCoordinator(platform: platform, store: store)

        let receipt = await coordinator.schedule(
            request,
            context: context,
            creationFence: { nil },
            postWriteFence: { nil }
        )

        XCTAssertEqual(receipt.state, .scheduled)
        XCTAssertEqual(platform.scheduleWriteCalls, 1)
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.lifecycle,
            .managedScheduled
        )
    }

    // I3 review E1: a crash before rename leaves the previous committed ledger intact.
    @MainActor
    func testInterruptedPreRenameWritePreservesThePreviousLedger() throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        let request = makeRequest()
        let original = StoredSystemSchedule(
            operationID: request.operationID,
            owner: owner,
            intent: StoredSystemScheduleIntent(request: request),
            systemIdentifier: "system-1",
            authorization: .authorized,
            lifecycle: .reserved,
            effectMayExist: false,
            generation: 1,
            createState: nil,
            lastError: nil,
            updatedAt: now
        )
        XCTAssertTrue(store.save(original))

        let interrupted = FileSystemSystemScheduleStore(
            fileURL: directory.appendingPathComponent("ledger.json"),
            beforeRename: {
                throw NSError(domain: "intentional-pre-rename", code: 1)
            }
        )
        var changed = original
        changed.lifecycle = .managedScheduled
        changed.effectMayExist = true
        changed.generation += 1
        XCTAssertFalse(interrupted.save(changed))

        let reopened = FileSystemSystemScheduleStore(
            fileURL: directory.appendingPathComponent("ledger.json")
        )
        XCTAssertEqual(
            reopened.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            ),
            original
        )
    }

    // I3 review E1/E5: explicit logout cannot advance past an uncommitted detach.
    @MainActor
    func testInterruptedDetachPreservesActiveOwnerAndManagedRecord() throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        let request = makeRequest()
        let original = StoredSystemSchedule(
            operationID: request.operationID,
            owner: owner,
            intent: StoredSystemScheduleIntent(request: request),
            systemIdentifier: "system-1",
            authorization: .authorized,
            lifecycle: .managedScheduled,
            effectMayExist: true,
            generation: 1,
            createState: .scheduled,
            lastError: nil,
            updatedAt: now
        )
        XCTAssertTrue(store.save(original))

        let interrupted = FileSystemSystemScheduleStore(
            fileURL: directory.appendingPathComponent("ledger.json"),
            beforeRename: {
                throw NSError(domain: "intentional-pre-rename", code: 1)
            }
        )
        XCTAssertFalse(interrupted.detachActiveOwner())

        let reopened = FileSystemSystemScheduleStore(
            fileURL: directory.appendingPathComponent("ledger.json")
        )
        XCTAssertTrue(reopened.isActive(owner: owner))
        XCTAssertEqual(
            reopened.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            ),
            original
        )
    }

    @MainActor
    func testConfirmedAccountMatchDoesNotDetachThePersistedOwner() throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        let request = makeRequest()
        let record = StoredSystemSchedule(
            operationID: request.operationID,
            owner: owner,
            intent: StoredSystemScheduleIntent(request: request),
            systemIdentifier: "system-1",
            authorization: .authorized,
            lifecycle: .managedScheduled,
            effectMayExist: true,
            generation: 1,
            createState: .scheduled,
            lastError: nil,
            updatedAt: now
        )
        XCTAssertTrue(store.save(record))
        let ledgerURL = directory.appendingPathComponent("ledger.json")
        let committed = try Data(contentsOf: ledgerURL)

        XCTAssertTrue(
            store.detachActiveOwner(unlessAccountID: "account-1")
        )
        XCTAssertTrue(store.isActive(owner: owner))
        XCTAssertEqual(try Data(contentsOf: ledgerURL), committed)
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            ),
            record
        )
    }

    @MainActor
    func testDetachingTheActiveOwnerDoesNotRewritePriorSameAccountEpochs() throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let firstOwner = try XCTUnwrap(
            store.managementOwner(accountID: "account-1")
        )
        let firstRequest = makeRequest(operationID: "operation-first")
        let firstRecord = StoredSystemSchedule(
            operationID: firstRequest.operationID,
            owner: firstOwner,
            intent: StoredSystemScheduleIntent(request: firstRequest),
            systemIdentifier: "system-first",
            authorization: .authorized,
            lifecycle: .managedScheduled,
            effectMayExist: true,
            generation: 1,
            createState: .scheduled,
            lastError: nil,
            updatedAt: now
        )
        XCTAssertTrue(store.save(firstRecord))
        XCTAssertTrue(store.detachActiveOwner())
        let detachedFirst = try XCTUnwrap(
            store.record(
                operationID: firstRequest.operationID,
                instanceID: firstRequest.instanceID,
                owner: firstOwner
            )
        )

        let secondOwner = try XCTUnwrap(
            store.managementOwner(accountID: "account-1")
        )
        let secondRequest = makeRequest(operationID: "operation-second")
        let secondRecord = StoredSystemSchedule(
            operationID: secondRequest.operationID,
            owner: secondOwner,
            intent: StoredSystemScheduleIntent(request: secondRequest),
            systemIdentifier: "system-second",
            authorization: .authorized,
            lifecycle: .managedScheduled,
            effectMayExist: true,
            generation: 1,
            createState: .scheduled,
            lastError: nil,
            updatedAt: now
        )
        XCTAssertTrue(store.save(secondRecord))
        XCTAssertTrue(store.detachActiveOwner())

        XCTAssertEqual(
            store.record(
                operationID: firstRequest.operationID,
                instanceID: firstRequest.instanceID,
                owner: firstOwner
            ),
            detachedFirst
        )
        XCTAssertEqual(
            store.record(
                operationID: secondRequest.operationID,
                instanceID: secondRequest.instanceID,
                owner: secondOwner
            )?.lifecycle,
            .detachedRetained
        )
    }

    @MainActor
    func testScheduleAndUserCancellationAreIdempotent() async throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        let context = makeContext(owner: owner)
        let request = makeRequest()
        let platform = SystemSchedulePlatformSpy()
        let coordinator = SystemScheduleCoordinator(platform: platform, store: store)

        let first = await coordinator.schedule(
            request,
            context: context,
            creationFence: { nil },
            postWriteFence: { nil }
        )
        XCTAssertEqual(first.state, .scheduled)
        XCTAssertEqual(platform.scheduleWriteCalls, 1)

        platform.alarmAuthorization = .denied
        let retry = await coordinator.schedule(
            makeRequest(requestID: "request-2"),
            context: makeContext(
                owner: owner,
                runtimeIdentity: context.runtimeIdentity + 1,
                navigationEpoch: context.navigationEpoch + 1
            ),
            creationFence: { nil },
            postWriteFence: { nil }
        )
        XCTAssertEqual(retry.state, .scheduled)
        XCTAssertEqual(retry.requestID, "request-2")
        XCTAssertEqual(retry.systemIdentifier, first.systemIdentifier)
        XCTAssertEqual(retry.authorization, .denied)
        XCTAssertEqual(platform.scheduleWriteCalls, 1)

        platform.alarmAuthorization = .authorized
        let cancellation = makeCancelRequest()
        let cancelled = await coordinator.cancel(
            cancellation,
            context: context,
            stillCurrent: { true }
        )
        XCTAssertEqual(cancelled.state, .cancelled)
        XCTAssertEqual(platform.cancelWriteCalls, 1)

        platform.alarmAuthorization = .denied
        let repeated = await coordinator.cancel(
            makeCancelRequest(requestID: "cancel-2"),
            context: context,
            stillCurrent: { true }
        )
        XCTAssertEqual(repeated.state, .cancelled)
        XCTAssertEqual(repeated.requestID, "cancel-2")
        XCTAssertEqual(repeated.authorization, .denied)
        XCTAssertEqual(platform.cancelWriteCalls, 1)
    }

    // I3 review C2: detached cross-owner history blocks duplicate creation after relogin.
    @MainActor
    func testDetachIsPermanentAndReloginGetsANewOwnerEpoch() async throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let firstOwner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        let firstContext = makeContext(owner: firstOwner)
        let request = makeRequest()
        let platform = SystemSchedulePlatformSpy()
        let coordinator = SystemScheduleCoordinator(platform: platform, store: store)

        let scheduled = await coordinator.schedule(
            request,
            context: firstContext,
            creationFence: { nil },
            postWriteFence: { nil }
        )
        XCTAssertEqual(scheduled.state, .scheduled)

        coordinator.detachActiveOwner()
        let detached = try XCTUnwrap(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: firstOwner
            )
        )
        XCTAssertEqual(detached.lifecycle, .detachedRetained)

        var attemptedResurrection = detached
        attemptedResurrection.lifecycle = .managedScheduled
        XCTAssertFalse(store.save(attemptedResurrection))

        let secondOwner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        XCTAssertNotEqual(secondOwner.ownerEpoch, firstOwner.ownerEpoch)
        let secondContext = makeContext(owner: secondOwner)
        let retry = try XCTUnwrap(
            coordinator.prepare(
                makeRequest(requestID: "request-after-relogin"),
                context: secondContext
            )
        )
        XCTAssertEqual(retry.state, .scheduled)
        XCTAssertEqual(retry.systemIdentifier, scheduled.systemIdentifier)
        XCTAssertEqual(platform.scheduleWriteCalls, 1)
        XCTAssertNil(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: secondOwner
            )
        )
        let cancellation = await coordinator.cancel(
            makeCancelRequest(),
            context: secondContext,
            stillCurrent: { true }
        )
        XCTAssertEqual(cancellation.state, .failed)
        XCTAssertEqual(cancellation.error?.code, "item_not_found")
        XCTAssertEqual(platform.cancelWriteCalls, 0)

        XCTAssertNotEqual(
            platform.systemIdentifier(for: request, owner: firstOwner),
            platform.systemIdentifier(for: request, owner: secondOwner)
        )
    }

    @MainActor
    func testLogoutCreateCleanupFailureStaysDetachedAcrossReloginAndRetries() async throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        let context = makeContext(owner: owner)
        let request = makeRequest()
        let platform = SystemSchedulePlatformSpy()
        let coordinator = SystemScheduleCoordinator(platform: platform, store: store)
        platform.cancelResult = .failure(
            .failed(
                code: "cleanup_failed",
                message: "Cleanup failed.",
                systemIdentifier: platform.systemIdentifier(
                    for: request,
                    owner: owner
                ),
                effectMayExist: true
            )
        )
        platform.onScheduleWrite = {
            coordinator.detachActiveOwner()
        }

        let receipt = await coordinator.schedule(
            request,
            context: context,
            creationFence: {
                store.isActive(owner: owner) ? nil : .staleContext
            },
            postWriteFence: {
                store.isActive(owner: owner) ? nil : .staleContext
            }
        )

        XCTAssertEqual(receipt.state, .staleContext)
        XCTAssertEqual(platform.scheduleWriteCalls, 1)
        XCTAssertEqual(platform.cancelWriteCalls, 1)
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.lifecycle,
            .detachedCleanupPending
        )

        let newOwner = try XCTUnwrap(
            store.managementOwner(accountID: owner.accountID)
        )
        XCTAssertNotEqual(newOwner, owner)
        let newContext = makeContext(owner: newOwner)
        let createRetry = try XCTUnwrap(
            coordinator.prepare(
                makeRequest(requestID: "request-after-relogin"),
                context: newContext
            )
        )
        XCTAssertEqual(createRetry.state, .staleContext)
        XCTAssertEqual(createRetry.systemIdentifier, receipt.systemIdentifier)
        XCTAssertEqual(platform.scheduleWriteCalls, 1)
        XCTAssertNil(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: newOwner
            )
        )
        let userCancel = await coordinator.cancel(
            makeCancelRequest(requestID: "cancel-after-relogin"),
            context: newContext,
            stillCurrent: { true }
        )
        XCTAssertEqual(userCancel.error?.code, "item_not_found")
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.lifecycle,
            .detachedCleanupPending
        )

        platform.cancelResult = .success(.authorized)
        await coordinator.retryPendingCleanup()
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.lifecycle,
            .detachedCleaned
        )
    }

    // I3 review D1: pending create and user-cancel continuations derive from cleanup truth.
    @MainActor
    func testUserCancelWhilePlatformCreateIsInFlightRemovesTheCreatedItem() async throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        let context = makeContext(owner: owner)
        let request = makeRequest()
        let platform = SystemSchedulePlatformSpy()
        platform.suspendScheduleAfterWrite = true
        let coordinator = SystemScheduleCoordinator(platform: platform, store: store)

        let createTask = Task { @MainActor in
            await coordinator.schedule(
                request,
                context: context,
                creationFence: { nil },
                postWriteFence: { nil }
            )
        }
        await eventually { platform.hasSuspendedSchedule }

        let cancellation = await coordinator.cancel(
            makeCancelRequest(),
            context: context,
            stillCurrent: { true }
        )
        XCTAssertEqual(cancellation.state, .cancelled)
        XCTAssertEqual(platform.cancelWriteCalls, 1)

        platform.resumeSchedule()
        let createReceipt = await createTask.value
        XCTAssertEqual(createReceipt.state, .cancelled)
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.lifecycle,
            .closedRemoved
        )
    }

    @MainActor
    func testAcceptedItemRecoversFromFinalLedgerWriteFailureWithoutDuplicateCreate() async {
        let owner = SystemScheduleManagementOwner(
            accountID: "account-1",
            ownerEpoch: "owner-epoch-1"
        )
        let store = SystemScheduleStoreSpy(
            activeOwner: owner,
            failOnSaveCalls: [3]
        )
        let platform = SystemSchedulePlatformSpy()
        let coordinator = SystemScheduleCoordinator(platform: platform, store: store)
        let request = makeRequest()
        let context = makeContext(owner: owner)

        let interrupted = await coordinator.schedule(
            request,
            context: context,
            creationFence: { nil },
            postWriteFence: { nil }
        )
        XCTAssertEqual(interrupted.state, .failed)
        XCTAssertEqual(interrupted.error?.code, "local_state_write_failed")
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.lifecycle,
            .creatingUnknown
        )
        XCTAssertEqual(platform.scheduleWriteCalls, 1)

        store.failOnSaveCalls = []
        platform.existenceResults = [.exists(.authorized)]
        let recovered = await coordinator.schedule(
            makeRequest(requestID: "request-recovered"),
            context: context,
            creationFence: { nil },
            postWriteFence: { nil }
        )
        XCTAssertEqual(recovered.state, .scheduled)
        XCTAssertEqual(platform.existenceCalls, 1)
        XCTAssertEqual(platform.scheduleWriteCalls, 1)
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.lifecycle,
            .managedScheduled
        )
    }

    @MainActor
    func testUnknownRecoveryKeepsCreatingUnknownAndItsIdentity() async {
        let owner = SystemScheduleManagementOwner(
            accountID: "account-1",
            ownerEpoch: "owner-epoch-1"
        )
        let store = SystemScheduleStoreSpy(
            activeOwner: owner,
            failOnSaveCalls: [3]
        )
        let platform = SystemSchedulePlatformSpy()
        let coordinator = SystemScheduleCoordinator(platform: platform, store: store)
        let request = makeRequest()
        let context = makeContext(owner: owner)

        _ = await coordinator.schedule(
            request,
            context: context,
            creationFence: { nil },
            postWriteFence: { nil }
        )
        let identifier = store.record(
            operationID: request.operationID,
            instanceID: request.instanceID,
            owner: owner
        )?.systemIdentifier

        store.failOnSaveCalls = []
        platform.existenceResults = [
            .unknown(
                .failed(
                    code: "lookup_unknown",
                    message: "The item could not be inspected.",
                    systemIdentifier: identifier,
                    effectMayExist: true
                )
            ),
        ]
        let receipt = await coordinator.schedule(
            makeRequest(requestID: "request-recovery"),
            context: context,
            creationFence: { nil },
            postWriteFence: { nil }
        )

        XCTAssertEqual(receipt.state, .failed)
        XCTAssertEqual(receipt.error?.code, "lookup_unknown")
        XCTAssertEqual(receipt.systemIdentifier, identifier)
        XCTAssertEqual(platform.scheduleWriteCalls, 1)
        let record = store.record(
            operationID: request.operationID,
            instanceID: request.instanceID,
            owner: owner
        )
        XCTAssertEqual(record?.lifecycle, .creatingUnknown)
        XCTAssertEqual(record?.systemIdentifier, identifier)
        XCTAssertTrue(record?.effectMayExist == true)
    }

    // I3 review C3: recovery runs before request-expiry guards for every existence result.
    @MainActor
    func testExpiredCreatingUnknownRecoveryHandlesEveryExistenceResult() async {
        let cases: [(
            label: String,
            existence: SystemScheduleExistence,
            state: SystemScheduleState,
            lifecycle: StoredSystemSchedule.Lifecycle,
            cancelWrites: Int
        )] = [
            (
                "exists",
                .exists(.authorized),
                .expired,
                .closedRemoved,
                1
            ),
            (
                "absent",
                .absentConclusive(.authorized),
                .failed,
                .closedNoItem,
                0
            ),
            (
                "unknown",
                .unknown(
                    .failed(
                        code: "lookup_unknown",
                        message: "The item could not be inspected.",
                        effectMayExist: true
                    )
                ),
                .failed,
                .creatingUnknown,
                0
            ),
        ]

        for item in cases {
            let owner = SystemScheduleManagementOwner(
                accountID: "account-1",
                ownerEpoch: "owner-epoch-\(item.label)"
            )
            let store = SystemScheduleStoreSpy(
                activeOwner: owner,
                failOnSaveCalls: [3]
            )
            let platform = SystemSchedulePlatformSpy()
            let coordinator = SystemScheduleCoordinator(
                platform: platform,
                store: store
            )
            let request = makeRequest()
            let context = makeContext(owner: owner)
            _ = await coordinator.schedule(
                request,
                context: context,
                creationFence: { nil },
                postWriteFence: { nil }
            )
            let identifier = store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.systemIdentifier
            store.failOnSaveCalls = []
            platform.existenceResults = [item.existence]

            let receipt = await coordinator.schedule(
                makeRequest(requestID: "request-recovery-\(item.label)"),
                context: context,
                creationFence: { .expired },
                postWriteFence: { .expired }
            )

            XCTAssertEqual(receipt.state, item.state, item.label)
            XCTAssertEqual(platform.existenceCalls, 1, item.label)
            XCTAssertEqual(platform.scheduleWriteCalls, 1, item.label)
            XCTAssertEqual(platform.cancelWriteCalls, item.cancelWrites, item.label)
            let record = store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )
            XCTAssertEqual(record?.lifecycle, item.lifecycle, item.label)
            if item.label == "unknown" {
                XCTAssertEqual(receipt.systemIdentifier, identifier)
                XCTAssertEqual(record?.systemIdentifier, identifier)
            }
        }
    }

    // I3 review E6: conclusive absence is a terminal no-item tombstone, never a recreate.
    @MainActor
    func testConclusiveAbsenceClosesRecoveryWithoutAnotherCreate() async {
        let owner = SystemScheduleManagementOwner(
            accountID: "account-1",
            ownerEpoch: "owner-epoch-absence"
        )
        let store = SystemScheduleStoreSpy(activeOwner: owner)
        let request = makeRequest()
        let platform = SystemSchedulePlatformSpy()
        XCTAssertTrue(
            store.save(
                StoredSystemSchedule(
                    operationID: request.operationID,
                    owner: owner,
                    intent: StoredSystemScheduleIntent(request: request),
                    systemIdentifier: platform.systemIdentifier(for: request, owner: owner),
                    authorization: .authorized,
                    lifecycle: .creatingUnknown,
                    effectMayExist: true,
                    generation: 2,
                    createState: nil,
                    lastError: nil,
                    updatedAt: now
                )
            )
        )
        platform.existenceResults = [.absentConclusive(.authorized)]
        let coordinator = SystemScheduleCoordinator(platform: platform, store: store)

        let receipt = await coordinator.schedule(
            makeRequest(requestID: "request-recovery-absent"),
            context: makeContext(owner: owner),
            creationFence: { nil },
            postWriteFence: { nil }
        )

        XCTAssertEqual(receipt.state, .failed)
        XCTAssertEqual(receipt.error?.code, "recovery_item_absent")
        XCTAssertEqual(platform.existenceCalls, 1)
        XCTAssertEqual(platform.scheduleWriteCalls, 0)
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.lifecycle,
            .closedNoItem
        )
    }

    @MainActor
    func testContextChangeAfterCancelLookupPreventsSystemRemoval() async throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        let context = makeContext(owner: owner)
        let request = makeRequest()
        let platform = SystemSchedulePlatformSpy()
        let coordinator = SystemScheduleCoordinator(platform: platform, store: store)

        _ = await coordinator.schedule(
            request,
            context: context,
            creationFence: { nil },
            postWriteFence: { nil }
        )
        var current = true
        platform.onBeforeCancelWrite = { current = false }
        let receipt = await coordinator.cancel(
            makeCancelRequest(),
            context: context,
            stillCurrent: { current }
        )

        XCTAssertEqual(receipt.state, .staleContext)
        XCTAssertEqual(platform.cancelCalls, 1)
        XCTAssertEqual(platform.cancelWriteCalls, 0)
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.lifecycle,
            .managedCleanupPending
        )
    }

    @MainActor
    func testCleanupFailureRetainsIdentityAndLifecycleRetryRemovesIt() async throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        let context = makeContext(owner: owner)
        let request = makeRequest()
        let platform = SystemSchedulePlatformSpy()
        platform.scheduleResult = .failure(
            .failed(
                code: "schedule_unknown",
                message: "Creation may have succeeded.",
                systemIdentifier: platform.systemIdentifier(for: request, owner: owner),
                effectMayExist: true
            )
        )
        let coordinator = SystemScheduleCoordinator(platform: platform, store: store)

        let failed = await coordinator.schedule(
            request,
            context: context,
            creationFence: { nil },
            postWriteFence: { nil }
        )
        XCTAssertEqual(failed.state, .failed)
        XCTAssertNotNil(failed.systemIdentifier)
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.lifecycle,
            .managedCleanupPending
        )

        platform.cancelResult = .failure(
            .failed(
                code: "cleanup_failed",
                message: "Cleanup failed.",
                systemIdentifier: failed.systemIdentifier,
                effectMayExist: true
            )
        )
        await coordinator.retryPendingCleanup()
        XCTAssertEqual(platform.cancelSystemIdentifiers.last, failed.systemIdentifier)
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.lifecycle,
            .managedCleanupPending
        )

        platform.cancelResult = .success(.authorized)
        await coordinator.retryPendingCleanup()
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.lifecycle,
            .closedRemoved
        )
    }

    // I3 review D5: a completed cleanup tombstone replays without another OS write.
    @MainActor
    func testCompletedCleanupTombstoneReplaysUserCancelWithoutAnotherOSWrite() async throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        let context = makeContext(owner: owner)
        let request = makeRequest()
        let platform = SystemSchedulePlatformSpy()
        let coordinator = SystemScheduleCoordinator(platform: platform, store: store)

        let scheduled = await coordinator.schedule(
            request,
            context: context,
            creationFence: { nil },
            postWriteFence: { nil }
        )
        XCTAssertEqual(scheduled.state, .scheduled)
        let first = await coordinator.cancel(
            makeCancelRequest(),
            context: context,
            stillCurrent: { true }
        )
        XCTAssertEqual(first.state, .cancelled)
        XCTAssertEqual(platform.cancelWriteCalls, 1)
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.lifecycle,
            .closedRemoved
        )

        let replay = await coordinator.cancel(
            makeCancelRequest(requestID: "user-cancel-replay"),
            context: context,
            stillCurrent: { true }
        )
        XCTAssertEqual(replay.state, .cancelled)
        XCTAssertNil(replay.systemIdentifier)
        XCTAssertEqual(platform.cancelWriteCalls, 1)
    }

    @MainActor
    func testLifecycleCleanupPrefersPendingRecordOverNewerTombstone() async throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let request = makeRequest()
        let firstOwner = try XCTUnwrap(
            store.managementOwner(accountID: "account-1")
        )
        let platform = SystemSchedulePlatformSpy()
        let pendingIdentifier = platform.systemIdentifier(
            for: request,
            owner: firstOwner
        )
        XCTAssertTrue(
            store.save(
                cleanupPendingRecord(
                    request: request,
                    owner: firstOwner,
                    systemIdentifier: pendingIdentifier,
                    updatedAt: now
                )
            )
        )
        let currentOwner = try XCTUnwrap(
            store.managementOwner(accountID: "account-2")
        )
        var tombstone = cleanupPendingRecord(
            request: request,
            owner: currentOwner,
            systemIdentifier: platform.systemIdentifier(
                for: request,
                owner: currentOwner
            ),
            updatedAt: now.addingTimeInterval(1)
        )
        tombstone.lifecycle = .closedRemoved
        tombstone.effectMayExist = false
        XCTAssertTrue(store.save(tombstone))
        let coordinator = SystemScheduleCoordinator(platform: platform, store: store)

        await coordinator.retryPendingCleanup()

        XCTAssertEqual(platform.cancelSystemIdentifiers, [pendingIdentifier])
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: firstOwner
            )?.lifecycle,
            .detachedCleaned
        )
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: currentOwner
            )?.lifecycle,
            .closedRemoved
        )
    }

    // I3 review D6: terminal platform evidence persists the observed authorization atomically.
    @MainActor
    func testTerminalPlatformFailurePersistsObservedAuthorization() async throws {
        let cases: [(
            failure: SystemSchedulePlatformFailure,
            state: SystemScheduleState,
            authorization: SystemScheduleAuthorization
        )] = [
            (.permissionDenied(.denied), .permissionDenied, .denied),
            (.unsupported, .unsupported, .unsupported),
        ]
        for testCase in cases {
            let (directory, store) = try makeStore()
            defer { try? FileManager.default.removeItem(at: directory) }
            let owner = try XCTUnwrap(
                store.managementOwner(accountID: "account-1")
            )
            let context = makeContext(owner: owner)
            let request = makeRequest()
            let platform = SystemSchedulePlatformSpy()
            platform.alarmAuthorization = .notDetermined
            platform.scheduleResult = .failure(testCase.failure)
            let coordinator = SystemScheduleCoordinator(
                platform: platform,
                store: store
            )

            let receipt = await coordinator.schedule(
                request,
                context: context,
                creationFence: { nil },
                postWriteFence: { nil }
            )

            XCTAssertEqual(receipt.state, testCase.state)
            XCTAssertEqual(receipt.authorization, testCase.authorization)
            let record = try XCTUnwrap(
                store.record(
                    operationID: request.operationID,
                    instanceID: request.instanceID,
                    owner: owner
                )
            )
            XCTAssertEqual(record.authorization, testCase.authorization)
            XCTAssertEqual(record.lifecycle, .closedNoItem)
        }
    }

    // I3 review D3: a lookup returning after logout keeps exact detached cleanup identity.
    @MainActor
    func testLogoutDuringRecoveryLookupPreservesIdentityForLocalCleanup() async throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        let context = makeContext(owner: owner)
        let request = makeRequest()
        let platform = SystemSchedulePlatformSpy()
        let identifier = platform.systemIdentifier(for: request, owner: owner)
        XCTAssertTrue(
            store.save(
                StoredSystemSchedule(
                    operationID: request.operationID,
                    owner: owner,
                    intent: StoredSystemScheduleIntent(request: request),
                    systemIdentifier: identifier,
                    authorization: .authorized,
                    lifecycle: .creatingUnknown,
                    effectMayExist: true,
                    generation: 2,
                    createState: nil,
                    lastError: nil,
                    updatedAt: now
                )
            )
        )
        platform.existenceResults = [.exists(.authorized)]
        platform.suspendExistence = true
        platform.cancelResult = .failure(
            .failed(
                code: "cleanup_failed",
                message: "Cleanup failed.",
                systemIdentifier: identifier,
                effectMayExist: true
            )
        )
        let coordinator = SystemScheduleCoordinator(platform: platform, store: store)

        let recovery = Task { @MainActor in
            await coordinator.schedule(
                request,
                context: context,
                creationFence: {
                    store.isActive(owner: owner) ? nil : .staleContext
                },
                postWriteFence: {
                    store.isActive(owner: owner) ? nil : .staleContext
                }
            )
        }
        await eventually { platform.hasSuspendedExistence }
        coordinator.detachActiveOwner()
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.lifecycle,
            .detachedCleanupPending
        )
        platform.resumeExistence()

        let receipt = await recovery.value
        XCTAssertEqual(receipt.state, .staleContext)
        XCTAssertEqual(receipt.systemIdentifier, identifier)
        XCTAssertEqual(platform.cancelSystemIdentifiers, [identifier])
        XCTAssertEqual(platform.cancelWriteCalls, 1)
        let retained = try XCTUnwrap(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )
        )
        XCTAssertEqual(retained.lifecycle, .detachedCleanupPending)
        XCTAssertEqual(retained.systemIdentifier, identifier)
        XCTAssertTrue(retained.effectMayExist)
        XCTAssertEqual(retained.lastError?.code, "cleanup_failed")
    }

    @MainActor
    func testDeadlineCrossedBeforePlatformWriteCreatesNoSystemItem() async throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        let context = makeContext(owner: owner)
        let request = makeRequest(expiresAt: now.addingTimeInterval(10))
        var currentTime = now
        let platform = SystemSchedulePlatformSpy()
        platform.onBeforeScheduleWrite = {
            currentTime = self.now.addingTimeInterval(20)
        }
        let coordinator = SystemScheduleCoordinator(platform: platform, store: store)
        let creationFence = {
            currentTime >= request.expiresAt
                ? SystemScheduleFenceFailure.expired
                : nil
        }

        let receipt = await coordinator.schedule(
            request,
            context: context,
            creationFence: creationFence,
            postWriteFence: { nil }
        )

        XCTAssertEqual(receipt.state, .expired)
        XCTAssertEqual(platform.scheduleCalls, 1)
        XCTAssertEqual(platform.scheduleWriteCalls, 0)
        XCTAssertEqual(platform.cancelWriteCalls, 0)
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.lifecycle,
            .closedNoItem
        )
    }

    @MainActor
    func testDeadlineCrossedAfterPlatformWriteKeepsScheduledResultWithoutCleanup() async throws {
        let (directory, store) = try makeStore()
        defer { try? FileManager.default.removeItem(at: directory) }
        let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        let context = makeContext(owner: owner)
        let request = makeRequest(expiresAt: now.addingTimeInterval(10))
        var currentTime = now
        let platform = SystemSchedulePlatformSpy()
        platform.onScheduleWrite = {
            currentTime = self.now.addingTimeInterval(20)
        }
        let coordinator = SystemScheduleCoordinator(platform: platform, store: store)

        let receipt = await coordinator.schedule(
            request,
            context: context,
            creationFence: {
                currentTime >= request.expiresAt ? .expired : nil
            },
            postWriteFence: { nil }
        )

        XCTAssertEqual(receipt.state, .scheduled)
        XCTAssertEqual(platform.scheduleWriteCalls, 1)
        XCTAssertEqual(platform.cancelWriteCalls, 0)
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.lifecycle,
            .managedScheduled
        )
    }

    @MainActor
    func testPlatformIdentityIncludesOwnerEpochAndInstance() throws {
        let platform = LiveSystemSchedulePlatform()
        let request = makeRequest()
        let owner = SystemScheduleManagementOwner(
            accountID: "account-1",
            ownerEpoch: "owner-epoch-1"
        )
        let otherOwner = SystemScheduleManagementOwner(
            accountID: "account-1",
            ownerEpoch: "owner-epoch-2"
        )

        XCTAssertEqual(
            platform.systemIdentifier(for: request, owner: owner),
            platform.systemIdentifier(
                for: makeRequest(requestID: "retry"),
                owner: owner
            )
        )
        XCTAssertNotEqual(
            platform.systemIdentifier(for: request, owner: owner),
            platform.systemIdentifier(for: request, owner: otherOwner)
        )
        XCTAssertNotEqual(
            platform.systemIdentifier(for: request, owner: owner),
            platform.systemIdentifier(
                for: makeRequest(instanceID: "instance-2"),
                owner: owner
            )
        )
    }

    @MainActor
    func testReminderDatesHaveAStartMatchingTheDueDate() throws {
        let reminderRequest = SystemScheduleRequest(
            requestID: "reminder-request",
            operationID: "reminder-operation",
            kind: .reminder,
            title: "Take medicine",
            fireAt: now.addingTimeInterval(3_600),
            timeZoneIdentifier: "Asia/Shanghai",
            instanceID: "instance-1",
            sessionID: "session-1",
            expiresAt: now.addingTimeInterval(300)
        )
        let reminder = EKReminder(eventStore: EKEventStore())
        let timeZone = try XCTUnwrap(TimeZone(identifier: "Asia/Shanghai"))
        LiveSystemSchedulePlatform.configureReminderDates(
            reminder,
            request: reminderRequest,
            timeZone: timeZone
        )
        let start = try XCTUnwrap(reminder.startDateComponents)
        let due = try XCTUnwrap(reminder.dueDateComponents)
        for component in [
            Calendar.Component.year,
            .month,
            .day,
            .hour,
            .minute,
            .second,
        ] {
            XCTAssertEqual(
                start.value(for: component),
                due.value(for: component),
                "Mismatched \(component)"
            )
        }
        XCTAssertEqual(start.timeZone, due.timeZone)
    }

    @MainActor
    func testAccessibilityAnnouncerMovesFocusAndReportsBothScreenChanges() {
        var focused = false
        var notifications: [UIAccessibility.Notification] = []
        let announcer = SystemScheduleAccessibilityAnnouncer {
            notifications.append($0)
        }

        announcer.didPresent { focused = true }
        announcer.didDismiss()

        XCTAssertTrue(focused)
        XCTAssertEqual(notifications, [.screenChanged, .screenChanged])
    }

    private func contractVectors() throws -> [String: Any] {
        let repositoryRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let url = repositoryRoot
            .appendingPathComponent("tests/fixtures/system_schedule_contract_vectors.json")
        return try XCTUnwrap(
            JSONSerialization.jsonObject(with: Data(contentsOf: url))
                as? [String: Any]
        )
    }

    private func parseVectorDate(_ value: String) throws -> Date {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return try XCTUnwrap(formatter.date(from: value))
    }

    private func canonicalJSON(_ value: Any) throws -> Data {
        try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
    }

    private func validParams() -> [String: Any] {
        [
            "requestId": "request-1",
            "operationId": "operation-1",
            "kind": "alarm",
            "title": "  Take medicine  ",
            "fireAt": "2027-01-15T19:30:00+08:00",
            "timeZone": "Asia/Shanghai",
            "recurrence": NSNull(),
            "instanceId": "instance-1",
            "sessionId": "session-1",
            "expiresAt": "2027-01-15T19:35:00+08:00",
        ]
    }

    private func makeRequest(
        requestID: String = "request-1",
        operationID: String = "operation-1",
        instanceID: String = "instance-1",
        expiresAt: Date? = nil
    ) -> SystemScheduleRequest {
        SystemScheduleRequest(
            requestID: requestID,
            operationID: operationID,
            kind: .alarm,
            title: "Take medicine",
            fireAt: now.addingTimeInterval(3_600),
            timeZoneIdentifier: "UTC",
            instanceID: instanceID,
            sessionID: "session-1",
            expiresAt: expiresAt ?? now.addingTimeInterval(300)
        )
    }

    private func makeCancelRequest(
        requestID: String = "cancel-1",
        purpose: SystemScheduleCancelPurpose = .user
    ) -> SystemScheduleCancelRequest {
        SystemScheduleCancelRequest(
            requestID: requestID,
            operationID: "operation-1",
            kind: .alarm,
            purpose: purpose
        )
    }

    private func makeContext(
        owner: SystemScheduleManagementOwner = SystemScheduleManagementOwner(
            accountID: "account-1",
            ownerEpoch: "owner-epoch-1"
        ),
        instanceID: String = "instance-1",
        runtimeIdentity: Int = 7,
        navigationEpoch: Int = 11
    ) -> SystemScheduleContext {
        SystemScheduleContext(
            owner: owner,
            instanceID: instanceID,
            origin: "https://instance-one.example",
            runtimeIdentity: runtimeIdentity,
            navigationEpoch: navigationEpoch
        )
    }

    private func cleanupPendingRecord(
        request: SystemScheduleRequest,
        owner: SystemScheduleManagementOwner,
        systemIdentifier: String,
        updatedAt: Date
    ) -> StoredSystemSchedule {
        StoredSystemSchedule(
            operationID: request.operationID,
            owner: owner,
            intent: StoredSystemScheduleIntent(request: request),
            systemIdentifier: systemIdentifier,
            authorization: .authorized,
            lifecycle: .managedCleanupPending,
            effectMayExist: true,
            generation: 1,
            createState: .cancelled,
            lastError: nil,
            updatedAt: updatedAt
        )
    }

    @MainActor
    private func makeStore() throws -> (
        directory: URL,
        store: FileSystemSystemScheduleStore
    ) {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(
            "SystemScheduleTests.\(UUID().uuidString)",
            isDirectory: true
        )
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true
        )
        return (
            directory,
            FileSystemSystemScheduleStore(
                fileURL: directory.appendingPathComponent("ledger.json")
            )
        )
    }

    @MainActor
    private func eventually(
        _ condition: @escaping @MainActor () -> Bool,
        attempts: Int = 1_000
    ) async {
        for _ in 0..<attempts {
            if condition() { return }
            await Task.yield()
        }
        XCTFail("Condition did not become true")
    }
}

@MainActor
private final class SystemSchedulePlatformSpy: SystemSchedulePlatforming {
    var scheduleCalls = 0
    var scheduleWriteCalls = 0
    var cancelCalls = 0
    var cancelWriteCalls = 0
    var cancelSystemIdentifiers: [String] = []
    var existenceCalls = 0
    var alarmAuthorization: SystemScheduleAuthorization = .authorized
    var remindersAuthorization: SystemScheduleAuthorization = .authorized
    var scheduleResult: Result<
        SystemScheduleSubmission,
        SystemSchedulePlatformFailure
    >?
    var cancelResult: Result<
        SystemScheduleAuthorization,
        SystemSchedulePlatformFailure
    > = .success(.authorized)
    var existenceResults: [SystemScheduleExistence] = []
    var suspendExistence = false
    var onBeforeScheduleWrite: (() -> Void)?
    var onScheduleWrite: (() -> Void)?
    var onBeforeCancelWrite: (() -> Void)?
    var suspendScheduleAfterWrite = false
    private var suspendedExistence: CheckedContinuation<
        SystemScheduleExistence,
        Never
    >?
    private var suspendedExistenceResult: SystemScheduleExistence?
    private var suspendedSchedule: CheckedContinuation<
        Result<SystemScheduleSubmission, SystemSchedulePlatformFailure>,
        Never
    >?
    private var suspendedScheduleResult: Result<
        SystemScheduleSubmission,
        SystemSchedulePlatformFailure
    >?

    var hasSuspendedExistence: Bool { suspendedExistence != nil }
    var hasSuspendedSchedule: Bool { suspendedSchedule != nil }

    func capabilities() -> SystemScheduleCapabilities {
        SystemScheduleCapabilities(
            iosVersion: "26.6.2",
            alarmKitAvailable: true,
            alarmAuthorization: alarmAuthorization,
            remindersAvailable: true,
            remindersAuthorization: remindersAuthorization,
            supportedKinds: [.alarm, .reminder]
        )
    }

    func systemIdentifier(
        for request: SystemScheduleRequest,
        owner: SystemScheduleManagementOwner
    ) -> String {
        "system-\(owner.ownerEpoch)-\(request.instanceID)-\(request.operationID)"
    }

    func existence(
        kind: SystemScheduleKind,
        systemIdentifier: String
    ) async -> SystemScheduleExistence {
        existenceCalls += 1
        let result: SystemScheduleExistence
        if !existenceResults.isEmpty {
            result = existenceResults.removeFirst()
        } else {
            result = .absentConclusive(authorization(for: kind))
        }
        if suspendExistence {
            suspendedExistenceResult = result
            return await withCheckedContinuation { continuation in
                suspendedExistence = continuation
            }
        }
        return result
    }

    func resumeExistence() {
        let continuation = suspendedExistence
        let result = suspendedExistenceResult
        suspendedExistence = nil
        suspendedExistenceResult = nil
        suspendExistence = false
        continuation?.resume(
            returning: result ?? .unknown(.staleContext)
        )
    }

    func schedule(
        _ request: SystemScheduleRequest,
        systemIdentifier: String,
        stillCurrent: @escaping @MainActor () -> Bool
    ) async -> Result<SystemScheduleSubmission, SystemSchedulePlatformFailure> {
        scheduleCalls += 1
        onBeforeScheduleWrite?()
        guard stillCurrent() else {
            return .failure(.staleContext)
        }
        scheduleWriteCalls += 1
        onScheduleWrite?()
        let result = scheduleResult ?? .success(
            SystemScheduleSubmission(
                systemIdentifier: systemIdentifier,
                authorization: authorization(for: request.kind)
            )
        )
        if suspendScheduleAfterWrite {
            suspendedScheduleResult = result
            return await withCheckedContinuation { continuation in
                suspendedSchedule = continuation
            }
        }
        return result
    }

    func resumeSchedule() {
        let continuation = suspendedSchedule
        let result = suspendedScheduleResult
        suspendedSchedule = nil
        suspendedScheduleResult = nil
        continuation?.resume(returning: result ?? .failure(.staleContext))
    }

    func cancel(
        kind: SystemScheduleKind,
        systemIdentifier: String,
        stillAuthorized: @escaping @MainActor () -> Bool
    ) async -> Result<SystemScheduleAuthorization, SystemSchedulePlatformFailure> {
        cancelCalls += 1
        cancelSystemIdentifiers.append(systemIdentifier)
        onBeforeCancelWrite?()
        guard stillAuthorized() else {
            return .failure(.staleContext)
        }
        cancelWriteCalls += 1
        return cancelResult
    }

    private func authorization(
        for kind: SystemScheduleKind
    ) -> SystemScheduleAuthorization {
        kind == .alarm ? alarmAuthorization : remindersAuthorization
    }
}

@MainActor
private final class SystemScheduleStoreSpy: SystemScheduleStoring {
    var failOnSaveCalls: Set<Int>
    private(set) var activeOwner: SystemScheduleManagementOwner?
    private var saveCalls = 0
    private var ownerGeneration = 1
    private var records: [StoredSystemSchedule] = []

    init(
        activeOwner: SystemScheduleManagementOwner?,
        failOnSaveCalls: Set<Int> = []
    ) {
        self.activeOwner = activeOwner
        self.failOnSaveCalls = failOnSaveCalls
    }

    var isAvailable: Bool { true }

    func managementOwner(accountID: String) -> SystemScheduleManagementOwner? {
        if activeOwner?.accountID == accountID {
            return activeOwner
        }
        if activeOwner != nil {
            _ = detachActiveOwner()
        }
        ownerGeneration += 1
        let owner = SystemScheduleManagementOwner(
            accountID: accountID,
            ownerEpoch: "owner-epoch-\(ownerGeneration)"
        )
        activeOwner = owner
        return owner
    }

    func isActive(owner: SystemScheduleManagementOwner) -> Bool {
        activeOwner == owner
    }

    func record(
        operationID: String,
        instanceID: String,
        owner: SystemScheduleManagementOwner
    ) -> StoredSystemSchedule? {
        records.first {
            $0.operationID == operationID
                && $0.intent.instanceID == instanceID
                && $0.owner == owner
        }
    }

    func latestRecord(
        operationID: String,
        instanceID: String
    ) -> StoredSystemSchedule? {
        records
            .filter {
                $0.operationID == operationID
                    && $0.intent.instanceID == instanceID
            }
            .max(by: { $0.updatedAt < $1.updatedAt })
    }

    func cleanupRecords() -> [StoredSystemSchedule] {
        records
            .filter {
                [.managedCleanupPending, .detachedCleanupPending]
                    .contains($0.lifecycle)
            }
            .sorted(by: { $0.updatedAt < $1.updatedAt })
    }

    func save(_ record: StoredSystemSchedule) -> Bool {
        saveCalls += 1
        if failOnSaveCalls.contains(saveCalls) {
            return false
        }
        if let existing = records.first(where: {
            $0.operationID == record.operationID
                && $0.intent.instanceID == record.intent.instanceID
                && $0.owner == record.owner
        }), existing.lifecycle.isDetached, !record.lifecycle.isDetached {
            return false
        }
        records.removeAll {
            $0.operationID == record.operationID
                && $0.intent.instanceID == record.intent.instanceID
                && $0.owner == record.owner
        }
        records.append(record)
        return true
    }

    @discardableResult
    func detachActiveOwner() -> Bool {
        guard let activeOwner else { return true }
        return detach(activeOwner)
    }

    @discardableResult
    func detachActiveOwner(unlessAccountID accountID: String) -> Bool {
        guard let activeOwner else { return true }
        guard activeOwner.accountID != accountID else { return true }
        return detach(activeOwner)
    }

    private func detach(_ owner: SystemScheduleManagementOwner) -> Bool {
        activeOwner = nil
        for index in records.indices
            where records[index].owner == owner
        {
            switch records[index].lifecycle {
            case .managedScheduled:
                records[index].lifecycle = .detachedRetained
            case .reserved:
                records[index].lifecycle = .closedNoItem
                records[index].effectMayExist = false
            case .creatingUnknown, .managedCleanupPending:
                records[index].lifecycle = .detachedCleanupPending
                records[index].effectMayExist = true
            case .detachedRetained, .detachedCleanupPending,
                    .closedNoItem, .closedRemoved, .detachedCleaned,
                    .detachedAbsent:
                break
            }
            records[index].generation += 1
            records[index].updatedAt = Date()
        }
        return true
    }
}
