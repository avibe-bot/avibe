import CoreFoundation
import Foundation
import WebKit

// Fixture-only dependencies required to compile the production NativeBridge
// source. The capture routes do not exercise push, notification reconciliation,
// or direct transport; these shapes preserve the production bridge's parser
// and handler table without importing the full App target.
struct DirectPathStatus: Equatable, Sendable {
    enum Preference: String, Equatable, Sendable { case tunnel, direct }
    let preferred: Preference
    let sequence: Int
    let closure: Int
    var jsonObject: [String: Any] {
        ["preferred": preferred.rawValue, "sequence": sequence, "closure": closure]
    }
}

typealias DirectStatusProvider = @MainActor (
    WebOrigin,
    @escaping @MainActor (DirectPathStatus?) -> Void
) -> Void

enum DirectHandoverSkip: String, Equatable, Sendable {
    case notUsable = "not_usable"
    case otherDocuments = "other_documents"
}

enum DirectHandoverOutcome: Equatable, Sendable {
    case done(closed: Int, kept: Int)
    case skipped(DirectHandoverSkip)

    var jsonObject: [String: Any] {
        switch self {
        case .done(let closed, let kept):
            return ["closed": closed, "kept": kept]
        case .skipped(let reason):
            return ["closed": 0, "kept": 0, "skipped": reason.rawValue]
        }
    }
}

enum DirectOtherDocument: String, Equatable, Sendable {
    case sole = "none"
    case unknown
}

typealias DirectHandoverProvider = @MainActor (
    WebOrigin,
    _ other: DirectOtherDocument,
    @escaping @MainActor (DirectHandoverOutcome) -> Void
) -> Void

/// The fixture has no direct transport: no WebView is ever the sole document.
@MainActor
final class DirectWebViewRegistry {
    static let shared = DirectWebViewRegistry()

    func otherDocument(than webView: WKWebView, host: String) -> DirectOtherDocument {
        .unknown
    }
}

/// The production bridge parses `health.connect` into these options
/// (HealthSyncCoordinator.swift); the fixture never connects Health.
struct HealthConnectionOptions {
    var supportedMetrics: [String] = []
    var requestedMetrics: [String] = []
    var expansion = false
}

enum NativePushError: Error, Equatable {
    case unavailable
    case permissionDenied
    case invalidVapidKey
    case management(status: Int, code: String?)
    case invalidResponse
    case staleAccount
    case generationExhausted

    var bridgeMessage: String {
        switch self {
        case .unavailable:
            return "Native push is not available for this account."
        case .permissionDenied:
            return "Notification permission was not granted."
        case .invalidVapidKey:
            return "The instance VAPID key is invalid."
        case .management(let status, let code):
            return "Push management request failed (\(status), \(code ?? "unknown"))."
        case .invalidResponse:
            return "The push service returned an invalid response."
        case .staleAccount:
            return "The push request belongs to an older account session."
        case .generationExhausted:
            return "The push generation limit has been reached on this installation."
        }
    }
}

enum SystemScheduleAdmission {}

struct SystemScheduleRequest {
    static func parse(_ params: [String: Any]) -> SystemScheduleRequest? {
        nil
    }
}

struct SystemScheduleCancelRequest {
    static func parse(_ params: [String: Any]) -> SystemScheduleCancelRequest? {
        nil
    }
}

struct NotificationReconcile: Equatable {
    static let maximumSessions = 1000
    static let maximumSessionIDLength = 200

    let unreadSessions: Set<String>
    let asOf: Date

    static func parse(_ params: [String: Any]) -> NotificationReconcile? {
        guard
            params.count == 2,
            let ids = params["unreadSessions"] as? [Any],
            ids.count <= maximumSessions,
            let number = params["asOf"] as? NSNumber,
            CFGetTypeID(number) != CFBooleanGetTypeID()
        else {
            return nil
        }
        let milliseconds = number.doubleValue
        guard milliseconds.isFinite, milliseconds >= 0 else { return nil }
        var unread = Set<String>()
        for id in ids {
            guard
                let id = id as? String,
                !id.isEmpty,
                id.count <= maximumSessionIDLength,
                unread.insert(id).inserted
            else {
                return nil
            }
        }
        return NotificationReconcile(
            unreadSessions: unread,
            asOf: Date(timeIntervalSince1970: milliseconds / 1000)
        )
    }
}

/// NativeHoldCapture's check for an Action Button take, which the fixture
/// never records.
enum VoiceCaptureAudioState {
    nonisolated(unsafe) static var active = false
}
