import ActivityKit
import Foundation
import UIKit

@MainActor
protocol TurnActivityHandle: AnyObject {
    var id: String { get }
    var attributes: TurnActivityAttributes { get }
    var state: ActivityState { get }
    var content: TurnActivityAttributes.ContentState { get }
    var token: Data? { get }
    var tokens: AsyncStream<Data> { get }
    var states: AsyncStream<ActivityState> { get }
    func update(_ content: TurnActivityAttributes.ContentState) async
    func end(_ content: TurnActivityAttributes.ContentState?, immediately: Bool) async
}

@MainActor
final class SystemTurnActivity: TurnActivityHandle {
    let activity: Activity<TurnActivityAttributes>
    init(_ activity: Activity<TurnActivityAttributes>) { self.activity = activity }
    var id: String { activity.id }
    var attributes: TurnActivityAttributes { activity.attributes }
    var state: ActivityState { activity.activityState }
    var content: TurnActivityAttributes.ContentState { activity.content.state }
    var token: Data? { activity.pushToken }
    var tokens: AsyncStream<Data> {
        AsyncStream { continuation in
            let task = Task {
                for await token in activity.pushTokenUpdates { continuation.yield(token) }
                continuation.finish()
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
    var states: AsyncStream<ActivityState> {
        AsyncStream { continuation in
            let task = Task {
                for await state in activity.activityStateUpdates { continuation.yield(state) }
                continuation.finish()
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
    func update(_ content: TurnActivityAttributes.ContentState) async {
        await activity.update(ActivityContent(state: content, staleDate: content.staleDate))
    }
    func end(_ content: TurnActivityAttributes.ContentState?, immediately: Bool) async {
        let final = content ?? self.content
        await activity.end(ActivityContent(state: final, staleDate: nil),
                           dismissalPolicy: immediately ? .immediate : .after(final.dismissalDate))
    }
}

/// One owner for requests, token rotation, dismissal and account boundaries.
/// The ledger detects a card removed while the process was away; it never
/// recreates an Activity. Only an explicit, admitted bridge start can do that.
@MainActor
final class TurnActivityCoordinator {
    struct Target: Codable, Equatable {
        let accountID: String
        let instanceOrigin: String
        let storeID: UUID
    }
    struct Record: Codable {
        let id: String
        let target: Target
        let sessionID: String
        let turnID: String
        let registrationID: String
        let requestedAt: Int
        var registeredAt: Date?
        var observedToken: Data?
    }
    private struct Ledger: Codable {
        var records: [String: Record] = [:]
    }
    typealias Request = @MainActor (Target, String, String, [String: Any]?) async throws -> VoiceCaptureInstanceClient.Response
    typealias Create = @MainActor (TurnActivityAttributes, TurnActivityAttributes.ContentState) throws -> any TurnActivityHandle

    private let defaults: UserDefaults
    private let now: @MainActor () -> Date
    private let enabled: @MainActor () -> Bool
    private let activities: @MainActor () -> [any TurnActivityHandle]
    private let create: Create
    private let requestOverride: Request?
    private var clients: [UUID: VoiceCaptureInstanceClient] = [:]
    private var ledger: Ledger
    private var tokenTasks: [String: Task<Void, Never>] = [:]
    private var stateTasks: [String: Task<Void, Never>] = [:]
    private var locallyEnding: Set<String> = []
    private var epoch = 0
    private var starting = false
    private var startingSessionID: String?
    private var selectedTarget: Target?
    private var authorizationTask: Task<Void, Never>?
    private var mutations: [String: Task<Bool, Error>] = [:]
    private enum Failure: Error { case unavailable, rejected(String) }
    private static let ledgerKey = "avibe.liveActivities.turns.v1"

    init(defaults: UserDefaults = .standard, now: @escaping @MainActor () -> Date = { Date() },
         enabled: @escaping @MainActor () -> Bool = { ActivityAuthorizationInfo().areActivitiesEnabled },
         activities: @escaping @MainActor () -> [any TurnActivityHandle] = { Activity<TurnActivityAttributes>.activities.map { SystemTurnActivity($0) } },
         create: @escaping Create = { attributes, state in
             SystemTurnActivity(try Activity.request(attributes: attributes,
                 content: ActivityContent(state: state, staleDate: state.staleDate), pushType: .token))
         }, request: Request? = nil) {
        self.defaults = defaults
        self.now = now
        self.enabled = enabled
        self.activities = activities
        self.create = create
        requestOverride = request
        ledger = defaults.data(forKey: Self.ledgerKey).flatMap {
            try? JSONDecoder().decode(Ledger.self, from: $0)
        } ?? Ledger()
    }

    func observeAuthorization() {
        guard authorizationTask == nil else { return }
        authorizationTask = Task { [weak self] in
            for await enabled in ActivityAuthorizationInfo().activityEnablementUpdates {
                guard !Task.isCancelled else { return }
                if !enabled { await self?.endAll(reason: "disabled") }
            }
        }
    }

    deinit {
        authorizationTask?.cancel()
        tokenTasks.values.forEach { $0.cancel() }
        stateTasks.values.forEach { $0.cancel() }
    }

    func status(sessionID: String, target: Target) -> [String: Any] {
        ["enabled": enabled(), "tracking": activities().contains {
            $0.state != .dismissed && $0.attributes.sessionID == sessionID
                && $0.attributes.accountID == target.accountID
                && $0.attributes.instanceOrigin == target.instanceOrigin
        }]
    }

    func start(sessionID: String, agentName: String, sessionTitle: String?,
               target: Target, registrationID: String?) async -> [String: Any] {
        func failure(_ code: String) -> [String: Any] { ["ok": false, "code": code] }
        guard enabled() else { return failure("disabled") }
        guard let registrationID else { return failure("push_unavailable") }
        guard !starting else { return failure("failed") }
        starting = true
        startingSessionID = sessionID
        defer { starting = false; startingSessionID = nil }
        let generation = epoch
        var created: (any TurnActivityHandle)?
        do {
            var query = URLComponents()
            query.queryItems = [URLQueryItem(name: "session_id", value: sessionID)]
            let response = try await request(target, "GET", Self.base + "/turn?" + (query.percentEncodedQuery ?? ""), nil)
            guard generation == epoch else { return failure("failed") }
            if response.status == 409 || response.status == 404 { return failure("no_active_turn") }
            guard response.status == 200, !response.redirected,
                  let turn = response.json?["turn"] as? [String: Any],
                  let turnID = turn["turn_id"] as? String,
                  let content = Self.content(turn), content.phase.isRunning else { return failure("failed") }
            if activities().contains(where: {
                $0.attributes.accountID == target.accountID && $0.attributes.instanceOrigin == target.instanceOrigin
                    && $0.attributes.turnID == turnID && $0.attributes.sessionID == sessionID
                    && ($0.state == .active || $0.state == .stale)
            }) { return ["ok": true] }
            // Replacement ends all cards, including a terminal card still on the Lock Screen.
            await endAll(reason: "replaced", invalidate: false)
            guard generation == epoch, enabled() else { return failure("failed") }
            let attributes = TurnActivityAttributes(
                agentName: TurnActivityAttributes.displayName(agentName, limit: 40),
                sessionTitle: TurnActivityAttributes.displayTitle(sessionTitle, agentName: agentName),
                accountID: target.accountID, instanceOrigin: target.instanceOrigin, sessionID: sessionID, turnID: turnID)
            let requestedAt = Int(now().timeIntervalSince1970)
            let activity = try create(attributes, content)
            created = activity
            let record = Record(id: activity.id, target: target, sessionID: sessionID,
                                turnID: turnID, registrationID: registrationID, requestedAt: requestedAt)
            ledger.records[record.id] = record
            save()
            observeState(activity, record: record)
            // Bound the initial token wait; a silent APNs registration must not hang the bridge.
            let token = await firstToken(activity)
            guard generation == epoch else {
                // The boundary owner already retires the row. Do not reopen its WebKit store.
                await activity.end(nil, immediately: true)
                return failure("failed")
            }
            guard !locallyEnding.contains(record.id), activity.state == .active || activity.state == .stale else {
                await finish(activity, record: record, reason: "user_stopped")
                return failure("failed")
            }
            guard let token else {
                await finish(activity, record: record, reason: "user_stopped")
                return failure("failed")
            }
            guard try await register(token, activity: activity, record: record, generation: generation) else {
                return failure("no_active_turn")
            }
            observeTokens(activity, record: record)
            return ["ok": true]
        } catch {
            // Once created, a missing PUT answer is recoverable on the next foreground.
            if let created, generation == epoch, let record = ledger.records[created.id] {
                if case Failure.unavailable = error {
                    observeTokens(created, record: record)
                    return ["ok": true]
                }
                await finish(created, record: record, reason: "user_stopped")
            }
            if case Failure.rejected(let code) = error { return failure(code) }
            return failure("failed")
        }
    }

    func stop(sessionID: String, target: Target) async {
        let matching = activities().filter { $0.attributes.sessionID == sessionID
            && $0.attributes.accountID == target.accountID && $0.attributes.instanceOrigin == target.instanceOrigin }
        guard !matching.isEmpty || startingSessionID == sessionID && selectedTarget == target else { return }
        epoch += 1
        for activity in matching {
            await finish(activity, record: ledger.records[activity.id], reason: "user_stopped")
        }
    }

    func endAll(reason: String, invalidate: Bool = true) async {
        if invalidate { epoch += 1 }
        let current = activities()
        let missing = ledger.records.values.filter { record in !current.contains { $0.id == record.id } }
        for activity in current {
            await finish(activity, record: ledger.records[activity.id], reason: reason)
        }
        // Use the captured set: a cleanup must never consume a newer start's row.
        for record in missing { await remove(record, reason: reason) }
    }

    func removeInstances(outside origins: Set<String>, accountID: String) async {
        let removed = activities().filter {
            $0.attributes.accountID == accountID && !origins.contains($0.attributes.instanceOrigin)
        }
        if let target = selectedTarget, target.accountID == accountID, !origins.contains(target.instanceOrigin) {
            selectTarget(nil)
        }
        for activity in removed {
            await finish(activity, record: ledger.records[activity.id], reason: "instance_switch")
        }
    }

    func selectTarget(_ target: Target?) {
        guard selectedTarget != target else { return }
        selectedTarget = target
        epoch += 1
        tokenTasks.values.forEach { $0.cancel() }
        tokenTasks.removeAll()
    }

    func reconcile(target: Target?) async {
        guard target == selectedTarget else { return }
        guard enabled() else { await endAll(reason: "disabled"); return }
        let generation = epoch
        let current = activities()
        for record in Array(ledger.records.values) where !current.contains(where: { $0.id == record.id }) {
            await remove(record, reason: "dismissed")
        }
        for activity in current {
            guard generation == epoch else { return }
            let record = ledger.records[activity.id]
            guard let target, activity.attributes.accountID == target.accountID,
                  activity.attributes.instanceOrigin == target.instanceOrigin else {
                await finish(activity, record: record, reason: "instance_switch")
                continue
            }
            guard let record else {
                await finish(activity, record: nil, reason: "system_ended")
                continue
            }
            if activity.state == .dismissed {
                await remove(record, reason: "dismissed")
                continue
            }
            if activity.state != .ended {
                observeState(activity, record: record)
                observeTokens(activity, record: record)
            }
            do {
                if record.registeredAt == nil || activity.token.map({ $0 != record.observedToken }) == true {
                    // A terminal PUT response already supplied the final one-hour card.
                    if activity.state == .ended { continue }
                    if now().timeIntervalSince1970 >= Double(record.requestedAt + 28800) {
                        await finish(activity, record: record, reason: "system_ended")
                    } else if let token = activity.token {
                        _ = try await register(token, activity: activity, record: record, generation: generation)
                    }
                    continue
                }
                let response = try await request(record.target, "GET", Self.base + "/" + record.id, nil)
                // A GET describes the token registered when it began. Rotation may
                // have started a PUT while this request was suspended.
                guard generation == epoch, let current = ledger.records[record.id],
                      current.registeredAt != nil, current.observedToken == record.observedToken,
                      activity.token == nil || activity.token == record.observedToken else { continue }
                if response.status == 404 {
                    await finish(activity, record: record, reason: "system_ended")
                } else if response.status == 200, let body = response.json?["activity"] as? [String: Any] {
                    await apply(body, activity: activity, record: record)
                }
            } catch { /* Offline: keep the server's stale deadline; never advance it locally. */ }
        }
    }

    /// Synchronously fences suspended work and releases account-owned WebViews.
    /// Actual logout cleanup runs in the account coordinator's awaited push hook.
    func invalidateRequests() -> [WebsiteDataStoreReleaseBarrier] {
        selectedTarget = nil
        epoch += 1
        tokenTasks.values.forEach { $0.cancel() }
        tokenTasks.removeAll()
        stateTasks.values.forEach { $0.cancel() }
        stateTasks.removeAll()
        let barriers = clients.values.compactMap(\.releaseBarrier)
        clients.values.forEach { $0.close() }
        clients.removeAll()
        return barriers
    }

    func attributes(for url: URL) -> TurnActivityAttributes? {
        guard let id = TurnActivityAttributes.activityID(from: url) else { return nil }
        return activities().first { $0.id == id && $0.state != .dismissed }?.attributes
    }

    private static let base = "/api/web-push/live-activities"
    private func save() {
        if let data = try? JSONEncoder().encode(ledger) { defaults.set(data, forKey: Self.ledgerKey) }
    }
    private func request(_ target: Target, _ method: String, _ path: String, _ body: [String: Any]?) async throws -> VoiceCaptureInstanceClient.Response {
        if let requestOverride { return try await requestOverride(target, method, path, body) }
        guard let url = URL(string: target.instanceOrigin), let origin = WebOrigin(url: url) else { throw NativePushError.unavailable }
        let id = UUID()
        let client = VoiceCaptureInstanceClient(target: VoiceCaptureTarget(accountID: target.accountID,
            instanceID: "", origin: origin, storeIdentifier: target.storeID))
        clients[id] = client
        defer { client.close(); clients.removeValue(forKey: id) }
        return try await client.request(method, path, json: body)
    }
    static func content(_ body: [String: Any]) -> TurnActivityAttributes.ContentState? {
        guard let phaseString = body["phase"] as? String, let phase = TurnActivityAttributes.Phase(rawValue: phaseString),
              let started = body["started_at"] as? Int, let confirmed = body["confirmed_at"] as? Int else { return nil }
        let ended = body["ended_at"] as? Int
        guard phase.isRunning || ended != nil else { return nil }
        return .init(phase: phase, startedAt: started, endedAt: ended, confirmedAt: confirmed)
    }
    private func firstToken(_ activity: any TurnActivityHandle) async -> Data? {
        if let token = activity.token { return token }
        let stream = activity.tokens
        return await withTaskGroup(of: Data?.self) { group in
            group.addTask { for await token in stream { return token }; return nil }
            group.addTask { try? await Task.sleep(for: .seconds(10)); return nil }
            let token = await group.next() ?? nil
            group.cancelAll()
            return token
        }
    }
    private func observeTokens(_ activity: any TurnActivityHandle, record: Record) {
        guard tokenTasks[record.id] == nil, !locallyEnding.contains(record.id) else { return }
        let generation = epoch
        // Subscribe before returning, so a rotation cannot fall between start and observation.
        let stream = activity.tokens
        tokenTasks[record.id] = Task { [weak self] in
            var previous = self?.ledger.records[record.id]?.observedToken ?? activity.token
            if let token = activity.token, token != previous {
                previous = token
                do { _ = try await self?.register(token, activity: activity, record: record, generation: generation) }
                catch { /* Retry an unregistered card on the next foreground. */ }
            }
            for await token in stream {
                guard let self, !Task.isCancelled, generation == self.epoch else { return }
                guard token != previous else { continue }
                previous = token
                do {
                    guard try await self.register(token, activity: activity, record: record, generation: generation) else { return }
                } catch { /* The next rotation or unregistered foreground retries. */ }
            }
        }
    }
    private func observeState(_ activity: any TurnActivityHandle, record: Record) {
        guard stateTasks[record.id] == nil else { return }
        stateTasks[record.id] = Task { [weak self] in
            for await state in activity.states {
                guard let self, !Task.isCancelled else { return }
                if state == .dismissed || state == .ended {
                    if !self.locallyEnding.contains(record.id) {
                        await self.remove(record, reason: state == .dismissed ? "dismissed" : "system_ended", keepCard: state == .ended)
                    }
                    return
                }
            }
        }
    }
    private func register(_ token: Data, activity: any TurnActivityHandle, record: Record, generation: Int) async throws -> Bool {
        guard generation == epoch, ledger.records[record.id] != nil, !locallyEnding.contains(record.id) else { return false }
        // Invalidate registration before yielding to queued work or a GET response.
        if ledger.records[record.id]?.observedToken != token {
            ledger.records[record.id]?.registeredAt = nil
            ledger.records[record.id]?.observedToken = token
            save()
        }
        let previous = mutations[record.id]
        let operation = Task { @MainActor in
            _ = try? await previous?.value
            return try await self.registerNow(token, activity: activity, record: record, generation: generation)
        }
        mutations[record.id] = operation
        return try await operation.value
    }

    private func registerNow(_ token: Data, activity: any TurnActivityHandle, record: Record, generation: Int) async throws -> Bool {
        guard generation == epoch, ledger.records[record.id] != nil, !locallyEnding.contains(record.id),
              activity.state == .active || activity.state == .stale else { return false }
        // Foreground reconciliation and the token stream can observe the same
        // rotation. A newer token supersedes queued work; a successful PUT needs
        // no duplicate registration for the same token.
        guard ledger.records[record.id]?.observedToken == token else { return true }
        if ledger.records[record.id]?.registeredAt != nil { return true }
        let response: VoiceCaptureInstanceClient.Response
        do {
            response = try await request(record.target, "PUT", Self.base + "/" + record.id, [
                "session_id": record.sessionID, "turn_id": record.turnID,
                "push_registration_id": record.registrationID, "requested_at": record.requestedAt,
                "activity_token": token.map { String(format: "%02x", $0) }.joined(),
            ])
        } catch { throw Failure.unavailable }
        guard generation == epoch, ledger.records[record.id] != nil, !locallyEnding.contains(record.id) else {
            // A PUT may have reached the instance after a concurrent DELETE.
            if selectedTarget == record.target {
                _ = try? await request(record.target, "DELETE", Self.base + "/" + record.id, ["reason": "system_ended"])
            }
            return false
        }
        // A response for an obsolete token cannot retire or register the current
        // token. Its observer will submit the replacement through this same queue.
        guard ledger.records[record.id]?.observedToken == token,
              activity.token == nil || activity.token == token else { return true }
        if response.status == 503 || response.status == 0 && !response.redirected { throw Failure.unavailable }
        if [400, 403, 404].contains(response.status) || response.status == 409 && ["activity_conflict", "activity_expired"].contains(response.errorCode ?? "") {
            // Retire locally here; remove() awaits this mutation and would deadlock.
            await retire(activity, record: record)
            throw Failure.rejected(response.status == 403 ? "push_unavailable" : "failed")
        }
        guard (response.status == 200 || response.status == 409 && response.errorCode == "turn_not_active"),
              let body = response.json?["activity"] as? [String: Any], Self.content(body) != nil else {
            throw NativePushError.invalidResponse
        }
        if response.status == 200 {
            ledger.records[record.id]?.registeredAt = now()
            save()
        }
        await apply(body, activity: activity, record: record)
        return response.status == 200
    }
    private func apply(_ body: [String: Any], activity: any TurnActivityHandle, record: Record) async {
        // Stopped is authoritative even when the last delivered content was newer.
        if body["status"] as? String == "stopped" {
            await retire(activity, record: record)
            return
        }
        guard let content = Self.content(body), content.confirmedAt >= activity.content.confirmedAt else { return }
        if !content.phase.isRunning {
            locallyEnding.insert(record.id)
            tokenTasks.removeValue(forKey: record.id)?.cancel()
            await activity.end(content, immediately: false)
        } else if activity.state == .active || activity.state == .stale {
            await activity.update(content)
        }
    }
    private func retire(_ activity: any TurnActivityHandle, record: Record) async {
        locallyEnding.insert(record.id)
        tokenTasks.removeValue(forKey: record.id)?.cancel()
        stateTasks.removeValue(forKey: record.id)?.cancel()
        ledger.records.removeValue(forKey: record.id)
        save()
        await activity.end(nil, immediately: true)
    }
    private func finish(_ activity: any TurnActivityHandle, record: Record?, reason: String) async {
        locallyEnding.insert(activity.id)
        tokenTasks.removeValue(forKey: activity.id)?.cancel()
        stateTasks.removeValue(forKey: activity.id)?.cancel()
        await activity.end(nil, immediately: true)
        if let record { await remove(record, reason: reason) }
    }
    private func remove(_ record: Record, reason: String, keepCard: Bool = false) async {
        tokenTasks.removeValue(forKey: record.id)?.cancel()
        if !keepCard { ledger.records.removeValue(forKey: record.id) }
        save()
        _ = try? await mutations[record.id]?.value
        _ = try? await request(record.target, "DELETE", Self.base + "/" + record.id, ["reason": reason])
    }
}
