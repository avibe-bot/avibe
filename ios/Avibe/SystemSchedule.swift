import AlarmKit
import CryptoKit
import EventKit
import Foundation
import OSLog
import SwiftUI

enum SystemScheduleDeviceIdentity {
    private static let key = "avibe.systemSchedule.deviceID"

    static func resolve(defaults: UserDefaults = .standard) -> String {
        if let raw = defaults.string(forKey: key),
           let identifier = UUID(uuidString: raw)
        {
            return identifier.uuidString.lowercased()
        }
        let identifier = UUID().uuidString.lowercased()
        defaults.set(identifier, forKey: key)
        return identifier
    }
}

private enum SystemScheduleISO8601 {
    static func date(from value: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fractional.date(from: value) {
            return date
        }
        let ordinary = ISO8601DateFormatter()
        ordinary.formatOptions = [.withInternetDateTime]
        return ordinary.date(from: value)
    }

    static func string(from date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }
}

enum SystemScheduleKind: String, Codable, CaseIterable, Equatable {
    case alarm
    case reminder
}

enum SystemScheduleAuthorization: String, Codable, Equatable {
    case notDetermined = "not_determined"
    case authorized
    case denied
    case restricted
    case unsupported
}

enum SystemScheduleState: String, Codable, Equatable {
    case userDenied = "user_denied"
    case permissionDenied = "permission_denied"
    case unsupported
    case scheduled
    case cancelled
    case failed
    case expired
    case staleContext = "stale_context"
}

struct SystemScheduleRequest: Codable, Equatable {
    static let maximumIdentifierLength = 200
    static let maximumTitleLength = 200
    static let maximumTimeZoneLength = 100

    let requestID: String
    let operationID: String
    let kind: SystemScheduleKind
    let title: String
    let fireAt: Date
    let timeZoneIdentifier: String
    let instanceID: String
    let sessionID: String
    let expiresAt: Date

    static func parse(_ params: [String: Any]) -> SystemScheduleRequest? {
        let expectedKeys: Set<String> = [
            "requestId",
            "operationId",
            "kind",
            "title",
            "fireAt",
            "timeZone",
            "recurrence",
            "instanceId",
            "sessionId",
            "expiresAt",
        ]
        guard
            Set(params.keys) == expectedKeys,
            params["recurrence"] is NSNull,
            let requestID = boundedIdentifier(params["requestId"]),
            let operationID = boundedIdentifier(params["operationId"]),
            let rawKind = params["kind"] as? String,
            let kind = SystemScheduleKind(rawValue: rawKind),
            let rawTitle = params["title"] as? String,
            let fireAtValue = params["fireAt"] as? String,
            let fireAt = SystemScheduleISO8601.date(from: fireAtValue),
            let timeZoneIdentifier = params["timeZone"] as? String,
            !timeZoneIdentifier.isEmpty,
            timeZoneIdentifier.count <= maximumTimeZoneLength,
            TimeZone(identifier: timeZoneIdentifier) != nil,
            let instanceID = boundedIdentifier(params["instanceId"]),
            let sessionID = boundedIdentifier(params["sessionId"]),
            let expiresAtValue = params["expiresAt"] as? String,
            let expiresAt = SystemScheduleISO8601.date(from: expiresAtValue)
        else {
            return nil
        }
        let title = rawTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty, title.count <= maximumTitleLength else {
            return nil
        }
        return SystemScheduleRequest(
            requestID: requestID,
            operationID: operationID,
            kind: kind,
            title: title,
            fireAt: fireAt,
            timeZoneIdentifier: timeZoneIdentifier,
            instanceID: instanceID,
            sessionID: sessionID,
            expiresAt: expiresAt
        )
    }

    func hasSameIntent(as other: SystemScheduleRequest) -> Bool {
        operationID == other.operationID
            && kind == other.kind
            && title == other.title
            && fireAt == other.fireAt
            && timeZoneIdentifier == other.timeZoneIdentifier
            && instanceID == other.instanceID
            && sessionID == other.sessionID
    }

    private static func boundedIdentifier(_ value: Any?) -> String? {
        guard let value = value as? String else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard
            !trimmed.isEmpty,
            trimmed.count <= maximumIdentifierLength,
            !trimmed.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
        else {
            return nil
        }
        return trimmed
    }
}

struct SystemScheduleCancelRequest: Equatable {
    let requestID: String
    let operationID: String
    let kind: SystemScheduleKind
    let purpose: SystemScheduleCancelPurpose

    static func parse(_ params: [String: Any]) -> SystemScheduleCancelRequest? {
        guard
            Set(params.keys) == ["requestId", "operationId", "kind", "purpose"],
            let requestID = boundedIdentifier(params["requestId"]),
            let operationID = boundedIdentifier(params["operationId"]),
            let rawKind = params["kind"] as? String,
            let kind = SystemScheduleKind(rawValue: rawKind),
            let rawPurpose = params["purpose"] as? String,
            let purpose = SystemScheduleCancelPurpose(rawValue: rawPurpose)
        else {
            return nil
        }
        return SystemScheduleCancelRequest(
            requestID: requestID,
            operationID: operationID,
            kind: kind,
            purpose: purpose
        )
    }

    private static func boundedIdentifier(_ value: Any?) -> String? {
        guard let value = value as? String else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard
            !trimmed.isEmpty,
            trimmed.count <= SystemScheduleRequest.maximumIdentifierLength,
            !trimmed.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
        else {
            return nil
        }
        return trimmed
    }
}

struct SystemScheduleConfirmation: Identifiable, Equatable {
    let id: UUID
    let request: SystemScheduleRequest
    let instanceName: String
    let isSubmitting: Bool
}

struct SystemScheduleErrorDetail: Equatable {
    let code: String
    let message: String

    var jsonObject: [String: Any] {
        ["code": code, "message": message]
    }
}

struct SystemScheduleReceipt: Equatable {
    static let contract = 1

    let requestID: String
    let operationID: String
    let kind: SystemScheduleKind
    let state: SystemScheduleState
    let systemIdentifier: String?
    let fireAt: Date?
    let timeZoneIdentifier: String?
    let authorization: SystemScheduleAuthorization
    let error: SystemScheduleErrorDetail?

    var jsonObject: [String: Any] {
        [
            "contract": Self.contract,
            "requestId": requestID,
            "operationId": operationID,
            "kind": kind.rawValue,
            "state": state.rawValue,
            "systemIdentifier": systemIdentifier ?? NSNull(),
            "fireAt": fireAt.map(SystemScheduleISO8601.string(from:)) ?? NSNull(),
            "timeZone": timeZoneIdentifier ?? NSNull(),
            "authorization": authorization.rawValue,
            "error": error?.jsonObject ?? NSNull(),
        ]
    }

    func replacingRequestID(_ requestID: String) -> SystemScheduleReceipt {
        SystemScheduleReceipt(
            requestID: requestID,
            operationID: operationID,
            kind: kind,
            state: state,
            systemIdentifier: systemIdentifier,
            fireAt: fireAt,
            timeZoneIdentifier: timeZoneIdentifier,
            authorization: authorization,
            error: error
        )
    }

    static func terminal(
        request: SystemScheduleRequest,
        state: SystemScheduleState,
        authorization: SystemScheduleAuthorization,
        systemIdentifier: String? = nil,
        code: String? = nil,
        message: String? = nil
    ) -> SystemScheduleReceipt {
        SystemScheduleReceipt(
            requestID: request.requestID,
            operationID: request.operationID,
            kind: request.kind,
            state: state,
            systemIdentifier: systemIdentifier,
            fireAt: request.fireAt,
            timeZoneIdentifier: request.timeZoneIdentifier,
            authorization: authorization,
            error: code.map {
                SystemScheduleErrorDetail(
                    code: $0,
                    message: message ?? "The system schedule request failed."
                )
            }
        )
    }
}

struct SystemScheduleCapabilities: Equatable {
    let iosVersion: String
    let alarmKitAvailable: Bool
    let alarmAuthorization: SystemScheduleAuthorization
    let remindersAvailable: Bool
    let remindersAuthorization: SystemScheduleAuthorization
    let supportedKinds: [SystemScheduleKind]

    var jsonObject: [String: Any] {
        [
            "contract": SystemScheduleReceipt.contract,
            "iosVersion": iosVersion,
            "alarmKit": [
                "available": alarmKitAvailable,
                "authorization": alarmAuthorization.rawValue,
            ],
            "reminders": [
                "available": remindersAvailable,
                "authorization": remindersAuthorization.rawValue,
            ],
            "supportedKinds": supportedKinds.map(\.rawValue),
        ]
    }
}

struct SystemScheduleSubmission: Equatable {
    let systemIdentifier: String
    let authorization: SystemScheduleAuthorization
}

struct SystemScheduleManagementOwner: Codable, Equatable {
    let accountID: String
    let ownerEpoch: String
}

struct SystemScheduleContext: Equatable {
    let owner: SystemScheduleManagementOwner
    let instanceID: String
    let origin: String
    let runtimeIdentity: Int
    let navigationEpoch: Int

    var accountID: String { owner.accountID }

    func hasSameOwner(as other: SystemScheduleContext) -> Bool {
        owner == other.owner && instanceID == other.instanceID
    }
}

struct StoredSystemScheduleError: Codable, Equatable {
    let code: String
    let message: String
}

enum SystemSchedulePlatformFailure: Error, Equatable {
    case permissionDenied(SystemScheduleAuthorization)
    case unsupported
    case staleContext
    case failed(
        code: String,
        message: String,
        systemIdentifier: String? = nil,
        effectMayExist: Bool = false
    )
}

enum SystemScheduleExistence: Equatable {
    case exists(SystemScheduleAuthorization)
    case absentConclusive(SystemScheduleAuthorization)
    case unknown(SystemSchedulePlatformFailure)
}

@MainActor
protocol SystemSchedulePlatforming: AnyObject {
    func capabilities() -> SystemScheduleCapabilities
    func systemIdentifier(
        for request: SystemScheduleRequest,
        owner: SystemScheduleManagementOwner
    ) -> String
    func existence(
        kind: SystemScheduleKind,
        systemIdentifier: String
    ) async -> SystemScheduleExistence
    func schedule(
        _ request: SystemScheduleRequest,
        systemIdentifier: String,
        stillCurrent: @escaping @MainActor () -> Bool
    ) async -> Result<SystemScheduleSubmission, SystemSchedulePlatformFailure>
    func cancel(
        kind: SystemScheduleKind,
        systemIdentifier: String,
        stillAuthorized: @escaping @MainActor () -> Bool
    ) async -> Result<SystemScheduleAuthorization, SystemSchedulePlatformFailure>
}

struct StoredSystemScheduleIntent: Codable, Equatable {
    let kind: SystemScheduleKind
    let title: String
    let fireAt: Date
    let timeZoneIdentifier: String
    let instanceID: String
    let sessionID: String

    init(request: SystemScheduleRequest) {
        kind = request.kind
        title = request.title
        fireAt = request.fireAt
        timeZoneIdentifier = request.timeZoneIdentifier
        instanceID = request.instanceID
        sessionID = request.sessionID
    }
}

struct StoredSystemSchedule: Codable, Equatable {
    enum Lifecycle: String, Codable, Equatable {
        case reserved
        case creatingUnknown
        case managedScheduled
        case managedCleanupPending
        case detachedRetained
        case detachedCleanupPending
        case closedNoItem
        case closedRemoved
        case detachedCleaned
        case detachedAbsent

        var isDetached: Bool {
            switch self {
            case .detachedRetained, .detachedCleanupPending,
                    .detachedCleaned, .detachedAbsent:
                return true
            default:
                return false
            }
        }
    }

    let operationID: String
    let owner: SystemScheduleManagementOwner
    let intent: StoredSystemScheduleIntent
    let systemIdentifier: String
    var authorization: SystemScheduleAuthorization
    var lifecycle: Lifecycle
    var effectMayExist: Bool
    var generation: Int
    var createState: SystemScheduleState?
    var lastError: StoredSystemScheduleError?
    var updatedAt: Date

    func createReceipt(
        requestID: String,
        authorization currentAuthorization: SystemScheduleAuthorization? = nil
    ) -> SystemScheduleReceipt? {
        let state = createState ?? (lifecycle == .managedScheduled ? .scheduled : nil)
        guard let state else { return nil }
        return SystemScheduleReceipt(
            requestID: requestID,
            operationID: operationID,
            kind: intent.kind,
            state: state,
            systemIdentifier: effectMayExist || state == .scheduled
                ? systemIdentifier
                : nil,
            fireAt: intent.fireAt,
            timeZoneIdentifier: intent.timeZoneIdentifier,
            authorization: currentAuthorization ?? authorization,
            error: lastError.map {
                SystemScheduleErrorDetail(code: $0.code, message: $0.message)
            }
        )
    }

    func cancelReceipt(
        requestID: String,
        authorization currentAuthorization: SystemScheduleAuthorization? = nil
    ) -> SystemScheduleReceipt {
        let removed = [
            Lifecycle.closedNoItem,
            .closedRemoved,
            .detachedCleaned,
            .detachedAbsent,
        ].contains(lifecycle)
        return SystemScheduleReceipt(
            requestID: requestID,
            operationID: operationID,
            kind: intent.kind,
            state: removed ? .cancelled : .failed,
            systemIdentifier: effectMayExist ? systemIdentifier : nil,
            fireAt: intent.fireAt,
            timeZoneIdentifier: intent.timeZoneIdentifier,
            authorization: currentAuthorization ?? authorization,
            error: removed ? nil : lastError.map {
                SystemScheduleErrorDetail(code: $0.code, message: $0.message)
            }
        )
    }
}

@MainActor
protocol SystemScheduleStoring: AnyObject {
    var isAvailable: Bool { get }
    func managementOwner(accountID: String) -> SystemScheduleManagementOwner?
    func isActive(owner: SystemScheduleManagementOwner) -> Bool
    func record(
        operationID: String,
        instanceID: String,
        owner: SystemScheduleManagementOwner
    ) -> StoredSystemSchedule?
    func latestRecord(
        operationID: String,
        instanceID: String
    ) -> StoredSystemSchedule?
    func compensationRecord(
        operationID: String,
        instanceID: String,
        kind: SystemScheduleKind
    ) -> StoredSystemSchedule?
    @discardableResult
    func save(_ record: StoredSystemSchedule) -> Bool
    func detach(accountID: String)
    func detachActiveOwner()
}

@MainActor
final class UserDefaultsSystemScheduleStore: SystemScheduleStoring {
    private struct Envelope: Codable, Equatable {
        let version: Int
        var activeOwner: SystemScheduleManagementOwner?
        var records: [StoredSystemSchedule]
    }

    private static let key = "avibe.systemSchedule.records"
    private static let version = 1
    private static let logger = Logger(
        subsystem: "bot.avibe.ios",
        category: "system-schedule-store"
    )

    private let defaults: UserDefaults
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    var isAvailable: Bool {
        if case .success = loadEnvelope() { return true }
        return false
    }

    func managementOwner(accountID: String) -> SystemScheduleManagementOwner? {
        guard case .success(var envelope) = loadEnvelope() else { return nil }
        if envelope.activeOwner?.accountID == accountID {
            return envelope.activeOwner
        }
        if let active = envelope.activeOwner {
            detach(active.accountID, in: &envelope)
        }
        let owner = SystemScheduleManagementOwner(
            accountID: accountID,
            ownerEpoch: UUID().uuidString.lowercased()
        )
        envelope.activeOwner = owner
        return persist(envelope) ? owner : nil
    }

    func isActive(owner: SystemScheduleManagementOwner) -> Bool {
        guard case .success(let envelope) = loadEnvelope() else { return false }
        return envelope.activeOwner == owner
    }

    func record(
        operationID: String,
        instanceID: String,
        owner: SystemScheduleManagementOwner
    ) -> StoredSystemSchedule? {
        guard case .success(let envelope) = loadEnvelope() else { return nil }
        return envelope.records.first {
            $0.operationID == operationID
                && $0.intent.instanceID == instanceID
                && $0.owner == owner
        }
    }

    func latestRecord(
        operationID: String,
        instanceID: String
    ) -> StoredSystemSchedule? {
        guard case .success(let envelope) = loadEnvelope() else { return nil }
        return envelope.records
            .filter {
                $0.operationID == operationID
                    && $0.intent.instanceID == instanceID
            }
            .max(by: { $0.updatedAt < $1.updatedAt })
    }

    func compensationRecord(
        operationID: String,
        instanceID: String,
        kind: SystemScheduleKind
    ) -> StoredSystemSchedule? {
        guard case .success(let envelope) = loadEnvelope() else { return nil }
        let candidates = envelope.records.filter {
            $0.operationID == operationID
                && $0.intent.instanceID == instanceID
                && $0.intent.kind == kind
                && [
                    .managedCleanupPending,
                    .detachedCleanupPending,
                    .closedNoItem,
                    .closedRemoved,
                    .detachedCleaned,
                    .detachedAbsent,
                ].contains($0.lifecycle)
        }
        let pending = candidates.filter {
            [.managedCleanupPending, .detachedCleanupPending]
                .contains($0.lifecycle)
        }
        return (pending.isEmpty ? candidates : pending)
            .max(by: { $0.updatedAt < $1.updatedAt })
    }

    @discardableResult
    func save(_ record: StoredSystemSchedule) -> Bool {
        guard case .success(var envelope) = loadEnvelope() else { return false }
        if let existing = envelope.records.first(where: {
            $0.operationID == record.operationID
                && $0.intent.instanceID == record.intent.instanceID
                && $0.owner == record.owner
        }), existing.lifecycle.isDetached, !record.lifecycle.isDetached {
            return false
        }
        envelope.records.removeAll {
            $0.operationID == record.operationID
                && $0.intent.instanceID == record.intent.instanceID
                && $0.owner == record.owner
        }
        envelope.records.append(record)
        return persist(envelope)
    }

    func detach(accountID: String) {
        guard case .success(var envelope) = loadEnvelope() else { return }
        let original = envelope
        detach(accountID, in: &envelope)
        guard envelope != original else { return }
        _ = persist(envelope)
    }

    func detachActiveOwner() {
        guard case .success(let envelope) = loadEnvelope(),
              let activeOwner = envelope.activeOwner
        else { return }
        detach(accountID: activeOwner.accountID)
    }

    private func detach(_ accountID: String, in envelope: inout Envelope) {
        if envelope.activeOwner?.accountID == accountID {
            envelope.activeOwner = nil
        }
        let now = Date()
        for index in envelope.records.indices
            where envelope.records[index].owner.accountID == accountID
        {
            switch envelope.records[index].lifecycle {
            case .managedScheduled:
                envelope.records[index].lifecycle = .detachedRetained
            case .reserved:
                envelope.records[index].lifecycle = .closedNoItem
                envelope.records[index].effectMayExist = false
            case .creatingUnknown, .managedCleanupPending:
                envelope.records[index].lifecycle = .detachedCleanupPending
                envelope.records[index].effectMayExist = true
            case .detachedRetained, .detachedCleanupPending,
                    .closedNoItem, .closedRemoved, .detachedCleaned,
                    .detachedAbsent:
                break
            }
            envelope.records[index].generation += 1
            envelope.records[index].updatedAt = now
        }
    }

    private enum LoadFailure: Error {
        case unsupportedVersion(Int)
        case invalidData(String)
    }

    private func loadEnvelope() -> Result<Envelope, LoadFailure> {
        guard let data = defaults.data(forKey: Self.key) else {
            return .success(
                Envelope(version: Self.version, activeOwner: nil, records: [])
            )
        }
        do {
            let envelope = try decoder.decode(Envelope.self, from: data)
            guard envelope.version == Self.version else {
                Self.logger.error(
                    "Unsupported system schedule store version: \(envelope.version, privacy: .public)"
                )
                return .failure(.unsupportedVersion(envelope.version))
            }
            return .success(envelope)
        } catch {
            let description = String(describing: error)
            Self.logger.error(
                "System schedule store could not be decoded: \(description, privacy: .public)"
            )
            return .failure(.invalidData(description))
        }
    }

    private func persist(_ envelope: Envelope) -> Bool {
        do {
            defaults.set(try encoder.encode(envelope), forKey: Self.key)
            guard case .success(let persisted) = loadEnvelope() else {
                return false
            }
            return persisted == envelope
        } catch {
            Self.logger.error(
                "System schedule store could not be encoded: \(String(describing: error), privacy: .public)"
            )
            return false
        }
    }
}

enum SystemScheduleFenceFailure: Equatable {
    case cancelled
    case expired
    case fireTimeInPast
    case staleContext
}

enum SystemScheduleCancelPurpose: String, Equatable {
    case user
    case compensation
}

@MainActor
final class SystemScheduleCoordinator {
    private let platform: SystemSchedulePlatforming
    private let store: SystemScheduleStoring
    private let now: () -> Date

    init(
        platform: SystemSchedulePlatforming,
        store: SystemScheduleStoring,
        now: @escaping () -> Date = Date.init
    ) {
        self.platform = platform
        self.store = store
        self.now = now
    }

    func capabilities() -> SystemScheduleCapabilities {
        platform.capabilities()
    }

    func managementOwner(accountID: String) -> SystemScheduleManagementOwner? {
        store.managementOwner(accountID: accountID)
    }

    func requiresRecovery(
        _ request: SystemScheduleRequest,
        context: SystemScheduleContext
    ) -> Bool {
        guard let record = store.record(
            operationID: request.operationID,
            instanceID: request.instanceID,
            owner: context.owner
        ) else {
            return false
        }
        return record.intent == StoredSystemScheduleIntent(request: request)
            && record.lifecycle == .creatingUnknown
    }

    func prepare(
        _ request: SystemScheduleRequest,
        context: SystemScheduleContext
    ) -> SystemScheduleReceipt? {
        guard store.isAvailable, store.isActive(owner: context.owner) else {
            return localStateUnavailable(request)
        }
        let intent = StoredSystemScheduleIntent(request: request)
        if let existing = store.record(
            operationID: request.operationID,
            instanceID: request.instanceID,
            owner: context.owner
        ) {
            guard existing.intent == intent else {
                return terminal(
                    request,
                    state: .failed,
                    code: "operation_conflict",
                    message: "This operation ID already belongs to a different system schedule."
                )
            }
            return existing.createReceipt(
                requestID: request.requestID,
                authorization: authorization(for: request.kind)
            )
        }
        if var historical = store.latestRecord(
            operationID: request.operationID,
            instanceID: request.instanceID
        ) {
            guard historical.intent == intent else {
                return terminal(
                    request,
                    state: .failed,
                    code: "operation_conflict",
                    message: "This operation ID already belongs to a different system schedule."
                )
            }
            if let receipt = historical.createReceipt(
                requestID: request.requestID,
                authorization: authorization(for: request.kind)
            ) {
                return receipt
            }
            historical.createState = .staleContext
            historical.lastError = error(
                code: code(for: .staleContext),
                message: message(for: .staleContext)
            )
            historical.generation += 1
            historical.updatedAt = now()
            guard store.save(historical) else {
                return localStateUnavailable(request)
            }
            return historical.createReceipt(requestID: request.requestID)!
        }
        let record = StoredSystemSchedule(
            operationID: request.operationID,
            owner: context.owner,
            intent: intent,
            systemIdentifier: platform.systemIdentifier(
                for: request,
                owner: context.owner
            ),
            authorization: authorization(for: request.kind),
            lifecycle: .reserved,
            effectMayExist: false,
            generation: 1,
            createState: nil,
            lastError: nil,
            updatedAt: now()
        )
        guard store.save(record) else { return localStateUnavailable(request) }
        return nil
    }

    func finishWithoutEffect(
        _ request: SystemScheduleRequest,
        context: SystemScheduleContext,
        state: SystemScheduleState,
        code: String?,
        message: String?
    ) -> SystemScheduleReceipt {
        guard var record = store.record(
            operationID: request.operationID,
            instanceID: request.instanceID,
            owner: context.owner
        ), record.intent == StoredSystemScheduleIntent(request: request)
        else {
            return terminal(request, state: state, code: code, message: message)
        }
        guard record.lifecycle == .reserved else {
            return record.createReceipt(requestID: request.requestID)
                ?? terminal(request, state: state, code: code, message: message)
        }
        record.lifecycle = .closedNoItem
        record.effectMayExist = false
        record.createState = state
        record.lastError = error(code: code, message: message)
        record.generation += 1
        record.updatedAt = now()
        guard store.save(record) else { return localStateUnavailable(request) }
        return record.createReceipt(requestID: request.requestID)!
    }

    func schedule(
        _ request: SystemScheduleRequest,
        context: SystemScheduleContext,
        creationFence: @escaping @MainActor () -> SystemScheduleFenceFailure?,
        postWriteFence: @escaping @MainActor () -> SystemScheduleFenceFailure?
    ) async -> SystemScheduleReceipt {
        if let prepared = prepare(request, context: context) {
            return prepared
        }
        guard var record = store.record(
            operationID: request.operationID,
            instanceID: request.instanceID,
            owner: context.owner
        ) else { return localStateUnavailable(request) }

        if record.lifecycle == .creatingUnknown {
            let recovery = await platform.existence(
                kind: record.intent.kind,
                systemIdentifier: record.systemIdentifier
            )
            guard let current = store.record(
                operationID: record.operationID,
                instanceID: record.intent.instanceID,
                owner: record.owner
            ) else {
                return localStateUnavailable(request)
            }
            if current.generation != record.generation {
                return await reconcileRecoveryAfterRecordChange(
                    current,
                    recovery: recovery,
                    request: request,
                    context: context
                )
            }
            record = current
            switch recovery {
            case .exists(let authorization):
                if let failure = creationFence() {
                    return await settleStaleCreatedItem(
                        &record,
                        request: request,
                        context: context,
                        failure: failure
                    )
                }
                record.authorization = authorization
                record.lifecycle = .managedScheduled
                record.effectMayExist = true
                record.createState = .scheduled
                record.lastError = nil
                record.generation += 1
                record.updatedAt = now()
                guard store.save(record) else {
                    return writeFailure(request, identifier: record.systemIdentifier)
                }
                return record.createReceipt(requestID: request.requestID)!
            case .absentConclusive:
                record.lifecycle = .reserved
                record.effectMayExist = false
                record.generation += 1
                record.updatedAt = now()
                guard store.save(record) else { return localStateUnavailable(request) }
            case .unknown(let failure):
                record.authorization = observedAuthorization(
                    for: failure,
                    fallback: record.authorization
                )
                record.lastError = storedError(failure)
                record.updatedAt = now()
                _ = store.save(record)
                return receipt(for: failure, request: request, fallback: record.systemIdentifier)
            }
        }

        if let failure = creationFence() {
            return finishWithoutEffect(
                request,
                context: context,
                state: state(for: failure),
                code: code(for: failure),
                message: message(for: failure)
            )
        }
        record.lifecycle = .creatingUnknown
        record.effectMayExist = true
        record.generation += 1
        record.updatedAt = now()
        guard store.save(record),
              let persisted = store.record(
                  operationID: record.operationID,
                  instanceID: record.intent.instanceID,
                  owner: record.owner
              ), persisted == record
        else { return localStateUnavailable(request) }
        let enteredGeneration = record.generation

        let result = await platform.schedule(
            request,
            systemIdentifier: record.systemIdentifier,
            stillCurrent: { creationFence() == nil }
        )
        guard var latest = store.record(
            operationID: record.operationID,
            instanceID: record.intent.instanceID,
            owner: record.owner
        ) else { return localStateUnavailable(request) }
        if latest.lifecycle == .detachedCleanupPending,
           latest.generation != enteredGeneration
        {
            switch result {
            case .success(let submission):
                guard submission.systemIdentifier == record.systemIdentifier else {
                    latest.createState = .failed
                    latest.lastError = StoredSystemScheduleError(
                        code: "platform_identity_mismatch",
                        message: "The system returned an unexpected item identity."
                    )
                    latest.updatedAt = now()
                    _ = store.save(latest)
                    return latest.createReceipt(requestID: request.requestID)!
                }
                return await settleStaleCreatedItem(
                    &latest,
                    request: request,
                    context: context,
                    failure: .staleContext
                )
            case .failure(let failure) where failure.effectMayExist:
                return await settleStaleCreatedItem(
                    &latest,
                    request: request,
                    context: context,
                    failure: .staleContext
                )
            case .failure:
                latest.lifecycle = .detachedAbsent
                latest.effectMayExist = false
                latest.createState = .staleContext
                latest.lastError = error(
                    code: code(for: .staleContext),
                    message: message(for: .staleContext)
                )
                latest.generation += 1
                latest.updatedAt = now()
                guard store.save(latest) else {
                    return writeFailure(
                        request,
                        identifier: latest.systemIdentifier
                    )
                }
                return latest.createReceipt(requestID: request.requestID)!
            }
        }
        if latest.generation != enteredGeneration
            || latest.lifecycle == .managedCleanupPending
        {
            if latest.createState == nil { latest.createState = .cancelled }
            return latest.createReceipt(requestID: request.requestID)
                ?? terminal(request, state: .cancelled, code: nil, message: nil)
        }
        switch result {
        case .success(let submission):
            guard submission.systemIdentifier == record.systemIdentifier else {
                latest.lifecycle = store.isActive(owner: latest.owner)
                    ? .managedCleanupPending
                    : .detachedCleanupPending
                latest.effectMayExist = true
                latest.createState = .failed
                latest.lastError = StoredSystemScheduleError(
                    code: "platform_identity_mismatch",
                    message: "The system returned an unexpected item identity."
                )
                latest.generation += 1
                latest.updatedAt = now()
                _ = store.save(latest)
                return latest.createReceipt(requestID: request.requestID)!
            }
            if let failure = postWriteFence() {
                return await settleStaleCreatedItem(
                    &latest,
                    request: request,
                    context: context,
                    failure: failure
                )
            }
            latest.authorization = submission.authorization
            latest.lifecycle = .managedScheduled
            latest.effectMayExist = true
            latest.createState = .scheduled
            latest.lastError = nil
            latest.generation += 1
            latest.updatedAt = now()
            guard store.save(latest) else {
                return writeFailure(request, identifier: latest.systemIdentifier)
            }
            return latest.createReceipt(requestID: request.requestID)!
        case .failure(let failure):
            latest.authorization = observedAuthorization(
                for: failure,
                fallback: latest.authorization
            )
            let effectMayExist = failure.effectMayExist
            if effectMayExist {
                latest.lifecycle = store.isActive(owner: latest.owner)
                    ? .managedCleanupPending
                    : .detachedCleanupPending
                latest.effectMayExist = true
            } else {
                latest.lifecycle = .closedNoItem
                latest.effectMayExist = false
            }
            let fenceFailure = effectMayExist
                ? postWriteFence()
                : creationFence()
            latest.createState = fenceFailure.map(state(for:))
                ?? state(for: failure)
            latest.lastError = fenceFailure.map {
                error(code: code(for: $0), message: message(for: $0))
            } ?? storedError(failure)
            latest.generation += 1
            latest.updatedAt = now()
            _ = store.save(latest)
            return latest.createReceipt(requestID: request.requestID)
                ?? receipt(for: failure, request: request, fallback: latest.systemIdentifier)
        }
    }

    func cancel(
        _ request: SystemScheduleCancelRequest,
        context: SystemScheduleContext,
        stillCurrent: @escaping @MainActor () -> Bool
    ) async -> SystemScheduleReceipt {
        guard store.isAvailable else {
            return cancelFailure(request, code: "local_state_unavailable")
        }
        var record: StoredSystemSchedule?
        switch request.purpose {
        case .user:
            guard store.isActive(owner: context.owner) else {
                return cancelFailure(request, code: "item_not_found")
            }
            record = store.record(
                operationID: request.operationID,
                instanceID: context.instanceID,
                owner: context.owner
            )
        case .compensation:
            record = store.compensationRecord(
                operationID: request.operationID,
                instanceID: context.instanceID,
                kind: request.kind
            )
        }
        guard var record, record.intent.kind == request.kind else {
            return cancelFailure(request, code: "item_not_found")
        }
        if [.closedNoItem, .closedRemoved, .detachedCleaned, .detachedAbsent]
            .contains(record.lifecycle)
        {
            return record.cancelReceipt(
                requestID: request.requestID,
                authorization: authorization(for: request.kind)
            )
        }
        if record.lifecycle == .detachedRetained {
            return cancelFailure(request, code: "item_not_managed")
        }
        if record.lifecycle == .reserved {
            record.lifecycle = .closedNoItem
            record.effectMayExist = false
            record.createState = record.createState ?? .cancelled
            record.lastError = nil
            record.generation += 1
            record.updatedAt = now()
            guard store.save(record) else {
                return cancelFailure(request, code: "local_state_write_failed")
            }
            return record.cancelReceipt(requestID: request.requestID)
        }
        if request.purpose == .user { record.createState = record.createState ?? .cancelled }
        record.lifecycle = record.lifecycle.isDetached
            ? .detachedCleanupPending
            : .managedCleanupPending
        record.effectMayExist = true
        record.generation += 1
        record.updatedAt = now()
        guard store.save(record) else {
            return cancelFailure(request, code: "local_state_write_failed")
        }
        let expectedGeneration = record.generation
        let result = await platform.cancel(
            kind: record.intent.kind,
            systemIdentifier: record.systemIdentifier,
            stillAuthorized: {
                guard stillCurrent(),
                      let current = self.store.record(
                          operationID: record.operationID,
                          instanceID: record.intent.instanceID,
                          owner: record.owner
                      )
                else { return false }
                return current.generation == expectedGeneration
                    && [.managedCleanupPending, .detachedCleanupPending]
                        .contains(current.lifecycle)
            }
        )
        guard var current = store.record(
            operationID: record.operationID,
            instanceID: record.intent.instanceID,
            owner: record.owner
        ), current.generation == expectedGeneration
        else {
            return cancelFailure(request, code: "stale_context")
        }
        switch result {
        case .success(let authorization):
            current.authorization = authorization
            current.lifecycle = current.lifecycle == .detachedCleanupPending
                ? .detachedCleaned
                : .closedRemoved
            current.effectMayExist = false
            current.lastError = nil
            current.generation += 1
            current.updatedAt = now()
            guard store.save(current) else {
                return cancelFailure(
                    request,
                    code: "local_state_write_failed",
                    record: current
                )
            }
            return current.cancelReceipt(requestID: request.requestID)
        case .failure(let failure):
            current.authorization = observedAuthorization(
                for: failure,
                fallback: current.authorization
            )
            current.lastError = storedError(failure)
            current.updatedAt = now()
            _ = store.save(current)
            return receipt(
                for: failure,
                request: current.request(requestID: request.requestID),
                fallback: current.systemIdentifier
            )
        }
    }

    func pendingCancellationReceipt(
        _ request: SystemScheduleCancelRequest,
        context: SystemScheduleContext
    ) -> SystemScheduleReceipt {
        guard store.isAvailable else {
            return cancelFailure(request, code: "local_state_unavailable")
        }
        guard let record = store.record(
            operationID: request.operationID,
            instanceID: context.instanceID,
            owner: context.owner
        ), record.intent.kind == request.kind
        else {
            return cancelFailure(request, code: "item_not_found")
        }
        return record.cancelReceipt(requestID: request.requestID)
    }

    func detach(accountID: String) {
        store.detach(accountID: accountID)
    }

    func synchronizeAccount(accountID: String?) {
        if let accountID {
            _ = store.managementOwner(accountID: accountID)
        } else {
            store.detachActiveOwner()
        }
    }

    private func reconcileRecoveryAfterRecordChange(
        _ current: StoredSystemSchedule,
        recovery: SystemScheduleExistence,
        request: SystemScheduleRequest,
        context: SystemScheduleContext
    ) async -> SystemScheduleReceipt {
        guard current.intent == StoredSystemScheduleIntent(request: request) else {
            return terminal(
                request,
                state: .failed,
                code: "operation_conflict",
                message: "This operation ID already belongs to a different system schedule."
            )
        }
        if let receipt = current.createReceipt(requestID: request.requestID) {
            return receipt
        }
        guard current.lifecycle == .detachedCleanupPending else {
            return SystemScheduleReceipt.terminal(
                request: request,
                state: .staleContext,
                authorization: current.authorization,
                systemIdentifier: current.effectMayExist
                    ? current.systemIdentifier
                    : nil,
                code: "stale_context",
                message: "The account lifecycle changed while the system item was checked."
            )
        }

        var record = current
        switch recovery {
        case .exists(let authorization):
            record.authorization = authorization
            return await settleStaleCreatedItem(
                &record,
                request: request,
                context: context,
                failure: .staleContext
            )
        case .absentConclusive(let authorization):
            record.authorization = authorization
            record.lifecycle = .detachedAbsent
            record.effectMayExist = false
            record.createState = .staleContext
            record.lastError = error(
                code: code(for: .staleContext),
                message: message(for: .staleContext)
            )
        case .unknown(let failure):
            record.authorization = observedAuthorization(
                for: failure,
                fallback: record.authorization
            )
            record.effectMayExist = true
            record.createState = .staleContext
            record.lastError = storedError(failure)
        }
        record.generation += 1
        record.updatedAt = now()
        guard store.save(record) else {
            return writeFailure(request, identifier: record.systemIdentifier)
        }
        return record.createReceipt(requestID: request.requestID)!
    }

    private func settleStaleCreatedItem(
        _ record: inout StoredSystemSchedule,
        request: SystemScheduleRequest,
        context: SystemScheduleContext,
        failure: SystemScheduleFenceFailure
    ) async -> SystemScheduleReceipt {
        record.lifecycle = store.isActive(owner: record.owner)
            ? .managedCleanupPending
            : .detachedCleanupPending
        record.effectMayExist = true
        record.createState = state(for: failure)
        record.lastError = error(code: code(for: failure), message: message(for: failure))
        record.generation += 1
        record.updatedAt = now()
        guard store.save(record) else {
            return writeFailure(request, identifier: record.systemIdentifier)
        }
        let cleanupOwner = record.owner
        let cleanupOperationID = record.operationID
        _ = await cancel(
            SystemScheduleCancelRequest(
                requestID: request.requestID,
                operationID: request.operationID,
                kind: request.kind,
                purpose: .compensation
            ),
            context: context,
            stillCurrent: { true }
        )
        return store.record(
            operationID: cleanupOperationID,
            instanceID: record.intent.instanceID,
            owner: cleanupOwner
        )?
            .createReceipt(requestID: request.requestID)
            ?? terminal(
                request,
                state: state(for: failure),
                code: code(for: failure),
                message: message(for: failure)
            )
    }

    private func authorization(
        for kind: SystemScheduleKind
    ) -> SystemScheduleAuthorization {
        let capabilities = platform.capabilities()
        return kind == .alarm
            ? capabilities.alarmAuthorization
            : capabilities.remindersAuthorization
    }

    func localStateUnavailable(
        _ request: SystemScheduleRequest
    ) -> SystemScheduleReceipt {
        terminal(
            request,
            state: .failed,
            code: "local_state_unavailable",
            message: "Avibe can't read its saved alarm and reminder records on this iPhone. Update Avibe, then try again."
        )
    }

    func localStateUnavailable(
        _ request: SystemScheduleCancelRequest
    ) -> SystemScheduleReceipt {
        cancelFailure(request, code: "local_state_unavailable")
    }

    private func writeFailure(
        _ request: SystemScheduleRequest,
        identifier: String
    ) -> SystemScheduleReceipt {
        SystemScheduleReceipt.terminal(
            request: request,
            state: .failed,
            authorization: authorization(for: request.kind),
            systemIdentifier: identifier,
            code: "local_state_write_failed",
            message: "The system item changed, but Avibe could not finish saving its management record."
        )
    }

    private func terminal(
        _ request: SystemScheduleRequest,
        state: SystemScheduleState,
        code: String?,
        message: String?
    ) -> SystemScheduleReceipt {
        SystemScheduleReceipt.terminal(
            request: request,
            state: state,
            authorization: authorization(for: request.kind),
            code: code,
            message: message
        )
    }

    private func cancelFailure(
        _ request: SystemScheduleCancelRequest,
        code: String,
        record: StoredSystemSchedule? = nil
    ) -> SystemScheduleReceipt {
        SystemScheduleReceipt(
            requestID: request.requestID,
            operationID: request.operationID,
            kind: request.kind,
            state: code == "stale_context" ? .staleContext : .failed,
            systemIdentifier: record?.effectMayExist == true
                ? record?.systemIdentifier
                : nil,
            fireAt: record?.intent.fireAt,
            timeZoneIdentifier: record?.intent.timeZoneIdentifier,
            authorization: authorization(for: request.kind),
            error: SystemScheduleErrorDetail(
                code: code,
                message: cancelFailureMessage(code: code)
            )
        )
    }

    private func cancelFailureMessage(code: String) -> String {
        switch code {
        case "item_not_found":
            return "This iPhone no longer manages that item, for example after signing out. It stays in the Clock or Reminders app."
        case "local_state_unavailable":
            return "Avibe can't read its saved alarm and reminder records on this iPhone. Update Avibe, then try again."
        default:
            return "Avibe couldn't confirm the item was removed. Check the Clock or Reminders app, or try cancelling again."
        }
    }

    private func receipt(
        for failure: SystemSchedulePlatformFailure,
        request: SystemScheduleRequest,
        fallback: String? = nil
    ) -> SystemScheduleReceipt {
        switch failure {
        case .permissionDenied(let authorization):
            return SystemScheduleReceipt.terminal(
                request: request,
                state: .permissionDenied,
                authorization: authorization,
                systemIdentifier: fallback,
                code: "permission_denied",
                message: "Avibe doesn't have permission. Allow it in iPhone Settings, then try again."
            )
        case .unsupported:
            return SystemScheduleReceipt.terminal(
                request: request,
                state: .unsupported,
                authorization: .unsupported,
                systemIdentifier: fallback,
                code: "unsupported",
                message: "This system schedule is not available on this iPhone."
            )
        case .staleContext:
            return terminal(
                request,
                state: .staleContext,
                code: "stale_context",
                message: "The account, instance, or Runtime changed before the system operation."
            )
        case .failed(let code, let message, let identifier, _):
            return SystemScheduleReceipt.terminal(
                request: request,
                state: .failed,
                authorization: authorization(for: request.kind),
                systemIdentifier: identifier ?? fallback,
                code: code,
                message: message
            )
        }
    }

    private func observedAuthorization(
        for failure: SystemSchedulePlatformFailure,
        fallback: SystemScheduleAuthorization
    ) -> SystemScheduleAuthorization {
        switch failure {
        case .permissionDenied(let authorization): return authorization
        case .unsupported: return .unsupported
        case .staleContext, .failed: return fallback
        }
    }

    private func storedError(
        _ failure: SystemSchedulePlatformFailure
    ) -> StoredSystemScheduleError {
        switch failure {
        case .permissionDenied:
            return StoredSystemScheduleError(
                code: "permission_denied",
                message: "Avibe doesn't have permission. Allow it in iPhone Settings, then try again."
            )
        case .unsupported:
            return StoredSystemScheduleError(
                code: "unsupported",
                message: "This system schedule is not available on this iPhone."
            )
        case .staleContext:
            return StoredSystemScheduleError(
                code: "stale_context",
                message: "The request context changed."
            )
        case .failed(let code, let message, _, _):
            return StoredSystemScheduleError(code: code, message: message)
        }
    }

    private func state(
        for failure: SystemSchedulePlatformFailure
    ) -> SystemScheduleState {
        switch failure {
        case .permissionDenied: return .permissionDenied
        case .unsupported: return .unsupported
        case .staleContext: return .staleContext
        case .failed: return .failed
        }
    }

    private func state(for failure: SystemScheduleFenceFailure) -> SystemScheduleState {
        switch failure {
        case .cancelled: return .cancelled
        case .expired: return .expired
        case .fireTimeInPast: return .failed
        case .staleContext: return .staleContext
        }
    }

    private func code(for failure: SystemScheduleFenceFailure) -> String {
        switch failure {
        case .cancelled: return "cancelled"
        case .expired: return "expired"
        case .fireTimeInPast: return "fire_time_in_past"
        case .staleContext: return "stale_context"
        }
    }

    private func message(for failure: SystemScheduleFenceFailure) -> String {
        switch failure {
        case .cancelled: return "The system schedule was cancelled."
        case .expired: return "The system schedule request expired before it was created."
        case .fireTimeInPast: return "The requested time has already passed."
        case .staleContext:
            return "The account, instance, document, or Runtime changed before the system schedule was created."
        }
    }

    private func error(code: String?, message: String?) -> StoredSystemScheduleError? {
        guard let code, let message, code != "cancelled" else { return nil }
        return StoredSystemScheduleError(code: code, message: message)
    }
}

private extension SystemSchedulePlatformFailure {
    var effectMayExist: Bool {
        if case .failed(_, _, _, let effectMayExist) = self {
            return effectMayExist
        }
        return false
    }
}

private extension StoredSystemSchedule {
    func request(requestID: String) -> SystemScheduleRequest {
        SystemScheduleRequest(
            requestID: requestID,
            operationID: operationID,
            kind: intent.kind,
            title: intent.title,
            fireAt: intent.fireAt,
            timeZoneIdentifier: intent.timeZoneIdentifier,
            instanceID: intent.instanceID,
            sessionID: intent.sessionID,
            expiresAt: .distantFuture
        )
    }
}

@available(iOS 26.0, *)
private struct AvibeAlarmMetadata: AlarmMetadata {
    let operationID: String
}

private enum SystemScheduleStableIdentity {
    private static func digest(
        ownerEpoch: String,
        instanceID: String,
        operationID: String
    ) -> [UInt8] {
        Array(
            SHA256.hash(
                data: Data(
                    "avibe-system-schedule-v2\0\(ownerEpoch)\0\(instanceID)\0\(operationID)".utf8
                )
            )
        )
    }

    static func alarmID(
        ownerEpoch: String,
        instanceID: String,
        operationID: String
    ) -> UUID {
        var bytes = Array(
            digest(
                ownerEpoch: ownerEpoch,
                instanceID: instanceID,
                operationID: operationID
            ).prefix(16)
        )
        bytes[6] = (bytes[6] & 0x0f) | 0x50
        bytes[8] = (bytes[8] & 0x3f) | 0x80
        let hex = bytes.map { String(format: "%02x", $0) }.joined()
        return UUID(
            uuidString: "\(hex.prefix(8))-\(hex.dropFirst(8).prefix(4))-\(hex.dropFirst(12).prefix(4))-\(hex.dropFirst(16).prefix(4))-\(hex.dropFirst(20))"
        )!
    }

    static func reminderURL(
        ownerEpoch: String,
        instanceID: String,
        operationID: String
    ) -> URL {
        let hex = digest(
            ownerEpoch: ownerEpoch,
            instanceID: instanceID,
            operationID: operationID
        ).map { String(format: "%02x", $0) }.joined()
        return URL(string: "avibe-system-schedule://operation/\(hex)")!
    }
}

private enum SystemScheduleLookupError: Error {
    case unavailable
}

@MainActor
final class LiveSystemSchedulePlatform: SystemSchedulePlatforming {
    private let eventStore: EKEventStore
    private let processInfo: ProcessInfo

    init(
        eventStore: EKEventStore = EKEventStore(),
        processInfo: ProcessInfo = .processInfo
    ) {
        self.eventStore = eventStore
        self.processInfo = processInfo
    }

    func systemIdentifier(
        for request: SystemScheduleRequest,
        owner: SystemScheduleManagementOwner
    ) -> String {
        switch request.kind {
        case .alarm:
            return SystemScheduleStableIdentity.alarmID(
                ownerEpoch: owner.ownerEpoch,
                instanceID: request.instanceID,
                operationID: request.operationID
            ).uuidString
        case .reminder:
            return SystemScheduleStableIdentity.reminderURL(
                ownerEpoch: owner.ownerEpoch,
                instanceID: request.instanceID,
                operationID: request.operationID
            ).absoluteString
        }
    }

    func capabilities() -> SystemScheduleCapabilities {
        let alarmAvailable: Bool
        let alarmAuthorization: SystemScheduleAuthorization
        let remindersAvailable: Bool
        let reminderAuthorization: SystemScheduleAuthorization
        let supportedKinds: [SystemScheduleKind]
        if #available(iOS 26.0, *) {
            alarmAvailable = true
            alarmAuthorization = Self.alarmAuthorization(
                AlarmManager.shared.authorizationState
            )
            remindersAvailable = false
            reminderAuthorization = .unsupported
            supportedKinds = [.alarm]
        } else {
            alarmAvailable = false
            alarmAuthorization = .unsupported
            remindersAvailable = true
            reminderAuthorization = Self.reminderAuthorization(
                EKEventStore.authorizationStatus(for: .reminder)
            )
            supportedKinds = [.reminder]
        }
        let version = processInfo.operatingSystemVersion
        return SystemScheduleCapabilities(
            iosVersion: "\(version.majorVersion).\(version.minorVersion).\(version.patchVersion)",
            alarmKitAvailable: alarmAvailable,
            alarmAuthorization: alarmAuthorization,
            remindersAvailable: remindersAvailable,
            remindersAuthorization: reminderAuthorization,
            supportedKinds: supportedKinds
        )
    }

    func existence(
        kind: SystemScheduleKind,
        systemIdentifier: String
    ) async -> SystemScheduleExistence {
        switch kind {
        case .alarm:
            guard #available(iOS 26.0, *),
                  let identifier = UUID(uuidString: systemIdentifier)
            else { return .unknown(.unsupported) }
            let authorization = Self.alarmAuthorization(
                AlarmManager.shared.authorizationState
            )
            guard authorization == .authorized else {
                return .unknown(.permissionDenied(authorization))
            }
            do {
                return try AlarmManager.shared.alarms.contains(where: {
                    $0.id == identifier
                }) ? .exists(authorization) : .absentConclusive(authorization)
            } catch {
                return .unknown(
                    .failed(
                        code: "alarm_lookup_unknown",
                        message: "The system alarm could not be inspected.",
                        systemIdentifier: systemIdentifier,
                        effectMayExist: true
                    )
                )
            }
        case .reminder:
            let authorization = Self.reminderAuthorization(
                EKEventStore.authorizationStatus(for: .reminder)
            )
            guard authorization == .authorized else {
                return .unknown(.permissionDenied(authorization))
            }
            guard let metadataURL = URL(string: systemIdentifier) else {
                return .unknown(
                    .failed(
                        code: "reminder_identity_invalid",
                        message: "The reminder identity is invalid.",
                        systemIdentifier: systemIdentifier,
                        effectMayExist: true
                    )
                )
            }
            switch await reminders(matching: metadataURL) {
            case .success(let reminders):
                return reminders.isEmpty
                    ? .absentConclusive(authorization)
                    : .exists(authorization)
            case .failure:
                return .unknown(
                    .failed(
                        code: "reminder_lookup_unknown",
                        message: "The system reminders could not be inspected.",
                        systemIdentifier: systemIdentifier,
                        effectMayExist: true
                    )
                )
            }
        }
    }

    func schedule(
        _ request: SystemScheduleRequest,
        systemIdentifier: String,
        stillCurrent: @escaping @MainActor () -> Bool
    ) async -> Result<SystemScheduleSubmission, SystemSchedulePlatformFailure> {
        switch request.kind {
        case .alarm:
            guard #available(iOS 26.0, *) else { return .failure(.unsupported) }
            return await scheduleAlarm(
                request,
                systemIdentifier: systemIdentifier,
                stillCurrent: stillCurrent
            )
        case .reminder:
            if #available(iOS 26.0, *) { return .failure(.unsupported) }
            return await scheduleReminder(
                request,
                systemIdentifier: systemIdentifier,
                stillCurrent: stillCurrent
            )
        }
    }

    func cancel(
        kind: SystemScheduleKind,
        systemIdentifier: String,
        stillAuthorized: @escaping @MainActor () -> Bool
    ) async -> Result<SystemScheduleAuthorization, SystemSchedulePlatformFailure> {
        let lookup = await existence(kind: kind, systemIdentifier: systemIdentifier)
        guard stillAuthorized() else { return .failure(.staleContext) }
        switch lookup {
        case .absentConclusive(let authorization):
            return .success(authorization)
        case .unknown(let failure):
            return .failure(failure)
        case .exists(let authorization):
            guard stillAuthorized() else { return .failure(.staleContext) }
            switch kind {
            case .alarm:
                guard #available(iOS 26.0, *),
                      let identifier = UUID(uuidString: systemIdentifier)
                else { return .failure(.unsupported) }
                do {
                    try AlarmManager.shared.cancel(id: identifier)
                    return .success(authorization)
                } catch {
                    let after = await existence(
                        kind: kind,
                        systemIdentifier: systemIdentifier
                    )
                    if case .absentConclusive = after { return .success(authorization) }
                    return .failure(
                        .failed(
                            code: "alarm_cancel_failed",
                            message: "The system alarm could not be cancelled.",
                            systemIdentifier: systemIdentifier,
                            effectMayExist: true
                        )
                    )
                }
            case .reminder:
                guard let metadataURL = URL(string: systemIdentifier) else {
                    return .failure(.unsupported)
                }
                let fetched = await reminders(matching: metadataURL)
                guard stillAuthorized() else { return .failure(.staleContext) }
                guard case .success(let reminders) = fetched else {
                    return .failure(
                        .failed(
                            code: "reminder_lookup_unknown",
                            message: "The system reminders could not be inspected.",
                            systemIdentifier: systemIdentifier,
                            effectMayExist: true
                        )
                    )
                }
                do {
                    for reminder in reminders {
                        guard stillAuthorized() else {
                            return .failure(.staleContext)
                        }
                        try eventStore.remove(reminder, commit: false)
                    }
                    guard stillAuthorized() else { return .failure(.staleContext) }
                    try eventStore.commit()
                    return .success(authorization)
                } catch {
                    return .failure(
                        .failed(
                            code: "reminder_cancel_failed",
                            message: "The system reminder could not be cancelled.",
                            systemIdentifier: systemIdentifier,
                            effectMayExist: true
                        )
                    )
                }
            }
        }
    }

    @available(iOS 26.0, *)
    private func scheduleAlarm(
        _ request: SystemScheduleRequest,
        systemIdentifier: String,
        stillCurrent: @escaping @MainActor () -> Bool
    ) async -> Result<SystemScheduleSubmission, SystemSchedulePlatformFailure> {
        guard let identifier = UUID(uuidString: systemIdentifier) else {
            return .failure(.unsupported)
        }
        let manager = AlarmManager.shared
        var authorization = Self.alarmAuthorization(manager.authorizationState)
        if authorization == .notDetermined {
            do {
                authorization = Self.alarmAuthorization(
                    try await manager.requestAuthorization()
                )
            } catch {
                return .failure(
                    .failed(
                        code: "alarm_authorization_failed",
                        message: "Alarm permission could not be requested."
                    )
                )
            }
        }
        guard stillCurrent() else { return .failure(.staleContext) }
        guard authorization == .authorized else {
            return .failure(.permissionDenied(authorization))
        }
        let lookup = await existence(kind: .alarm, systemIdentifier: systemIdentifier)
        guard stillCurrent() else { return .failure(.staleContext) }
        switch lookup {
        case .exists:
            return .success(
                SystemScheduleSubmission(
                    systemIdentifier: systemIdentifier,
                    authorization: authorization
                )
            )
        case .unknown(let failure):
            return .failure(failure)
        case .absentConclusive:
            break
        }
        guard stillCurrent() else { return .failure(.staleContext) }
        let alert: AlarmPresentation.Alert
        if #available(iOS 26.1, *) {
            alert = AlarmPresentation.Alert(
                title: LocalizedStringResource(stringLiteral: request.title)
            )
        } else {
            alert = AlarmPresentation.Alert(
                title: LocalizedStringResource(stringLiteral: request.title),
                stopButton: AlarmButton(
                    text: LocalizedStringResource(
                        stringLiteral: String(localized: "system_schedule.alarm.stop")
                    ),
                    textColor: .white,
                    systemImageName: "stop.fill"
                )
            )
        }
        let attributes = AlarmAttributes(
            presentation: AlarmPresentation(alert: alert),
            metadata: AvibeAlarmMetadata(operationID: request.operationID),
            tintColor: .accentColor
        )
        do {
            guard stillCurrent() else { return .failure(.staleContext) }
            _ = try await manager.schedule(
                id: identifier,
                configuration: .alarm(
                    schedule: .fixed(request.fireAt),
                    attributes: attributes
                )
            )
            return .success(
                SystemScheduleSubmission(
                    systemIdentifier: systemIdentifier,
                    authorization: authorization
                )
            )
        } catch {
            let after = await existence(kind: .alarm, systemIdentifier: systemIdentifier)
            if case .exists = after {
                return .success(
                    SystemScheduleSubmission(
                        systemIdentifier: systemIdentifier,
                        authorization: authorization
                    )
                )
            }
            let unknown: Bool
            if case .unknown = after { unknown = true } else { unknown = false }
            return .failure(
                .failed(
                    code: "alarm_schedule_failed",
                    message: "The system alarm could not be created.",
                    systemIdentifier: unknown ? systemIdentifier : nil,
                    effectMayExist: unknown
                )
            )
        }
    }

    private func scheduleReminder(
        _ request: SystemScheduleRequest,
        systemIdentifier: String,
        stillCurrent: @escaping @MainActor () -> Bool
    ) async -> Result<SystemScheduleSubmission, SystemSchedulePlatformFailure> {
        var authorization = Self.reminderAuthorization(
            EKEventStore.authorizationStatus(for: .reminder)
        )
        if authorization == .notDetermined {
            do {
                let granted = try await eventStore.requestFullAccessToReminders()
                authorization = granted ? .authorized : .denied
            } catch {
                return .failure(
                    .failed(
                        code: "reminder_authorization_failed",
                        message: "Reminder permission could not be requested."
                    )
                )
            }
        }
        guard stillCurrent() else { return .failure(.staleContext) }
        guard authorization == .authorized else {
            return .failure(.permissionDenied(authorization))
        }
        guard let calendar = eventStore.defaultCalendarForNewReminders() else {
            return .failure(
                .failed(
                    code: "reminder_calendar_unavailable",
                    message: "No default Reminders list is available."
                )
            )
        }
        guard let timeZone = TimeZone(identifier: request.timeZoneIdentifier),
              let metadataURL = URL(string: systemIdentifier)
        else { return .failure(.unsupported) }
        let lookup = await existence(kind: .reminder, systemIdentifier: systemIdentifier)
        guard stillCurrent() else { return .failure(.staleContext) }
        switch lookup {
        case .exists:
            return .success(
                SystemScheduleSubmission(
                    systemIdentifier: systemIdentifier,
                    authorization: authorization
                )
            )
        case .unknown(let failure):
            return .failure(failure)
        case .absentConclusive:
            break
        }
        let reminder = EKReminder(eventStore: eventStore)
        reminder.title = request.title
        reminder.calendar = calendar
        reminder.url = metadataURL
        Self.configureReminderDates(reminder, request: request, timeZone: timeZone)
        reminder.addAlarm(EKAlarm(absoluteDate: request.fireAt))
        guard stillCurrent() else { return .failure(.staleContext) }
        do {
            guard stillCurrent() else { return .failure(.staleContext) }
            try eventStore.save(reminder, commit: true)
            return .success(
                SystemScheduleSubmission(
                    systemIdentifier: systemIdentifier,
                    authorization: authorization
                )
            )
        } catch {
            let after = await existence(kind: .reminder, systemIdentifier: systemIdentifier)
            if case .exists = after {
                return .success(
                    SystemScheduleSubmission(
                        systemIdentifier: systemIdentifier,
                        authorization: authorization
                    )
                )
            }
            let unknown: Bool
            if case .unknown = after { unknown = true } else { unknown = false }
            return .failure(
                .failed(
                    code: "reminder_schedule_failed",
                    message: "The system reminder could not be created.",
                    systemIdentifier: unknown ? systemIdentifier : nil,
                    effectMayExist: unknown
                )
            )
        }
    }

    static func configureReminderDates(
        _ reminder: EKReminder,
        request: SystemScheduleRequest,
        timeZone: TimeZone
    ) {
        var scheduleCalendar = Calendar(identifier: .gregorian)
        scheduleCalendar.timeZone = timeZone
        var components = scheduleCalendar.dateComponents(
            [.year, .month, .day, .hour, .minute, .second],
            from: request.fireAt
        )
        components.calendar = scheduleCalendar
        components.timeZone = timeZone
        reminder.startDateComponents = components
        reminder.dueDateComponents = components
    }

    private func reminders(
        matching metadataURL: URL
    ) async -> Result<[EKReminder], Error> {
        let predicate = eventStore.predicateForReminders(in: nil)
        return await withCheckedContinuation { continuation in
            eventStore.fetchReminders(matching: predicate) { reminders in
                guard let reminders else {
                    continuation.resume(
                        returning: .failure(SystemScheduleLookupError.unavailable)
                    )
                    return
                }
                continuation.resume(
                    returning: .success(
                        reminders.filter {
                            $0.url?.absoluteString == metadataURL.absoluteString
                        }
                    )
                )
            }
        }
    }

    @available(iOS 26.0, *)
    private static func alarmAuthorization(
        _ state: AlarmManager.AuthorizationState
    ) -> SystemScheduleAuthorization {
        switch state {
        case .notDetermined: return .notDetermined
        case .authorized: return .authorized
        case .denied: return .denied
        @unknown default: return .unsupported
        }
    }

    private static func reminderAuthorization(
        _ state: EKAuthorizationStatus
    ) -> SystemScheduleAuthorization {
        switch state {
        case .notDetermined: return .notDetermined
        case .restricted: return .restricted
        case .denied: return .denied
        case .fullAccess: return .authorized
        case .writeOnly: return .restricted
        @unknown default: return .unsupported
        }
    }
}
