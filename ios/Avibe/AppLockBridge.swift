import Foundation

// What the `appLock` bridge methods reach (wire contract: ui/src/lib/appLockContract.ts).
// Kept apart from the controller so every target that compiles NativeBridge.swift can
// build without App Lock's window and lifecycle.

enum AppLockPolicy: String, CaseIterable, Equatable {
    case off
    case immediately
    case oneMinute
    case fiveMinutes

    /// Time away, in nanoseconds, after which a return locks. Nil when off.
    var threshold: UInt64? {
        switch self {
        case .off: nil
        case .immediately: 0
        case .oneMinute: 60 * 1_000_000_000
        case .fiveMinutes: 300 * 1_000_000_000
        }
    }
}

/// Answers `appLock.status` and `appLock.setPolicy` with the contract's JSON objects.
@MainActor
protocol AppLockBridging: AnyObject {
    func status() -> [String: Any]
    func setPolicy(_ policy: AppLockPolicy) async -> [String: Any]
}

@MainActor
enum AppLockBridge {
    /// The App's lock owner, set when it starts. Without one the methods are unsupported.
    static var current: AppLockBridging?
}
