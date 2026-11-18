import Foundation
import SwiftUI
import UIKit
import Security

struct HealthConnectionTarget: Equatable {
    let accountID: String
    let instanceID: String
    let name: String
    let origin: String
}

struct HealthConsentRequest: Identifiable {
    let id = UUID()
    let target: HealthConnectionTarget
}

@MainActor
final class HealthSyncCoordinator: ObservableObject {
    @Published private(set) var consent: HealthConsentRequest?
    @Published private(set) var notice: String?
    private let credentials: HealthCredentialStoring
    private let reader: HealthDataReading
    private let client: HealthDeviceRequesting
    private let uploader: HealthUploading
    private let directory: URL
    private let now: () -> Date
    private var store: HealthDeviceStore?
    private var identity: HealthDeviceIdentity?
    // A committed identity owns ACKs across temporary account authorization pauses.
    private var committedEpoch: UUID?
    private var uploadEpochs: [String: UUID] = [:]
    private var authorizationPaused = false
    private var epoch = UUID()
    private var consentContinuation: CheckedContinuation<Bool, Never>?
    private var connecting: UUID?
    private var cleanupTask: Task<Void, Never>?
    private var startupBindingUntrusted = false
    var websiteDataStoreIDProvider: (() -> UUID?)?
    var protectedDataAvailable: () -> Bool = { UIApplication.shared.isProtectedDataAvailable }
    private var foreground = false
    private var restoring = false
    private var uploadingIDs: Set<String> = []
    private var backgroundDelivery = false
    private var pollTask: Task<Void, Never>?
    private var syncTask: Task<[String: String], Error>?
    private var syncID: UUID?
    private var retryTask: Task<Void, Never>?
    private var uploadWaiters: [String: [CheckedContinuation<Void, Error>]] = [:]
    private var retryAfter: Date?
    var targetProvider: (() -> HealthConnectionTarget?)?
    var accountIDProvider: (() -> String?)?
    private var cleanupPending = false

    private var currentAccountID: String? {
        if let accountIDProvider { return accountIDProvider() }
        return targetProvider?()?.accountID
    }

    private func requireAccount() throws {
        guard let identity, currentAccountID == identity.accountID, !cleanupPending,
              !startupBindingUntrusted, bindingMatches(identity),
              store?.state.committedDeviceID == identity.deviceID else {
            throw HealthSyncError.code("connection_changed")
        }
    }

    init(credentials: HealthCredentialStoring = HealthCredentialStore(),
         reader: HealthDataReading? = nil,
         client: HealthDeviceRequesting = HealthDeviceClient(),
         uploader: HealthUploading = HealthBackgroundUploader(),
         directory: URL = URL.applicationSupportDirectory.appendingPathComponent("HealthSync", isDirectory: true),
         now: @escaping () -> Date = Date.init) {
        self.credentials = credentials
        self.reader = reader ?? HealthDataReader()
        self.client = client
        self.uploader = uploader
        self.directory = directory
        self.now = now
        uploader.onComplete = { [weak self] id, result in
            await self?.uploadCompleted(id: id, result: result)
        }
    }

    var backgroundUploader: HealthBackgroundUploader? { uploader as? HealthBackgroundUploader }

    func dismissNotice() { notice = nil }

    func answerConsent(_ allowed: Bool) {
        let continuation = consentContinuation
        consentContinuation = nil
        consent = nil
        continuation?.resume(returning: allowed)
    }

    /// Capture before account-store construction; an AFU Keychain read can
    /// distinguish first-unlock protection from an ordinary later lock.
    func captureStartupReadiness() {
        do { _ = try credentials.load() }
        catch HealthCredentialReadError.status(let status) where status == errSecInteractionNotAllowed {
            startupBindingUntrusted = true
        } catch { /* Restore classifies corruption and other read failures. */ }
    }

    private func bindingMatches(_ candidate: HealthDeviceIdentity) -> Bool {
        guard let binding = candidate.websiteDataStoreID else { return false }
        return websiteDataStoreIDProvider?() == binding
    }

    func accountEvent(_ event: HealthAccountEvent) {
        switch event {
        case .signedOut(let explicit):
            if startupBindingUntrusted && !explicit {
                guard protectedDataAvailable() else { return }
                startupBindingUntrusted = false
            }
            _ = beginCleanup(unregister: true)
        case .authorizationUnavailable:
            guard !cleanupPending else { return }
            if connecting != nil { _ = beginCleanup(unregister: true) }
            else { pauseAuthority() }
        case .authenticated(let accountID, let instances):
            if startupBindingUntrusted {
                guard protectedDataAvailable() else { return }
                startupBindingUntrusted = false
            }
            if let identity, identity.accountID != accountID || !bindingMatches(identity)
                || (!instances.isEmpty && !instances.contains(identity.instanceID)) {
                _ = beginCleanup(unregister: true)
            } else if identity != nil, authorizationPaused {
                let token = epoch
                Task { @MainActor [weak self] in
                    guard let self, self.epoch == token else { return }
                    self.resumeAuthorizedConnection()
                }
            }
        }
    }

    private func resumeAuthorizedConnection() {
        guard authorizationPaused, !restoring, connecting == nil, !cleanupPending,
              (try? requireAccount()) != nil else { return }
        authorizationPaused = false
        let token = epoch
        Task { @MainActor [weak self] in
            guard let self, self.epoch == token, self.connecting == nil else { return }
            await self.resume(foreground: self.foreground)
        }
    }

    private func pauseAuthority() {
        authorizationPaused = true
        epoch = UUID()
        reader.stopObserving()
        uploader.invalidate()
        uploadingIDs.removeAll()
        pollTask?.cancel()
        pollTask = nil
        syncTask?.cancel()
        syncTask = nil
        syncID = nil
        retryTask?.cancel()
        retryTask = nil
        for id in Array(uploadWaiters.keys) {
            finishWaiters(id, result: .failure(HealthSyncError.code("connection_changed")))
        }
        let previous = cleanupTask
        cleanupTask = Task { @MainActor in
            await previous?.value
            await self.uploader.cancelAll()
            _ = await self.reader.disableBackgroundDelivery()
        }
    }

    func connect(requestID: String, pairingCode: String, origin: WebOrigin) async -> String {
        guard connecting == nil, !startupBindingUntrusted,
              let binding = websiteDataStoreIDProvider?(),
              let target = targetProvider?(), currentAccountID == target.accountID,
              target.origin == origin.serialized,
              !requestID.isEmpty, !pairingCode.isEmpty else { return "failed" }
        let connection = UUID()
        connecting = connection
        defer { if connecting == connection { connecting = nil } }
        let token = epoch
        let approved = await withCheckedContinuation { continuation in
            consentContinuation = continuation
            consent = HealthConsentRequest(target: target)
        }
        guard epoch == token, targetProvider?() == target else { return "failed" }
        guard approved else { return "declined" }
        var operationEpoch = token
        var provisional: HealthDeviceIdentity?
        var committed = false
        do {
            try await reader.authorize()
            try requireCurrent(token)
            let installationID = try credentials.installationID()
            let wasForeground = foreground
            operationEpoch = beginCleanup(unregister: true)
            connecting = connection
            await cleanupTask?.value
            // This connect still owns its generation after replacing the old device.
            guard connecting == nil || connecting == connection else { throw HealthSyncError.code("connection_changed") }
            connecting = connection
            try requireCurrent(operationEpoch)
            guard !cleanupPending, targetProvider?() == target, currentAccountID == target.accountID,
                  websiteDataStoreIDProvider?() == binding else { throw HealthSyncError.code("connection_changed") }
            foreground = wasForeground
            let response = try await client.request(origin: target.origin, credential: nil, method: "POST",
                path: "/api/mobile/device/v1/pair", body: [
                    "pairing_code": pairingCode, "installation_id": installationID,
                    "device_name": UIDevice.current.name, "platform": "ios",
                    "app_version": Self.appVersion, "metrics": HealthQueryWindow.metrics,
                ])
            guard let deviceID = response["device_id"] as? String, !deviceID.isEmpty,
                  let credential = response["credential"] as? String, !credential.isEmpty else {
                throw HealthSyncError.code("invalid_pair")
            }
            let candidate = HealthDeviceIdentity(installationID: installationID,
                accountID: target.accountID, instanceID: target.instanceID,
                instanceName: target.name, origin: target.origin, deviceID: deviceID,
                credential: credential, websiteDataStoreID: binding)
            provisional = candidate
            guard epoch == operationEpoch else {
                await revoke(candidate)
                return "failed"
            }
            guard targetProvider?() == target, currentAccountID == target.accountID,
                  let instance = response["instance"] as? [String: Any],
                  instance["id"] as? String == target.instanceID else { throw HealthSyncError.code("invalid_pair") }
            let provisionalStore = try HealthDeviceStore(directory: directory)
            try provisionalStore.update { $0 = HealthSyncState() }
            store = provisionalStore
            try credentials.save(candidate)
            func setup(_ method: String, _ path: String, _ body: [String: Any]) async throws -> [String: Any] {
                let response = try await client.request(origin: candidate.origin, credential: candidate.credential,
                    method: method, path: "/api/mobile/device/v1/" + path, body: body)
                try requireCurrent(operationEpoch)
                guard currentAccountID == target.accountID, bindingMatches(candidate) else {
                    throw HealthSyncError.code("connection_changed")
                }
                return response
            }
            _ = try await setup("PUT", "manifest", manifestBody(background: false))
            let responseClaim = try await setup("POST", "requests/claim", ["request_id": requestID, "wait_seconds": 0, "max": 1])
            guard let claims = responseClaim["requests"] as? [[String: Any]], claims.count == 1,
                  claims[0]["id"] as? String == requestID,
                  claims[0]["operation"] as? String == "health.connect" else {
                throw HealthSyncError.code("request_unavailable")
            }
            let reservation = try Self.reservation(claims[0])
            // This single replacement is the commitment point. No await or
            // observer/query/upload can interleave with publishing the owner.
            try provisionalStore.update {
                $0.completed[requestID] = reservation
                $0.committedDeviceID = candidate.deviceID
            }
            identity = candidate
            committedEpoch = operationEpoch
            authorizationPaused = false
            committed = true
            uploader.reconnect()
            let workEpoch = operationEpoch
            Task { @MainActor [weak self] in
                guard let self, self.epoch == workEpoch else { return }
                await self.registerObservers()
                guard self.epoch == workEpoch else { return }
                do {
                    _ = try await self.synchronize(requestID: requestID, waitForUpload: false)
                    try await self.sendResults()
                    if self.foreground { self.beginPolling() }
                } catch { await self.handle(error, expectedEpoch: workEpoch) }
            }
            return "connected"
        } catch {
            guard epoch == operationEpoch else {
                if let provisional { await revoke(provisional) }
                return "failed"
            }
            if !committed {
                _ = await stopLocal(unregister: !Self.isAuthorizationFailure(error),
                                    expectedEpoch: operationEpoch, capturedIdentity: provisional)
            }
            notice = Self.safeCode(error) == "contract_unsupported" || Self.httpStatus(error) == 426
                ? "health.update_required" : "health.connection_failed"
            return "failed"
        }
    }

    private static func reservation(_ claim: [String: Any]) throws -> HealthPendingResult {
        guard let id = claim["id"] as? String, validID(id),
              let token = claim["claim_token"] as? String, !token.isEmpty,
              let operation = claim["operation"] as? String,
              ["health.connect", "health.refresh"].contains(operation),
              let metrics = claim["metrics"] as? [String], !metrics.isEmpty,
              Set(metrics).count == metrics.count,
              Set(metrics).isSubset(of: Set(HealthQueryWindow.metrics)) else { throw HealthSyncError.code("invalid_claim") }
        return HealthPendingResult(id: id, claimToken: token, outcome: "failed",
            states: Dictionary(uniqueKeysWithValues: metrics.map { ($0, "internal") }), error: "internal", uploadID: nil)
    }

    /// Called on launch and whenever the account becomes active. Background
    /// observer delivery needs its persisted device even without a WebView.
    func resume(foreground: Bool) async {
        self.foreground = foreground
        if let identity, let accountID = currentAccountID,
           identity.accountID != accountID || (!startupBindingUntrusted && !bindingMatches(identity)) {
            await stopLocal(unregister: true)
            return
        }
        guard connecting == nil, !restoring else { return }
        restoring = true
        let token = epoch
        defer {
            restoring = false
            resumeAuthorizedConnection()
        }
        await cleanupTask?.value
        guard epoch == token else { return }
        do {
            if store == nil { store = try HealthDeviceStore(directory: directory) }
            if cleanupPending || store?.state.cleanupPending == true {
                await stopLocal(unregister: true)
                return
            }
            guard !startupBindingUntrusted else { return }
            let saved: HealthDeviceIdentity?
            do { saved = try credentials.load() }
            catch is DecodingError {
                await stopLocal(unregister: false)
                return
            }
            guard let saved else {
                if store?.state.committedDeviceID != nil { await stopLocal(unregister: false) }
                return
            }
            guard store?.state.committedDeviceID == saved.deviceID else {
                await stopLocal(unregister: true, capturedIdentity: saved)
                return
            }
            guard let accountID = currentAccountID else { return }
            guard accountID == saved.accountID, bindingMatches(saved) else {
                await stopLocal(unregister: true, capturedIdentity: saved)
                return
            }
            guard store?.state.suspended != true else { return }
            if identity == nil { committedEpoch = token }
            identity = saved
            uploader.reconnect()
            await registerObservers()
            try requireCurrent(token)
            try await manifest()
            if let store {
                for reservation in Array(store.state.completed.values) where !reservation.ready {
                    _ = try await synchronize(requestID: reservation.id, waitForUpload: false)
                }
            }
            try await drainUploads()
            try await sendResults()
            if self.foreground {
                beginPolling()
                _ = try await synchronize()
            }
        } catch is DecodingError {
            await stopLocal(unregister: true, expectedEpoch: token)
        } catch { await handle(error, expectedEpoch: token) }
    }

    func setBackground() {
        foreground = false
        pollTask?.cancel()
        pollTask = nil
    }

    func logout() async { await stopLocal(unregister: true) }

    private func registerObservers() async {
        let token = epoch
        let enabled = await reader.startObserving { [weak self] in
            guard let self, self.epoch == token, self.identity != nil else { return }
            do {
                // Return after durable enqueue, leaving transfers to URLSession.
                _ = try await self.synchronize(waitForUpload: false)
            } catch { if self.epoch == token { await self.handle(error, expectedEpoch: token) } }
        }
        if epoch == token { backgroundDelivery = enabled }
    }

    private func manifestBody(background: Bool) -> [String: Any] {
        [
            "contract": 1, "app_version": Self.appVersion,
            "os_version": UIDevice.current.systemVersion,
            "timezone": TimeZone.current.identifier, "metrics": HealthQueryWindow.metrics,
            "background_delivery": background, "push_wake": false,
        ]
    }

    private func manifest() async throws {
        _ = try await request("PUT", "manifest", manifestBody(background: backgroundDelivery))
    }

    private func beginPolling() {
        guard pollTask == nil, foreground, identity != nil, store?.state.suspended != true else { return }
        let token = epoch
        pollTask = Task { [weak self] in
            guard let self else { return }
            defer { if self.epoch == token { self.pollTask = nil } }
            while !Task.isCancelled && self.epoch == token && self.foreground && self.identity != nil {
                do { try await self.claimAndProcess(requestID: nil, wait: 25) }
                catch {
                    guard self.epoch == token else { break }
                    await self.handle(error, expectedEpoch: token)
                    if self.identity == nil { break }
                    let delay = max(5, self.retryAfter?.timeIntervalSince(self.now()) ?? 5)
                    try? await Task.sleep(for: .seconds(delay))
                }
            }
        }
    }

    private func claimAndProcess(requestID: String?, wait: Int) async throws {
        var body: [String: Any] = ["wait_seconds": wait, "max": 1]
        if let requestID { body["request_id"] = requestID }
        let token = epoch
        let response = try await request("POST", "requests/claim", body)
        guard let claims = response["requests"] as? [[String: Any]] else { throw HealthSyncError.code("invalid_claim") }
        if requestID != nil && claims.isEmpty { throw HealthSyncError.code("request_unavailable") }
        for claim in claims {
            try requireCurrent(token)
            guard let id = claim["id"] as? String, Self.validID(id),
                  let claimToken = claim["claim_token"] as? String,
                  let operation = claim["operation"] as? String,
                  ["health.connect", "health.refresh"].contains(operation),
                  let metrics = claim["metrics"] as? [String],
                  Set(metrics).isSubset(of: Set(HealthQueryWindow.metrics)), !metrics.isEmpty,
                  Set(metrics).count == metrics.count, !claimToken.isEmpty,
                  let store else { throw HealthSyncError.code("invalid_claim") }
            if var previous = store.state.completed[id] {
                previous.claimToken = claimToken
                previous.acknowledged = false
                try store.update { $0.completed[id] = previous }
                if !previous.ready { _ = try await synchronize(requestID: id) }
                try await drainUploads()
                try await sendResults()
                continue
            }
            // A bound connect is authorized by the preceding native consent.
            // Record before executing so lease reclaim cannot execute it twice.
            let reservation = HealthPendingResult(id: id, claimToken: claimToken,
                outcome: "failed", states: Dictionary(uniqueKeysWithValues: metrics.map { ($0, "internal") }),
                error: "internal", uploadID: nil)
            try store.update { $0.completed[id] = reservation }
            do {
                _ = try await synchronize(requestID: id)
                try requireCurrent(token)
            } catch {
                try requireCurrent(token)
                // Network interruption keeps the upload/result dependency durable.
                if store.state.completed[id]?.uploadID == nil {
                    let code = Self.safeCode(error)
                    try store.update {
                        $0.completed[id] = HealthPendingResult(id: id, claimToken: claimToken,
                            outcome: "failed", states: Dictionary(uniqueKeysWithValues: metrics.map { ($0, code) }),
                            error: code, uploadID: nil, ready: true)
                    }
                }
                throw error
            }
            try await sendResults()
        }
    }

    private func synchronize(requestID: String? = nil, waitForUpload: Bool = true) async throws -> [String: String] {
        let token = epoch
        // Every invocation represents work requested after the active query began.
        // Keep it waiting, then query again; an older snapshot cannot satisfy it.
        while let active = syncTask {
            let activeID = syncID
            _ = try? await active.value
            try requireCurrent(token)
            if syncID == activeID { syncTask = nil; syncID = nil }
        }
        if let requestID, let completed = store?.state.completed[requestID], completed.ready {
            try requireAccount()
            try await drainUploads()
            return completed.states
        }
        let taskID = UUID()
        let task = Task { @MainActor () throws -> [String: String] in
            try self.requireAccount()
            guard let store = self.store, !store.state.suspended else { throw HealthSyncError.code("not_connected") }
            let window = try await self.reader.query(days: store.state.initialBackfillPending ? 30 : 8, now: self.now())
            try self.requireCurrent(token)
            try self.requireAccount()
            let seq = try store.nextSequence()
            let id = try store.enqueue(window.payload(sequence: seq)) { state, uploadID in
                if let requestID, var result = state.completed[requestID] {
                    result.states = window.metricStates.filter { result.states.keys.contains($0.key) }
                    let failure = result.states.values.first { !["ok", "empty"].contains($0) }
                    result.error = failure
                    result.outcome = failure == nil ? "succeeded" : "failed"
                    result.uploadID = uploadID
                    result.ready = true
                    state.completed[requestID] = result
                }
            }
            try await self.drainUploads()
            if waitForUpload { try await self.waitForUpload(id) }
            try self.requireCurrent(token)
            return window.metricStates
        }
        syncID = taskID
        syncTask = task
        defer {
            if syncID == taskID { syncTask = nil; syncID = nil }
        }
        return try await task.value
    }

    private func waitForUpload(_ id: String) async throws {
        guard store?.state.uploads.contains(id) == true else { return }
        let token = epoch
        try await withCheckedThrowingContinuation { continuation in
            uploadWaiters[id, default: []].append(continuation)
            Task { @MainActor [weak self] in
                try? await Task.sleep(for: .seconds(40))
                guard let self, self.epoch == token else { return }
                self.finishWaiters(id, result: .failure(HealthSyncError.code("upload_pending")))
            }
        }
    }

    private func drainUploads() async throws {
        guard let store, !store.state.suspended, let identity else { return }
        try requireAccount()
        if let retryAfter, retryAfter > now() { throw HealthSyncError.http(503, "retry_later", retryAfter.timeIntervalSince(now())) }
        // Send one monotonic sequence at a time; background completion advances it.
        if let id = store.state.uploads.first, !uploadingIDs.contains(id) {
            uploadingIDs.insert(id)
            uploadEpochs[id] = committedEpoch
            do { try await uploader.upload(id: id, file: store.file(id), identity: identity) }
            catch { uploadingIDs.remove(id); throw error }
        }
    }

    private func uploadCompleted(id: String, result: Result<[String: Any], Error>) async {
        guard let store, store.state.uploads.first == id, let identity,
              !cleanupPending, store.state.committedDeviceID == identity.deviceID,
              let committedEpoch, uploadEpochs[id] == committedEpoch else { return }
        uploadingIDs.remove(id)
        let token = epoch
        do {
            let response = try result.get()
            guard let accepted = response["accepted_seq"] as? NSNumber,
                  let data = try JSONSerialization.jsonObject(with: Data(contentsOf: store.file(id))) as? [String: Any],
                  data["device_seq"] as? NSNumber == accepted else { throw HealthSyncError.code("invalid_ack") }
            try store.removeUpload(id) {
                if (data["failures"] as? [[String: Any]])?.isEmpty == true { $0.initialBackfillPending = false }
                for key in $0.completed.keys where $0.completed[key]?.uploadID == id { $0.completed[key]?.uploadID = nil }
            }
            uploadEpochs.removeValue(forKey: id)
            finishWaiters(id, result: .success(()))
            try await sendResults()
            try await drainUploads()
        } catch {
            guard epoch == token else { return }
            let stale = (error as? HealthSyncError)?.code == "stale_seq"
            let terminal = Self.isTerminal(error) || stale
            if terminal {
                try? store.removeUpload(id) {
                    for key in $0.completed.keys where $0.completed[key]?.uploadID == id {
                        $0.completed[key]?.uploadID = nil
                        $0.completed[key]?.outcome = "failed"
                        $0.completed[key]?.error = "internal"
                        if let states = $0.completed[key]?.states {
                            $0.completed[key]?.states = states.mapValues { _ in "internal" }
                        }
                    }
                }
            }
            finishWaiters(id, result: .failure(error))
            await handle(error, expectedEpoch: token)
            if !terminal { scheduleRetry() }
        }
    }

    private func finishWaiters(_ id: String, result: Result<Void, Error>) {
        for continuation in uploadWaiters.removeValue(forKey: id) ?? [] { continuation.resume(with: result) }
    }

    private func sendResults() async throws {
        guard let store else { return }
        let token = epoch
        for result in Array(store.state.completed.values) where result.ready && !result.acknowledged && result.uploadID == nil {
            try requireCurrent(token)
            var body: [String: Any] = ["claim_token": result.claimToken, "outcome": result.outcome,
                                      "result": ["metrics": result.states]]
            if let error = result.error { body["error"] = ["code": error] }
            do { _ = try await request("POST", "requests/\(result.id)/result", body) }
            catch {
                if ["claim_mismatch", "request_terminal"].contains((error as? HealthSyncError)?.code ?? "") {
                    try requireCurrent(token)
                    try store.update {
                        if $0.completed[result.id]?.claimToken == result.claimToken { $0.completed[result.id]?.acknowledged = true }
                    }
                    continue
                }
                throw error
            }
            try requireCurrent(token)
            try store.update {
                if $0.completed[result.id]?.claimToken == result.claimToken { $0.completed[result.id]?.acknowledged = true }
            }
            // Keep value-free execution records across lease expiry and relaunch.
        }
    }

    private func request(_ method: String, _ path: String, _ body: [String: Any]? = nil) async throws -> [String: Any] {
        guard let identity else { throw HealthSyncError.code("not_connected") }
        try requireAccount()
        if let retryAfter, retryAfter > now() {
            throw HealthSyncError.http(503, "retry_later", retryAfter.timeIntervalSince(now()))
        }
        let token = epoch
        let result = try await client.request(origin: identity.origin, credential: identity.credential,
            method: method, path: "/api/mobile/device/v1/" + path, body: body)
        try requireCurrent(token)
        try requireAccount()
        return result
    }

    private func handle(_ error: Error, expectedEpoch: UUID) async {
        guard epoch == expectedEpoch else { return }
        guard let error = error as? HealthSyncError else { return }
        let status: Int?
        if case .http(let value, _, _) = error { status = value } else { status = nil }
        if status == 401 || ["device_unauthorized", "device_revoked", "instance_not_eligible"].contains(error.code) {
            await stopLocal(unregister: false, expectedEpoch: expectedEpoch)
        } else if status == 422 || status == 426 || error.code == "contract_unsupported"
                    || ["invalid_payload", "incomplete_window"].contains(error.code) {
            notice = status == 426 || error.code == "contract_unsupported" ? "health.update_required" : "health.connection_failed"
            try? store?.update { $0.suspended = true }
            reader.stopObserving()
            setBackground()
            retryTask?.cancel()
            retryTask = nil
        } else if case .http(let status, _, let delay) = error, [429, 503].contains(status) {
            retryAfter = now().addingTimeInterval(max(1, delay ?? 30))
        }
    }

    private func scheduleRetry() {
        guard retryTask == nil, foreground else { return }
        let token = epoch
        retryTask = Task { [weak self] in
            guard let self else { return }
            try? await Task.sleep(for: .seconds(max(5, self.retryAfter?.timeIntervalSince(self.now()) ?? 30)))
            guard !Task.isCancelled, self.epoch == token else { return }
            self.retryTask = nil
            do { try await self.drainUploads() } catch { await self.handle(error, expectedEpoch: token) }
        }
    }

    private func revoke(_ captured: HealthDeviceIdentity) async {
        _ = try? await client.request(origin: captured.origin, credential: captured.credential,
            method: "DELETE", path: "/api/mobile/device/v1/", body: nil)
    }

    /// Revocation authority is removed synchronously, before account consumers
    /// can await their unrelated push/network cleanup.
    @discardableResult
    private func beginCleanup(unregister: Bool, expectedEpoch: UUID? = nil,
                              capturedIdentity: HealthDeviceIdentity? = nil) -> UUID {
        if let expectedEpoch, expectedEpoch != epoch { return epoch }
        let old = capturedIdentity ?? identity ?? (try? credentials.load())
        epoch = UUID()
        let stoppedEpoch = epoch
        connecting = nil
        answerConsent(false)
        identity = nil
        committedEpoch = nil
        uploadEpochs.removeAll()
        authorizationPaused = false
        reader.stopObserving()
        uploader.invalidate()
        setBackground()
        syncTask?.cancel()
        syncTask = nil
        syncID = nil
        retryTask?.cancel()
        retryTask = nil
        for id in Array(uploadWaiters.keys) { finishWaiters(id, result: .failure(HealthSyncError.code("connection_changed"))) }
        cleanupPending = true
        let cleanupStore = store ?? (try? HealthDeviceStore(directory: directory))
        do { try cleanupStore?.prepareForCleanup() } catch { notice = "health.cleanup_failed" }
        var credentialsCleared = false
        do { try credentials.clear(); credentialsCleared = true } catch { notice = "health.cleanup_failed" }
        store = nil
        uploadingIDs.removeAll()
        retryAfter = nil
        let previous = cleanupTask
        cleanupTask = Task { @MainActor in
            await previous?.value
            await self.uploader.cancelAll()
            let disabled = await self.reader.disableBackgroundDelivery()
            guard self.epoch == stoppedEpoch else {
                if unregister, let old { await self.revoke(old) }
                return
            }
            if credentialsCleared && disabled {
                do {
                    if let cleanupStore { try cleanupStore.clear() }
                    self.cleanupPending = false
                } catch { self.notice = "health.cleanup_failed" }
            } else { self.notice = "health.cleanup_failed" }
            if unregister, let old { await self.revoke(old) }
        }
        return stoppedEpoch
    }

    @discardableResult
    private func stopLocal(unregister: Bool, expectedEpoch: UUID? = nil,
                           capturedIdentity: HealthDeviceIdentity? = nil) async -> UUID {
        let stopped = beginCleanup(unregister: unregister, expectedEpoch: expectedEpoch, capturedIdentity: capturedIdentity)
        await cleanupTask?.value
        return stopped
    }

    private static func httpStatus(_ error: Error) -> Int? {
        guard case .http(let status, _, _) = error as? HealthSyncError else { return nil }
        return status
    }

    private static func isAuthorizationFailure(_ error: Error) -> Bool {
        httpStatus(error) == 401 || ["device_unauthorized", "device_revoked", "instance_not_eligible"].contains((error as? HealthSyncError)?.code ?? "")
    }

    private func requireCurrent(_ token: UUID) throws {
        guard token == epoch, !Task.isCancelled else { throw HealthSyncError.code("connection_changed") }
    }

    private static func safeCode(_ error: Error) -> String {
        let code = (error as? HealthSyncError)?.code ?? HealthReadError.code(for: error)
        return ["protected_data_unavailable", "capability_unavailable", "user_declined", "contract_unsupported"].contains(code) ? code : "internal"
    }

    private static func validID(_ id: String) -> Bool {
        !id.isEmpty && id.count <= 256 && id.unicodeScalars.allSatisfy { CharacterSet.alphanumerics.contains($0) || "_-".unicodeScalars.contains($0) }
    }

    private static func isTerminal(_ error: Error) -> Bool {
        guard let syncError = error as? HealthSyncError, case .http(let status, _, _) = syncError else { return false }
        return (400..<500).contains(status) && ![403, 408, 429].contains(status)
    }

    private static var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
    }
}
