import Foundation
import LocalAuthentication
import UIKit

// App Lock (docs/plans/app-lock.md; wire contract ui/src/lib/appLockContract.ts).
//
// One per-device choice, stored here. When it is on and the device has a passcode, the
// App opens locked and locks again once it has been in the background for the chosen
// time. The lock is a window above every other window of the scene; it covers what is on
// screen and nothing else — background work keeps running.

enum AppLockBiometry: String, Equatable {
    case faceID, touchID, opticID, none
}

enum AppLockBiometryState: String, Equatable {
    case usable, notEnrolled, denied, unavailable, lockout
}

/// What this device can do right now (the contract's `available`, `biometry`, `biometryState`).
struct AppLockCapability: Equatable {
    var available: Bool
    var biometry: AppLockBiometry
    var biometryState: AppLockBiometryState

    /// Whether the lock can engage. Only a missing passcode lets it stand down: any other
    /// refusal (a transient system state) keeps it engaged, since failing open shows the content.
    static func available(canEvaluate: Bool, error: LAError.Code?) -> Bool {
        canEvaluate || error != .passcodeNotSet
    }

    /// Decodes LocalAuthentication's answers: whether `.deviceOwnerAuthentication` can be
    /// evaluated, the device's biometry type, and the error from evaluating biometrics alone
    /// (nil when that succeeded).
    static func make(
        available: Bool,
        biometryType: LABiometryType,
        biometricsError: LAError.Code?
    ) -> AppLockCapability {
        let biometry: AppLockBiometry = switch biometryType {
        case .faceID: .faceID
        case .touchID: .touchID
        case .opticID: .opticID
        default: .none
        }
        let state: AppLockBiometryState = switch biometricsError {
        case nil: .usable
        case .biometryNotEnrolled?: .notEnrolled
        case .biometryLockout?: .lockout
        // With biometric hardware, "not available" on iOS means the user turned Face ID off for Avibe.
        case .biometryNotAvailable? where biometry != .none: .denied
        default: .unavailable
        }
        return AppLockCapability(available: available, biometry: biometry, biometryState: state)
    }
}

enum AppLockAuthOutcome: Equatable {
    case success
    case cancelled
    case failed
    /// The device has no passcode any more.
    case unavailable
}

/// The system's device-owner check. Production uses `LocalAuthenticator`; tests use a stand-in.
@MainActor
protocol AppLockAuthenticating: AnyObject {
    func capability() -> AppLockCapability
    func authenticate(reason: String) async -> AppLockAuthOutcome
}

@MainActor
final class LocalAuthenticator: AppLockAuthenticating {
    func capability() -> AppLockCapability {
        var ownerError: NSError?
        let canEvaluate = LAContext().canEvaluatePolicy(.deviceOwnerAuthentication, error: &ownerError)
        let available = AppLockCapability.available(
            canEvaluate: canEvaluate,
            error: ownerError.flatMap { LAError.Code(rawValue: $0.code) }
        )
        let biometrics = LAContext()
        var biometricsError: NSError?
        let usable = biometrics.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &biometricsError)
        return .make(
            available: available,
            biometryType: biometrics.biometryType,
            biometricsError: usable ? nil : biometricsError.map { LAError.Code(rawValue: $0.code) ?? .biometryNotAvailable }
        )
    }

    func authenticate(reason: String) async -> AppLockAuthOutcome {
        // A fresh context each time: an earlier success never carries over.
        let context = LAContext()
        do {
            return try await context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason)
                ? .success : .failed
        } catch let error as LAError {
            switch error.code {
            case .userCancel, .systemCancel, .appCancel, .notInteractive: return .cancelled
            case .passcodeNotSet: return .unavailable
            default: return .failed
            }
        } catch {
            return .failed
        }
    }
}

/// A clock that keeps counting while the device sleeps, so time asleep counts as time away.
enum AppLockClock {
    static func now() -> UInt64 { clock_gettime_nsec_np(CLOCK_MONOTONIC_RAW) }
}

/// What App Lock decides, with no UIKit in it: the lifecycle events in, whether the scene
/// is covered or locked out.
struct AppLockMachine: Equatable {
    private(set) var policy: AppLockPolicy
    private(set) var available: Bool
    /// The content needs a verification before it shows.
    private(set) var locked: Bool
    /// The lock window is up: locked, or covering the App Switcher snapshot.
    private(set) var covered: Bool
    /// A system verification sheet is up (an unlock or a settings change).
    private(set) var verifying = false
    /// The last unlock attempt did not verify.
    private(set) var lastAttemptFailed = false
    private var leftAt: UInt64?
    /// One automatic prompt per arrival in front; after a cancel the button waits.
    private var autoPromptSpent = false

    init(policy: AppLockPolicy, available: Bool) {
        self.policy = policy
        self.available = available
        let engaged = policy != .off && available
        locked = engaged
        covered = engaged
    }

    /// The lock applies: a policy is chosen and the device has a passcode.
    var engaged: Bool { policy != .off && available }

    /// Going inactive (App Switcher, Control Center, a system sheet): cover the snapshot.
    mutating func resignActive() {
        guard engaged, !verifying else { return }
        covered = true
    }

    mutating func enterBackground(now: UInt64) {
        guard engaged else { return }
        covered = true
        if !locked && leftAt == nil { leftAt = now }
        autoPromptSpent = false
        lastAttemptFailed = false
    }

    mutating func enterForeground(now: UInt64) {
        defer { leftAt = nil }
        guard engaged else { return release() }
        if !locked, let leftAt, let threshold = policy.threshold, now &- leftAt >= threshold {
            locked = true
        }
    }

    /// Returns whether to ask for verification automatically now.
    mutating func becomeActive(promptAllowed: Bool) -> Bool {
        guard !verifying else { return false }
        guard engaged else { release(); return false }
        guard locked else {
            covered = false
            return false
        }
        covered = true
        guard promptAllowed, !autoPromptSpent else { return false }
        autoPromptSpent = true
        return true
    }

    /// Starts a verification; false while another one is up.
    mutating func beginVerification() -> Bool {
        guard !verifying else { return false }
        verifying = true
        return true
    }

    /// Ends an unlock attempt.
    mutating func finishUnlock(_ outcome: AppLockAuthOutcome) {
        verifying = false
        switch outcome {
        case .success:
            release()
        case .cancelled:
            lastAttemptFailed = false
        case .failed:
            lastAttemptFailed = true
        case .unavailable:
            available = false
            release()
        }
    }

    /// Ends a settings verification; `policy` is the newly saved choice, if any.
    mutating func finishChange(saving policy: AppLockPolicy?) {
        verifying = false
        if let policy { self.policy = policy }
        if !engaged { release() }
    }

    /// A fresh reading of whether the device has a passcode.
    mutating func update(available: Bool) {
        self.available = available
        if !engaged { release() }
    }

    private mutating func release() {
        locked = false
        covered = false
        leftAt = nil
        lastAttemptFailed = false
    }
}

/// The policy as it is kept on this device.
struct AppLockStore {
    static let key = "avibe.appLock.policy"
    let defaults: UserDefaults

    func load() -> AppLockPolicy {
        defaults.string(forKey: Self.key).flatMap(AppLockPolicy.init(rawValue:)) ?? .off
    }

    func save(_ policy: AppLockPolicy) {
        defaults.set(policy.rawValue, forKey: Self.key)
    }
}

/// The contract's `setPolicy` failures.
enum AppLockSetFailure: String {
    case cancelled, failed, unavailable, busy
}

/// Owns App Lock for the App: the machine, the stored policy, the system check, and the
/// lock window. Lifecycle notifications drive it; the bridge reads and changes the policy.
@MainActor
final class AppLockController: ObservableObject, AppLockBridging {
    static let shared = AppLockController()

    @Published private(set) var machine: AppLockMachine
    @Published private(set) var capability: AppLockCapability

    private let store: AppLockStore
    private let authenticator: AppLockAuthenticating
    private let clock: () -> UInt64
    private let promptAllowed: @MainActor () -> Bool
    private var observers: [NSObjectProtocol] = []
    private(set) var window: AppLockWindow?
    private var renderedLocked = false
    private(set) var started = false

    init(
        store: AppLockStore = AppLockStore(defaults: .standard),
        authenticator: AppLockAuthenticating? = nil,
        clock: @escaping () -> UInt64 = AppLockClock.now,
        promptAllowed: (@MainActor () -> Bool)? = nil
    ) {
        let authenticator = authenticator ?? LocalAuthenticator()
        self.store = store
        self.authenticator = authenticator
        self.clock = clock
        self.promptAllowed = promptAllowed ?? { AppLockController.voiceCaptureIsIdle() }
        let capability = authenticator.capability()
        self.capability = capability
        machine = AppLockMachine(policy: store.load(), available: capability.available)
    }

    /// A take the Action Button started is recording or about to: asking for Face ID now
    /// would take the App inactive under it. The button still unlocks.
    static func voiceCaptureIsIdle() -> Bool {
        switch VoiceCaptureController.shared.phase {
        case .preparing, .listening: false
        default: true
        }
    }

    /// Called once at launch, before the first scene draws.
    func start() {
        guard !started else { return }
        started = true
        AppLockBridge.current = self
        let center = NotificationCenter.default
        func observe(_ name: Notification.Name, _ body: @escaping @MainActor (Notification) -> Void) {
            observers.append(center.addObserver(forName: name, object: nil, queue: .main) { note in
                MainActor.assumeIsolated { body(note) }
            })
        }
        observe(UIScene.willConnectNotification) { [weak self] note in
            guard let scene = note.object as? UIWindowScene else { return }
            self?.attach(to: scene)
        }
        observe(UIApplication.willResignActiveNotification) { [weak self] _ in self?.resignActive() }
        observe(UIApplication.didEnterBackgroundNotification) { [weak self] _ in self?.enterBackground() }
        observe(UIApplication.willEnterForegroundNotification) { [weak self] _ in self?.enterForeground() }
        observe(UIApplication.didBecomeActiveNotification) { [weak self] _ in self?.becomeActive() }
        for case let scene as UIWindowScene in UIApplication.shared.connectedScenes {
            attach(to: scene)
        }
    }

    // MARK: Lifecycle

    func resignActive() {
        machine.resignActive()
        render()
    }

    func enterBackground() {
        machine.enterBackground(now: clock())
        render()
    }

    func enterForeground() {
        refreshCapability()
        machine.enterForeground(now: clock())
        render()
    }

    func becomeActive() {
        if !machine.verifying { refreshCapability() }
        let prompt = machine.becomeActive(promptAllowed: promptAllowed())
        render()
        if prompt { unlock() }
    }

    // MARK: Unlock

    /// The lock screen's button, and the automatic prompt.
    func unlock() {
        guard machine.locked, machine.beginVerification() else { return }
        render()
        Task { @MainActor in
            let outcome = await authenticator.authenticate(reason: String(localized: "appLock.lock.unlockReason"))
            machine.finishUnlock(outcome)
            refreshCapability()
            render()
        }
    }

    // MARK: Bridge

    var statusObject: [String: Any] {
        [
            "policy": machine.policy.rawValue,
            "available": capability.available,
            "biometry": capability.biometry.rawValue,
            "biometryState": capability.biometryState.rawValue,
        ]
    }

    func status() -> [String: Any] {
        if !machine.verifying { refreshCapability() }
        return statusObject
    }

    /// Saves `policy` once the device owner verifies.
    func setPolicy(_ policy: AppLockPolicy) async -> [String: Any] {
        if !machine.verifying { refreshCapability() }
        if policy == machine.policy { return ["ok": true, "status": statusObject] }
        guard capability.available else { return failure(.unavailable) }
        guard machine.beginVerification() else { return failure(.busy) }
        let outcome = await authenticator.authenticate(reason: String(localized: "appLock.lock.changeReason"))
        guard outcome == .success else {
            machine.finishChange(saving: nil)
            refreshCapability()
            render()
            return failure(outcome == .cancelled ? .cancelled : outcome == .unavailable ? .unavailable : .failed)
        }
        store.save(policy)
        machine.finishChange(saving: policy)
        render()
        return ["ok": true, "status": statusObject]
    }

    private func failure(_ reason: AppLockSetFailure) -> [String: Any] {
        ["ok": false, "reason": reason.rawValue, "status": statusObject]
    }

    private func refreshCapability() {
        capability = authenticator.capability()
        machine.update(available: capability.available)
    }

    // MARK: Window

    func attach(to scene: UIWindowScene) {
        guard window == nil else { return }
        window = AppLockWindow(scene: scene, controller: self)
        render()
    }

    /// Takes the window down (tests that attach one).
    func detach() {
        window?.isHidden = true
        window = nil
    }

    private func render() {
        guard let window else { return }
        window.isHidden = !machine.covered
        if machine.locked && !renderedLocked {
            // Nothing is typed under the lock: drop the keyboard of whatever had focus.
            for other in window.windowScene?.windows ?? [] where other !== window {
                other.endEditing(true)
            }
        }
        renderedLocked = machine.locked
    }
}
