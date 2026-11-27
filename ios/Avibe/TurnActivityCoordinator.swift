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
/// recreates an Activity. Only an admitted scene departure can create one.
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
        var shownTurns: [String: Int] = [:]
        enum CodingKeys: CodingKey { case records, shownTurns }
        init() {}
        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            records = try values.decode([String: Record].self, forKey: .records)
            shownTurns = try values.decodeIfPresent([String: Int].self, forKey: .shownTurns) ?? [:]
            // An older build's surviving cards count as already shown, too.
            for record in records.values { shownTurns[record.turnID] = record.requestedAt }
        }
    }
    struct Observation: Equatable {
        let sessionID: String
        let agentName: String
        let sessionTitle: String?
        let running: Bool
        let target: Target
    }
    private struct PrefetchedTurn {
        let id: String
        let content: TurnActivityAttributes.ContentState
    }
    typealias BackgroundTask = @MainActor (@escaping @MainActor () -> Void) -> (@MainActor () -> Void)
    private var observation: Observation?
    private var prefetched: PrefetchedTurn?
    private var observationRevision = 0
    private var departureTask: Task<Void, Never>?
    private let beginBackgroundTask: BackgroundTask
    private let isActive: @MainActor () -> Bool
    private static let settingKey = "avibe.liveActivities.enabled"
    /// Lock Screen and Dynamic Island progress ships (back on 11-27, after the
    /// black-card fix). Off, the setting reads and stays off, nothing starts a
    /// card, and a card an earlier build left ends at launch and whenever the
    /// App comes to the front. Switching off also means removing the
    /// `lockScreenProgress` line from NativeShellMetadata.capabilities by hand.
    static let shipsLockScreenProgress = true
    private let available: Bool
    var deviceEnabled: Bool { available && defaults.bool(forKey: Self.settingKey) }

    typealias Request = @MainActor (Target, String, String, [String: Any]?) async throws -> VoiceCaptureInstanceClient.Response
    typealias Create = @MainActor (TurnActivityAttributes, TurnActivityAttributes.ContentState) throws -> any TurnActivityHandle

    private let defaults: UserDefaults
    private let now: @MainActor () -> Date
    private let enabled: @MainActor () -> Bool
    private let activities: @MainActor () -> [any TurnActivityHandle]
    private let create: Create
    private let requestOverride: Request?
    /// Whether a request may go to a record's instance now: the session
    /// lists that address for that account, and avibe.bot confirmed the
    /// session within its lifetime (`MobileSessionConfirmation`). A row
    /// kept from an earlier launch names an address that may since have
    /// left the account; nothing is loaded from it or sent to it then.
    private let mayRequest: @MainActor (Target) -> Bool
    private var clients: [UUID: VoiceCaptureInstanceClient] = [:]
    private var ledger: Ledger
    private var tokenTasks: [String: Task<Void, Never>] = [:]
    private var stateTasks: [String: Task<Void, Never>] = [:]
    private var locallyEnding: Set<String> = []
    private var epoch = 0
    private var starting = false
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
         }, request: Request? = nil,
         mayRequest: @escaping @MainActor (Target) -> Bool,
         available: Bool = TurnActivityCoordinator.shipsLockScreenProgress,
         isActive: @escaping @MainActor () -> Bool = { UIApplication.shared.applicationState == .active },
         beginBackgroundTask: @escaping BackgroundTask = { expiration in
             let lease = TurnActivityBackgroundLease(expiration: expiration)
             return { lease.end() }
         }) {
        self.isActive = isActive
        self.beginBackgroundTask = beginBackgroundTask
        self.defaults = defaults
        self.available = available
        // Switched off: a setting an earlier build stored on is forced off.
        if !available { defaults.set(false, forKey: Self.settingKey) }
        self.mayRequest = mayRequest
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

    func setting() -> [String: Any] {
        ["enabled": deviceEnabled, "systemEnabled": enabled()]
    }

    func setSetting(_ value: Bool) async {
        let value = value && available
        defaults.set(value, forKey: Self.settingKey)
        if !value { await endAll(reason: "disabled") }
    }

    func observe(_ value: Observation?) async {
        // Switched off: no observation, so no prefetch request and nothing to start.
        guard available else { return }
        guard value?.target == selectedTarget || value == nil else { return }
        guard observation != value else { return }
        observation = value
        await refreshObservation()
    }

    private func clearObservation() {
        observation = nil
        prefetched = nil
        observationRevision += 1
    }

    private func refreshObservation() async {
        observationRevision += 1
        let revision = observationRevision
        prefetched = nil
        guard let observation, observation.running, observation.target == selectedTarget else { return }
        var query = URLComponents()
        query.queryItems = [URLQueryItem(name: "session_id", value: observation.sessionID)]
        do {
            let response = try await request(observation.target, "GET", Self.base + "/turn?" + (query.percentEncodedQuery ?? ""), nil)
            guard revision == observationRevision, response.status == 200, !response.redirected,
                  let turn = response.json?["turn"] as? [String: Any],
                  let id = turn["turn_id"] as? String, !id.isEmpty,
                  let content = Self.content(turn), content.phase.isRunning else { return }
            prefetched = PrefetchedTurn(id: id, content: content)
        } catch { /* A failed prefetch cannot authorize a departure start. */ }
    }

    func foreground(target: Target?) async {
        // Switched off: a card still showing (an earlier build's) ends here.
        guard available else { await endAll(reason: "disabled"); return }
        guard target == selectedTarget else { return }
        await refreshObservation()
        await reconcile(target: target)
    }

    /// Called synchronously by UIScene.willDeactivateNotification. There is no
    /// suspension before Activity.request: UIKit may leave active on return.
    @discardableResult
    func willDeactivate(registrationID: String?) -> Task<Void, Never>? {
        guard !starting, isActive(), deviceEnabled, enabled() else { return nil }
        guard let observation, observation.running, observation.target == selectedTarget,
              mayRequest(observation.target) else { return nil }
        ledger.shownTurns = ledger.shownTurns.filter { now().timeIntervalSince1970 - Double($0.value) < 30 * 86400 }
        guard let turn = prefetched, ledger.shownTurns[turn.id] == nil else { return nil }
        guard let registrationID else { return nil }
        starting = true
        let generation = epoch
        let displaced = activities()
        let oldRecords = Array(ledger.records.values)
        let activity: any TurnActivityHandle
        let requestedAt = Int(now().timeIntervalSince1970)
        do {
            activity = try create(TurnActivityAttributes(
                agentName: TurnActivityAttributes.displayName(observation.agentName, limit: 40),
                sessionTitle: TurnActivityAttributes.displayTitle(observation.sessionTitle, agentName: observation.agentName),
                accountID: observation.target.accountID, instanceOrigin: observation.target.instanceOrigin,
                sessionID: observation.sessionID, turnID: turn.id), turn.content)
        } catch {
            starting = false
            return nil // The previous card and its observers remain untouched.
        }
        for old in displaced {
            locallyEnding.insert(old.id)
            tokenTasks.removeValue(forKey: old.id)?.cancel()
            stateTasks.removeValue(forKey: old.id)?.cancel()
        }
        // Persist retirement together with the replacement. Even if the process
        // exits before end(), an old card cannot register or accept a suspended
        // GET; its captured row is only used for best-effort DELETE.
        ledger.records.removeAll()
        let record = Record(id: activity.id, target: observation.target, sessionID: observation.sessionID,
                            turnID: turn.id, registrationID: registrationID, requestedAt: requestedAt)
        ledger.records[record.id] = record
        ledger.shownTurns[turn.id] = requestedAt
        save()
        let endBackgroundTask = beginBackgroundTask { [weak self] in self?.departureTask?.cancel() }
        let task = Task { @MainActor in
            defer { endBackgroundTask(); self.starting = false; self.departureTask = nil }
            for old in displaced { await old.end(nil, immediately: true) }
            for old in oldRecords { await self.remove(old, reason: "replaced") }
            guard generation == self.epoch, !Task.isCancelled else {
                await activity.end(nil, immediately: true)
                return
            }
            self.observeState(activity, record: record)
            let token = await self.firstToken(activity)
            guard generation == self.epoch, !Task.isCancelled, let token,
                  !self.locallyEnding.contains(record.id),
                  activity.state == .active || activity.state == .stale else {
                await self.finish(activity, record: record, reason: "system_ended")
                return
            }
            do {
                if try await self.register(token, activity: activity, record: record, generation: generation) {
                    self.observeTokens(activity, record: record)
                }
            } catch {
                if case Failure.unavailable = error {
                    self.observeTokens(activity, record: record)
                } else {
                    await self.finish(activity, record: record, reason: "system_ended")
                }
            }
        }
        departureTask = task
        return task
    }

    func endAll(reason: String, invalidate: Bool = true) async {
        if invalidate { epoch += 1; departureTask?.cancel() }
        let current = activities()
        let missing = ledger.records.values.filter { record in !current.contains { $0.id == record.id } }
        for activity in current {
            await finish(activity, record: ledger.records[activity.id], reason: reason)
        }
        // Use the captured set: a cleanup must never consume a newer start's row.
        for record in missing { await remove(record, reason: reason) }
    }

    /// Startup with the device setting off retires old cards locally before
    /// account reconciliation. Keep the shown-Turn history, never its tokens.
    func endAllLocally() async {
        epoch += 1
        tokenTasks.values.forEach { $0.cancel() }
        tokenTasks.removeAll()
        stateTasks.values.forEach { $0.cancel() }
        stateTasks.removeAll()
        for activity in activities() {
            ledger.shownTurns[activity.attributes.turnID] = ledger.shownTurns[activity.attributes.turnID] ?? Int(now().timeIntervalSince1970)
        }
        ledger.records.removeAll()
        save()
        for activity in activities() where activity.state != .dismissed {
            await activity.end(nil, immediately: true)
        }
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
        clearObservation()
        epoch += 1
        tokenTasks.values.forEach { $0.cancel() }
        tokenTasks.removeAll()
    }

    func reconcile(target: Target?) async {
        guard target == selectedTarget else { return }
        guard deviceEnabled else { return }
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
        clearObservation()
        departureTask?.cancel()
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
    /// A request that got no answer from the instance this soon lost its
    /// connection rather than ran out of time.
    static let lostConnectionWithin: TimeInterval = 5

    private func request(_ target: Target, _ method: String, _ path: String, _ body: [String: Any]?) async throws -> VoiceCaptureInstanceClient.Response {
        // The card's hidden WebView does not hold off the instance page's
        // direct hand-over (`holdsOffHandover` below): with Lock Screen
        // progress on it would refuse one through every running Turn. So a
        // hand-over may close the connection a request was about to use.
        // Such a request fails at once, with no answer, and is made once
        // more; every request here is safe to repeat (two reads, a replace,
        // a removal). One that waited out its deadline is not: the
        // instance is not answering, and a second wait helps nobody.
        let started = now()
        do {
            let response = try await requestOnce(target, method, path, body)
            if response.status != 0 || now().timeIntervalSince(started) >= Self.lostConnectionWithin { return response }
        } catch VoiceCaptureInstanceClient.Failure.offline where now().timeIntervalSince(started) < Self.lostConnectionWithin {
            // Made again below.
        }
        return try await requestOnce(target, method, path, body)
    }

    private func requestOnce(_ target: Target, _ method: String, _ path: String, _ body: [String: Any]?) async throws -> VoiceCaptureInstanceClient.Response {
        // The one way out to an instance: refused for an address the
        // session no longer vouches for. A refused removal is left to the
        // instance's own expiry.
        guard mayRequest(target) else { throw NativePushError.unavailable }
        if let requestOverride { return try await requestOverride(target, method, path, body) }
        guard let url = URL(string: target.instanceOrigin), let origin = WebOrigin(url: url) else { throw NativePushError.unavailable }
        let id = UUID()
        let client = VoiceCaptureInstanceClient(target: VoiceCaptureTarget(accountID: target.accountID,
            instanceID: "", origin: origin, storeIdentifier: target.storeID), holdsOffHandover: false)
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

/// Keeps UIKit's expiration handler and the normal completion on one owner.
@MainActor
private final class TurnActivityBackgroundLease {
    private var identifier = UIBackgroundTaskIdentifier.invalid
    init(expiration: @escaping @MainActor () -> Void) {
        identifier = UIApplication.shared.beginBackgroundTask(withName: "TurnActivityRegistration") { [weak self] in
            MainActor.assumeIsolated { expiration(); self?.end() }
        }
    }
    func end() {
        guard identifier != .invalid else { return }
        UIApplication.shared.endBackgroundTask(identifier)
        identifier = .invalid
    }
}
