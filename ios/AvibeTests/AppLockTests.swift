import LocalAuthentication
import UIKit
import WebKit
import XCTest
@testable import Avibe

@MainActor
private final class FakeAuthenticator: AppLockAuthenticating {
    var current = AppLockCapability(available: true, biometry: .faceID, biometryState: .usable)
    var reasons: [String] = []
    private var pending: [CheckedContinuation<AppLockAuthOutcome, Never>] = []

    func capability() -> AppLockCapability { current }

    func authenticate(reason: String) async -> AppLockAuthOutcome {
        reasons.append(reason)
        return await withCheckedContinuation { pending.append($0) }
    }

    var waiting: Int { pending.count }

    func answer(_ outcome: AppLockAuthOutcome) {
        pending.removeFirst().resume(returning: outcome)
    }
}

private let second: UInt64 = 1_000_000_000

@MainActor
final class AppLockMachineTests: XCTestCase {
    /// Every lifecycle event a user can cause, in an order that visits each state.
    private func everyEvent(_ machine: inout AppLockMachine) {
        machine.resignActive()
        machine.enterBackground(now: 1)
        machine.enterForeground(now: 1 + 600 * second)
        _ = machine.becomeActive(promptAllowed: true)
        machine.resignActive()
        _ = machine.becomeActive(promptAllowed: true)
    }

    func testOffOrWithoutPasscodeNeverCoversAnything() {
        for (policy, available) in [(AppLockPolicy.off, true), (.fiveMinutes, false), (.immediately, false)] {
            var machine = AppLockMachine(policy: policy, available: available)
            XCTAssertFalse(machine.locked)
            XCTAssertFalse(machine.covered)
            everyEvent(&machine)
            XCTAssertFalse(machine.locked, "\(policy) available=\(available)")
            XCTAssertFalse(machine.covered, "\(policy) available=\(available)")
        }
    }

    func testColdStartIsLockedAndPromptsOncePerArrival() {
        var machine = AppLockMachine(policy: .oneMinute, available: true)
        XCTAssertTrue(machine.locked)
        XCTAssertTrue(machine.covered)
        XCTAssertTrue(machine.becomeActive(promptAllowed: true))
        XCTAssertTrue(machine.beginVerification())
        machine.finishUnlock(.cancelled)
        XCTAssertTrue(machine.locked)
        // Back in front after the sheet: the button waits, no second automatic prompt.
        XCTAssertFalse(machine.becomeActive(promptAllowed: true))
        // A new arrival from the background prompts again.
        machine.enterBackground(now: 10)
        machine.enterForeground(now: 11)
        XCTAssertTrue(machine.becomeActive(promptAllowed: true))
    }

    func testReturnLocksExactlyAtTheThreshold() {
        for policy in AppLockPolicy.allCases where policy != .off {
            let threshold = policy.threshold!
            for (away, expected) in [(threshold == 0 ? 0 : threshold - 1, threshold == 0), (threshold, true), (threshold + second, true)] {
                var machine = AppLockMachine(policy: policy, available: true)
                XCTAssertTrue(machine.beginVerification())
                machine.finishUnlock(.success)
                XCTAssertFalse(machine.locked)
                machine.resignActive()
                machine.enterBackground(now: 100 * second)
                XCTAssertTrue(machine.covered, "the App Switcher snapshot is covered")
                machine.enterForeground(now: 100 * second + away)
                let prompt = machine.becomeActive(promptAllowed: true)
                XCTAssertEqual(machine.locked, expected, "\(policy) away \(away)")
                XCTAssertEqual(machine.covered, expected, "\(policy) away \(away)")
                XCTAssertEqual(prompt, expected, "\(policy) away \(away)")
            }
        }
    }

    func testAGlanceAwayWithoutBackgroundOnlyCoversForAMoment() {
        var machine = AppLockMachine(policy: .immediately, available: true)
        _ = machine.beginVerification()
        machine.finishUnlock(.success)
        machine.resignActive()
        XCTAssertTrue(machine.covered)
        XCTAssertFalse(machine.becomeActive(promptAllowed: true))
        XCTAssertFalse(machine.locked)
        XCTAssertFalse(machine.covered)
    }

    func testOwnVerificationSheetDoesNotCoverOrRelock() {
        var machine = AppLockMachine(policy: .immediately, available: true)
        _ = machine.beginVerification()
        machine.finishUnlock(.success)
        // A settings change shows the system sheet: the App goes inactive and back.
        XCTAssertTrue(machine.beginVerification())
        XCTAssertFalse(machine.beginVerification(), "a second sheet is busy")
        machine.resignActive()
        XCTAssertFalse(machine.covered)
        XCTAssertFalse(machine.becomeActive(promptAllowed: true))
        machine.finishChange(saving: .fiveMinutes)
        XCTAssertEqual(machine.policy, .fiveMinutes)
        XCTAssertFalse(machine.locked)
        XCTAssertFalse(machine.covered)
    }

    func testFailureStaysLockedAndSaysSo() {
        var machine = AppLockMachine(policy: .fiveMinutes, available: true)
        _ = machine.beginVerification()
        machine.finishUnlock(.failed)
        XCTAssertTrue(machine.locked)
        XCTAssertTrue(machine.lastAttemptFailed)
        _ = machine.beginVerification()
        machine.finishUnlock(.success)
        XCTAssertFalse(machine.locked)
        XCTAssertFalse(machine.lastAttemptFailed)
    }

    func testRemovingThePasscodeReleasesInsteadOfLockingOut() {
        var machine = AppLockMachine(policy: .fiveMinutes, available: true)
        machine.update(available: false)
        XCTAssertFalse(machine.locked)
        XCTAssertFalse(machine.covered)
        XCTAssertEqual(machine.policy, .fiveMinutes, "the choice is kept for when a passcode is back")
        machine.update(available: true)
        machine.enterBackground(now: 0)
        machine.enterForeground(now: 600 * second)
        XCTAssertTrue(machine.becomeActive(promptAllowed: true))
        XCTAssertTrue(machine.locked)
    }

    /// Whatever turns the lock on, other than a verified settings change, shows nothing first.
    func testTheLockNeverEngagesUnlocked() {
        // A passcode appears while a policy is chosen (set in iOS Settings, then back).
        for policy in AppLockPolicy.allCases where policy != .off {
            var machine = AppLockMachine(policy: policy, available: false)
            machine.resignActive()
            machine.enterBackground(now: 0)
            machine.update(available: true)
            XCTAssertTrue(machine.locked, "\(policy)")
            machine.enterForeground(now: 1)
            XCTAssertTrue(machine.becomeActive(promptAllowed: true), "\(policy)")
            XCTAssertTrue(machine.covered)
        }
        // A launch with a policy and a passcode.
        XCTAssertTrue(AppLockMachine(policy: .fiveMinutes, available: true).locked)
        // A verified change is the one way on without locking: the user just proved who they are.
        var machine = AppLockMachine(policy: .off, available: true)
        XCTAssertTrue(machine.beginVerification())
        machine.finishChange(saving: .immediately)
        XCTAssertTrue(machine.engaged)
        XCTAssertFalse(machine.locked)
        // A fresh reading that is unchanged never relocks the App in hand.
        machine.update(available: true)
        XCTAssertFalse(machine.locked)
    }

    func testAVoiceTakeDefersTheAutomaticPromptWithoutSpendingIt() {
        var machine = AppLockMachine(policy: .immediately, available: true)
        XCTAssertFalse(machine.becomeActive(promptAllowed: false))
        XCTAssertTrue(machine.locked)
        XCTAssertTrue(machine.becomeActive(promptAllowed: true))
    }
}

@MainActor
final class AppLockCapabilityTests: XCTestCase {
    func testDecodesLocalAuthenticationAnswers() {
        let cases: [(LABiometryType, LAError.Code?, AppLockBiometry, AppLockBiometryState)] = [
            (.faceID, nil, .faceID, .usable),
            (.touchID, nil, .touchID, .usable),
            (.faceID, .biometryNotEnrolled, .faceID, .notEnrolled),
            (.faceID, .biometryLockout, .faceID, .lockout),
            (.faceID, .biometryNotAvailable, .faceID, .denied),
            (.none, .biometryNotAvailable, .none, .unavailable),
            (.touchID, .systemCancel, .touchID, .unavailable),
        ]
        for (type, error, biometry, state) in cases {
            let capability = AppLockCapability.make(available: true, biometryType: type, biometricsError: error)
            XCTAssertEqual(capability.biometry, biometry)
            XCTAssertEqual(capability.biometryState, state, "\(type.rawValue) \(String(describing: error))")
        }
    }

    func testOnlyAMissingPasscodeLetsTheLockStandDown() {
        let passing: [LAError.Code?] = [.notInteractive, .systemCancel, .biometryLockout, .authenticationFailed, nil]
        for previous in [true, false, nil] as [Bool?] {
            XCTAssertTrue(AppLockCapability.available(canEvaluate: true, error: nil, previous: previous))
            XCTAssertFalse(AppLockCapability.available(canEvaluate: false, error: .passcodeNotSet, previous: previous))
            // A passing refusal keeps the last known answer; with none, the lock stays engaged.
            for code in passing {
                XCTAssertEqual(
                    AppLockCapability.available(canEvaluate: false, error: code, previous: previous),
                    previous ?? true,
                    "\(String(describing: code)) after \(String(describing: previous))"
                )
            }
        }
    }

    /// A locked App stays locked through a passing refusal and opens only on a verified unlock.
    func testAPassingRefusalNeverUnlocks() async {
        let auth = FakeAuthenticator()
        let defaults = UserDefaults(suiteName: "AppLockPassingRefusal.\(UUID().uuidString)")!
        AppLockStore(defaults: defaults).save(.immediately)
        let lock = AppLockController(store: AppLockStore(defaults: defaults), authenticator: auth, clock: { 0 }, promptAllowed: { false })
        XCTAssertTrue(lock.machine.locked)
        // The system refuses for a moment (a canEvaluate error other than passcodeNotSet):
        // the authenticator keeps the last known `available`, so nothing is released.
        auth.current = AppLockCapability(
            available: AppLockCapability.available(canEvaluate: false, error: .systemCancel, previous: true),
            biometry: .faceID,
            biometryState: .unavailable
        )
        lock.enterForeground()
        lock.becomeActive()
        _ = lock.status()
        XCTAssertTrue(lock.machine.locked)
        XCTAssertEqual(lock.status()["available"] as? Bool, true)
        lock.unlock()
        for _ in 0..<20 { await Task.yield() }
        auth.answer(.success)
        for _ in 0..<20 { await Task.yield() }
        XCTAssertFalse(lock.machine.locked)
    }

    func testLockScreenNamesOnlyWhatCanUnlockNow() {
        func copy(_ biometry: AppLockBiometry, _ state: AppLockBiometryState, failed: Bool = false) -> AppLockScreenCopy {
            .make(capability: AppLockCapability(available: true, biometry: biometry, biometryState: state), failed: failed)
        }
        XCTAssertEqual(copy(.faceID, .usable).action, "appLock.lock.iphone.unlockFaceID")
        XCTAssertEqual(copy(.touchID, .usable).action, "appLock.lock.unlockTouchID")
        for state in [AppLockBiometryState.notEnrolled, .denied, .unavailable, .lockout] {
            XCTAssertEqual(copy(.faceID, state).action, "appLock.lock.iphone.unlockPasscode", "\(state)")
        }
        XCTAssertEqual(copy(.faceID, .lockout).line, .muted("appLock.lock.iphone.faceIDLockout"))
        XCTAssertEqual(copy(.faceID, .denied).line, .muted("appLock.lock.iphone.faceIDDenied"))
        XCTAssertEqual(copy(.faceID, .notEnrolled).line, .muted("appLock.lock.iphone.faceIDNotEnrolled"))
        XCTAssertEqual(copy(.faceID, .usable, failed: true).line, .failed)
        XCTAssertEqual(copy(.faceID, .usable).line, .none)
    }
}

@MainActor
final class AppLockControllerTests: XCTestCase {
    private var defaults: UserDefaults!
    private var suite: String!
    private var auth: FakeAuthenticator!
    private var now: UInt64 = 0

    override func setUp() async throws {
        suite = "AppLockControllerTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)
        auth = FakeAuthenticator()
    }

    override func tearDown() async throws {
        defaults.removePersistentDomain(forName: suite)
    }

    private func controller(policy: AppLockPolicy = .off, promptAllowed: Bool = true) -> AppLockController {
        AppLockStore(defaults: defaults).save(policy)
        return AppLockController(
            store: AppLockStore(defaults: defaults),
            authenticator: auth,
            clock: { [unowned self] in now },
            promptAllowed: { promptAllowed }
        )
    }

    private func settle() async {
        for _ in 0..<20 { await Task.yield() }
    }

    func testStatusIsTheContractShape() throws {
        let lock = controller(policy: .fiveMinutes)
        let data = try JSONSerialization.data(withJSONObject: lock.status())
        let decoded = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: AnyHashable])
        XCTAssertEqual(decoded, ["policy": "fiveMinutes", "available": true, "biometry": "faceID", "biometryState": "usable"])
    }

    func testAChangeIsSavedOnlyAfterVerification() async {
        let lock = controller()
        let change = Task { await lock.setPolicy(.oneMinute) }
        await settle()
        XCTAssertEqual(auth.waiting, 1)
        XCTAssertEqual(AppLockStore(defaults: defaults).load(), .off, "nothing is saved before the sheet answers")
        let busy = await lock.setPolicy(.immediately)
        XCTAssertEqual(busy["reason"] as? String, "busy")
        auth.answer(.success)
        let result = await change.value
        XCTAssertEqual(result["ok"] as? Bool, true)
        XCTAssertEqual((result["status"] as? [String: Any])?["policy"] as? String, "oneMinute")
        XCTAssertEqual(AppLockStore(defaults: defaults).load(), .oneMinute)
        XCTAssertEqual(auth.reasons, [String(localized: "appLock.lock.changeReason")])
        XCTAssertFalse(lock.machine.locked, "turning it on does not lock the App in hand")
    }

    func testACancelledOrFailedChangeKeepsTheChoice() async {
        for (outcome, reason) in [(AppLockAuthOutcome.cancelled, "cancelled"), (.failed, "failed"), (.unavailable, "unavailable")] {
            let lock = controller(policy: .fiveMinutes)
            // A cold start is locked: unlock first, as a user reaching Settings has.
            lock.unlock()
            await settle()
            auth.answer(.success)
            await settle()
            let change = Task { await lock.setPolicy(.off) }
            await settle()
            auth.answer(outcome)
            let result = await change.value
            XCTAssertEqual(result["ok"] as? Bool, false)
            XCTAssertEqual(result["reason"] as? String, reason)
            XCTAssertEqual(AppLockStore(defaults: defaults).load(), .fiveMinutes)
        }
    }

    func testTheSameChoiceOrNoPasscodeAsksNothing() async {
        let lock = controller(policy: .immediately)
        let same = await lock.setPolicy(.immediately)
        XCTAssertEqual(same["ok"] as? Bool, true)
        auth.current = AppLockCapability(available: false, biometry: .faceID, biometryState: .notEnrolled)
        let none = await lock.setPolicy(.oneMinute)
        XCTAssertEqual(none["reason"] as? String, "unavailable")
        XCTAssertEqual((none["status"] as? [String: Any])?["available"] as? Bool, false)
        XCTAssertTrue(auth.reasons.isEmpty)
    }

    func testTheLockWindowCoversEverythingAndNeverTakesKey() async throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let lock = controller(policy: .oneMinute)
        lock.attach(to: scene)
        defer { lock.detach() }
        let window = try XCTUnwrap(lock.window)
        XCTAssertGreaterThan(window.windowLevel, .alert)
        XCTAssertFalse(window.canBecomeKey)
        XCTAssertFalse(window.isHidden, "a cold start opens behind the lock")

        lock.becomeActive()
        await settle()
        XCTAssertEqual(auth.waiting, 1, "the App asks once it is in front")
        XCTAssertEqual(auth.reasons, [String(localized: "appLock.lock.unlockReason")])
        auth.answer(.success)
        await settle()
        XCTAssertTrue(window.isHidden)

        // Away 59 seconds: back without a prompt. Away 60: locked again.
        lock.resignActive()
        XCTAssertFalse(window.isHidden)
        now = 1_000 * second
        lock.enterBackground()
        now += 59 * second
        lock.enterForeground()
        lock.becomeActive()
        XCTAssertTrue(window.isHidden)
        lock.enterBackground()
        now += 60 * second
        lock.enterForeground()
        lock.becomeActive()
        await settle()
        XCTAssertFalse(window.isHidden)
        XCTAssertTrue(lock.machine.locked)
        auth.answer(.cancelled)
        await settle()
        XCTAssertFalse(window.isHidden, "Cancel keeps the lock")
        XCTAssertEqual(auth.waiting, 0)
    }

    func testBridgeOffersAppLockToTheWebUI() {
        XCTAssertEqual(NativeShellMetadata.capabilities["appLock"], 1)
        XCTAssertEqual(NativeBridgeMethod(rawValue: "appLock.status"), .appLockStatus)
        XCTAssertEqual(NativeBridgeMethod(rawValue: "appLock.setPolicy"), .appLockSetPolicy)
    }

    /// The page reaches the owner through the real bridge, and the answer is the contract's.
    func testThePageReadsAndChangesThePolicyThroughTheBridge() async throws {
        let lock = controller(policy: .off)
        let instance = try XCTUnwrap(WebOrigin.parseConfigured("https://instance-one.example"))
        let webView = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: instance,
                setOuterScrollLocked: { _, _ in false },
                appLock: lock
            )
        )
        let loader = AppLockPageLoader()
        webView.navigationDelegate = loader
        try await loader.load("<!doctype html><title>instance</title>", baseURL: instance.url, in: webView)

        let reading = Task { @MainActor in
            try await webView.callAsyncJavaScript(
                """
                const status = await window.avibeNative.call("appLock.status");
                const refused = [];
                for (const params of [{policy: "sometimes"}, {policy: "off", extra: 1}, {}]) {
                  try { await window.avibeNative.call("appLock.setPolicy", params); refused.push("accepted"); }
                  catch (error) { refused.push(error.code); }
                }
                const changed = await window.avibeNative.call("appLock.setPolicy", {policy: "immediately"});
                return JSON.stringify({advertised: window.avibeNative.capabilities.appLock, status, refused, changed});
                """,
                arguments: [:],
                in: nil,
                contentWorld: .page
            )
        }
        for _ in 0..<200 where auth.waiting == 0 { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(auth.waiting, 1)
        auth.answer(.success)
        let value = try await reading.value
        let json = try XCTUnwrap(value as? String)
        let result = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any])
        let status: [String: AnyHashable] = ["policy": "off", "available": true, "biometry": "faceID", "biometryState": "usable"]
        XCTAssertEqual(result["advertised"] as? Int, 1)
        XCTAssertEqual(result["status"] as? [String: AnyHashable], status)
        XCTAssertEqual(result["refused"] as? [String], ["invalid_argument", "invalid_argument", "invalid_argument"])
        let changed = try XCTUnwrap(result["changed"] as? [String: Any])
        XCTAssertEqual(changed["ok"] as? Bool, true)
        XCTAssertEqual((changed["status"] as? [String: AnyHashable])?["policy"], "immediately")
        XCTAssertEqual(AppLockStore(defaults: defaults).load(), .immediately)
    }
}

private final class AppLockPageLoader: NSObject, WKNavigationDelegate {
    private var continuation: CheckedContinuation<Void, Error>?

    @MainActor
    func load(_ html: String, baseURL: URL, in webView: WKWebView) async throws {
        try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            webView.loadHTMLString(html, baseURL: baseURL)
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        continuation?.resume()
        continuation = nil
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        continuation?.resume(throwing: error)
        continuation = nil
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        continuation?.resume(throwing: error)
        continuation = nil
    }
}
