import ActivityKit
import XCTest
import SwiftUI
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
    private func coordinator(enabled: Bool = true, response: TurnActivityCoordinator.Request? = nil) -> TurnActivityCoordinator {
        let owner = TurnActivityCoordinator(defaults: defaults, enabled: { enabled }, activities: { self.cards }, create: { attributes, state in
            let card = FakeTurnActivity(attributes: attributes, content: state)
            self.cards.append(card)
            return card
        }, request: { target, method, path, body in
            self.calls.append((method, path, body))
            if let response { return try await response(target, method, path, body) }
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
            return .init(status: 200, json: ["activity": self.initial.merging(["status": "tracking"]) { _, new in new }], redirected: false)
        })
        owner.selectTarget(target)
        return owner
    }
    private func start(_ owner: TurnActivityCoordinator) async -> [String: Any] {
        await owner.start(sessionID: "session-1", agentName: "首席安全官", sessionTitle: "依赖审计 · avault 🔒", target: target, registrationID: "registration-1")
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
        let result = await start(owner)
        XCTAssertEqual(result["ok"] as? Bool, true)
        let card = try XCTUnwrap(cards.first)
        XCTAssertEqual(card.attributes.agentName, "首席安全官")
        XCTAssertEqual(card.attributes.sessionTitle, "依赖审计 · avault 🔒")
        let put = try XCTUnwrap(calls.first { $0.0 == "PUT" }?.2)
        XCTAssertEqual(Set(put.keys), ["session_id", "turn_id", "push_registration_id", "activity_token"])
        XCTAssertEqual(put["activity_token"] as? String, "0102ff")
        await owner.endAll(reason: "logout")
    }

    func testDisabledAndMissingPushRefuseBeforeRequests() async {
        let owner = coordinator(enabled: false)
        let result = await start(owner)
        XCTAssertEqual(result["code"] as? String, "disabled")
        XCTAssertTrue(calls.isEmpty)
        let enabled = coordinator()
        let noPush = await enabled.start(sessionID: "s", agentName: "a", sessionTitle: nil, target: target, registrationID: nil)
        XCTAssertEqual(noPush["code"] as? String, "push_unavailable")
        XCTAssertTrue(calls.isEmpty)
    }

    func testDismissedTurnNeverRecreatedAcrossCoordinatorRelaunch() async throws {
        let owner = coordinator()
        _ = await start(owner)
        let card = try XCTUnwrap(cards.first)
        card.state = .dismissed
        await owner.reconcile(target: target)
        let relaunched = coordinator()
        let result = await start(relaunched)
        XCTAssertEqual(result["code"] as? String, "no_active_turn")
        XCTAssertEqual(cards.count, 1)
        XCTAssertTrue(calls.contains { $0.0 == "DELETE" && $0.2?["reason"] as? String == "dismissed" })
    }

    func testMissingCardAfterRelaunchRetiresServerTracking() async {
        let owner = coordinator()
        _ = await start(owner)
        _ = owner.invalidateRequests()
        cards.removeAll()
        let relaunched = coordinator()
        await relaunched.reconcile(target: target)
        let result = await start(relaunched)
        XCTAssertEqual(result["code"] as? String, "no_active_turn")
        XCTAssertTrue(calls.contains { $0.0 == "DELETE" })
    }

    func testAccountAndInstanceBoundariesEndEveryCardImmediately() async {
        for reason in ["logout", "account_switch", "instance_switch", "disabled"] {
            let owner = coordinator()
            _ = await owner.start(sessionID: reason, agentName: "Agent", sessionTitle: nil, target: target, registrationID: "r")
            await owner.endAll(reason: reason)
            XCTAssertTrue(cards.allSatisfy { $0.state == .dismissed })
            XCTAssertTrue(calls.contains { $0.0 == "DELETE" && $0.2?["reason"] as? String == reason })
            cards.removeAll()
        }
    }

    func testRemovedInstanceEndsCardEvenAfterSelectionWasCleared() async throws {
        let owner = coordinator()
        _ = await start(owner)
        owner.selectTarget(nil)
        await owner.removeInstances(outside: [], accountID: target.accountID)
        XCTAssertEqual(try XCTUnwrap(cards.first).state, .dismissed)
        XCTAssertTrue(calls.contains { $0.0 == "DELETE" && $0.2?["reason"] as? String == "instance_switch" })
    }

    func testExplicitStopCanBeRetriedWithoutDismissalSuppression() async {
        let owner = coordinator()
        _ = await start(owner)
        await owner.stop(sessionID: "session-1", target: target)
        let result = await start(owner)
        XCTAssertEqual(result["ok"] as? Bool, true)
        XCTAssertEqual(cards.filter { $0.state == .active }.count, 1)
        await owner.endAll(reason: "logout")
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
        let result = await task.value
        XCTAssertEqual(result["ok"] as? Bool, false)
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
        let result = await start(owner)
        XCTAssertEqual(result["code"] as? String, "no_active_turn")
        let card = try XCTUnwrap(cards.first)
        XCTAssertEqual(card.state, .ended)
        XCTAssertEqual(card.content.dismissalDate.timeIntervalSince1970, 1795007200)
        XCTAssertFalse(card.endedImmediately)
        await owner.endAll(reason: "logout")
    }

    func testDeepLinkOnlyResolvesAnOwnedLocalActivity() async throws {
        let owner = coordinator()
        _ = await start(owner)
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
        _ = await start(owner)
        let card = try XCTUnwrap(cards.first)
        while card.tokenContinuation == nil { await Task.yield() }
        card.token = Data([0xaa, 0xbb])
        card.tokenContinuation?.yield(card.token!)
        await fulfillment(of: [rotated], timeout: 2)
        for call in calls where call.0 == "PUT" {
            XCTAssertEqual(Set(call.2!.keys), ["session_id", "turn_id", "push_registration_id", "activity_token"])
        }
        await owner.endAll(reason: "logout")
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
        let result = await start(owner)
        XCTAssertEqual(result["code"] as? String, "failed")
        let card = try XCTUnwrap(cards.first)
        XCTAssertEqual(card.state, .active)
        // Let the first observer consume its failed upload before the next foreground.
        for _ in 0..<30 { await Task.yield() }
        unavailable = false
        await owner.reconcile(target: target)
        await fulfillment(of: [retry], timeout: 2)
        XCTAssertEqual(card.state, .active)
        XCTAssertFalse(calls.contains { $0.0 == "DELETE" })
        await owner.endAll(reason: "logout")
    }

    func testSystemEndRetainsCardAcrossForegroundAndRelaunch() async throws {
        let owner = coordinator()
        _ = await start(owner)
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

    func testAllBridgeMethodsAreRecognizedAndCapabilityIsVersionOne() {
        XCTAssertEqual(NativeShellMetadata.capabilities["liveActivities"], 1)
        for method in ["status", "start", "stop", "openSettings"] {
            guard case .success(let request) = NativeBridgeRequest.parse(["method": "liveActivity." + method, "params": [:]]) else {
                XCTFail("Missing bridge method: " + method); continue
            }
            XCTAssertEqual(request.method.rawValue, "liveActivity." + method)
        }
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
    var tokenContinuation: AsyncStream<Data>.Continuation?
    var stateContinuation: AsyncStream<ActivityState>.Continuation?
    var tokens: AsyncStream<Data> { AsyncStream { tokenContinuation = $0 } }
    var states: AsyncStream<ActivityState> { AsyncStream { stateContinuation = $0 } }
    init(attributes: TurnActivityAttributes, content: TurnActivityAttributes.ContentState) {
        self.attributes = attributes; self.content = content
    }
    func update(_ content: TurnActivityAttributes.ContentState) async { self.content = content }
    func end(_ content: TurnActivityAttributes.ContentState?, immediately: Bool) async {
        if let content { self.content = content }
        endedImmediately = immediately
        state = immediately ? .dismissed : .ended
        stateContinuation?.yield(state)
    }
}
