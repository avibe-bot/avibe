import AlarmKit
import CryptoKit
import Darwin
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

enum SystemScheduleAdmission {
    case authorized(SystemScheduleContext)
    case localStateUnavailable
}

struct SystemScheduleContext: Equatable {
    let owner: SystemScheduleManagementOwner
    let instanceID: String
    let origin: String
    let runtimeIdentity: Int
    let navigationEpoch: Int
    let webViewIdentity: UUID?

    init(owner: SystemScheduleManagementOwner, instanceID: String, origin: String,
         runtimeIdentity: Int, navigationEpoch: Int, webViewIdentity: UUID? = nil) {
        self.owner = owner
        self.instanceID = instanceID
        self.origin = origin
        self.runtimeIdentity = runtimeIdentity
        self.navigationEpoch = navigationEpoch
        self.webViewIdentity = webViewIdentity
    }

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
    func cleanupRecords() -> [StoredSystemSchedule]
    @discardableResult
    func save(_ record: StoredSystemSchedule) -> Bool
    @discardableResult
    func detachActiveOwner() -> Bool
    @discardableResult
    func detachActiveOwner(unlessAccountID accountID: String) -> Bool
}

@MainActor
final class FileSystemSystemScheduleStore: SystemScheduleStoring {
    private struct Envelope: Codable, Equatable {
        let version: Int
        var activeOwner: SystemScheduleManagementOwner?
        var records: [StoredSystemSchedule]
    }

    private enum LoadFailure: Error {
        case unsupportedVersion(Int)
        case invalidData(String)
    }

    private enum StoreIOError: Error {
        case posix(String, Int32)
    }

    private static let version = 1
    private static let logger = Logger(
        subsystem: "bot.avibe.ios",
        category: "system-schedule-store"
    )

    private let fileURL: URL
    private let fileManager: FileManager
    private let beforeRename: (() throws -> Void)?
    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()

    static func defaultFileURL(
        deviceID: String,
        fileManager: FileManager = .default
    ) -> URL {
        let directory = fileManager.urls(
            for: .applicationSupportDirectory,
            in: .userDomainMask
        ).first ?? URL(
            fileURLWithPath: NSHomeDirectory(),
            isDirectory: true
        ).appendingPathComponent(
            "Library/Application Support",
            isDirectory: true
        )
        return directory.appendingPathComponent(
            "system-schedule-ledger-\(deviceID)-v1.json"
        )
    }

    init(
        fileURL: URL? = nil,
        fileManager: FileManager = .default,
        beforeRename: (() throws -> Void)? = nil
    ) {
        self.fileManager = fileManager
        self.beforeRename = beforeRename
        if let fileURL {
            self.fileURL = fileURL
        } else {
            self.fileURL = Self.defaultFileURL(
                deviceID: "default",
                fileManager: fileManager
            )
        }
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
            detach(active, in: &envelope)
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

    func cleanupRecords() -> [StoredSystemSchedule] {
        guard case .success(let envelope) = loadEnvelope() else { return [] }
        return envelope.records
            .filter {
                [.managedCleanupPending, .detachedCleanupPending]
                    .contains($0.lifecycle)
            }
            .sorted(by: { $0.updatedAt < $1.updatedAt })
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

    @discardableResult
    func detachActiveOwner() -> Bool {
        switch loadEnvelope() {
        case .failure:
            return false
        case .success(var envelope):
            guard let activeOwner = envelope.activeOwner else { return true }
            detach(activeOwner, in: &envelope)
            return persist(envelope)
        }
    }

    @discardableResult
    func detachActiveOwner(unlessAccountID accountID: String) -> Bool {
        switch loadEnvelope() {
        case .failure:
            return false
        case .success(var envelope):
            guard let activeOwner = envelope.activeOwner else { return true }
            guard activeOwner.accountID != accountID else { return true }
            detach(activeOwner, in: &envelope)
            return persist(envelope)
        }
    }

    private func detach(
        _ owner: SystemScheduleManagementOwner,
        in envelope: inout Envelope
    ) {
        if envelope.activeOwner == owner {
            envelope.activeOwner = nil
        }
        let now = Date()
        for index in envelope.records.indices
            where envelope.records[index].owner == owner
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

    private func loadEnvelope() -> Result<Envelope, LoadFailure> {
        guard fileManager.fileExists(atPath: fileURL.path) else {
            return .success(
                Envelope(version: Self.version, activeOwner: nil, records: [])
            )
        }
        do {
            let envelope = try decoder.decode(
                Envelope.self,
                from: Data(contentsOf: fileURL)
            )
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
        let directory = fileURL.deletingLastPathComponent()
        let temporaryURL = directory.appendingPathComponent(
            ".\(fileURL.lastPathComponent).\(UUID().uuidString).tmp"
        )
        do {
            try fileManager.createDirectory(
                at: directory,
                withIntermediateDirectories: true
            )
            try writeAndSync(try encoder.encode(envelope), to: temporaryURL)
            try beforeRename?()
            try replaceAtomically(temporaryURL: temporaryURL)
            try syncDirectory(directory)
            return true
        } catch {
            try? fileManager.removeItem(at: temporaryURL)
            Self.logger.error(
                "System schedule store could not be committed: \(String(describing: error), privacy: .public)"
            )
            return false
        }
    }

    private func writeAndSync(_ data: Data, to url: URL) throws {
        let descriptor = url.path.withCString {
            open($0, O_WRONLY | O_CREAT | O_TRUNC, S_IRUSR | S_IWUSR)
        }
        guard descriptor >= 0 else {
            throw StoreIOError.posix("open", errno)
        }
        var needsClose = true
        defer {
            if needsClose { _ = close(descriptor) }
        }
        try data.withUnsafeBytes { rawBuffer in
            guard let baseAddress = rawBuffer.baseAddress else { return }
            var offset = 0
            while offset < rawBuffer.count {
                let count = Darwin.write(
                    descriptor,
                    baseAddress.advanced(by: offset),
                    rawBuffer.count - offset
                )
                if count < 0, errno == EINTR { continue }
                guard count > 0 else {
                    throw StoreIOError.posix("write", errno)
                }
                offset += count
            }
        }
        guard fsync(descriptor) == 0 else {
            throw StoreIOError.posix("fsync-file", errno)
        }
        guard close(descriptor) == 0 else {
            needsClose = false
            throw StoreIOError.posix("close", errno)
        }
        needsClose = false
    }

    private func replaceAtomically(temporaryURL: URL) throws {
        let result = temporaryURL.path.withCString { temporaryPath in
            fileURL.path.withCString { destinationPath in
                rename(temporaryPath, destinationPath)
            }
        }
        guard result == 0 else {
            throw StoreIOError.posix("rename", errno)
        }
    }

    private func syncDirectory(_ directory: URL) throws {
        let descriptor = directory.path.withCString {
            open($0, O_RDONLY)
        }
        guard descriptor >= 0 else {
            throw StoreIOError.posix("open-directory", errno)
        }
        defer { _ = close(descriptor) }
        guard fsync(descriptor) == 0 else {
            throw StoreIOError.posix("fsync-directory", errno)
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
}

@MainActor
final class SystemScheduleCoordinator {
    private let platform: SystemSchedulePlatforming
    private let store: SystemScheduleStoring
    private let now: () -> Date

    private struct ExecutionKey: Hashable {
        let accountID: String
        let ownerEpoch: String
        let instanceID: String
        let operationID: String

        init(owner: SystemScheduleManagementOwner, instanceID: String, operationID: String) {
            accountID = owner.accountID
            ownerEpoch = owner.ownerEpoch
            self.instanceID = instanceID
            self.operationID = operationID
        }
    }

    /// MainActor isolation serializes admission, not effects across suspension.
    /// This slot, installed before the driver Task, owns every issued completion.
    private final class ExecutionSlot {
        enum Effect { case create, recovery, remove }
        struct RunningEffect {
            let token = UUID()
            let kind: Effect
        }
        enum Observation {
            case create(Result<SystemScheduleSubmission, SystemSchedulePlatformFailure>)
            case recovery(SystemScheduleExistence)
            case remove(Result<SystemScheduleAuthorization, SystemSchedulePlatformFailure>)
        }
        struct Waiter {
            let requestID: String
            let isCancel: Bool
            let continuation: CheckedContinuation<SystemScheduleReceipt, Never>
        }
        let key: ExecutionKey
        var waiters: [Waiter] = []
        var runningEffect: RunningEffect?
        var observation: Observation?
        var revoked: SystemScheduleFenceFailure?
        var cleanupRequested = false
        var cleanupReceipt: SystemScheduleReceipt?
        var writeFailure: SystemScheduleReceipt?
        var settled = false
        var drivesCreate = false
        var recommitRequired = false

        init(key: ExecutionKey) { self.key = key }
    }
    private var executions: [ExecutionKey: ExecutionSlot] = [:]

    private func key(_ request: SystemScheduleRequest, _ context: SystemScheduleContext) -> ExecutionKey {
        ExecutionKey(owner: context.owner, instanceID: request.instanceID, operationID: request.operationID)
    }

    private func join(
        _ slot: ExecutionSlot, requestID: String, isCancel: Bool,
        driver: (@MainActor () async -> SystemScheduleReceipt)? = nil
    ) async -> SystemScheduleReceipt {
        await withCheckedContinuation { continuation in
            slot.waiters.append(.init(requestID: requestID, isCancel: isCancel, continuation: continuation))
            if let driver {
                Task { @MainActor in
                    let receipt: SystemScheduleReceipt
                    if slot.recommitRequired,
                       let current = self.store.record(operationID: slot.key.operationID,
                           instanceID: slot.key.instanceID,
                           owner: .init(accountID: slot.key.accountID, ownerEpoch: slot.key.ownerEpoch)),
                       !self.commit(current, slot: slot) {
                        receipt = slot.writeFailure!
                    } else {
                        receipt = await driver()
                    }
                    precondition(slot.runningEffect == nil)
                    slot.settled = true
                    let waiters = slot.waiters
                    slot.waiters.removeAll()
                    // Retain a failed commit until an explicit recovery joins it;
                    // prepare/replay may not turn an older ledger row into success.
                    if slot.writeFailure == nil { self.executions.removeValue(forKey: slot.key) }
                    let record = self.store.record(operationID: slot.key.operationID,
                        instanceID: slot.key.instanceID,
                        owner: .init(accountID: slot.key.accountID, ownerEpoch: slot.key.ownerEpoch))
                    let createOutcome = slot.drivesCreate ? receipt
                        : (record?.createReceipt(requestID: receipt.requestID) ?? receipt)
                    let cancelOutcome = slot.cleanupReceipt
                        ?? record?.cancelReceipt(requestID: receipt.requestID) ?? receipt
                    for waiter in waiters {
                        let outcome = slot.writeFailure ?? (waiter.isCancel ? cancelOutcome : createOutcome)
                        waiter.continuation.resume(returning: outcome.replacingRequestID(waiter.requestID))
                    }
                }
            }
        }
    }

    private func beginEffect(_ kind: ExecutionSlot.Effect, in slot: ExecutionSlot) -> UUID {
        precondition(slot.runningEffect == nil)
        let effect = ExecutionSlot.RunningEffect(kind: kind)
        slot.runningEffect = effect
        return effect.token
    }

    private func endEffect(_ token: UUID, in slot: ExecutionSlot, observation: ExecutionSlot.Observation) {
        precondition(slot.runningEffect?.token == token)
        slot.runningEffect = nil
        slot.observation = observation
        if let failure = slot.writeFailure {
            let observed: SystemScheduleAuthorization
            switch observation {
            case .create(.success(let submission)): observed = submission.authorization
            case .create(.failure(let error)), .remove(.failure(let error)), .recovery(.unknown(let error)):
                observed = observedAuthorization(for: error, fallback: failure.authorization)
            case .remove(.success(let authorization)), .recovery(.exists(let authorization)),
                 .recovery(.absentConclusive(let authorization)): observed = authorization
            }
            slot.writeFailure = SystemScheduleReceipt(
                requestID: failure.requestID, operationID: failure.operationID, kind: failure.kind,
                state: failure.state, systemIdentifier: failure.systemIdentifier, fireAt: failure.fireAt,
                timeZoneIdentifier: failure.timeZoneIdentifier, authorization: observed, error: failure.error
            )
        }
    }

    private func commit(_ record: StoredSystemSchedule, slot: ExecutionSlot) -> Bool {
        guard slot.writeFailure == nil else { return false }
        let didPersist = store.save(record)
        guard didPersist else {
            slot.writeFailure = SystemScheduleReceipt.terminal(
                request: record.request(requestID: "native-commit"), state: .failed,
                authorization: record.authorization, systemIdentifier: record.systemIdentifier,
                code: "local_state_write_failed",
                message: "The system item changed, but Avibe could not finish saving its management record."
            )
            return false
        }
        return true
    }

    private func persist(_ record: StoredSystemSchedule) -> Bool {
        let executionKey = ExecutionKey(owner: record.owner, instanceID: record.intent.instanceID,
                                        operationID: record.operationID)
        if let slot = executions[executionKey], !slot.settled {
            return commit(record, slot: slot)
        }
        return store.save(record)
    }

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
        guard detachActiveOwner(unlessAccountID: accountID) else { return nil }
        return store.managementOwner(accountID: accountID)
    }

    func isActive(owner: SystemScheduleManagementOwner) -> Bool {
        store.isActive(owner: owner)
    }

    func requiresRecovery(
        _ request: SystemScheduleRequest,
        context: SystemScheduleContext
    ) -> Bool {
        let execution = executions[key(request, context)]
        if let execution, !execution.settled { return true }
        if let historical = store.latestRecord(operationID: request.operationID, instanceID: request.instanceID),
           historical.owner != context.owner,
           historical.intent == StoredSystemScheduleIntent(request: request),
           historical.lifecycle == .detachedCleanupPending { return true }
        guard let record = store.record(
            operationID: request.operationID,
            instanceID: request.instanceID,
            owner: context.owner
        ) else {
            return false
        }
        if record.lifecycle == .reserved { return execution?.cleanupRequested == true }
        if execution != nil { return true }
        return record.intent == StoredSystemScheduleIntent(request: request)
            && [.creatingUnknown, .managedCleanupPending, .detachedCleanupPending].contains(record.lifecycle)
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
            if executions[key(request, context)] != nil
                || [.creatingUnknown, .managedCleanupPending, .detachedCleanupPending].contains(existing.lifecycle) {
                return nil
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
            if historical.lifecycle == .detachedCleanupPending { return nil }
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
            guard persist(historical) else {
                return writeFailure(request, identifier: historical.systemIdentifier)
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
        guard persist(record) else { return writeFailure(request, identifier: record.systemIdentifier) }
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
        guard persist(record) else { return writeFailure(request, identifier: record.systemIdentifier) }
        return record.createReceipt(requestID: request.requestID)!
    }

    func schedule(
        _ request: SystemScheduleRequest,
        context: SystemScheduleContext,
        creationFence: @escaping @MainActor () -> SystemScheduleFenceFailure?,
        postWriteFence: @escaping @MainActor () -> SystemScheduleFenceFailure?
    ) async -> SystemScheduleReceipt {
        let executionKey = key(request, context)
        if let slot = executions[executionKey], !slot.settled {
            guard store.record(operationID: request.operationID, instanceID: request.instanceID,
                               owner: context.owner)?.intent == StoredSystemScheduleIntent(request: request) else {
                return terminal(request, state: .failed, code: "operation_conflict",
                                message: "This operation ID already belongs to a different system schedule.")
            }
            return await join(slot, requestID: request.requestID, isCancel: false)
        }
        if let historical = store.latestRecord(operationID: request.operationID, instanceID: request.instanceID),
           historical.owner != context.owner,
           historical.intent == StoredSystemScheduleIntent(request: request),
           historical.lifecycle == .detachedCleanupPending {
            return await joinCleanup(historical, requestID: request.requestID, isCancel: false)
        }
        if let prepared = prepare(request, context: context) { return prepared }
        let previous = executions[executionKey]
        let slot = ExecutionSlot(key: executionKey)
        slot.drivesCreate = true
        slot.recommitRequired = previous?.writeFailure != nil
        slot.cleanupRequested = previous?.cleanupRequested ?? false
        slot.revoked = previous?.revoked
        executions[executionKey] = slot
        return await join(slot, requestID: request.requestID, isCancel: false, driver: {
            await self.driveSchedule(request, context: context, slot: slot,
                creationFence: { slot.revoked ?? creationFence() },
                postWriteFence: { slot.revoked ?? postWriteFence() })
        })
    }

    private func driveSchedule(
        _ request: SystemScheduleRequest,
        context: SystemScheduleContext,
        slot: ExecutionSlot,
        creationFence: @escaping @MainActor () -> SystemScheduleFenceFailure?,
        postWriteFence: @escaping @MainActor () -> SystemScheduleFenceFailure?
    ) async -> SystemScheduleReceipt {
        if store.isActive(owner: context.owner) {
            if let prepared = prepare(request, context: context) { return prepared }
        }
        guard var record = store.record(
            operationID: request.operationID,
            instanceID: request.instanceID,
            owner: context.owner
        ) else { return localStateUnavailable(request) }

        if record.lifecycle == .detachedRetained {
            return record.createReceipt(requestID: request.requestID)
                ?? terminal(request, state: .staleContext, code: "stale_context", message: nil)
        }
        if let failure = slot.writeFailure { return failure }
        if [.managedCleanupPending, .detachedCleanupPending].contains(record.lifecycle)
            || slot.cleanupRequested {
            return await settleStaleCreatedItem(&record, request: request, context: context,
                slot: slot, failure: slot.revoked ?? .cancelled)
        }
        // A slot can have been installed just before a synchronous cancellation
        // closed a reservation. Never turn that tombstone into another create.
        if record.lifecycle != .reserved && record.lifecycle != .creatingUnknown {
            return record.createReceipt(requestID: request.requestID)
                ?? terminal(request, state: .staleContext, code: "stale_context", message: nil)
        }
        if record.lifecycle == .creatingUnknown {
            let token = beginEffect(.recovery, in: slot)
            let recovery = await platform.existence(
                kind: record.intent.kind,
                systemIdentifier: record.systemIdentifier
            )
            endEffect(token, in: slot, observation: .recovery(recovery))
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
                    context: context,
                    slot: slot
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
                        slot: slot,
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
                guard persist(record) else {
                    return writeFailure(request, identifier: record.systemIdentifier)
                }
                return record.createReceipt(requestID: request.requestID)!
            case .absentConclusive(let authorization):
                record.authorization = authorization
                record.lifecycle = .closedNoItem
                record.effectMayExist = false
                record.createState = .failed
                record.lastError = StoredSystemScheduleError(
                    code: "recovery_item_absent",
                    message: "The previously confirmed system item is absent. Create a new operation to schedule it again."
                )
                record.generation += 1
                record.updatedAt = now()
                guard persist(record) else { return writeFailure(request, identifier: record.systemIdentifier) }
                return record.createReceipt(requestID: request.requestID)!
            case .unknown(let failure):
                record.authorization = observedAuthorization(
                    for: failure,
                    fallback: record.authorization
                )
                record.lastError = storedError(failure)
                record.updatedAt = now()
                guard persist(record) else {
                    return writeFailure(
                        request,
                        identifier: record.systemIdentifier
                    )
                }
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
        guard persist(record),
              let persisted = store.record(
                  operationID: record.operationID,
                  instanceID: record.intent.instanceID,
                  owner: record.owner
              ), persisted == record
        else { return writeFailure(request, identifier: record.systemIdentifier) }
        let token = beginEffect(.create, in: slot)
        let result = await platform.schedule(
            request,
            systemIdentifier: record.systemIdentifier,
            stillCurrent: { slot.writeFailure == nil && creationFence() == nil }
        )
        endEffect(token, in: slot, observation: .create(result))
        guard var latest = store.record(
            operationID: record.operationID,
            instanceID: record.intent.instanceID,
            owner: record.owner
        ) else { return localStateUnavailable(request) }
        if latest.lifecycle == .detachedCleanupPending {
            switch result {
            case .success(let submission):
                latest.authorization = submission.authorization
                guard submission.systemIdentifier == record.systemIdentifier else {
                    latest.createState = .failed
                    latest.lastError = StoredSystemScheduleError(
                        code: "platform_identity_mismatch",
                        message: "The system returned an unexpected item identity."
                    )
                    latest.updatedAt = now()
                    guard persist(latest) else {
                        return writeFailure(
                            request,
                            identifier: latest.systemIdentifier
                        )
                    }
                    return latest.createReceipt(requestID: request.requestID)!
                }
                return await settleStaleCreatedItem(
                    &latest,
                    request: request,
                    context: context,
                    slot: slot,
                    failure: .staleContext
                )
            case .failure(let failure) where failure.effectMayExist:
                return await settleStaleCreatedItem(
                    &latest,
                    request: request,
                    context: context,
                    slot: slot,
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
                guard persist(latest) else {
                    return writeFailure(
                        request,
                        identifier: latest.systemIdentifier
                    )
                }
                return latest.createReceipt(requestID: request.requestID)!
            }
        }
        if let writeFailure = slot.writeFailure { return writeFailure }
        switch result {
        case .success(let submission):
            latest.authorization = submission.authorization
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
                guard persist(latest) else {
                    return writeFailure(
                        request,
                        identifier: latest.systemIdentifier
                    )
                }
                return latest.createReceipt(requestID: request.requestID)!
            }
            if let failure = postWriteFence() {
                return await settleStaleCreatedItem(
                    &latest,
                    request: request,
                    context: context,
                    slot: slot,
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
            guard persist(latest) else {
                return writeFailure(request, identifier: latest.systemIdentifier)
            }
            return latest.createReceipt(requestID: request.requestID)!
        case .failure(let failure):
            latest.authorization = observedAuthorization(
                for: failure,
                fallback: latest.authorization
            )
            let effectMayExist = failure.effectMayExist
            if effectMayExist, let revoked = postWriteFence() {
                return await settleStaleCreatedItem(&latest, request: request,
                    context: context, slot: slot, failure: revoked)
            }
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
            guard persist(latest) else {
                return writeFailure(
                    request,
                    identifier: latest.systemIdentifier
                )
            }
            return latest.createReceipt(requestID: request.requestID)
                ?? receipt(for: failure, request: request, fallback: latest.systemIdentifier)
        }
    }

    func cancel(
        _ request: SystemScheduleCancelRequest,
        context: SystemScheduleContext,
        stillCurrent: @escaping @MainActor () -> Bool
    ) async -> SystemScheduleReceipt {
        guard stillCurrent() else { return cancelFailure(request, code: "stale_context") }
        let executionKey = ExecutionKey(owner: context.owner, instanceID: context.instanceID,
                                        operationID: request.operationID)
        if let slot = executions[executionKey], !slot.settled {
            guard var record = store.record(operationID: request.operationID,
                instanceID: context.instanceID, owner: context.owner), record.intent.kind == request.kind,
                store.isActive(owner: context.owner) else {
                return cancelFailure(request, code: "item_not_found")
            }
            slot.revoked = .cancelled
            slot.cleanupRequested = true
            // Control events never wait behind create. Persist the cleanup
            // demand, but leave every issued effect with its original driver.
            if record.lifecycle == .creatingUnknown {
                record.lifecycle = .managedCleanupPending
                record.createState = .cancelled
                record.generation += 1
                record.updatedAt = now()
                _ = commit(record, slot: slot)
            }
            return await join(slot, requestID: request.requestID, isCancel: true)
        }
        let slot = ExecutionSlot(key: executionKey)
        slot.recommitRequired = executions[executionKey]?.writeFailure != nil
        slot.revoked = .cancelled
        slot.cleanupRequested = true
        executions[executionKey] = slot
        return await join(slot, requestID: request.requestID, isCancel: true, driver: {
            let receipt = await self.driveCancel(request, context: context, slot: slot, stillCurrent: stillCurrent)
            slot.cleanupReceipt = receipt
            return receipt
        })
    }

    private func driveCancel(
        _ request: SystemScheduleCancelRequest,
        context: SystemScheduleContext,
        slot: ExecutionSlot,
        stillCurrent: @escaping @MainActor () -> Bool
    ) async -> SystemScheduleReceipt {
        guard stillCurrent() else { return cancelFailure(request, code: "stale_context") }
        guard store.isAvailable else {
            return cancelFailure(request, code: "local_state_unavailable")
        }
        guard store.isActive(owner: context.owner) else {
            return cancelFailure(request, code: "item_not_found")
        }
        guard var record = store.record(
            operationID: request.operationID,
            instanceID: context.instanceID,
            owner: context.owner
        ), record.intent.kind == request.kind else {
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
            guard persist(record) else {
                return cancelFailure(request, code: "local_state_write_failed")
            }
            return record.cancelReceipt(requestID: request.requestID)
        }
        record.createState = record.createState ?? .cancelled
        record.lifecycle = record.lifecycle.isDetached
            ? .detachedCleanupPending
            : .managedCleanupPending
        record.effectMayExist = true
        record.generation += 1
        record.updatedAt = now()
        guard persist(record) else {
            return cancelFailure(request, code: "local_state_write_failed")
        }
        return await cleanup(
            record,
            requestID: request.requestID,
            slot: slot,
            stillAuthorized: stillCurrent
        )
    }

    private func cleanup(
        _ record: StoredSystemSchedule,
        requestID: String,
        slot: ExecutionSlot,
        stillAuthorized: @escaping @MainActor () -> Bool
    ) async -> SystemScheduleReceipt {
        if let failure = slot.writeFailure { return failure.replacingRequestID(requestID) }
        let request = SystemScheduleCancelRequest(
            requestID: requestID,
            operationID: record.operationID,
            kind: record.intent.kind,
            purpose: .user
        )
        let token = beginEffect(.remove, in: slot)
        let result = await platform.cancel(
            kind: record.intent.kind,
            systemIdentifier: record.systemIdentifier,
            stillAuthorized: {
                guard stillAuthorized(),
                      let current = self.store.record(
                          operationID: record.operationID,
                          instanceID: record.intent.instanceID,
                          owner: record.owner
                      )
                else { return false }
                return slot.writeFailure == nil
                    && [.managedCleanupPending, .detachedCleanupPending]
                        .contains(current.lifecycle)
            }
        )
        endEffect(token, in: slot, observation: .remove(result))
        guard var current = store.record(
            operationID: record.operationID,
            instanceID: record.intent.instanceID,
            owner: record.owner
        )
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
            guard persist(current) else {
                return cancelFailure(
                    request,
                    code: "local_state_write_failed",
                    record: current
                )
            }
            return current.cancelReceipt(requestID: requestID)
        case .failure(let failure):
            current.authorization = observedAuthorization(
                for: failure,
                fallback: current.authorization
            )
            current.lastError = storedError(failure)
            current.updatedAt = now()
            guard persist(current) else {
                return cancelFailure(
                    request,
                    code: "local_state_write_failed",
                    record: current
                )
            }
            return receipt(
                for: failure,
                request: current.request(requestID: requestID),
                fallback: current.systemIdentifier
            )
        }
    }

    @discardableResult
    func detachActiveOwner() -> Bool {
        let affected = executions.values.filter { slot in
            store.isActive(owner: .init(accountID: slot.key.accountID, ownerEpoch: slot.key.ownerEpoch))
        }
        let committed = store.detachActiveOwner()
        revokeDetached(affected, committed: committed)
        return committed
    }

    @discardableResult
    func detachActiveOwner(unlessAccountID accountID: String) -> Bool {
        let affected = executions.values.filter { slot in
            slot.key.accountID != accountID
                && store.isActive(owner: .init(accountID: slot.key.accountID, ownerEpoch: slot.key.ownerEpoch))
        }
        let committed = store.detachActiveOwner(unlessAccountID: accountID)
        revokeDetached(affected, committed: committed)
        return committed
    }

    private func revokeDetached(_ slots: [ExecutionSlot], committed: Bool) {
        for slot in slots {
            slot.revoked = .staleContext
            if let record = store.record(operationID: slot.key.operationID, instanceID: slot.key.instanceID,
                owner: .init(accountID: slot.key.accountID, ownerEpoch: slot.key.ownerEpoch)) {
                slot.cleanupRequested = [.creatingUnknown, .managedCleanupPending, .detachedCleanupPending]
                    .contains(record.lifecycle)
                if !committed {
                    slot.writeFailure = writeFailure(record.request(requestID: "native-detach"),
                                                     identifier: record.systemIdentifier)
                }
            }
        }
    }

    @discardableResult
    func synchronizeAccount(accountID: String?) -> Bool {
        guard let accountID else { return true }
        guard detachActiveOwner(unlessAccountID: accountID) else { return false }
        return store.managementOwner(accountID: accountID) != nil
    }

    func retryPendingCleanup(
        excludingOperationID: String? = nil,
        context: SystemScheduleContext? = nil
    ) async {
        guard store.isAvailable else { return }
        let records = store.cleanupRecords().filter { record in
            !(record.operationID == excludingOperationID
                && record.owner == context?.owner && record.intent.instanceID == context?.instanceID)
        }
        let tasks = records.map { record in
            Task { @MainActor in
                await self.joinCleanup(record,
                    requestID: "native-cleanup-\(UUID().uuidString.lowercased())", isCancel: true)
            }
        }
        for task in tasks { _ = await task.value }
    }

    private func joinCleanup(_ record: StoredSystemSchedule, requestID: String, isCancel: Bool) async -> SystemScheduleReceipt {
        let executionKey = ExecutionKey(owner: record.owner, instanceID: record.intent.instanceID,
                                        operationID: record.operationID)
        if let slot = executions[executionKey], !slot.settled {
            slot.cleanupRequested = true
            return await join(slot, requestID: requestID, isCancel: isCancel)
        }
        let slot = ExecutionSlot(key: executionKey)
        slot.recommitRequired = executions[executionKey]?.writeFailure != nil
        slot.cleanupRequested = true
        executions[executionKey] = slot
        return await join(slot, requestID: requestID, isCancel: isCancel, driver: {
            // Re-read only to inherit synchronous detach, never to arbitrate
            // a competing OS effect. This slot is its sole owner.
            guard let current = self.store.record(operationID: record.operationID,
                instanceID: record.intent.instanceID, owner: record.owner),
                [.managedCleanupPending, .detachedCleanupPending].contains(current.lifecycle) else {
                return record.cancelReceipt(requestID: requestID)
            }
            let result = await self.cleanup(current, requestID: requestID, slot: slot, stillAuthorized: { true })
            slot.cleanupReceipt = result
            return result
        })
    }

    private func reconcileRecoveryAfterRecordChange(
        _ current: StoredSystemSchedule,
        recovery: SystemScheduleExistence,
        request: SystemScheduleRequest,
        context: SystemScheduleContext,
        slot: ExecutionSlot
    ) async -> SystemScheduleReceipt {
        guard current.intent == StoredSystemScheduleIntent(request: request) else {
            return terminal(
                request,
                state: .failed,
                code: "operation_conflict",
                message: "This operation ID already belongs to a different system schedule."
            )
        }
        guard [.managedCleanupPending, .detachedCleanupPending].contains(current.lifecycle) else {
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
                slot: slot,
                failure: slot.revoked ?? .staleContext
            )
        case .absentConclusive(let authorization):
            record.authorization = authorization
            record.lifecycle = current.lifecycle.isDetached ? .detachedAbsent : .closedNoItem
            record.effectMayExist = false
            record.createState = state(for: slot.revoked ?? .staleContext)
            record.lastError = error(
                code: code(for: slot.revoked ?? .staleContext),
                message: message(for: slot.revoked ?? .staleContext)
            )
        case .unknown(let failure):
            record.authorization = observedAuthorization(
                for: failure,
                fallback: record.authorization
            )
            record.effectMayExist = true
            record.createState = state(for: slot.revoked ?? .staleContext)
            record.lastError = storedError(failure)
        }
        record.generation += 1
        record.updatedAt = now()
        guard persist(record) else {
            return writeFailure(request, identifier: record.systemIdentifier)
        }
        return record.createReceipt(requestID: request.requestID)!
    }

    private func settleStaleCreatedItem(
        _ record: inout StoredSystemSchedule,
        request: SystemScheduleRequest,
        context: SystemScheduleContext,
        slot: ExecutionSlot,
        failure: SystemScheduleFenceFailure
    ) async -> SystemScheduleReceipt {
        slot.cleanupRequested = true
        record.lifecycle = store.isActive(owner: record.owner)
            ? .managedCleanupPending
            : .detachedCleanupPending
        record.effectMayExist = true
        if record.createState != .scheduled { record.createState = state(for: failure) }
        record.lastError = error(code: code(for: failure), message: message(for: failure))
        record.generation += 1
        record.updatedAt = now()
        guard persist(record) else {
            return writeFailure(request, identifier: record.systemIdentifier)
        }
        let cleanupReceipt = await cleanup(
            record,
            requestID: request.requestID,
            slot: slot,
            stillAuthorized: { true }
        )
        slot.cleanupReceipt = cleanupReceipt
        if cleanupReceipt.error?.code == "local_state_write_failed" {
            return cleanupReceipt
        }
        return store.record(
            operationID: record.operationID,
            instanceID: record.intent.instanceID,
            owner: record.owner
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
