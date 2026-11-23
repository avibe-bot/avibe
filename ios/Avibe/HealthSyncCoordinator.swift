import Foundation
import SwiftUI
import UIKit
import Security
import OSLog

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

/// One live setup owns its deadline and every late callback. This is not a
/// second device identity: provisional credentials never publish authorization.
@MainActor
private final class HealthConnectionAttempt {
    let id: UUID
    var epoch: UUID
    var provisional: HealthDeviceIdentity?
    var work: Task<Void, Never>?
    var timer: Task<Void, Never>?
    var completion: CheckedContinuation<String, Never>?
    var deadline: ContinuousClock.Instant?

    init(id: UUID, epoch: UUID) { self.id = id; self.epoch = epoch }

    func finish(_ state: String) {
        let completion = self.completion
        self.completion = nil
        timer?.cancel()
        timer = nil
        work = nil
        completion?.resume(returning: state)
    }
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
    private var authorizationPaused = false
    private var authorizationResumePending = false
    private var authorizationResumeID: UUID?
    private var epoch = UUID()
    private var consentContinuation: CheckedContinuation<Bool, Never>?
    private var approvedConsentID: UUID?
    private var connectionAttempt: HealthConnectionAttempt?
    // Only a new local commitment may offer this help. Restoration does not
    // repeat a dismissed offer; no new persisted health or permission state.
    private var firstSyncHelpDeviceID: String?
    private let connectionTimeout: Duration
    private static let logger = Logger(subsystem: "bot.avibe.ios", category: "health-connection")
    private var connecting: UUID?
    private var cleanupTask: Task<Void, Never>?
    private var startupBindingUntrusted = false
    var websiteDataStoreIDProvider: (() -> UUID?)?
    var protectedDataAvailable: () -> Bool = { UIApplication.shared.isProtectedDataAvailable }
    private var foreground = false
    private var restoring = false

    /// Cleanups still running (`cleanupTask` stays set once one has run).
    private var cleanupsRunning = 0

    /// A connection, restoration, sync, convergence or cleanup under way
    /// (not the idle poll or retry timers), for voice capture's check before
    /// it ends a background process.
    var hasWorkInFlight: Bool {
        connecting != nil || restoring || syncTask != nil || convergeTask != nil || cleanupsRunning > 0
    }
    private var uploadingIDs: Set<String> = []
    private var backgroundDelivery = false
    private var pollTask: Task<Void, Never>?
    // Identifies the loop `pollTask` tracks; a retired loop never clears a newer handle.
    private var pollID: UUID?
    private var syncTask: Task<[String: String], Error>?
    private var syncID: UUID?
    private var retryTask: Task<Void, Never>?
    // One convergence pass at a time per epoch; requests during a pass coalesce.
    private var convergeTask: Task<Void, Never>?
    private var convergeEpoch: UUID?
    private var convergeAgain = false
    // In memory: each epoch, each resume entry and each background-delivery change
    // make the manifest due again; a success clears only what it actually reported.
    private var manifestDue = true
    private var manifestGeneration = 0
    private var uploadWaiters: [String: [CheckedContinuation<Void, Error>]] = [:]
    private var retryAfter: Date?
    var targetProvider: (() -> HealthConnectionTarget?)?
    var accountIDProvider: (() -> String?)?
    /// The account's instances as avibe.bot last confirmed them in this
    /// process, instance id to origin; `nil` while that is unknown.
    var instancesProvider: (() -> [String: String]?)?
    private var cleanupPending = false

    private var currentAccountID: String? {
        if let accountIDProvider { return accountIDProvider() }
        return targetProvider?()?.accountID
    }

    /// The committed connection, while the current account and WebKit
    /// store still own it; read without side effects.
    private var authorizedIdentity: HealthDeviceIdentity? {
        guard let identity, currentAccountID == identity.accountID, !cleanupPending,
              !startupBindingUntrusted, bindingMatches(identity),
              store?.state.committedDeviceID == identity.deviceID else { return nil }
        return identity
    }

    private func requireAccount() throws {
        guard let identity = authorizedIdentity else {
            throw HealthSyncError.code("connection_changed")
        }
        // Nothing more goes to an origin the account no longer lists for
        // this instance: the device is disconnected here, without a revoke.
        // A connect under way replaces it instead.
        if delisted(identity) {
            if connecting == nil { beginCleanup(unregister: false) }
            throw HealthSyncError.code("connection_changed")
        }
    }

    /// Whether a confirmed instance list no longer lists `candidate`'s
    /// instance at its origin: gone from the list, or listed at another
    /// origin (renamed). Only the current account's list decides, and an
    /// unknown list decides nothing. A confirmed empty list (the account has
    /// no instance left) does not list it either: a deleted instance's name
    /// can later belong to another account.
    private func delisted(_ candidate: HealthDeviceIdentity) -> Bool {
        guard candidate.accountID == currentAccountID,
              let listed = instancesProvider?() else { return false }
        return listed[candidate.instanceID] != candidate.origin
    }

    init(credentials: HealthCredentialStoring = HealthCredentialStore(),
         reader: HealthDataReading? = nil,
         client: HealthDeviceRequesting = HealthDeviceClient(),
         uploader: HealthUploading = HealthBackgroundUploader(),
         directory: URL = URL.applicationSupportDirectory.appendingPathComponent("HealthSync", isDirectory: true),
          connectionTimeout: Duration = .seconds(90),
         now: @escaping () -> Date = Date.init) {
        self.credentials = credentials
        self.reader = reader ?? HealthDataReader()
        self.client = client
        self.uploader = uploader
        self.directory = directory
        self.now = now
        self.connectionTimeout = connectionTimeout
        uploader.onComplete = { [weak self] id, result in
            self?.uploadCompleted(id: id, result: result)
        }
    }

    var backgroundUploader: HealthBackgroundUploader? { uploader as? HealthBackgroundUploader }

    func dismissNotice() { notice = nil }

    func answerConsent(_ allowed: Bool, afterDismissal: Bool = false) {
        if allowed, let attempt = connectionAttempt { startConnectionDeadline(attempt) }
        if allowed, afterDismissal, let consent {
            approvedConsentID = consent.id
            self.consent = nil
            return
        }
        approvedConsentID = nil
        let continuation = consentContinuation
        consentContinuation = nil
        consent = nil
        continuation?.resume(returning: allowed)
    }

    func consentDismissed(id: UUID) {
        guard approvedConsentID == id else { return }
        answerConsent(true)
    }

    private func startConnectionDeadline(_ attempt: HealthConnectionAttempt) {
        guard attempt.timer == nil else { return }
        let deadline = ContinuousClock.now.advanced(by: connectionTimeout)
        attempt.deadline = deadline
        attempt.timer = Task { @MainActor [weak self, weak attempt] in
            do { try await Task.sleep(until: deadline, clock: .continuous) }
            catch { return }
            guard let self, let attempt, self.connectionAttempt === attempt else { return }
            Self.logger.error("Health connection setup timed out")
            // Freeze authority before settling Native. Cancellation alone
            // cannot stop a HealthKit or URLSession callback already issued.
            attempt.work?.cancel()
            if self.epoch == attempt.epoch {
                self.beginCleanup(unregister: true, capturedIdentity: attempt.provisional)
            }
            self.connectionAttempt = nil
            self.notice = "health.connection_timed_out"
            attempt.finish("failed")
        }
    }

    private func requireConnection(_ attempt: HealthConnectionAttempt) throws {
        try requireCurrent(attempt.epoch)
        // A resumed callback can be scheduled ahead of the timer after the App
        // was suspended. Its deadline still forbids starting pair or commit.
        guard connectionAttempt === attempt,
              let deadline = attempt.deadline, ContinuousClock.now < deadline else {
            throw HealthSyncError.code("connection_timed_out")
        }
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
            } else if identity == nil || authorizationPaused {
                authorizationResumePending = true
                resumeAuthorizedConnection()
            } else if manifestDue || (foreground && pollTask == nil) {
                // A converged identity triggers no network; an unconverged one finishes setup.
                requestConvergence()
            }
            // The list this answer carries is published after this event, so
            // the instance-list gate reads it on the next turn: a rename ends
            // the device now, not at a later sign-out that would revoke it at
            // the old origin.
            if identity != nil {
                Task { @MainActor [weak self] in _ = try? self?.requireAccount() }
            }
        }
    }

    private func resumeAuthorizedConnection() {
        if identity != nil, !authorizationPaused, !restoring {
            authorizationResumePending = false
        }
        guard authorizationResumePending, authorizationResumeID == nil else { return }
        let attempt = UUID()
        authorizationResumeID = attempt
        let token = epoch
        Task { @MainActor [weak self] in
            guard let self else { return }
            defer {
                if self.authorizationResumeID == attempt {
                    self.authorizationResumeID = nil
                    if self.identity != nil, !self.authorizationPaused {
                        self.authorizationResumePending = false
                    } else if !self.restoring, self.connecting == nil, !self.cleanupPending,
                              self.currentAccountID != nil {
                        self.resumeAuthorizedConnection()
                    }
                }
            }
            // The account callback publishes its snapshot before this task
            // runs. Blocked owners retain one pass for their completion.
            guard self.epoch == token, self.authorizationResumeID == attempt,
                  !self.restoring, self.connecting == nil, !self.cleanupPending,
                  self.currentAccountID != nil else { return }
            self.authorizationResumePending = false
            await self.resume(foreground: self.foreground)
        }
    }

    private func pauseAuthority() {
        authorizationPaused = true
        epoch = UUID()
        retireConvergence()
        authorizationResumeID = nil
        authorizationResumePending = false
        reader.stopObserving()
        uploader.invalidate()
        uploadingIDs.removeAll()
        pollTask?.cancel()
        pollTask = nil
        pollID = nil
        syncTask?.cancel()
        syncTask = nil
        syncID = nil
        retryTask?.cancel()
        retryTask = nil
        for id in Array(uploadWaiters.keys) {
            finishWaiters(id, result: .failure(HealthSyncError.code("connection_changed")))
        }
        let previous = cleanupTask
        cleanupsRunning += 1
        cleanupTask = Task { @MainActor in
            defer { self.cleanupsRunning -= 1 }
            await previous?.value
            await self.uploader.cancelAll()
            _ = await self.reader.disableBackgroundDelivery()
        }
    }

    func connect(requestID: String, pairingCode: String, origin: WebOrigin) async -> String {
        guard connecting == nil, connectionAttempt == nil, !startupBindingUntrusted,
              let binding = websiteDataStoreIDProvider?(),
              let target = targetProvider?(), currentAccountID == target.accountID,
              target.origin == origin.serialized,
              !requestID.isEmpty, !pairingCode.isEmpty else { return "failed" }
        let connection = UUID()
        connecting = connection
        defer {
            if connecting == connection { connecting = nil }
            if connectionAttempt?.id == connection {
                connectionAttempt?.timer?.cancel()
                connectionAttempt = nil
            }
            resumeAuthorizedConnection()
        }
        let token = epoch
        let attempt = HealthConnectionAttempt(id: connection, epoch: token)
        connectionAttempt = attempt
        let approved = await withCheckedContinuation { continuation in
            consentContinuation = continuation
            consent = HealthConsentRequest(target: target)
        }
        guard epoch == token, targetProvider?() == target else { return "failed" }
        guard approved else { return "declined" }
        return await withCheckedContinuation { completion in
            attempt.completion = completion
            attempt.work = Task { @MainActor in
                let state = await self.establishConnection(
                    requestID: requestID, pairingCode: pairingCode,
                    target: target, binding: binding, attempt: attempt)
                if self.connectionAttempt === attempt { self.connectionAttempt = nil }
                attempt.finish(state)
            }
        }
    }

    private func establishConnection(requestID: String, pairingCode: String,
                                     target: HealthConnectionTarget, binding: UUID,
                                     attempt: HealthConnectionAttempt) async -> String {
        var committed = false
        do {
            try requireConnection(attempt)
            Self.logger.info("Health authorization requested")
            try await reader.authorize()
            Self.logger.info("Health authorization request completed")
            try requireConnection(attempt)
            let installationID = try credentials.installationID()
            // The device being replaced is revoked, unless its origin is no
            // longer listed for it: nothing more is sent there.
            let replaced = identity ?? (try? credentials.load())
            Self.logger.info("Health replacement cleanup started")
            attempt.epoch = beginCleanup(unregister: replaced.map { !delisted($0) } ?? true,
                                         preservingConnection: attempt.id)
            connecting = attempt.id
            await cleanupTask?.value
            // This connect still owns its generation after replacing the old device.
            try requireConnection(attempt)
            guard connecting == nil || connecting == attempt.id else { throw HealthSyncError.code("connection_changed") }
            connecting = attempt.id
            guard !cleanupPending, targetProvider?() == target, currentAccountID == target.accountID,
                  websiteDataStoreIDProvider?() == binding else { throw HealthSyncError.code("connection_changed") }
            Self.logger.info("Health replacement cleanup completed; pairing requested")
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
            attempt.provisional = candidate
            guard epoch == attempt.epoch, !Task.isCancelled else {
                await revoke(candidate)
                return "failed"
            }
            try requireConnection(attempt)
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
                try requireConnection(attempt)
                guard currentAccountID == target.accountID, bindingMatches(candidate) else {
                    throw HealthSyncError.code("connection_changed")
                }
                return response
            }
            Self.logger.info("Health pairing completed; manifest requested")
            _ = try await setup("PUT", "manifest", manifestBody(background: false))
            Self.logger.info("Health manifest completed; claim requested")
            let responseClaim = try await setup("POST", "requests/claim", ["request_id": requestID, "wait_seconds": 0, "max": 1])
            guard let claims = responseClaim["requests"] as? [[String: Any]], claims.count == 1,
                  claims[0]["id"] as? String == requestID,
                  claims[0]["operation"] as? String == "health.connect" else {
                throw HealthSyncError.code("request_unavailable")
            }
            let reservation = try Self.reservation(claims[0])
            try requireConnection(attempt)
            // This single replacement is the commitment point. No await or
            // observer/query/upload can interleave with publishing the owner.
            try provisionalStore.update {
                $0.completed[requestID] = reservation
                $0.committedDeviceID = candidate.deviceID
            }
            identity = candidate
            firstSyncHelpDeviceID = candidate.deviceID
            authorizationPaused = false
            authorizationResumePending = false
            committed = true
            uploader.reconnect()
            Self.logger.info("Health connection committed")
            let workEpoch = attempt.epoch
            Task { @MainActor [weak self] in
                guard let self, self.epoch == workEpoch else { return }
                await self.registerObservers()
                guard self.epoch == workEpoch else { return }
                do { _ = try await self.synchronize(requestID: requestID, waitForUpload: false) }
                catch { await self.handle(error, expectedEpoch: workEpoch) }
                // Polling, the manifest and any unready reservation converge whatever the first sync did.
                if self.epoch == workEpoch { self.requestConvergence() }
            }
            return "connected"
        } catch {
            guard epoch == attempt.epoch else {
                if let provisional = attempt.provisional { await revoke(provisional) }
                return "failed"
            }
            if !committed {
                Self.logger.info("Health setup failed; rollback started")
                _ = await stopLocal(unregister: !Self.isAuthorizationFailure(error),
                                    expectedEpoch: attempt.epoch, capturedIdentity: attempt.provisional,
                                    preservingConnection: attempt.id)
            }
            guard connectionAttempt === attempt else { return "failed" }
            if Self.safeCode(error) == "connection_timed_out" {
                notice = "health.connection_timed_out"
            } else {
                notice = Self.safeCode(error) == "contract_unsupported" || Self.httpStatus(error) == 426
                    ? "health.update_required" : "health.connection_failed"
            }
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
        guard connecting == nil, !restoring else {
            // Every resume entry converges, including one that meets a running restoration.
            if identity != nil {
                markManifestDue()
                requestConvergence()
            }
            return
        }
        restoring = true
        markManifestDue()
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
            guard !delisted(saved) else {
                await stopLocal(unregister: false, capturedIdentity: saved)
                return
            }
            guard store?.state.suspended != true else { return }
            identity = saved
            authorizationPaused = false
            uploader.reconnect()
            await registerObservers()
            try requireCurrent(token)
            await requestConvergence().value
            try requireCurrent(token)
            // The activation refresh belongs to this entry only; convergence never
            // queries for freshness.
            if self.foreground { _ = try await synchronize() }
        } catch is DecodingError {
            await stopLocal(unregister: true, expectedEpoch: token)
        } catch { await handle(error, expectedEpoch: token) }
    }

    /// Lifecycle only. Internal stops cancel work without rewriting `foreground`.
    func setBackground() {
        foreground = false
        stopForegroundWork()
    }

    private func stopForegroundWork() {
        pollTask?.cancel()
        pollTask = nil
        pollID = nil
        retryTask?.cancel()
        retryTask = nil
    }

    func logout() async { await stopLocal(unregister: true) }

    // MARK: Binding ticket (the Devices page's proof)

    /// How long `device.describe` waits for a ticket before it answers
    /// without one.
    var bindingTicketBudget: Duration = .seconds(3)
    /// Origins whose instance has no ticket endpoint (an older server),
    /// for this process.
    private var bindingTicketUnsupported: Set<String> = []
    /// No ticket is asked for before this: the instance issues at most one
    /// every 30 seconds per pairing.
    private var bindingTicketNotBefore: Date?

    /// A one-use ticket from the paired instance proving this App's Health
    /// pairing to the Devices page (`device.describe` with
    /// `health_binding`). Only in front, only for the pairing of
    /// `instanceID` at `origin` on `accountID`, while it is connected; nil
    /// otherwise, on any failure and after the budget. It is handed out
    /// once and kept nowhere.
    ///
    /// A side path: it never goes through `handle()`. No answer here
    /// (a 401 or 404 from an older server, 422, 429 or 503 included)
    /// disconnects, suspends or delays Health; sync's own requests
    /// discover a revocation.
    func bindingTicket(accountID: String, instanceID: String, origin: String) async -> String? {
        guard foreground, !authorizationPaused, let identity = authorizedIdentity, !delisted(identity),
              identity.accountID == accountID, identity.instanceID == instanceID, identity.origin == origin,
              !bindingTicketUnsupported.contains(origin) else { return nil }
        if let notBefore = bindingTicketNotBefore, notBefore > now() { return nil }
        bindingTicketNotBefore = now().addingTimeInterval(30)
        let token = epoch
        let client = self.client
        let budget = bindingTicketBudget
        let response: [String: Any]?
        do {
            response = try await withThrowingTaskGroup(of: [String: Any]?.self) { group in
                group.addTask {
                    try await client.request(origin: identity.origin, credential: identity.credential,
                        method: "POST", path: "/api/mobile/device/v1/binding-ticket", body: [:])
                }
                group.addTask {
                    try await Task.sleep(for: budget)
                    return nil
                }
                defer { group.cancelAll() }
                return try await group.next() ?? nil
            }
        } catch HealthSyncError.http(let status, let code, let delay) {
            if status == 404 || status == 405 || (status == 401 && code != "device_unauthorized") {
                bindingTicketUnsupported.insert(origin)
            } else if status == 429 {
                bindingTicketNotBefore = now().addingTimeInterval(max(30, delay ?? 30))
            }
            return nil
        } catch {
            return nil
        }
        // The same connection, still in front, once the answer is back.
        guard epoch == token, foreground, authorizedIdentity == identity, !delisted(identity),
              let response else { return nil }
        return Self.bindingTicket(from: response, now: now())
    }

    /// The ticket in an issue answer: 43 base64url characters (32 bytes,
    /// unpadded), and not yet expired.
    nonisolated static func bindingTicket(from response: [String: Any], now: Date) -> String? {
        guard let ticket = response["health_binding_ticket"] as? String, ticket.utf8.count == 43,
              ticket.unicodeScalars.allSatisfy({ ($0.isASCII && CharacterSet.alphanumerics.contains($0)) || $0 == "-" || $0 == "_" }),
              let text = response["expires_at"] as? String,
              let expires = ISO8601DateFormatter.healthTicket.date(from: text)
                ?? ISO8601DateFormatter().date(from: text),
              expires > now else { return nil }
        return ticket
    }

    private func registerObservers() async {
        let token = epoch
        let enabled = await reader.startObserving { [weak self] in
            guard let self, self.epoch == token, self.identity != nil else { return }
            do {
                // Return after durable enqueue, leaving transfers to URLSession.
                _ = try await self.synchronize(waitForUpload: false)
                if self.epoch == token { self.requestConvergence() }
            } catch {
                guard self.epoch == token else { return }
                await self.handle(error, expectedEpoch: token)
                if !Self.isTerminal(error) { self.scheduleRetry(token) }
            }
        }
        if epoch == token {
            if backgroundDelivery != enabled { markManifestDue() }
            backgroundDelivery = enabled
        }
    }

    private func manifestBody(background: Bool) -> [String: Any] {
        [
            "contract": 1, "app_version": Self.appVersion,
            "os_version": UIDevice.current.systemVersion,
            "timezone": TimeZone.current.identifier, "metrics": HealthQueryWindow.metrics,
            "background_delivery": background, "push_wake": false,
        ]
    }

    private func manifest(background: Bool) async throws {
        _ = try await request("PUT", "manifest", manifestBody(background: background))
    }

    private func markManifestDue() {
        manifestDue = true
        manifestGeneration += 1
    }

    private func beginPolling() {
        guard pollTask == nil, foreground, identity != nil, store?.state.suspended != true else { return }
        let token = epoch
        let id = UUID()
        pollID = id
        pollTask = Task { [weak self] in
            guard let self else { return }
            defer {
                if self.pollID == id {
                    self.pollTask = nil
                    self.pollID = nil
                }
            }
            while !Task.isCancelled && self.pollID == id && self.epoch == token
                    && self.foreground && self.identity != nil {
                do { try await self.claimAndProcess(requestID: nil, wait: 25) }
                catch {
                    // A retired loop handles nothing, schedules nothing and does not wait.
                    guard !Task.isCancelled, self.pollID == id, self.epoch == token else { break }
                    await self.handle(error, expectedEpoch: token)
                    if self.identity == nil { break }
                    if !Self.isTerminal(error) { self.scheduleRetry(token) }
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
                try requireCurrent(token)
                try await drainUploads()
                try requireCurrent(token)
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
            if let deviceID = self.firstSyncHelpDeviceID, deviceID == self.authorizedIdentity?.deviceID {
                self.firstSyncHelpDeviceID = nil
                if self.protectedDataAvailable(), window.hasCompleteEmptyRecentSteps, self.notice == nil {
                    self.notice = "health.no_recent_steps"
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
            do { try await uploader.upload(id: id, file: store.file(id), identity: identity) }
            catch { uploadingIDs.remove(id); throw error }
        }
    }

    private func uploadCompleted(id: String, result: Result<[String: Any], Error>) {
        guard !cleanupPending else { return }
        let token = epoch
        let completedStore: HealthDeviceStore
        do {
            // Each upload UUID is minted by enqueue for one immutable payload.
            // Pair/cleanup replace the queue; IDs are never reused across
            // devices. Thus a task's exact current head identifies its durable
            // committed owner even before Keychain/account restoration.
            guard FileManager.default.fileExists(atPath: directory.appendingPathComponent("state.json").path) else { return }
            completedStore = try store ?? HealthDeviceStore(directory: directory)
            guard !completedStore.state.cleanupPending,
                  completedStore.state.committedDeviceID != nil,
                  completedStore.state.uploads.first == id else { return }
            uploadingIDs.remove(id)
            let response = try result.get()
            guard let accepted = response["accepted_seq"] as? NSNumber,
                  CFGetTypeID(accepted) != CFBooleanGetTypeID(),
                  accepted.int64Value > 0,
                  accepted.compare(NSNumber(value: accepted.int64Value)) == .orderedSame,
                  let data = try JSONSerialization.jsonObject(with: Data(contentsOf: completedStore.file(id))) as? [String: Any],
                  data["device_seq"] as? NSNumber == accepted else { throw HealthSyncError.code("invalid_ack") }
            try completedStore.removeUpload(id) {
                if (data["failures"] as? [[String: Any]])?.isEmpty == true { $0.initialBackfillPending = false }
                for key in $0.completed.keys where $0.completed[key]?.uploadID == id { $0.completed[key]?.uploadID = nil }
            }
            if store == nil { store = completedStore }
            finishWaiters(id, result: .success(()))
        } catch {
            // Only the locally gated continuation may act on device errors.
            // Inaccessible storage retains its outbox and cannot prove ACK.
            Task { @MainActor [weak self] in
                guard let self, self.epoch == token, (try? self.requireAccount()) != nil else { return }
                self.uploadingIDs.remove(id)
                let terminal = Self.isTerminal(error) || (error as? HealthSyncError)?.code == "stale_seq"
                if terminal, let store = self.store, store.state.uploads.first == id {
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
                self.finishWaiters(id, result: .failure(error))
                await self.handle(error, expectedEpoch: token)
                // A retired payload is progress; a transient failure only retries.
                if terminal { if self.epoch == token { self.requestConvergence() } } else { self.scheduleRetry(token) }
            }
            return
        }
        // URLSession's completion barrier waits only for local accounting.
        // Network work belongs to the convergence owner of this epoch.
        if epoch == token { requestConvergence() }
    }

    /// Starts, or coalesces into, the single convergence pass of the current
    /// epoch. A task from a retired epoch is never reused.
    @discardableResult
    private func requestConvergence() -> Task<Void, Never> {
        let token = epoch
        if let convergeTask, convergeEpoch == token {
            convergeAgain = true
            return convergeTask
        }
        convergeAgain = false
        convergeEpoch = token
        let task = Task { @MainActor [weak self] in
            guard let self else { return }
            repeat {
                self.convergeAgain = false
                await self.convergencePass(token)
            } while self.convergeAgain && self.epoch == token && !Task.isCancelled
            if self.convergeEpoch == token {
                self.convergeTask = nil
                self.convergeEpoch = nil
            }
        }
        convergeTask = task
        return task
    }

    private func retireConvergence() {
        convergeTask?.cancel()
        convergeTask = nil
        convergeEpoch = nil
        convergeAgain = false
        markManifestDue()
    }

    /// Foreground polling first, then the due manifest, unready reservations,
    /// results and the outbox. Lost authority stops the pass; transient failures
    /// are recorded, later steps still run, and one retry is scheduled at the end.
    private func convergencePass(_ token: UUID) async {
        guard networkAuthorized(token) else { return }
        var transient = false
        if foreground { beginPolling() }
        if manifestDue {
            // The setup manifest from pairing does not clear this. A success clears it
            // only if nothing made it due again and the reported delivery is current.
            let generation = manifestGeneration
            let background = backgroundDelivery
            do {
                try await manifest(background: background)
                if epoch == token, generation == manifestGeneration, background == backgroundDelivery {
                    manifestDue = false
                }
            } catch { transient = await recordFailure(error, token) || transient }
            guard networkAuthorized(token) else { return }
        }
        if let store {
            for reservation in Array(store.state.completed.values) where !reservation.ready {
                do { _ = try await synchronize(requestID: reservation.id, waitForUpload: false) }
                catch { transient = await recordFailure(error, token) || transient }
                guard networkAuthorized(token) else { return }
            }
        }
        do { try await sendResults() } catch { transient = await recordFailure(error, token) || transient }
        guard networkAuthorized(token) else { return }
        do { try await drainUploads() } catch { transient = await recordFailure(error, token) || transient }
        if transient, networkAuthorized(token) { scheduleRetry(token) }
    }

    /// Applies the existing error policy and reports whether the failure is transient.
    private func recordFailure(_ error: Error, _ token: UUID) async -> Bool {
        await handle(error, expectedEpoch: token)
        return !Self.isTerminal(error)
    }

    private func networkAuthorized(_ token: UUID) -> Bool {
        epoch == token && !authorizationPaused && (try? requireAccount()) != nil && store?.state.suspended != true
    }

    private func finishWaiters(_ id: String, result: Result<Void, Error>) {
        for continuation in uploadWaiters.removeValue(forKey: id) ?? [] { continuation.resume(with: result) }
    }

    private func sendResults() async throws {
        guard let store, !store.state.suspended else { return }
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
            stopForegroundWork()
        } else if case .http(let status, _, let delay) = error, [429, 503].contains(status) {
            retryAfter = now().addingTimeInterval(max(1, delay ?? 30))
        }
    }

    /// The single foreground retry slot for every transient failure, never before
    /// Retry-After. Background recovery waits for existing system events.
    private func scheduleRetry(_ token: UUID) {
        guard retryTask == nil, foreground, networkAuthorized(token) else { return }
        retryTask = Task { [weak self] in
            guard let self else { return }
            try? await Task.sleep(for: .seconds(max(5, self.retryAfter?.timeIntervalSince(self.now()) ?? 30)))
            guard !Task.isCancelled, self.epoch == token else { return }
            self.retryTask = nil
            self.requestConvergence()
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
                              capturedIdentity: HealthDeviceIdentity? = nil,
                              preservingConnection: UUID? = nil) -> UUID {
        if let expectedEpoch, expectedEpoch != epoch { return epoch }
        let old = capturedIdentity ?? identity ?? (try? credentials.load())
        epoch = UUID()
        let stoppedEpoch = epoch
        connecting = nil
        answerConsent(false)
        identity = nil
        firstSyncHelpDeviceID = nil
        authorizationPaused = false
        authorizationResumePending = false
        authorizationResumeID = nil
        retireConvergence()
        reader.stopObserving()
        uploader.invalidate()
        stopForegroundWork()
        syncTask?.cancel()
        syncTask = nil
        syncID = nil
        for id in Array(uploadWaiters.keys) { finishWaiters(id, result: .failure(HealthSyncError.code("connection_changed"))) }
        cleanupPending = true
        let cleanupStore = store ?? (try? HealthDeviceStore(directory: directory))
        do { try cleanupStore?.prepareForCleanup() } catch { notice = "health.cleanup_failed" }
        var credentialsCleared = false
        do { try credentials.clear(); credentialsCleared = true } catch { notice = "health.cleanup_failed" }
        store = nil
        uploadingIDs.removeAll()
        retryAfter = nil
        if let attempt = connectionAttempt, attempt.id != preservingConnection {
            // Logout/supersession frees its bridge owner only after authority
            // and local credentials have been retired. The late task still has
            // its captured identity and can only revoke that identity.
            connectionAttempt = nil
            attempt.finish("failed")
        }
        let previous = cleanupTask
        cleanupsRunning += 1
        cleanupTask = Task { @MainActor in
            defer { self.cleanupsRunning -= 1 }
            Self.logger.info("Health cleanup waiting for previous cleanup")
            await previous?.value
            Self.logger.info("Health cleanup waiting for upload cancellation")
            await self.uploader.cancelAll()
            Self.logger.info("Health cleanup waiting for background delivery shutdown")
            let disabled = await self.reader.disableBackgroundDelivery()
            Self.logger.info("Health background delivery shutdown completed")
            guard self.epoch == stoppedEpoch else {
                if unregister, let old { await self.revoke(old) }
                return
            }
            if credentialsCleared && disabled {
                do {
                    // Explicit cleanup intent: an unreadable store is removed by
                    // directory, without decoding it.
                    if let cleanupStore { try cleanupStore.clear() }
                    else { try HealthDeviceStore.removeDirectory(self.directory) }
                    self.cleanupPending = false
                } catch { self.notice = "health.cleanup_failed" }
            } else { self.notice = "health.cleanup_failed" }
            self.resumeAuthorizedConnection()
            if unregister, let old { await self.revoke(old) }
        }
        return stoppedEpoch
    }

    @discardableResult
    private func stopLocal(unregister: Bool, expectedEpoch: UUID? = nil,
                           capturedIdentity: HealthDeviceIdentity? = nil,
                           preservingConnection: UUID? = nil) async -> UUID {
        let stopped = beginCleanup(unregister: unregister, expectedEpoch: expectedEpoch,
                                   capturedIdentity: capturedIdentity, preservingConnection: preservingConnection)
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

private extension ISO8601DateFormatter {
    /// RFC 3339 with fractional seconds; the plain form is tried after it.
    static let healthTicket: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
}
