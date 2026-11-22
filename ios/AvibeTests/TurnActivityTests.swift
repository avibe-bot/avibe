import ActivityKit
import XCTest
import SwiftUI
import WebKit
@testable import Avibe

@MainActor
final class TurnActivityTests: XCTestCase {
    private var defaults: UserDefaults!
    private var suite: String!
    private var cards: [FakeTurnActivity] = []
    private var calls: [(String, String, [String: Any]?)] = []
    private let target = TurnActivityCoordinator.Target(accountID: "account-a", instanceOrigin: "https://one.example", storeID: UUID())
    private let initial: [String: Any] = ["turn_id": "turn-1", "phase": "working", "started_at": 1795000000, "confirmed_at": 1795000900]

    override func setUp() {
        super.setUp()
        suite = "TurnActivityTests.\(UUID())"
        defaults = UserDefaults(suiteName: suite)!
        cards = []; calls = []
    }
    override func tearDown() {
        defaults.removePersistentDomain(forName: suite)
        defaults = nil
        super.tearDown()
    }
    private func coordinator(enabled: Bool = true, now: @escaping @MainActor () -> Date = { Date() }, response: TurnActivityCoordinator.Request? = nil,
                             mayRequest: @escaping @MainActor (TurnActivityCoordinator.Target) -> Bool = { _ in true },
                             active: @escaping @MainActor () -> Bool = { true },
                             onCreate: @escaping @MainActor (FakeTurnActivity) throws -> Void = { _ in },
                             background: @escaping TurnActivityCoordinator.BackgroundTask = { _ in {} }) -> TurnActivityCoordinator {
        let owner = TurnActivityCoordinator(defaults: defaults, now: now, enabled: { enabled }, activities: { self.cards }, create: { attributes, state in
            let card = FakeTurnActivity(attributes: attributes, content: state)
            try onCreate(card)
            self.cards.append(card)
            return card
        }, request: { target, method, path, body in
            self.calls.append((method, path, body))
            if let response { return try await response(target, method, path, body) }
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
            return .init(status: 200, json: ["activity": self.initial.merging(["status": "tracking"]) { _, new in new }], redirected: false)
        }, mayRequest: mayRequest, isActive: active, beginBackgroundTask: background)
        owner.selectTarget(target)
        return owner
    }
    private func observe(_ owner: TurnActivityCoordinator, sessionID: String = "session-1", running: Bool = true) async {
        await owner.observe(.init(sessionID: sessionID, agentName: "首席安全官", sessionTitle: "依赖审计 · avault 🔒", running: running, target: target))
    }
    private func start(_ owner: TurnActivityCoordinator) async {
        await owner.setSetting(true)
        await observe(owner)
        await owner.willDeactivate(registrationID: "registration-1")?.value
    }

    func testEveryRelayVectorDecodesAsUnixSeconds() throws {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "contract_vectors", withExtension: "json"))
        let fixture = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        let cases = try XCTUnwrap(fixture["relay_cases"] as? [[String: Any]])
        XCTAssertFalse(cases.isEmpty)
        for item in cases {
            let apns = try XCTUnwrap(item["apns"] as? [String: Any])
            let payload = try XCTUnwrap(apns["body_utf8"] as? String)
            struct Envelope: Decodable {
                struct APS: Decodable {
                    let content: TurnActivityAttributes.ContentState
                    enum CodingKeys: String, CodingKey { case content = "content-state" }
                }
                let aps: APS
            }
            let state = try JSONDecoder().decode(Envelope.self, from: Data(payload.utf8)).aps.content
            let request = try XCTUnwrap(item["request"] as? [String: Any])
            XCTAssertEqual(state.startedAt, request["started_at"] as? Int)
            XCTAssertEqual(state.confirmedAt, request["confirmed_at"] as? Int)
            XCTAssertEqual(state.endedAt, request["ended_at"] as? Int)
            XCTAssertEqual(state.phase.rawValue, request["phase"] as? String)
        }
    }

    func testStartKeepsNamesLocalAndRegistersOnlyContractFields() async throws {
        let owner = coordinator()
        await start(owner)
        let card = try XCTUnwrap(cards.first)
        XCTAssertEqual(card.attributes.agentName, "首席安全官")
        XCTAssertEqual(card.attributes.sessionTitle, "依赖审计 · avault 🔒")
        let put = try XCTUnwrap(calls.first { $0.0 == "PUT" }?.2)
        XCTAssertEqual(Set(put.keys), ["session_id", "turn_id", "push_registration_id", "activity_token", "requested_at"])
        XCTAssertEqual(put["activity_token"] as? String, "0102ff")
        await owner.endAll(reason: "logout")
    }


    func testDismissedTurnNeverRecreatedAcrossRelaunch() async throws {
        let owner = coordinator()
        await start(owner)
        let card = try XCTUnwrap(cards.first)
        card.state = .dismissed
        await owner.reconcile(target: target)
        let relaunched = coordinator()
        await relaunched.reconcile(target: target)
        XCTAssertEqual(cards.count, 1)
        await start(relaunched)
        XCTAssertEqual(cards.count, 1)
        XCTAssertTrue(calls.contains { $0.0 == "DELETE" && $0.2?["reason"] as? String == "dismissed" })
        await relaunched.endAll(reason: "logout")
    }

    func testMissingCardAfterRelaunchRetiresServerTracking() async {
        let owner = coordinator()
        await start(owner)
        _ = owner.invalidateRequests()
        cards.removeAll()
        let relaunched = coordinator()
        await relaunched.reconcile(target: target)
        XCTAssertTrue(cards.isEmpty)
        XCTAssertTrue(calls.contains { $0.0 == "DELETE" })
        await start(relaunched)
        await relaunched.endAll(reason: "logout")
    }

    /// A row kept from an earlier launch names an instance address. Once the
    /// session no longer vouches for that address (it left the account's
    /// list, or avibe.bot has not confirmed the session within its
    /// lifetime), nothing is sent to it: the row goes, with no request.
    func testAKeptRowSendsNothingToAnAddressTheSessionNoLongerVouchesFor() async {
        let owner = coordinator()
        await start(owner)
        _ = owner.invalidateRequests()
        cards.removeAll()
        calls.removeAll()
        var asked: [TurnActivityCoordinator.Target] = []
        let relaunched = coordinator(mayRequest: { target in
            asked.append(target)
            return false
        })
        await relaunched.reconcile(target: target)
        XCTAssertFalse(asked.isEmpty)
        XCTAssertTrue(asked.allSatisfy { $0 == target })
        XCTAssertTrue(calls.isEmpty)
        // The row is gone: a later launch asks nothing more for it.
        calls.removeAll()
        let later = coordinator()
        await later.reconcile(target: target)
        XCTAssertTrue(calls.isEmpty)
    }

    func testAccountAndInstanceBoundariesEndEveryCardImmediately() async {
        for reason in ["logout", "account_switch", "instance_switch", "disabled"] {
            defaults.removePersistentDomain(forName: suite)
            let owner = coordinator()
            await start(owner)
            await owner.endAll(reason: reason)
            XCTAssertTrue(cards.allSatisfy { $0.state == .dismissed })
            XCTAssertTrue(calls.contains { $0.0 == "DELETE" && $0.2?["reason"] as? String == reason })
            cards.removeAll()
        }
    }

    func testRemovedInstanceEndsCardEvenAfterSelectionWasCleared() async throws {
        let owner = coordinator()
        await start(owner)
        owner.selectTarget(nil)
        await owner.removeInstances(outside: [], accountID: target.accountID)
        XCTAssertEqual(try XCTUnwrap(cards.first).state, .dismissed)
        XCTAssertTrue(calls.contains { $0.0 == "DELETE" && $0.2?["reason"] as? String == "instance_switch" })
    }


    func testSelectionChangeWhileTurnFetchSuspendedCannotCreateCard() async {
        var resume: CheckedContinuation<Void, Never>?
        let owner = coordinator(response: { _, _, _, _ in
            await withCheckedContinuation { resume = $0 }
            return .init(status: 200, json: ["turn": self.initial], redirected: false)
        })
        let task = Task { await self.start(owner) }
        while resume == nil { await Task.yield() }
        owner.selectTarget(.init(accountID: "account-b", instanceOrigin: "https://two.example", storeID: UUID()))
        resume?.resume()
        await task.value
        XCTAssertTrue(cards.isEmpty)
        XCTAssertFalse(calls.contains { $0.0 == "PUT" })
    }

    func testEndedPutAppliesFinalContentAndOneHourDismissal() async throws {
        let owner = coordinator(response: { _, _, path, _ in
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
            return .init(status: 409, json: ["error": "turn_not_active", "activity": [
                "phase": "done", "started_at": 1795000000, "ended_at": 1795003600,
                "confirmed_at": 1795003601, "status": "ended"]], redirected: false)
        })
        await start(owner)
        let card = try XCTUnwrap(cards.first)
        XCTAssertEqual(card.state, .ended)
        XCTAssertEqual(card.content.dismissalDate.timeIntervalSince1970, 1795007200)
        XCTAssertFalse(card.endedImmediately)
        await owner.endAll(reason: "logout")
    }

    func testDeepLinkOnlyResolvesAnOwnedLocalActivity() async throws {
        let owner = coordinator()
        await start(owner)
        let card = try XCTUnwrap(cards.first)
        XCTAssertEqual(owner.attributes(for: URL(string: "avibe-app://live-activity/\(card.id)")!)?.sessionID, "session-1")
        for raw in ["avibe-app://live-activity/unknown", "https://live-activity/\(card.id)", "avibe-app://live-activity/\(card.id)?session=other", "avibe-app://live-activity/../session"] {
            XCTAssertNil(owner.attributes(for: URL(string: raw)!))
        }
        await owner.endAll(reason: "logout")
    }

    func testTokenRotationReplacesTokenWithoutTransmittingNames() async throws {
        let rotated = expectation(description: "rotated token registered")
        let owner = coordinator(response: { _, method, path, body in
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
            if method == "PUT", body?["activity_token"] as? String == "aabb" { rotated.fulfill() }
            return .init(status: 200, json: ["activity": self.initial], redirected: false)
        })
        await start(owner)
        let card = try XCTUnwrap(cards.first)
        while card.tokenContinuation == nil { await Task.yield() }
        card.token = Data([0xaa, 0xbb])
        card.tokenContinuation?.yield(card.token!)
        await fulfillment(of: [rotated], timeout: 2)
        for call in calls where call.0 == "PUT" {
            XCTAssertEqual(Set(call.2!.keys), ["session_id", "turn_id", "push_registration_id", "activity_token", "requested_at"])
        }
        await owner.endAll(reason: "logout")
    }

    func testFailedRotatedTokenBecomesUnregisteredAcrossRelaunch() async throws {
        let rotated = expectation(description: "rotated token attempted")
        let owner = coordinator(response: { _, method, path, body in
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
            if method == "PUT", body?["activity_token"] as? String == "aabb" {
                rotated.fulfill()
                return .init(status: 503, json: ["error": "service_unavailable"], redirected: false)
            }
            return .init(status: 200, json: ["activity": self.initial], redirected: false)
        })
        await start(owner)
        let card = try XCTUnwrap(cards.first)
        while card.tokenContinuation == nil { await Task.yield() }
        card.token = Data([0xaa, 0xbb])
        card.tokenContinuation?.yield(card.token!)
        await fulfillment(of: [rotated], timeout: 2)
        _ = owner.invalidateRequests()
        calls.removeAll()
        let relaunched = coordinator()
        await relaunched.reconcile(target: target)
        XCTAssertEqual(calls.map { $0.0 }, ["PUT"])
        XCTAssertEqual(calls.first?.2?["activity_token"] as? String, "aabb")
        await relaunched.endAll(reason: "logout")
    }

    func testServiceUnavailablePreservesCardAndForegroundRetries() async throws {
        var unavailable = true
        let retry = expectation(description: "foreground retried registration")
        let owner = coordinator(response: { _, method, path, _ in
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
            if unavailable { return .init(status: 503, json: ["error": "service_unavailable"], redirected: false) }
            if method == "PUT" { retry.fulfill() }
            return .init(status: 200, json: ["activity": self.initial], redirected: false)
        })
        await start(owner)
        let card = try XCTUnwrap(cards.first)
        XCTAssertEqual(card.state, .active)
        // Observing the initial token must not duplicate the first PUT.
        for _ in 0..<30 { await Task.yield() }
        unavailable = false
        await owner.reconcile(target: target)
        await fulfillment(of: [retry], timeout: 2)
        XCTAssertEqual(card.state, .active)
        XCTAssertFalse(calls.contains { $0.0 == "DELETE" })
        await owner.endAll(reason: "logout")
    }

    func testRegisteredForegroundOnlyGetsAcrossRelaunch() async throws {
        let owner = coordinator()
        await start(owner)
        _ = owner.invalidateRequests()
        calls.removeAll()
        let relaunched = coordinator()
        await relaunched.reconcile(target: target)
        for _ in 0..<20 { await Task.yield() }
        XCTAssertEqual(calls.map { $0.0 }, ["GET"])
        await relaunched.endAll(reason: "logout")
    }

    func testForegroundRegistersTokenRotatedWhileTerminatedBeforeGettingStoppedRow() async throws {
        let owner = coordinator()
        await start(owner)
        _ = owner.invalidateRequests()
        let card = try XCTUnwrap(cards.first)
        card.token = Data([0xaa, 0xbb]) // No stream event while the process was absent.
        calls.removeAll()
        let relaunched = coordinator(response: { _, method, _, _ in
            let status = method == "GET" ? "stopped" : "tracking"
            return .init(status: 200, json: ["activity": self.initial.merging(["status": status]) { _, new in new }], redirected: false)
        })
        await relaunched.reconcile(target: target)
        for _ in 0..<20 { await Task.yield() }
        XCTAssertEqual(calls.map { $0.0 }, ["PUT"])
        XCTAssertEqual(calls.first?.2?["activity_token"] as? String, "aabb")
        XCTAssertEqual(card.state, .active)
        await relaunched.endAll(reason: "logout")
    }

    func testForegroundRotation503PersistsUnregisteredTokenForNextLaunch() async throws {
        let owner = coordinator()
        await start(owner)
        _ = owner.invalidateRequests()
        let card = try XCTUnwrap(cards.first)
        card.token = Data([0xaa, 0xbb])
        calls.removeAll()
        let offline = coordinator(response: { _, _, _, _ in
            .init(status: 503, json: ["error": "service_unavailable"], redirected: false)
        })
        await offline.reconcile(target: target)
        for _ in 0..<20 { await Task.yield() }
        XCTAssertEqual(calls.map { $0.0 }, ["PUT"])
        XCTAssertEqual(card.state, .active)
        _ = offline.invalidateRequests()
        calls.removeAll()
        let recovered = coordinator()
        await recovered.reconcile(target: target)
        XCTAssertEqual(calls.map { $0.0 }, ["PUT"])
        XCTAssertEqual(calls.first?.2?["activity_token"] as? String, "aabb")
        XCTAssertEqual(card.state, .active)
        calls.removeAll()
        await recovered.reconcile(target: target)
        XCTAssertEqual(calls.map { $0.0 }, ["GET"])
        await recovered.endAll(reason: "logout")
    }

    func testStoppedGetForOldTokenCannotRetireNewRegistration() async throws {
        let fetching = expectation(description: "registered GET suspended")
        let rotated = expectation(description: "replacement token registered")
        var resumeGet: CheckedContinuation<Void, Never>?
        let owner = coordinator(response: { _, method, path, body in
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
            if method == "GET" {
                await withCheckedContinuation { resumeGet = $0; fetching.fulfill() }
                return .init(status: 200, json: ["activity": self.initial.merging(["status": "stopped"]) { _, new in new }], redirected: false)
            }
            if method == "PUT", body?["activity_token"] as? String == "aabb" { rotated.fulfill() }
            return .init(status: 200, json: ["activity": self.initial], redirected: false)
        })
        await start(owner)
        let card = try XCTUnwrap(cards.first)
        let reconciliation = Task { await owner.reconcile(target: target) }
        await fulfillment(of: [fetching], timeout: 2)
        card.token = Data([0xaa, 0xbb])
        card.tokenContinuation?.yield(card.token!)
        await fulfillment(of: [rotated], timeout: 2)
        resumeGet?.resume()
        await reconciliation.value
        XCTAssertEqual(card.state, .active)
        XCTAssertFalse(calls.contains { $0.0 == "DELETE" })
        await owner.endAll(reason: "logout")
    }

    func testStoppedPutForOldTokenCannotRetireQueuedReplacement() async throws {
        let registering = expectation(description: "old token PUT suspended")
        let rotated = expectation(description: "latest token registered")
        var resumePut: CheckedContinuation<Void, Never>?
        let owner = coordinator(response: { _, method, path, body in
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
            if method == "PUT", body?["activity_token"] as? String == "aabb" {
                await withCheckedContinuation { resumePut = $0; registering.fulfill() }
                return .init(status: 200, json: ["activity": self.initial.merging(["status": "stopped"]) { _, new in new }], redirected: false)
            }
            if method == "PUT", body?["activity_token"] as? String == "ccdd" { rotated.fulfill() }
            return .init(status: 200, json: ["activity": self.initial], redirected: false)
        })
        await start(owner)
        let card = try XCTUnwrap(cards.first)
        card.token = Data([0xaa, 0xbb])
        card.tokenContinuation?.yield(card.token!)
        await fulfillment(of: [registering], timeout: 2)
        card.token = Data([0xcc, 0xdd])
        card.tokenContinuation?.yield(card.token!)
        resumePut?.resume()
        await fulfillment(of: [rotated], timeout: 2)
        XCTAssertEqual(card.state, .active)
        XCTAssertFalse(calls.contains { $0.0 == "DELETE" })
        await owner.endAll(reason: "logout")
    }

    func testUnregisteredNetworkFailurePersistsAndRetriesPutAcrossRelaunch() async throws {
        let owner = coordinator(response: { _, _, path, _ in
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
            throw URLError(.notConnectedToInternet)
        })
        await start(owner)
        XCTAssertEqual(try XCTUnwrap(cards.first).state, .active)
        _ = owner.invalidateRequests()
        calls.removeAll()
        let relaunched = coordinator()
        await relaunched.reconcile(target: target)
        for _ in 0..<20 { await Task.yield() }
        XCTAssertEqual(calls.map { $0.0 }, ["PUT"])
        calls.removeAll()
        await relaunched.reconcile(target: target)
        XCTAssertEqual(calls.map { $0.0 }, ["GET"])
        await relaunched.endAll(reason: "logout")
    }

    func testWebKitStatusZeroKeepsUnregisteredCard() async throws {
        let owner = coordinator(response: { _, _, path, _ in
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
            return .init(status: 0, json: nil, redirected: false)
        })
        await start(owner)
        XCTAssertEqual(try XCTUnwrap(cards.first).state, .active)
        calls.removeAll()
        await owner.reconcile(target: target)
        XCTAssertEqual(calls.map { $0.0 }, ["PUT"])
        await owner.endAll(reason: "logout")
    }

    func testUnregisteredExpiresAfterEightHoursWithoutAdvancingStaleDate() async throws {
        var clock = Date(timeIntervalSince1970: 1795001000)
        let owner = coordinator(now: { clock }, response: { _, _, path, _ in
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
            return .init(status: 503, json: ["error": "service_unavailable"], redirected: false)
        })
        await start(owner)
        let card = try XCTUnwrap(cards.first)
        clock = clock.addingTimeInterval(28799)
        await owner.reconcile(target: target)
        XCTAssertEqual(card.state, .active)
        XCTAssertEqual(card.content.confirmedAt, self.initial["confirmed_at"] as? Int)
        clock = clock.addingTimeInterval(1)
        await owner.reconcile(target: target)
        XCTAssertEqual(card.state, .dismissed)
    }

    func testInitialRegistrationRefusalsEndImmediately() async throws {
        for (status, error) in [(403, "registration_not_owned"),
                                          (404, "not_found"),
                                          (400, "invalid_live_activity"),
                                          (409, "activity_conflict"),
                                          (409, "activity_expired")] {
            defaults.removePersistentDomain(forName: suite)
            let owner = coordinator(response: { _, _, path, _ in
                if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
                return .init(status: status, json: ["error": error], redirected: false)
            })
            await start(owner)
            XCTAssertEqual(try XCTUnwrap(cards.last).state, .dismissed)
            _ = owner.invalidateRequests()
            cards.removeAll()
        }
    }

    func testRequestedAtStaysFixedAcrossRelaunchRetriesAndRotation() async throws {
        var clock = Date(timeIntervalSince1970: 1795001000.9)
        let owner = coordinator(now: { clock }, response: { _, _, path, _ in
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
            return .init(status: 503, json: ["error": "service_unavailable"], redirected: false)
        })
        await start(owner)
        XCTAssertEqual(calls.first { $0.0 == "PUT" }?.2?["requested_at"] as? Int, 1795001000)
        _ = owner.invalidateRequests()
        clock = clock.addingTimeInterval(7200)
        let rotated = expectation(description: "new token carries original request time")
        let relaunched = coordinator(now: { clock }, response: { _, method, _, body in
            if method == "PUT", body?["activity_token"] as? String == "aabb" { rotated.fulfill() }
            return .init(status: 200, json: ["activity": self.initial], redirected: false)
        })
        await relaunched.reconcile(target: target)
        let card = try XCTUnwrap(cards.first)
        card.token = Data([0xaa, 0xbb])
        card.tokenContinuation?.yield(card.token!)
        await fulfillment(of: [rotated], timeout: 2)
        let puts = calls.filter { $0.0 == "PUT" }
        XCTAssertEqual(puts.count, 3)
        XCTAssertTrue(puts.allSatisfy { $0.2?["requested_at"] as? Int == 1795001000 })
        await relaunched.endAll(reason: "logout")
    }

    func testPutStoppedRetiresCardImmediatelyEvenWithTerminalContent() async throws {
        let owner = coordinator(response: { _, _, path, _ in
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
            return .init(status: 200, json: ["activity": ["status": "stopped", "phase": "done",
                "started_at": 1795000000, "ended_at": 1795001000, "confirmed_at": 1795001000]], redirected: false)
        })
        await start(owner)
        let card = try XCTUnwrap(cards.first)
        XCTAssertEqual(card.state, .dismissed)
        XCTAssertTrue(card.endedImmediately)
        calls.removeAll()
        await owner.reconcile(target: target)
        XCTAssertTrue(calls.isEmpty)
    }

    func testRegisteredStoppedOverridesNewerContentAndTerminalPhase() async throws {
        var stopped = false
        let owner = coordinator(response: { _, _, path, _ in
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
            let body = stopped ? ["status": "stopped", "phase": "done", "started_at": 1795000000,
                                  "ended_at": 1795000100, "confirmed_at": 1795000100] : self.initial
            return .init(status: 200, json: ["activity": body], redirected: false)
        })
        await start(owner)
        stopped = true
        await owner.reconcile(target: target)
        XCTAssertEqual(try XCTUnwrap(cards.first).state, .dismissed)
    }

    func testRegistered503KeepsContentAndDoesNotPut() async throws {
        var unavailable = false
        let owner = coordinator(response: { _, _, path, _ in
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
            return unavailable ? .init(status: 503, json: ["error": "service_unavailable"], redirected: false)
                : .init(status: 200, json: ["activity": self.initial], redirected: false)
        })
        await start(owner)
        calls.removeAll()
        unavailable = true
        await owner.reconcile(target: target)
        for _ in 0..<20 { await Task.yield() }
        XCTAssertEqual(calls.map { $0.0 }, ["GET"])
        XCTAssertEqual(try XCTUnwrap(cards.first).state, .active)
        await owner.endAll(reason: "logout")
    }

    func testSystemEndRetainsCardAcrossForegroundAndRelaunch() async throws {
        let owner = coordinator()
        await start(owner)
        let card = try XCTUnwrap(cards.first)
        while card.stateContinuation == nil { await Task.yield() }
        card.state = .ended
        card.content.phase = .done
        card.content.endedAt = card.content.confirmedAt
        card.stateContinuation?.yield(.ended)
        for _ in 0..<30 { await Task.yield() }
        let relaunched = coordinator()
        await relaunched.reconcile(target: target)
        XCTAssertEqual(card.state, .ended)
        XCTAssertFalse(card.endedImmediately)
        XCTAssertNotNil(relaunched.attributes(for: URL(string: "avibe-app://live-activity/\(card.id)")!))
        await relaunched.endAll(reason: "logout")
    }


    func testLockScreenProgressCapabilityReplacesLegacyCapability() {
        XCTAssertEqual(NativeShellMetadata.capabilities["lockScreenProgress"], 1)
        XCTAssertNil(NativeShellMetadata.capabilities["liveActivities"])
        for method in ["setting", "setSetting", "observe", "openSettings", "status", "start", "stop"] {
            guard case .success(let request) = NativeBridgeRequest.parse(["method": "liveActivity." + method, "params": [:]]) else {
                XCTFail("Missing bridge method: " + method); continue
            }
            XCTAssertEqual(request.method.rawValue, "liveActivity." + method)
        }
    }

    func testBridgeRefusesV1AndDispatchesOnlyV2() async throws {
        let origin = try XCTUnwrap(WebOrigin(url: URL(string: target.instanceOrigin)!))
        var received: [NativeBridgeMethod] = []
        let webView = WKWebView(frame: .zero, configuration: NativeBridgeConfiguration.make(
            instanceOrigin: origin, websiteDataStore: .nonPersistent(), setOuterScrollLocked: { _, _ in false },
            liveActivity: { request, _ in
                received.append(request.method)
                return ["ok": true]
            }))
        let loader = TurnActivityPageLoader()
        webView.navigationDelegate = loader
        try await loader.loadHTML("<!doctype html><title>Live Activity bridge</title>", baseURL: origin.url, in: webView)
        let result = try await webView.callAsyncJavaScript("""
            const codes = [];
            for (const [method, params] of [
              ['liveActivity.status', {sessionId: 'session-1'}],
              ['liveActivity.start', {sessionId: 'session-1', agentName: 'Agent', sessionTitle: null}],
              ['liveActivity.stop', {sessionId: 'session-1'}],
              ['liveActivity.setting', {}],
              ['liveActivity.setSetting', {enabled: true}],
              ['liveActivity.observe', {sessionId: null}],
              ['liveActivity.openSettings', {}],
            ]) {
              try { await window.avibeNative.call(method, params); codes.push('ok'); }
              catch (error) { codes.push(error.code); }
            }
            return codes;
            """, arguments: [:], in: nil, contentWorld: .page)
        XCTAssertEqual(result as? [String], ["unsupported", "unsupported", "unsupported", "ok", "ok", "ok", "ok"])
        XCTAssertEqual(received, [.liveActivitySetting, .liveActivitySetSetting, .liveActivityObserve, .liveActivityOpenSettings])
    }

    /// At launch every card an earlier build left ends at once, and the
    /// ledger goes, with no instance request then or at a later sign-out.
    func testEndingAllLocallyDismissesEveryCardAndForgetsTheLedgerWithoutRequests() async throws {
        let owner = coordinator()
        await start(owner)
        XCTAssertFalse(cards.isEmpty)
        calls.removeAll()

        await owner.endAllLocally()

        for card in cards {
            XCTAssertEqual(card.state, .dismissed)
        }
        XCTAssertTrue(calls.isEmpty)
        let stored = try XCTUnwrap(defaults.data(forKey: "avibe.liveActivities.turns.v1"))
        let ledger = try XCTUnwrap(JSONSerialization.jsonObject(with: stored) as? [String: Any])
        XCTAssertEqual((ledger["records"] as? [String: Any])?.isEmpty, true)

        await owner.endAll(reason: "logout")
        XCTAssertFalse(calls.contains { $0.0 == "DELETE" })
    }


    func testDeviceSettingDefaultsOffAndPersistsWithoutStartingAnything() async {
        let owner = coordinator()
        XCTAssertEqual(owner.setting()["enabled"] as? Bool, false)
        XCTAssertEqual(owner.setting()["systemEnabled"] as? Bool, true)
        await observe(owner)
        calls.removeAll()
        XCTAssertNil(owner.willDeactivate(registrationID: "r"))
        XCTAssertTrue(cards.isEmpty)
        XCTAssertTrue(calls.isEmpty)
        await owner.setSetting(true)
        XCTAssertTrue(coordinator().deviceEnabled)
    }

    func testDepartureNeedsActiveAppSystemPermissionAndPushRegistration() async {
        for (systemEnabled, active, registration) in [(false, true, "r" as String?), (true, false, "r"), (true, true, nil)] {
            let owner = coordinator(enabled: systemEnabled, active: { active })
            await owner.setSetting(true)
            await observe(owner)
            calls.removeAll()
            XCTAssertNil(owner.willDeactivate(registrationID: registration))
            XCTAssertTrue(cards.isEmpty)
            XCTAssertTrue(calls.isEmpty)
        }
    }

    func testRepeatedDepartureAndForegroundNeverStartSameTurnTwice() async {
        let owner = coordinator()
        await owner.setSetting(true)
        await observe(owner)
        XCTAssertTrue(cards.isEmpty)
        let task = owner.willDeactivate(registrationID: "r")
        XCTAssertEqual(cards.count, 1) // Created before this synchronous call returns.
        XCTAssertNil(owner.willDeactivate(registrationID: "r"))
        await task?.value
        await owner.foreground(target: target)
        XCTAssertNil(owner.willDeactivate(registrationID: "r"))
        XCTAssertEqual(cards.count, 1)
        await owner.endAll(reason: "logout")
    }

    func testNewTurnCreatesSynchronouslyBeforeDisplacedCardEnds() async throws {
        var turn = initial
        var leaseBegins = 0
        var leaseEnds = 0
        let owner = coordinator(response: { _, _, path, _ in
            .init(status: 200, json: [path.contains("/turn?") ? "turn" : "activity": turn], redirected: false)
        }, onCreate: { _ in
            if let previous = self.cards.last { XCTAssertEqual(previous.state, .active) }
        }, background: { _ in
            leaseBegins += 1
            return { leaseEnds += 1 }
        })
        await start(owner)
        let old = try XCTUnwrap(cards.first)
        turn["turn_id"] = "turn-2"
        await owner.foreground(target: target)
        let task = owner.willDeactivate(registrationID: "r")
        XCTAssertEqual(cards.count, 2)
        XCTAssertEqual(old.state, .active)
        XCTAssertEqual(leaseBegins, 2)
        await task?.value
        XCTAssertEqual(old.state, .dismissed)
        XCTAssertEqual(leaseEnds, 2)
        XCTAssertTrue(calls.contains { $0.0 == "DELETE" && $0.2?["reason"] as? String == "replaced" })
        await owner.endAll(reason: "logout")
    }

    func testTurningOffEndsCardAndReenableCannotRepeatTurn() async {
        let owner = coordinator()
        await start(owner)
        await owner.setSetting(false)
        XCTAssertEqual(cards.first?.state, .dismissed)
        XCTAssertTrue(calls.contains { $0.0 == "DELETE" && $0.2?["reason"] as? String == "disabled" })
        await owner.setSetting(true)
        XCTAssertNil(owner.willDeactivate(registrationID: "r"))
        XCTAssertEqual(cards.count, 1)
    }

    func testReplacementFencesSuspendedGetBeforeOldCardFinishesEnding() async throws {
        var turn = initial
        var resumeGet: CheckedContinuation<Void, Never>?
        var resumeEnd: CheckedContinuation<Void, Never>?
        let owner = coordinator(response: { _, method, path, _ in
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": turn], redirected: false) }
            if method == "GET" {
                await withCheckedContinuation { resumeGet = $0 }
                return .init(status: 200, json: ["activity": self.initial.merging([
                    "phase": "done", "ended_at": 1795001000, "confirmed_at": 1795001000
                ]) { _, new in new }], redirected: false)
            }
            return .init(status: 200, json: ["activity": turn], redirected: false)
        })
        await start(owner)
        let old = try XCTUnwrap(cards.first)
        let reconciliation = Task { await owner.reconcile(target: target) }
        while resumeGet == nil { await Task.yield() }
        turn["turn_id"] = "turn-2"
        await observe(owner, sessionID: "session-2")
        old.beforeEnd = { await withCheckedContinuation { resumeEnd = $0 } }
        let departure = owner.willDeactivate(registrationID: "r")
        let saved = try XCTUnwrap(defaults.data(forKey: "avibe.liveActivities.turns.v1"))
        let ledger = try XCTUnwrap(JSONSerialization.jsonObject(with: saved) as? [String: Any])
        let records = try XCTUnwrap(ledger["records"] as? [String: Any])
        XCTAssertNil(records[old.id], "A relaunch must not restore the displaced registration")
        while resumeEnd == nil { await Task.yield() }
        resumeGet?.resume()
        await reconciliation.value
        XCTAssertEqual(old.state, .active)
        XCTAssertEqual(old.content.phase, .working)
        old.beforeEnd = nil
        resumeEnd?.resume()
        await departure?.value
        XCTAssertEqual(old.state, .dismissed)
        XCTAssertTrue(old.endedImmediately)
        await owner.endAll(reason: "logout")
    }

    func testObserveDeduplicatesAndForegroundRefreshesWithoutCreating() async {
        let owner = coordinator()
        await owner.setSetting(true)
        await observe(owner)
        await observe(owner)
        XCTAssertEqual(calls.count, 1)
        await owner.foreground(target: target)
        XCTAssertEqual(calls.count, 2)
        await observe(owner, running: false)
        XCTAssertNil(owner.willDeactivate(registrationID: "r"))
        await observe(owner)
        await owner.observe(nil)
        XCTAssertNil(owner.willDeactivate(registrationID: "r"))
        XCTAssertEqual(calls.count, 3)
        XCTAssertTrue(cards.isEmpty)
    }

    func testPrefetchFailuresClearPreviouslyCachedTurn() async {
        var status = 200
        let owner = coordinator(response: { _, _, _, _ in
            if status == 0 { throw URLError(.notConnectedToInternet) }
            return .init(status: status, json: ["turn": self.initial], redirected: false)
        })
        await owner.setSetting(true)
        for failure in [404, 409, 503, 0] {
            status = 200
            await observe(owner)
            await owner.foreground(target: target)
            status = failure
            await owner.foreground(target: target)
            XCTAssertNil(owner.willDeactivate(registrationID: "r"))
            XCTAssertTrue(cards.isEmpty)
        }
    }

    func testStalePrefetchCannotReplaceNewObservation() async {
        var resume: CheckedContinuation<Void, Never>?
        let owner = coordinator(response: { _, _, _, _ in
            await withCheckedContinuation { resume = $0 }
            return .init(status: 200, json: ["turn": self.initial], redirected: false)
        })
        await owner.setSetting(true)
        let old = Task { await self.observe(owner) }
        while resume == nil { await Task.yield() }
        await owner.observe(nil)
        resume?.resume()
        await old.value
        XCTAssertNil(owner.willDeactivate(registrationID: "r"))
        XCTAssertTrue(cards.isEmpty)
    }

    func testRequestFailureDoesNotConsumeTurnAndCanRetryDeparture() async {
        var fail = true
        let owner = coordinator(onCreate: { _ in if fail { throw URLError(.unknown) } })
        await owner.setSetting(true)
        await observe(owner)
        await owner.willDeactivate(registrationID: "r")?.value
        XCTAssertTrue(cards.isEmpty)
        fail = false
        await owner.willDeactivate(registrationID: "r")?.value
        XCTAssertEqual(cards.count, 1)
        await owner.endAll(reason: "logout")
    }

    func testExpiryOfBackgroundTaskRetiresNewCardButKeepsTurnConsumed() async {
        var expire: (@MainActor () -> Void)?
        var ended = false
        let owner = coordinator(onCreate: { card in card.token = nil }, background: { callback in
            expire = callback
            return { ended = true }
        })
        await owner.setSetting(true)
        await observe(owner)
        let task = owner.willDeactivate(registrationID: "r")
        expire?()
        await task?.value
        XCTAssertEqual(cards.first?.state, .dismissed)
        XCTAssertTrue(ended)
        XCTAssertNil(owner.willDeactivate(registrationID: "r"))
        XCTAssertFalse(calls.contains { $0.0 == "PUT" })
        await owner.endAll(reason: "logout")
    }

    func testFailedReplacementPreservesOldCardAndTokenAndStateObservers() async throws {
        var fail = false
        var turn = initial
        let rotated = expectation(description: "old card still registers token rotations")
        let dismissed = expectation(description: "old card still observes dismissal")
        let owner = coordinator(response: { _, method, path, body in
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": turn], redirected: false) }
            if method == "PUT", body?["activity_token"] as? String == "aabb" { rotated.fulfill() }
            if method == "DELETE", body?["reason"] as? String == "dismissed" { dismissed.fulfill() }
            return .init(status: 200, json: ["activity": self.initial], redirected: false)
        }, onCreate: { _ in if fail { throw URLError(.unknown) } })
        await start(owner)
        let old = try XCTUnwrap(cards.first)
        turn["turn_id"] = "turn-2"
        await observe(owner, sessionID: "session-2")
        fail = true
        XCTAssertNil(owner.willDeactivate(registrationID: "r"))
        XCTAssertEqual(cards.count, 1)
        XCTAssertEqual(old.state, .active)
        XCTAssertFalse(calls.contains { $0.0 == "DELETE" })
        old.token = Data([0xaa, 0xbb])
        old.tokenContinuation?.yield(old.token!)
        await fulfillment(of: [rotated], timeout: 2)
        old.state = .dismissed
        old.stateContinuation?.yield(.dismissed)
        await fulfillment(of: [dismissed], timeout: 2)
    }

    func testTurnThatOutlivesActivityLimitStaysConsumedAfterRelaunch() async {
        var clock = Date(timeIntervalSince1970: 1795001000)
        let owner = coordinator(now: { clock })
        await start(owner)
        await owner.endAll(reason: "system_ended")
        _ = owner.invalidateRequests()
        clock = clock.addingTimeInterval(9 * 3600)
        let relaunched = coordinator(now: { clock })
        await observe(relaunched)
        XCTAssertNil(relaunched.willDeactivate(registrationID: "r"))
        XCTAssertEqual(cards.count, 1)
    }

    func testLegacyLedgerCleanupKeepsShownTurnAndRemovesTokensWithoutRequests() async throws {
        let owner = coordinator()
        await start(owner)
        _ = owner.invalidateRequests()
        let data = try XCTUnwrap(defaults.data(forKey: "avibe.liveActivities.turns.v1"))
        var old = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        old.removeValue(forKey: "shownTurns") // Released v1 ledger shape.
        defaults.set(try JSONSerialization.data(withJSONObject: old), forKey: "avibe.liveActivities.turns.v1")
        defaults.removeObject(forKey: "avibe.liveActivities.enabled")
        calls.removeAll()
        let relaunched = coordinator()
        XCTAssertFalse(relaunched.deviceEnabled)
        await relaunched.endAllLocally()
        await relaunched.reconcile(target: target)
        XCTAssertTrue(calls.isEmpty)
        XCTAssertEqual(cards.first?.state, .dismissed)
        await relaunched.setSetting(true)
        await observe(relaunched)
        XCTAssertNil(relaunched.willDeactivate(registrationID: "r"))
        XCTAssertEqual(cards.count, 1)
    }

    func testTokenTimeoutEndsCardAndConsumesTurn() async {
        let owner = coordinator(onCreate: { $0.token = nil })
        await start(owner)
        XCTAssertEqual(cards.first?.state, .dismissed)
        XCTAssertFalse(calls.contains { $0.0 == "PUT" })
        XCTAssertNil(owner.willDeactivate(registrationID: "r"))
    }

    func testSelectionSwitchClearsObservationAndEndsOldInstanceCard() async {
        let owner = coordinator()
        await start(owner)
        let other = TurnActivityCoordinator.Target(accountID: "other", instanceOrigin: "https://other.example", storeID: UUID())
        owner.selectTarget(other)
        await owner.reconcile(target: other)
        XCTAssertEqual(cards.first?.state, .dismissed)
        XCTAssertTrue(calls.contains { $0.0 == "DELETE" && $0.2?["reason"] as? String == "instance_switch" })
        await observe(owner) // Old instance cannot repopulate the observation.
        XCTAssertNil(owner.willDeactivate(registrationID: "r"))
    }

    func testCardDetailsResolveKeysAndRequestedLanguage() {
        for phase in [TurnActivityAttributes.Phase.working, .needsYou, .done, .failed, .stopped] {
            let shown = TurnActivityShown(state: .init(phase: phase, startedAt: 1795000000,
                endedAt: phase.isRunning ? nil : 1795000600, confirmedAt: 1795000600), isStale: false)
            let english = shown.detail(locale: Locale(identifier: "en"))
            let chinese = shown.detail(locale: Locale(identifier: "zh-Hans"))
            XCTAssertFalse(english.contains("turn."))
            XCTAssertFalse(chinese.contains("turn."))
            XCTAssertNotEqual(english, chinese)
            if !phase.isRunning {
                XCTAssertTrue(english.contains("10"))
                XCTAssertTrue(chinese.contains("10"))
            }
        }
        let stale = TurnActivityShown(state: .init(phase: .working, startedAt: 1795000000, confirmedAt: 1795000600), isStale: true)
        XCTAssertTrue(stale.detail(locale: Locale(identifier: "zh-Hans")).contains("最后确认"))
    }

    func testCardRenderMatrix() throws {
        let attributes = TurnActivityAttributes(agentName: "首席安全官", sessionTitle: "依赖审计 · avault 🔒",
            accountID: "a", instanceOrigin: "https://one.example", sessionID: "s", turnID: "t")
        for language in ["en", "zh-Hans"] {
            for dark in [false, true] {
                let rows = VStack(spacing: 12) {
                    ForEach(["working", "needsYou", "done", "failed", "stopped", "stale"], id: \.self) { phase in
                        let state = TurnActivityAttributes.ContentState(phase: .init(rawValue: phase) ?? .working,
                            startedAt: Int(Date().timeIntervalSince1970) - 600,
                            endedAt: ["done", "failed", "stopped"].contains(phase) ? Int(Date().timeIntervalSince1970) : nil,
                            confirmedAt: Int(Date().timeIntervalSince1970) - 10)
                        TurnActivityCard(attributes: attributes, state: state, isStale: phase == "stale")
                            .padding(16).background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 24))
                    }
                }.padding(12).frame(width: 390)
                    .background(Color(uiColor: .systemBackground))
                    .environment(\.locale, Locale(identifier: language))
                    .environment(\.colorScheme, dark ? .dark : .light)
                let renderer = ImageRenderer(content: rows)
                renderer.scale = 2
                let image = try XCTUnwrap(renderer.uiImage)
                let attachment = XCTAttachment(image: image)
                attachment.name = "turn-cards-\(language)-\(dark ? "dark" : "light")"
                attachment.lifetime = .keepAlways
                add(attachment)
            }
        }
    }

    func testNamesUseGraphemeLimitsAndStaleRunningStateStopsTimer() {
        let name = String(repeating: "👨‍👩‍👧‍👦", count: 41)
        XCTAssertEqual(TurnActivityAttributes.displayName(name, limit: 40).count, 40)
        XCTAssertNil(TurnActivityAttributes.displayTitle(" Agent ", agentName: "Agent"))
        for phase in [TurnActivityAttributes.Phase.working, .needsYou] {
            let shown = TurnActivityShown(state: .init(phase: phase, startedAt: 1, confirmedAt: 10), isStale: true)
            XCTAssertFalse(shown.runsTimer)
            XCTAssertEqual(shown.statusKey, "turn.stale")
        }
    }
}

@MainActor
private final class FakeTurnActivity: TurnActivityHandle {
    let id = UUID().uuidString
    let attributes: TurnActivityAttributes
    var state = ActivityState.active
    var content: TurnActivityAttributes.ContentState
    var token: Data? = Data([1, 2, 255])
    var endedImmediately = false
    var beforeEnd: (@MainActor () async -> Void)?
    var tokenContinuation: AsyncStream<Data>.Continuation?
    var stateContinuation: AsyncStream<ActivityState>.Continuation?
    var tokens: AsyncStream<Data> { AsyncStream { tokenContinuation = $0 } }
    var states: AsyncStream<ActivityState> { AsyncStream { stateContinuation = $0 } }
    init(attributes: TurnActivityAttributes, content: TurnActivityAttributes.ContentState) {
        self.attributes = attributes; self.content = content
    }
    func update(_ content: TurnActivityAttributes.ContentState) async { self.content = content }
    func end(_ content: TurnActivityAttributes.ContentState?, immediately: Bool) async {
        await beforeEnd?()
        if let content { self.content = content }
        endedImmediately = immediately
        state = immediately ? .dismissed : .ended
        stateContinuation?.yield(state)
    }
}

@MainActor
private final class TurnActivityPageLoader: NSObject, WKNavigationDelegate {
    private var continuation: CheckedContinuation<Void, Error>?
    func loadHTML(_ html: String, baseURL: URL, in webView: WKWebView) async throws {
        try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            webView.loadHTMLString(html, baseURL: baseURL)
        }
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation?) {
        continuation?.resume(); continuation = nil
    }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation?, withError error: Error) {
        continuation?.resume(throwing: error); continuation = nil
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation?, withError error: Error) {
        continuation?.resume(throwing: error); continuation = nil
    }
}
