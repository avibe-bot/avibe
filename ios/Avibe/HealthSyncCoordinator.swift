import Foundation
import SwiftUI
import UIKit

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
    private var epoch = UUID()
    private var consentContinuation: CheckedContinuation<Bool, Never>?
    private var connecting = false
    private var foreground = false
    private var restoring = false
    private var uploadingIDs: Set<String> = []
    private var backgroundDelivery = false
    private var pollTask: Task<Void, Never>?
    private var syncTask: Task<[String: String], Error>?
    private var retryTask: Task<Void, Never>?
    private var uploadWaiters: [String: [CheckedContinuation<Void, Error>]] = [:]
    private var retryAfter: Date?
    var targetProvider: (() -> HealthConnectionTarget?)?

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

    func connect(requestID: String, pairingCode: String, origin: WebOrigin) async -> String {
        guard !connecting, let target = targetProvider?(), target.origin == origin.serialized,
              !requestID.isEmpty, !pairingCode.isEmpty else { return "failed" }
        connecting = true
        defer { connecting = false }
        let token = epoch
        let approved = await withCheckedContinuation { continuation in
            consentContinuation = continuation
            consent = HealthConsentRequest(target: target)
        }
        guard epoch == token, targetProvider?() == target else { return "failed" }
        guard approved else { return "declined" }
        var operationEpoch = token
        do {
            try await reader.authorize()
            try requireCurrent(token)
            guard targetProvider?() == target else { throw HealthSyncError.code("connection_changed") }
            let installationID = try credentials.installationID()
            // End the previous phone-to-instance authorization before replacing it.
            let wasForeground = foreground
            let pairEpoch = await stopLocal(unregister: true)
            operationEpoch = pairEpoch
            try requireCurrent(pairEpoch)
            guard targetProvider?() == target else { throw HealthSyncError.code("connection_changed") }
            foreground = wasForeground
            let response = try await client.request(origin: target.origin, credential: nil, method: "POST",
                path: "/api/mobile/device/v1/pair", body: [
                    "pairing_code": pairingCode, "installation_id": installationID,
                    "device_name": UIDevice.current.name, "platform": "ios",
                    "app_version": Self.appVersion, "metrics": HealthQueryWindow.metrics,
                ])
            try requireCurrent(pairEpoch)
            guard targetProvider?() == target,
                  let deviceID = response["device_id"] as? String,
                  let credential = response["credential"] as? String, !credential.isEmpty,
                  let instance = response["instance"] as? [String: Any],
                  instance["id"] as? String == target.instanceID else { throw HealthSyncError.code("invalid_pair") }
            let identity = HealthDeviceIdentity(installationID: installationID,
                accountID: target.accountID, instanceID: target.instanceID,
                instanceName: target.name, origin: target.origin, deviceID: deviceID, credential: credential)
            try credentials.save(identity)
            self.identity = identity
            store = try HealthDeviceStore(directory: directory)
            uploader.reconnect()
            await registerObservers()
            try requireCurrent(pairEpoch)
            try await manifest()
            // Pairing binds this exact connect request to the new device.
            try await claimAndProcess(requestID: requestID, wait: 0)
            try requireCurrent(pairEpoch)
            if foreground { beginPolling() }
            return "connected"
        } catch {
            guard epoch == operationEpoch else { return "failed" }
            await handle(error)
            notice = Self.safeCode(error) == "contract_unsupported" ? "health.update_required" : "health.connection_failed"
            return "failed"
        }
    }

    /// Called on launch and whenever the account becomes active. Background
    /// observer delivery needs its persisted device even without a WebView.
    func resume(foreground: Bool) async {
        self.foreground = foreground
        guard !restoring, !connecting else { return }
        restoring = true
        let token = epoch
        defer { restoring = false }
        do {
            if identity == nil {
                guard let saved = try credentials.load() else { return }
                if let target = targetProvider?(), target.accountID != saved.accountID {
                    await stopLocal(unregister: true)
                    return
                }
                identity = saved
                store = try HealthDeviceStore(directory: directory)
                // Only durable payload creation completes local execution.
                try store?.update {
                    $0.completed = $0.completed.filter { now().timeIntervalSince($0.value.completedAt) < 7 * 86400 }
                }
                guard store?.state.suspended != true else { return }
                uploader.reconnect()
                await registerObservers()
            }
            guard identity != nil, store?.state.suspended != true else { return }
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
        } catch { if epoch == token { await handle(error) } }
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
            } catch { if self.epoch == token { await self.handle(error) } }
        }
        if epoch == token { backgroundDelivery = enabled }
    }

    private func manifest() async throws {
        _ = try await request("PUT", "manifest", [
            "contract": 1, "app_version": Self.appVersion,
            "os_version": UIDevice.current.systemVersion,
            "timezone": TimeZone.current.identifier, "metrics": HealthQueryWindow.metrics,
            "background_delivery": backgroundDelivery, "push_wake": false,
        ])
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
                    await self.handle(error)
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
        if let syncTask {
            let states = try await syncTask.value
            // A newly claimed request still needs its own durable dependency.
            if requestID == nil { return states }
        }
        let token = epoch
        let task = Task { @MainActor () throws -> [String: String] in
            guard let store = self.store, !store.state.suspended, self.identity != nil else { throw HealthSyncError.code("not_connected") }
            let window = try await self.reader.query(days: store.state.initialBackfillPending ? 30 : 8, now: self.now())
            try self.requireCurrent(token)
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
        syncTask = task
        defer { if epoch == token { syncTask = nil } }
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
        if let retryAfter, retryAfter > now() { throw HealthSyncError.http(503, "retry_later", retryAfter.timeIntervalSince(now())) }
        // Send one monotonic sequence at a time; background completion advances it.
        if let id = store.state.uploads.first, !uploadingIDs.contains(id) {
            uploadingIDs.insert(id)
            do { try await uploader.upload(id: id, file: store.file(id), identity: identity) }
            catch { uploadingIDs.remove(id); throw error }
        }
    }

    private func uploadCompleted(id: String, result: Result<[String: Any], Error>) async {
        guard let store, store.state.uploads.first == id, identity != nil else { return }
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
            await handle(error)
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
        if let retryAfter, retryAfter > now() {
            throw HealthSyncError.http(503, "retry_later", retryAfter.timeIntervalSince(now()))
        }
        let token = epoch
        let result = try await client.request(origin: identity.origin, credential: identity.credential,
            method: method, path: "/api/mobile/device/v1/" + path, body: body)
        try requireCurrent(token)
        return result
    }

    private func handle(_ error: Error) async {
        guard let error = error as? HealthSyncError else { return }
        let status: Int?
        if case .http(let value, _, _) = error { status = value } else { status = nil }
        if status == 401 || ["device_unauthorized", "device_revoked", "instance_not_eligible"].contains(error.code) {
            await stopLocal(unregister: false)
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
            do { try await self.drainUploads() } catch { await self.handle(error) }
        }
    }

    @discardableResult
    private func stopLocal(unregister: Bool) async -> UUID {
        epoch = UUID()
        let stoppedEpoch = epoch
        answerConsent(false)
        let old = identity ?? (try? credentials.load())
        identity = nil
        reader.stopObserving()
        setBackground()
        syncTask?.cancel()
        syncTask = nil
        retryTask?.cancel()
        retryTask = nil
        for id in Array(uploadWaiters.keys) { finishWaiters(id, result: .failure(HealthSyncError.code("connection_changed"))) }
        // Clear secrets and files before awaiting network or account transitions.
        do { try credentials.clear() } catch { notice = "health.cleanup_failed" }
        do {
            if let store { try store.clear() }
            else if FileManager.default.fileExists(atPath: directory.path) { try FileManager.default.removeItem(at: directory) }
        } catch { notice = "health.cleanup_failed" }
        store = nil
        uploadingIDs.removeAll()
        retryAfter = nil
        await uploader.cancelAll()
        if unregister, let old {
            _ = try? await client.request(origin: old.origin, credential: old.credential,
                method: "DELETE", path: "/api/mobile/device/v1/", body: nil)
        }
        return stoppedEpoch
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
        return (400..<500).contains(status) && ![408, 429].contains(status)
    }

    private static var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
    }
}
