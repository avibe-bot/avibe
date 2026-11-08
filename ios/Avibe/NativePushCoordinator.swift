import CryptoKit
import Foundation
import Security
import UIKit
import UserNotifications

struct PushManagementRequest {
    let method: String
    let path: String
    let headers: [String: String]
    let body: Data?
}

struct PushManagementResponse {
    let statusCode: Int
    let body: [String: Any]
}

struct PushNotificationTap: Equatable {
    let accountID: String
    let instanceOrigin: String
    let url: String
}

enum NativePushError: Error, Equatable {
    case unavailable
    case permissionDenied
    case invalidVapidKey
    case management(status: Int, code: String?)
    case invalidResponse
    case staleAccount
    case generationExhausted

    var bridgeMessage: String {
        switch self {
        case .unavailable:
            return "Native push is not available for this account."
        case .permissionDenied:
            return "Notification permission was not granted."
        case .invalidVapidKey:
            return "The instance VAPID key is invalid."
        case .management(let status, let code):
            return "Push management request failed (\(status), \(code ?? "unknown"))."
        case .invalidResponse:
            return "The push service returned an invalid response."
        case .staleAccount:
            return "The push request belongs to an older account session."
        case .generationExhausted:
            return "The push generation limit has been reached on this installation."
        }
    }
}

@MainActor
final class NativePushCoordinator {
    typealias ManagementRequest = @MainActor (
        PushManagementRequest
    ) async throws -> PushManagementResponse
    typealias AccountProvider = @MainActor () -> MobileAccount?
    typealias RemoteRegistration = @MainActor () -> Void
    typealias TapHandler = @MainActor (PushNotificationTap) -> Void
    typealias TapRecoveryHandler = @MainActor () -> Void
    typealias RequestNotificationAuthorization = @MainActor () async throws -> Bool
    typealias NotificationCleanup = @MainActor (String) async -> Void

    private static let publicBaseURL = "https://avibe.bot"
    private static let topic = "bot.avibe.ios"

    private final class TokenWaiter {
        let id = UUID()
        var continuation: CheckedContinuation<Void, Error>?
        var timeoutTask: Task<Void, Never>?
        var finished = false
    }

    private let keychain: PushKeychainStoring
    private let generationStore: PushGenerationStoring
    private let managementRequest: ManagementRequest
    private let accountProvider: AccountProvider
    private let registerForRemoteNotifications: RemoteRegistration
    private let requestNotificationAuthorization: RequestNotificationAuthorization
    private let onTap: TapHandler
    private let onTapRecovery: TapRecoveryHandler
    private let installationMarker: PushInstallationMarkerStoring
    private let removeAccountNotifications: NotificationCleanup
    private let tokenWaitSeconds: Double

    private var tokenWaiters: [UUID: TokenWaiter] = [:]
    private var isFrozen = false
    private var mutationTask: Task<Void, Never>?
    private var providerPatchTask: Task<Void, Error>?
    private var recoveryTask: Task<Void, Never>?

    init(
        keychain: PushKeychainStoring = PushKeychainStore(),
        generationStore: PushGenerationStoring = SharedPushGenerationStore(),
        managementRequest: @escaping ManagementRequest,
        accountProvider: @escaping AccountProvider,
        registerForRemoteNotifications: @escaping RemoteRegistration = {
            UIApplication.shared.registerForRemoteNotifications()
        },
        requestNotificationAuthorization: @escaping RequestNotificationAuthorization = {
            try await UNUserNotificationCenter.current().requestAuthorization(
                options: [.alert, .badge, .sound]
            )
        },
        installationMarker: PushInstallationMarkerStoring =
            UserDefaultsPushInstallationMarkerStore(),
        removeAccountNotifications: @escaping NotificationCleanup = { accountID in
            await NativePushCoordinator.removeNotifications(for: accountID)
        },
        tokenWaitSeconds: Double = 15,
        onTap: @escaping TapHandler = { _ in },
        onTapRecovery: @escaping TapRecoveryHandler = {}
    ) {
        self.keychain = keychain
        self.generationStore = generationStore
        self.managementRequest = managementRequest
        self.accountProvider = accountProvider
        self.registerForRemoteNotifications = registerForRemoteNotifications
        self.requestNotificationAuthorization = requestNotificationAuthorization
        self.installationMarker = installationMarker
        self.removeAccountNotifications = removeAccountNotifications
        self.tokenWaitSeconds = tokenWaitSeconds
        self.onTap = onTap
        self.onTapRecovery = onTapRecovery
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(didReceiveDeviceToken(_:)),
            name: .avibePushDidRegister,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(didFailDeviceTokenRegistration),
            name: .avibePushDidFailToRegister,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(didReceiveNotificationTap(_:)),
            name: .avibePushNotificationTapped,
            object: nil
        )
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
        mutationTask?.cancel()
        providerPatchTask?.cancel()
        recoveryTask?.cancel()
        for waiter in tokenWaiters.values {
            waiter.timeoutTask?.cancel()
            waiter.continuation?.resume(throwing: NativePushError.unavailable)
        }
    }

    @objc
    private func didReceiveDeviceToken(_ notification: Notification) {
        guard let token = notification.userInfo?["deviceToken"] as? Data else {
            return
        }
        #if DEBUG
        let environment = "sandbox"
        #else
        let environment = "production"
        #endif
        didRegister(deviceToken: token, environment: environment)
    }

    @objc
    private func didFailDeviceTokenRegistration() {
        didFailToRegister()
    }

    @objc
    private func didReceiveNotificationTap(_ notification: Notification) {
        guard
            let payload = notification.userInfo?["payload"]
                as? [AnyHashable: Any]
        else {
            return
        }
        handleNotificationTap(userInfo: payload)
    }

    func status(
        vapidPublicKey: String?,
        instanceOrigin: String
    ) throws -> [String: Any] {
        guard let account = accountProvider() else {
            return ["state": "unavailable"]
        }
        guard
            let installation = try keychain.loadInstallation(),
            installation.accountID == account.id,
            installation.bindGeneration > 0
        else {
            return ["state": "off"]
        }
        guard
            let vapidPublicKey,
            let vapidKeyHash = Self.vapidHash(vapidPublicKey)
        else {
            return ["state": "off"]
        }
        guard
            let record = try keychain.listRecords().first(where: {
                $0.state == .active
                    && !$0.pendingRevoke
                    && $0.accountID == account.id
                    && $0.bindGeneration == installation.bindGeneration
                    && $0.accountGeneration == generationStore.generation
                    && $0.instanceOrigin == instanceOrigin
                    && $0.vapidKeyHash == vapidKeyHash
            })
        else {
            return ["state": "off"]
        }
        return [
            "state": "on",
            "endpoint": "\(Self.publicBaseURL)/api/push/apns/\(record.kid)",
            "keys": record.publicSubscription["keys"] ?? [:],
        ]
    }

    func subscribe(
        vapidPublicKey: String,
        instanceOrigin: String
    ) async throws -> [String: Any] {
        try await enqueueMutation {
            try await self.subscribeNow(
                vapidPublicKey: vapidPublicKey,
                instanceOrigin: instanceOrigin
            )
        }
    }

    private func subscribeNow(
        vapidPublicKey: String,
        instanceOrigin: String
    ) async throws -> [String: Any] {
        guard !isFrozen else {
            throw NativePushError.unavailable
        }
        guard Self.vapidHash(vapidPublicKey) != nil else {
            throw NativePushError.invalidVapidKey
        }
        guard let account = accountProvider() else {
            throw NativePushError.unavailable
        }

        let installation = try await ensureBound(to: account)
        try await reconcilePendingOperations(for: installation)
        try await requestPermission()
        let provider = try await ensureProvider(installation: installation)
        guard provider.generation > 0 else {
            throw NativePushError.unavailable
        }

        let vapidKeyHash = try Self.requireVapidHash(vapidPublicKey)
        let privateKey = P256.KeyAgreement.PrivateKey()
        let authSecret = try Self.randomBytes(count: 16)
        let kid = PushBase64URL.encode(try Self.randomBytes(count: 32))
        let replacementIDs = try keychain.listRecords()
            .filter {
                $0.state == .active
                    && !$0.pendingRevoke
                    && $0.accountID == account.id
                    && $0.bindGeneration == installation.bindGeneration
                    && $0.instanceOrigin == instanceOrigin
            }
            .map(\.kid)
        var record = PushKeyRecord(
            kid: kid,
            instanceOrigin: instanceOrigin,
            accountID: account.id,
            accountGeneration: generationStore.generation,
            bindGeneration: installation.bindGeneration,
            privateKey: privateKey.rawRepresentation,
            authSecret: authSecret,
            vapidKeyHash: vapidKeyHash,
            state: .pending,
            pendingRevoke: false,
            replacementRegistrationIDs: replacementIDs
        )
        try keychain.saveRecord(record)

        do {
            let response = try await request(
                method: "PUT",
                path: "/api/push/apns/registrations/\(kid)",
                installation: installation,
                body: [
                    "bind_generation": installation.bindGeneration,
                    "vapid_key_sha256": vapidKeyHash,
                    "replaces_registration_ids": replacementIDs,
                ]
            )
            guard (200..<300).contains(response.statusCode) else {
                throw Self.managementError(response)
            }
            try completeRegistration(record)
            return [
                "endpoint": "\(Self.publicBaseURL)/api/push/apns/\(kid)",
                "keys": record.publicSubscription["keys"] ?? [:],
            ]
        } catch {
            try? keychain.saveRecord(record)
            throw error
        }
    }

    func unsubscribe(instanceOrigin: String) async throws -> Bool {
        try await enqueueMutation {
            try await self.unsubscribeNow(instanceOrigin: instanceOrigin)
        }
    }

    private func unsubscribeNow(instanceOrigin: String) async throws -> Bool {
        guard let account = accountProvider() else {
            throw NativePushError.unavailable
        }
        guard
            let installation = try keychain.loadInstallation(),
            installation.accountID == account.id
        else {
            return false
        }
        guard
            let record = try keychain.listRecords().first(where: {
                (
                    ($0.state == .active && !$0.pendingRevoke)
                        || ($0.state == .retiring && $0.pendingRevoke)
                )
                    && $0.accountID == account.id
                    && $0.bindGeneration == installation.bindGeneration
                    && $0.accountGeneration == generationStore.generation
                    && $0.instanceOrigin == instanceOrigin
            })
        else {
            return false
        }

        let pendingRecord = PushKeyRecord(
            kid: record.kid,
            instanceOrigin: record.instanceOrigin,
            accountID: record.accountID,
            accountGeneration: record.accountGeneration,
            bindGeneration: record.bindGeneration,
            privateKey: Data(),
            authSecret: Data(),
            vapidKeyHash: record.vapidKeyHash,
            state: .retiring,
            pendingRevoke: true,
            replacementRegistrationIDs: []
        )
        try keychain.saveRecord(pendingRecord)
        do {
            let response = try await request(
                method: "DELETE",
                path: "/api/push/apns/registrations/\(pendingRecord.kid)",
                installation: installation,
                body: nil
            )
            guard (200..<300).contains(response.statusCode) else {
                throw Self.managementError(response)
            }
            try keychain.deleteRecord(kid: pendingRecord.kid)
            return true
        } catch {
            return false
        }
    }

    func didRegister(deviceToken: Data, environment: String) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            do {
                var installation = try loadOrCreateInstallation()
                let nextGeneration = try Self.nextGeneration(
                    after: installation.providerGeneration
                )
                installation.providerGeneration = nextGeneration
                installation.provider = PushProviderState(
                    deviceToken: deviceToken,
                    environment: environment,
                    topic: Self.topic,
                    generation: nextGeneration
                )
                installation.providerPatchPending = true
                try keychain.saveInstallation(installation)
                resumeTokenWaiters()
                if installation.accountID != nil {
                    _ = try? await enqueueMutation {
                        guard !self.isFrozen else {
                            return
                        }
                        try await self.reconcileProvider()
                    }
                }
            } catch {
                resumeTokenWaiters(with: error)
            }
        }
    }

    func didFailToRegister() {
        resumeTokenWaiters(with: NativePushError.unavailable)
    }

    func prepareForLogout(throughBindGeneration: Int64) async {
        _ = try? await enqueueMutation {
            await self.prepareForLogoutNow(
                throughBindGeneration: throughBindGeneration
            )
        }
    }

    private func prepareForLogoutNow(throughBindGeneration: Int64) async {
        guard !isFrozen else { return }
        isFrozen = true
        guard var installation = try? loadOrCreateInstallation() else {
            advanceAccountGeneration()
            return
        }
        guard
            throughBindGeneration > 0,
            throughBindGeneration <= installation.bindGeneration
        else {
            advanceAccountGeneration()
            return
        }
        installation.pendingBulkRevokeThroughGeneration = throughBindGeneration
        try? keychain.saveInstallation(installation)
        advanceAccountGeneration()
        let accountID = installation.accountID

        let createsReconciled =
            (try? await reconcileRegistrationCreates(
                for: installation,
                throughBindGeneration: throughBindGeneration
            )) == true
        installation.accountID = nil
        zeroAccountKeys(
            throughBindGeneration: throughBindGeneration
        )
        if let accountID {
            await removeAccountNotifications(accountID)
        }
        try? keychain.saveInstallation(installation)
        guard createsReconciled else {
            return
        }
        do {
            try await sendPendingBulkRevoke(
                installation: installation,
                throughBindGeneration: throughBindGeneration
            )
        } catch {
            // The persisted bulk revoke remains for the next execution opportunity.
        }
    }

    func prepareForLogout() async {
        _ = try? await enqueueMutation {
            guard let installation = try? self.loadOrCreateInstallation(),
                  installation.bindGeneration > 0
            else {
                self.isFrozen = true
                self.advanceAccountGeneration()
                return
            }
            await self.prepareForLogoutNow(
                throughBindGeneration: installation.bindGeneration
            )
        }
    }

    func handleNotificationTap(userInfo: [AnyHashable: Any]) {
        guard
            let accountID = userInfo["account_id"] as? String,
            let instanceOrigin = userInfo["instance_origin"] as? String,
            let url = userInfo["url"] as? String,
            Self.isSafePath(url)
        else {
            onTapRecovery()
            return
        }
        onTap(
            PushNotificationTap(
                accountID: accountID,
                instanceOrigin: instanceOrigin,
                url: url
            )
        )
    }

    func resumeAfterAccountActivation() {
        isFrozen = false
        startRecovery()
    }

    private func ensureBound(to account: MobileAccount) async throws -> PushInstallationState {
        var installation = try loadOrCreateInstallation()
        let isRetry = installation.accountID == account.id
            && installation.bindGeneration > 0
        if !isRetry {
            let nextGeneration = try Self.nextGeneration(
                after: installation.bindGeneration
            )
            try advanceAccountGenerationOrThrow()
            installation.accountID = account.id
            installation.bindGeneration = nextGeneration
            try keychain.saveInstallation(installation)
        }
        let response = try await request(
            method: "PUT",
            path: "/api/push/apns/installations/\(installation.installationID.uuidString)/binding",
            installation: installation,
            body: ["bind_generation": installation.bindGeneration]
        )
        guard (200..<300).contains(response.statusCode) else {
            throw Self.managementError(response)
        }
        return installation
    }

    private func reconcilePendingOperations(
        for installation: PushInstallationState
    ) async throws {
        try await reconcilePendingRevokes(for: installation)
        try await reconcileRegistrationCreates(
            for: installation,
            throughBindGeneration: installation.bindGeneration
        )
        try await reconcilePersistedBulkRevoke(for: installation)
    }

    @discardableResult
    private func reconcileRegistrationCreates(
        for installation: PushInstallationState,
        throughBindGeneration: Int64
    ) async throws -> Bool {
        let records = try keychain.listRecords()
        for var record in records where
            record.state == .pending
                && record.bindGeneration <= throughBindGeneration
        {
            while true {
                let response = try await request(
                    method: "PUT",
                    path: "/api/push/apns/registrations/\(record.kid)",
                    installation: installation,
                    body: [
                        "bind_generation": record.bindGeneration,
                        "vapid_key_sha256": record.vapidKeyHash,
                        "replaces_registration_ids": record.replacementRegistrationIDs,
                    ]
                )
                if response.statusCode == 409,
                   let error = response.body["error"] as? [String: Any],
                   error["code"] as? String == "replacement_set_changed",
                   let activeIDs = response.body["active_registration_ids"] as? [String]
                {
                    record.replacementRegistrationIDs = activeIDs
                    try keychain.saveRecord(record)
                    continue
                }
                if response.statusCode == 409,
                   Self.errorCode(response) == "stale_generation"
                {
                    try keychain.deleteRecord(kid: record.kid)
                    break
                }
                if response.statusCode == 410,
                   Self.errorCode(response) == "registration_revoked"
                {
                    try keychain.deleteRecord(kid: record.kid)
                    break
                }
                guard (200..<300).contains(response.statusCode) else {
                    throw Self.managementError(response)
                }
                try completeRegistration(record)
                break
            }
        }
        return true
    }

    private func reconcilePendingRevokes(
        for installation: PushInstallationState
    ) async throws {
        let records = try keychain.listRecords()
        for record in records where
            record.state == .retiring && record.pendingRevoke
        {
            let response = try await request(
                method: "DELETE",
                path: "/api/push/apns/registrations/\(record.kid)",
                installation: installation,
                body: nil
            )
            if (200..<300).contains(response.statusCode)
                || (
                    response.statusCode == 404
                        && Self.errorCode(response) == "registration_not_found"
                )
            {
                try keychain.deleteRecord(kid: record.kid)
                continue
            }
            throw Self.managementError(response)
        }
    }

    private func completeRegistration(_ record: PushKeyRecord) throws {
        for replacementID in record.replacementRegistrationIDs {
            guard replacementID != record.kid,
                  var replacement = try keychain.loadRecord(kid: replacementID)
            else {
                continue
            }
            replacement.state = .retiring
            replacement.pendingRevoke = false
            replacement.replacementRegistrationIDs = []
            try keychain.saveRecord(replacement)
        }
        var active = record
        active.state = .active
        active.pendingRevoke = false
        active.replacementRegistrationIDs = []
        try keychain.saveRecord(active)
    }

    private func requestPermission() async throws {
        let granted = try await requestNotificationAuthorization()
        guard granted else {
            throw NativePushError.permissionDenied
        }
        registerForRemoteNotifications()
    }

    private func ensureProvider(
        installation: PushInstallationState
    ) async throws -> PushProviderState {
        if installation.provider != nil {
            try await reconcileProvider()
            let updated = try loadOrCreateInstallation()
            guard
                let provider = updated.provider,
                !updated.providerPatchPending
            else {
                throw NativePushError.unavailable
            }
            return provider
        }
        try await waitForProviderToken()
        let updated = try loadOrCreateInstallation()
        guard updated.provider != nil else {
            throw NativePushError.unavailable
        }
        try await reconcileProvider()
        let reconciled = try loadOrCreateInstallation()
        guard
            let provider = reconciled.provider,
            !reconciled.providerPatchPending
        else {
            throw NativePushError.unavailable
        }
        return provider
    }

    private func reconcileProvider() async throws {
        startProviderReconciliation()
        try await providerPatchTask?.value
    }

    private func startProviderReconciliation() {
        guard providerPatchTask == nil else {
            return
        }
        providerPatchTask = Task { @MainActor [weak self] in
            guard let self else {
                return
            }
            defer { self.providerPatchTask = nil }
            while true {
                let installation = try self.loadOrCreateInstallation()
                guard
                    installation.providerPatchPending,
                    let provider = installation.provider
                else {
                    return
                }
                let response = try await self.request(
                    method: "PATCH",
                    path: "/api/push/apns/installations/\(installation.installationID.uuidString)/provider",
                    installation: installation,
                    body: [
                        "provider_generation": provider.generation,
                        "device_token": PushBase64URL.encode(provider.deviceToken),
                        "environment": provider.environment,
                        "topic": provider.topic,
                    ]
                )
                var current = try self.loadOrCreateInstallation()
                if (200..<300).contains(response.statusCode) {
                    if current.provider?.generation == provider.generation {
                        current.providerPatchPending = false
                        try self.keychain.saveInstallation(current)
                        return
                    }
                    continue
                }
                if response.statusCode == 401 || response.statusCode == 502 {
                    throw Self.managementError(response)
                }
                if response.statusCode == 409 {
                    guard current.provider?.generation == provider.generation else {
                        throw Self.managementError(response)
                    }
                    let serverGeneration = Self.int64(
                        response.body["provider_generation"]
                    ) ?? provider.generation
                    let nextGeneration = try Self.nextGeneration(
                        after: max(current.providerGeneration, serverGeneration)
                    )
                    current.providerGeneration = nextGeneration
                    current.provider = PushProviderState(
                        deviceToken: provider.deviceToken,
                        environment: provider.environment,
                        topic: provider.topic,
                        generation: nextGeneration
                    )
                    current.providerPatchPending = true
                    try self.keychain.saveInstallation(current)
                    throw Self.managementError(response)
                }
                throw Self.managementError(response)
            }
        }
    }

    private func request(
        method: String,
        path: String,
        installation: PushInstallationState,
        body: [String: Any]?
    ) async throws -> PushManagementResponse {
        let data = body.flatMap {
            try? JSONSerialization.data(withJSONObject: $0, options: [.sortedKeys])
        }
        guard body == nil || data != nil else {
            throw NativePushError.invalidResponse
        }
        let response = try await managementRequest(
            PushManagementRequest(
                method: method,
                path: path,
                headers: [
                    "Authorization": "AvibeInstallation \(installation.installationID.uuidString).\(PushBase64URL.encode(installation.installationSecret))",
                    "Content-Type": "application/json",
                ],
                body: data
            )
        )
        return response
    }

    private func loadOrCreateInstallation() throws -> PushInstallationState {
        if let existing = try keychain.loadInstallation() {
            guard installationMarker.installationID == existing.installationID else {
                let orphan = PushOrphanedInstallation(
                    installationID: existing.installationID,
                    installationSecret: existing.installationSecret,
                    throughBindGeneration: existing.bindGeneration
                )
                var orphans = try keychain.loadOrphanedInstallations()
                if !orphans.contains(orphan) {
                    orphans.append(orphan)
                    try keychain.saveOrphanedInstallations(orphans)
                }
                for record in try keychain.listRecords() {
                    try keychain.deleteRecord(kid: record.kid)
                }
                let fresh = try makeFreshInstallation()
                try keychain.saveInstallation(fresh)
                installationMarker.installationID = fresh.installationID
                return fresh
            }
            return existing
        }
        let installation = try makeFreshInstallation()
        try keychain.saveInstallation(installation)
        installationMarker.installationID = installation.installationID
        return installation
    }

    private func makeFreshInstallation() throws -> PushInstallationState {
        PushInstallationState(
            installationID: UUID(),
            installationSecret: try Self.randomBytes(count: 32),
            accountID: nil,
            bindGeneration: 0,
            providerGeneration: 0,
            provider: nil,
            providerPatchPending: false,
            pendingBulkRevokeThroughGeneration: nil
        )
    }

    private func waitForProviderToken() async throws {
        let waiter = TokenWaiter()
        try await withTaskCancellationHandler(
            operation: {
                try await withCheckedThrowingContinuation {
                    (continuation: CheckedContinuation<Void, Error>) in
                    guard !Task.isCancelled else {
                        continuation.resume(throwing: CancellationError())
                        return
                    }
                    waiter.continuation = continuation
                    tokenWaiters[waiter.id] = waiter
                    waiter.timeoutTask = Task { @MainActor [weak self, weak waiter] in
                        do {
                            try await Task.sleep(
                                nanoseconds: UInt64(
                                    tokenWaitSeconds * 1_000_000_000
                                )
                            )
                        } catch {
                            return
                        }
                        guard let self, let waiter else {
                            return
                        }
                        self.finishTokenWaiter(
                            waiter,
                            error: NativePushError.unavailable
                        )
                    }
                }
            },
            onCancel: { [weak self, weak waiter] in
                Task { @MainActor in
                    self?.finishTokenWaiter(
                        waiter,
                        error: CancellationError()
                    )
                }
            }
        )
    }

    private func finishTokenWaiter(
        _ waiter: TokenWaiter?,
        error: Error? = nil
    ) {
        guard let waiter, !waiter.finished else {
            return
        }
        waiter.finished = true
        tokenWaiters.removeValue(forKey: waiter.id)
        waiter.timeoutTask?.cancel()
        let continuation = waiter.continuation
        waiter.continuation = nil
        if let error {
            continuation?.resume(throwing: error)
        } else {
            continuation?.resume()
        }
    }

    private func startRecovery() {
        guard recoveryTask == nil else {
            return
        }
        recoveryTask = Task { @MainActor [weak self] in
            guard let self else {
                return
            }
            defer { self.recoveryTask = nil }
            _ = try? await self.enqueueMutation {
                _ = try? self.loadOrCreateInstallation()
                try? await self.reconcileOrphanedInstallations()
                guard let installation = try? self.loadOrCreateInstallation() else {
                    return
                }
                try? await self.reconcilePersistedBulkRevoke(for: installation)
                try? await self.reconcilePendingRevokes(for: installation)
            }
        }
    }

    private func reconcileOrphanedInstallations() async throws {
        var orphans = try keychain.loadOrphanedInstallations()
        var remaining: [PushOrphanedInstallation] = []
        for orphan in orphans {
            let installation = PushInstallationState(
                installationID: orphan.installationID,
                installationSecret: orphan.installationSecret,
                accountID: nil,
                bindGeneration: orphan.throughBindGeneration,
                providerGeneration: 0,
                provider: nil,
                pendingBulkRevokeThroughGeneration: nil
            )
            let response = try await request(
                method: "POST",
                path: "/api/push/apns/installations/\(orphan.installationID.uuidString)/revoke",
                installation: installation,
                body: [
                    "through_bind_generation": orphan.throughBindGeneration,
                ]
            )
            if (200..<300).contains(response.statusCode)
                || (
                    response.statusCode == 404
                        && Self.errorCode(response) == "installation_not_found"
                )
            {
                continue
            }
            remaining.append(orphan)
        }
        orphans = remaining
        try keychain.saveOrphanedInstallations(orphans)
    }

    private func reconcilePersistedBulkRevoke(
        for installation: PushInstallationState
    ) async throws {
        guard
            let through = installation.pendingBulkRevokeThroughGeneration,
            through > 0
        else {
            return
        }
        try await reconcileRegistrationCreates(
            for: installation,
            throughBindGeneration: through
        )
        try await sendPendingBulkRevoke(
            installation: installation,
            throughBindGeneration: through
        )
    }

    private func sendPendingBulkRevoke(
        installation: PushInstallationState,
        throughBindGeneration: Int64
    ) async throws {
        let response = try await request(
            method: "POST",
            path: "/api/push/apns/installations/\(installation.installationID.uuidString)/revoke",
            installation: installation,
            body: ["through_bind_generation": throughBindGeneration]
        )
        guard (200..<300).contains(response.statusCode) else {
            throw Self.managementError(response)
        }
        var current = try loadOrCreateInstallation()
        if current.pendingBulkRevokeThroughGeneration
            == throughBindGeneration
        {
            current.pendingBulkRevokeThroughGeneration = nil
            try keychain.saveInstallation(current)
        }
        for record in try keychain.listRecords()
            where record.bindGeneration <= throughBindGeneration
        {
            try keychain.deleteRecord(kid: record.kid)
        }
    }

    private func zeroAccountKeys(throughBindGeneration: Int64) {
        guard let records = try? keychain.listRecords() else {
            return
        }
        for record in records where record.bindGeneration <= throughBindGeneration {
            let state: PushKeyRecord.State =
                record.state == .active ? .retiring : record.state
            let pendingRevoke = state == .retiring
                ? true
                : record.pendingRevoke
            let cleared = PushKeyRecord(
                kid: record.kid,
                instanceOrigin: record.instanceOrigin,
                accountID: record.accountID,
                accountGeneration: record.accountGeneration,
                bindGeneration: record.bindGeneration,
                privateKey: Data(),
                authSecret: Data(),
                vapidKeyHash: record.vapidKeyHash,
                state: state,
                pendingRevoke: pendingRevoke,
                replacementRegistrationIDs: record.replacementRegistrationIDs
            )
            try? keychain.saveRecord(cleared)
        }
    }

    private static func removeNotifications(for accountID: String) async {
        let center = UNUserNotificationCenter.current()
        let delivered: [UNNotification] = await withCheckedContinuation { continuation in
            center.getDeliveredNotifications { notifications in
                continuation.resume(returning: notifications)
            }
        }
        let pending: [UNNotificationRequest] = await withCheckedContinuation {
            continuation in
            center.getPendingNotificationRequests { requests in
                continuation.resume(returning: requests)
            }
        }
        let deliveredIDs = delivered.compactMap { notification in
            notification.request.content.userInfo["account_id"] as? String
                == accountID
                ? notification.request.identifier
                : nil
        }
        let pendingIDs = pending.compactMap { request in
            request.content.userInfo["account_id"] as? String == accountID
                ? request.identifier
                : nil
        }
        center.removeDeliveredNotifications(withIdentifiers: deliveredIDs)
        center.removePendingNotificationRequests(withIdentifiers: pendingIDs)
    }

    private func resumeTokenWaiters(with error: Error? = nil) {
        let waiters = Array(tokenWaiters.values)
        for waiter in waiters {
            finishTokenWaiter(
                waiter,
                error: error
            )
        }
    }

    private func enqueueMutation<Value>(
        _ operation: @escaping @MainActor () async throws -> Value
    ) async throws -> Value {
        let previous = mutationTask
        let current: Task<Value, Error> = Task { @MainActor in
            await previous?.value
            return try await operation()
        }
        mutationTask = Task { @MainActor in
            _ = try? await current.value
        }
        return try await current.value
    }

    private func advanceAccountGenerationOrThrow() throws {
        guard generationStore.generation < Int64.max else {
            throw NativePushError.generationExhausted
        }
        generationStore.generation = max(generationStore.generation + 1, 1)
    }

    private func advanceAccountGeneration() {
        guard generationStore.generation < Int64.max else {
            return
        }
        generationStore.generation = max(generationStore.generation + 1, 1)
    }

    private static func managementError(
        _ response: PushManagementResponse
    ) -> NativePushError {
        NativePushError.management(
            status: response.statusCode,
            code: response.body["error"] as? String
                ?? (response.body["error"] as? [String: Any])?["code"] as? String
        )
    }

    private static func errorCode(
        _ response: PushManagementResponse
    ) -> String? {
        response.body["error"] as? String
            ?? (response.body["error"] as? [String: Any])?["code"] as? String
    }

    private static func int64(_ value: Any?) -> Int64? {
        if let value = value as? Int64 {
            return value
        }
        if let value = value as? NSNumber {
            return value.int64Value
        }
        if let value = value as? Int {
            return Int64(value)
        }
        return nil
    }

    private static func nextGeneration(after current: Int64) throws -> Int64 {
        guard current < Int64.max else {
            throw NativePushError.generationExhausted
        }
        return max(current + 1, 1)
    }

    private static func requireVapidHash(_ value: String) throws -> String {
        guard let hash = vapidHash(value) else {
            throw NativePushError.invalidVapidKey
        }
        return hash
    }

    private static func vapidHash(_ value: String) -> String? {
        guard
            let decoded = PushBase64URL.decode(value),
            decoded.count == 65,
            decoded.first == 4
        else {
            return nil
        }
        let digest = SHA256.hash(data: decoded)
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    private static func randomBytes(count: Int) throws -> Data {
        var data = Data(count: count)
        let status = data.withUnsafeMutableBytes {
            SecRandomCopyBytes(kSecRandomDefault, count, $0.baseAddress!)
        }
        guard status == errSecSuccess else {
            throw NativePushError.unavailable
        }
        return data
    }

    private static func isSafePath(_ value: String) -> Bool {
        guard
            !value.isEmpty,
            value.first == "/",
            !value.contains("\\"),
            !value.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
        else {
            return false
        }
        guard
            let components = URLComponents(string: value),
            components.scheme == nil,
            components.host == nil,
            components.user == nil,
            components.password == nil
        else {
            return false
        }
        return true
    }
}
