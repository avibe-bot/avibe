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
    private func coordinator(enabled: Bool = true, now: @escaping @MainActor () -> Date = { Date() }, response: TurnActivityCoordinator.Request? = nil) -> TurnActivityCoordinator {
        let owner = TurnActivityCoordinator(defaults: defaults, now: now, enabled: { enabled }, activities: { self.cards }, create: { attributes, state in
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

    func testDismissedTurnOnlyRecreatedOnExplicitStartAcrossRelaunch() async throws {
        let owner = coordinator()
        _ = await start(owner)
        let card = try XCTUnwrap(cards.first)
        card.state = .dismissed
        await owner.reconcile(target: target)
        let relaunched = coordinator()
        await relaunched.reconcile(target: target)
        XCTAssertEqual(cards.count, 1)
        let result = await start(relaunched)
        XCTAssertEqual(result["ok"] as? Bool, true)
        XCTAssertEqual(cards.count, 2)
        XCTAssertTrue(calls.contains { $0.0 == "DELETE" && $0.2?["reason"] as? String == "dismissed" })
        await relaunched.endAll(reason: "logout")
    }

    func testMissingCardAfterRelaunchRetiresServerTracking() async {
        let owner = coordinator()
        _ = await start(owner)
        _ = owner.invalidateRequests()
        cards.removeAll()
        let relaunched = coordinator()
        await relaunched.reconcile(target: target)
        XCTAssertTrue(cards.isEmpty)
        XCTAssertTrue(calls.contains { $0.0 == "DELETE" })
        let result = await start(relaunched)
        XCTAssertEqual(result["ok"] as? Bool, true)
        await relaunched.endAll(reason: "logout")
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
        _ = await start(owner)
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
        let result = await start(owner)
        XCTAssertEqual(result["ok"] as? Bool, true)
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
        _ = await start(owner)
        _ = owner.invalidateRequests()
        calls.removeAll()
        let relaunched = coordinator()
        await relaunched.reconcile(target: target)
        for _ in 0..<20 { await Task.yield() }
        XCTAssertEqual(calls.map { $0.0 }, ["GET"])
        await relaunched.endAll(reason: "logout")
    }

    func testUnregisteredNetworkFailurePersistsAndRetriesPutAcrossRelaunch() async throws {
        let owner = coordinator(response: { _, _, path, _ in
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
            throw URLError(.notConnectedToInternet)
        })
        let result = await start(owner)
        XCTAssertEqual(result["ok"] as? Bool, true)
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
        let result = await start(owner)
        XCTAssertEqual(result["ok"] as? Bool, true)
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
        _ = await start(owner)
        let card = try XCTUnwrap(cards.first)
        clock = clock.addingTimeInterval(28799)
        await owner.reconcile(target: target)
        XCTAssertEqual(card.state, .active)
        XCTAssertEqual(card.content.confirmedAt, self.initial["confirmed_at"] as? Int)
        clock = clock.addingTimeInterval(1)
        await owner.reconcile(target: target)
        XCTAssertEqual(card.state, .dismissed)
    }

    func testInitialRegistrationRefusalsEndImmediatelyWithContractCode() async throws {
        for (status, error, expected) in [(403, "registration_not_owned", "push_unavailable"),
                                          (404, "not_found", "failed"),
                                          (400, "invalid_live_activity", "failed"),
                                          (409, "activity_conflict", "failed")] {
            let owner = coordinator(response: { _, _, path, _ in
                if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
                return .init(status: status, json: ["error": error], redirected: false)
            })
            let result = await start(owner)
            XCTAssertEqual(result["code"] as? String, expected)
            XCTAssertEqual(try XCTUnwrap(cards.last).state, .dismissed)
            _ = owner.invalidateRequests()
            cards.removeAll()
        }
    }

    func testRegisteredStoppedOverridesNewerContentAndTerminalPhase() async throws {
        var stopped = false
        let owner = coordinator(response: { _, _, path, _ in
            if path.contains("/turn?") { return .init(status: 200, json: ["turn": self.initial], redirected: false) }
            let body = stopped ? ["status": "stopped", "phase": "done", "started_at": 1795000000,
                                  "ended_at": 1795000100, "confirmed_at": 1795000100] : self.initial
            return .init(status: 200, json: ["activity": body], redirected: false)
        })
        _ = await start(owner)
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
        _ = await start(owner)
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

    func testSystemEndedCardCanBeExplicitlyReplacedForTheSameRunningTurn() async throws {
        let owner = coordinator()
        _ = await start(owner)
        let card = try XCTUnwrap(cards.first)
        card.state = .ended
        let result = await start(owner)
        XCTAssertEqual(result["ok"] as? Bool, true)
        XCTAssertEqual(card.state, .dismissed)
        XCTAssertEqual(cards.count, 2)
        XCTAssertEqual(cards.last?.state, .active)
        await owner.endAll(reason: "logout")
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

    func testBridgeInvokesAllFourMethodsFromTheOwnedDocument() async throws {
        let origin = try XCTUnwrap(WebOrigin(url: URL(string: target.instanceOrigin)!))
        var received: [NativeBridgeMethod] = []
        let webView = WKWebView(frame: .zero, configuration: NativeBridgeConfiguration.make(
            instanceOrigin: origin, websiteDataStore: .nonPersistent(), setOuterScrollLocked: { _, _ in false },
            liveActivity: { request, admittedOrigin in
                XCTAssertEqual(admittedOrigin, origin)
                received.append(request.method)
                return request.method == .liveActivityStatus ? ["enabled": true, "tracking": false] : ["ok": true]
            }))
        let loader = TurnActivityPageLoader()
        webView.navigationDelegate = loader
        try await loader.loadHTML("<!doctype html><title>Live Activity bridge</title>", baseURL: origin.url, in: webView)
        let result = try await webView.callAsyncJavaScript("""
            const status = await window.avibeNative.call('liveActivity.status', {sessionId: 'session-1'});
            const start = await window.avibeNative.call('liveActivity.start', {
              sessionId: 'session-1', agentName: 'Agent', sessionTitle: null
            });
            const stop = await window.avibeNative.call('liveActivity.stop', {sessionId: 'session-1'});
            const settings = await window.avibeNative.call('liveActivity.openSettings', {});
            return [status.enabled, status.tracking, start.ok, stop.ok, settings.ok];
            """, arguments: [:], in: nil, contentWorld: .page)
        XCTAssertEqual(result as? [Bool], [true, false, true, true, true])
        XCTAssertEqual(received, [.liveActivityStatus, .liveActivityStart, .liveActivityStop, .liveActivityOpenSettings])
    }

    func testRefusedStartKeepsExistingCardAndAdmittedReplacementUsesReplaced() async throws {
        var refuse = false
        let owner = coordinator(response: { _, _, path, _ in
            if path.contains("/turn?") {
                return refuse ? .init(status: 404, json: ["error": "session_not_found"], redirected: false)
                    : .init(status: 200, json: ["turn": self.initial], redirected: false)
            }
            return .init(status: 200, json: ["activity": self.initial], redirected: false)
        })
        _ = await start(owner)
        let original = try XCTUnwrap(cards.first)
        refuse = true
        let rejected = await owner.start(sessionID: "other", agentName: "Agent", sessionTitle: nil, target: target, registrationID: "r")
        XCTAssertEqual(rejected["code"] as? String, "no_active_turn")
        XCTAssertEqual(original.state, .active)
        XCTAssertFalse(calls.contains { $0.0 == "DELETE" })
        refuse = false
        let admitted = await owner.start(sessionID: "other", agentName: "Agent", sessionTitle: nil, target: target, registrationID: "r")
        XCTAssertEqual(admitted["ok"] as? Bool, true)
        XCTAssertEqual(original.state, .dismissed)
        XCTAssertTrue(calls.contains { $0.0 == "DELETE" && $0.2?["reason"] as? String == "replaced" })
        await owner.endAll(reason: "logout")
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
