import Foundation
import OSLog
import UIKit
import WebKit

/// The direct path's load gate for an instance origin on a WebKit store
/// (`DirectPathController.loadGate`), set by the App's model; `nil` without
/// a direct transport.
@MainActor
enum VoiceCaptureLoadGate {
    static var provider: ((WebOrigin, UUID) -> DirectLoadGate)?
}

/// Where a background voice take goes: the selected instance, through the
/// current account's WebKit store.
///
/// Read-only: it reads the selection and the store identifier the account
/// coordinator persisted, and never writes either (the coordinator owns
/// them, and may be running in this same process).
struct VoiceCaptureTarget: Equatable {
    let accountID: String
    let instanceID: String
    let origin: WebOrigin
    let storeIdentifier: UUID

    /// Mirrors `MobileSessionCoordinator`'s stored selection.
    private struct StoredSelection: Decodable {
        let accountID: String
        let instanceID: String
        let url: URL
    }

    static let selectionKey = "avibe.mobile.selection"
    static let storeIdentifierKey = "avibe.mobile.websiteDataStoreIdentifier"

    static func current(defaults: UserDefaults = .standard) -> VoiceCaptureTarget? {
        guard
            let data = defaults.data(forKey: selectionKey),
            let selection = try? JSONDecoder().decode(StoredSelection.self, from: data),
            let origin = WebOrigin(url: selection.url),
            let rawIdentifier = defaults.string(forKey: storeIdentifierKey),
            let storeIdentifier = UUID(uuidString: rawIdentifier)
        else {
            return nil
        }
        return VoiceCaptureTarget(
            accountID: selection.accountID,
            instanceID: selection.instanceID,
            origin: origin,
            storeIdentifier: storeIdentifier
        )
    }
}

/// Calls the selected instance's API from the background, as the signed-in
/// user, without the runtime WebView.
///
/// WebKit stays the only owner of the instance session: a hidden WKWebView on
/// the account's own store loads an instance document (`/api/csrf-token`,
/// which needs no session) and runs same-origin `fetch` calls in it, so the
/// session cookie, `Origin`, and the CSRF pair are WebKit's, exactly as for
/// the Workbench. `MobileAuthTransport` uses the same pattern for avibe.bot.
@MainActor
final class VoiceCaptureInstanceClient: NSObject, WKNavigationDelegate {
    enum Failure: Error, Equatable {
        /// The instance could not be reached.
        case offline
        /// The instance session is missing or expired; the App signs in again.
        case signIn
        /// The account has not said yes to voice input on this instance, or
        /// said no since (`voice_consent_required`).
        case consentRequired
        /// The instance predates the voice-input consent question.
        case olderAvibe
        /// Voice input is unavailable on the instance (no transcription).
        case unavailable
        /// The take had no words.
        case nothingHeard
        /// The account may not use this route on the instance.
        case forbidden
        /// Anything else the instance answered.
        case server(Int, String?)
        /// The App's local listener for this instance failed
        /// (`DirectLoadGate.fault`): nothing is loaded against it.
        case directFault
        /// The client was closed before this request was made (its take is
        /// over): provably nothing was sent.
        case closed
    }

    struct Status: Equatable {
        enum Consent: Equatable {
            case granted
            case declined
            case notAsked
        }

        let available: Bool
        let consent: Consent
    }

    private static let logger = Logger(
        subsystem: Bundle.main.bundleIdentifier ?? "bot.avibe.ios",
        category: "VoiceCapture"
    )
    static let mainSessionID = "ses_main"

    let target: VoiceCaptureTarget
    /// One id per take for its transcription: sending the take again
    /// repeats it, so the service can tell the same take from a new one.
    let dictationID: String
    private var webView: WKWebView?
    private var loadContinuation: CheckedContinuation<Void, Error>?
    private var ready = false

    /// Requests that did not reach the instance, newest last (the last 4):
    /// which request, where it gave up, and when (ms since this client was
    /// made). States and codes only, for `capture_earlier`: no body, no
    /// words.
    private(set) var failures: [String] = []
    private let madeAt = Date()
    /// The request the document is being loaded for.
    private var loadingFor = "-"

    /// The direct path's load gate for this instance, whether Avibe is in
    /// front, and a wait (tests replace them).
    private let gate: () -> DirectLoadGate
    private let isActive: () -> Bool
    private let sleep: (Duration) async -> Void

    init(
        target: VoiceCaptureTarget,
        dictationID: String = "ios-action-\(UUID().uuidString.lowercased())",
        gate: (() -> DirectLoadGate)? = nil,
        isActive: @escaping () -> Bool = { UIApplication.shared.applicationState == .active },
        sleep: @escaping (Duration) async -> Void = { try? await Task.sleep(for: $0) }
    ) {
        self.target = target
        self.dictationID = dictationID
        let origin = target.origin
        let store = target.storeIdentifier
        self.gate = gate ?? { VoiceCaptureLoadGate.provider?(origin, store) ?? .open }
        self.isActive = isActive
        self.sleep = sleep
    }

    /// How long a load waits for the listener's return to settle.
    static let gateWait: Duration = .seconds(5)
    static let gateStep: Duration = .milliseconds(50)

    /// The gate a load may go by. This document loads through the App's
    /// local listener for the instance, as the page does. Right after
    /// Avibe comes to the front that listener is closed or being bound
    /// again (`held`), and a load issued then fails at once (39.1, 00:44Z:
    /// a take recorded in front, "can't reach your Avibe", nothing reached
    /// the instance); so in front a held gate is waited for, up to
    /// `gateWait` by the clock. In the background the gate stays held for
    /// as long as Avibe is there, so a wait could never end: the load goes
    /// at once and rests on WebKit's own failover past the closed listener
    /// (`allowFailover`; every background take so far got through, but it
    /// is a safety net, not a guarantee, so a failure there stays
    /// retryable and is never a `direct_fault`).
    ///
    /// Between Avibe entering the foreground (the re-bind starts) and
    /// becoming active it reads as not active, so a load issued then does
    /// not wait; the listener refuses it at once, and `makeReady`'s second
    /// pass, by then in front, waits. That retry is what covers this window.
    static func settledGate(
        gate: () -> DirectLoadGate,
        isActive: () -> Bool,
        sleep: (Duration) async -> Void,
        now: () -> ContinuousClock.Instant = { ContinuousClock.now },
        limit: Duration = gateWait,
        step: Duration = gateStep
    ) async -> DirectLoadGate {
        let deadline = now() + limit
        while isActive(), gate() == .held, now() < deadline, !Task.isCancelled {
            await sleep(step)
        }
        return gate()
    }

    /// Whether a load that failed is made once more: only the first, only
    /// in front, only when the gate was held around it, and never for a
    /// client that was closed meanwhile.
    static func loadsAgain(attempt: Int, closed: Bool, active: Bool, heldBefore: Bool, heldAfter: Bool) -> Bool {
        attempt == 0 && !closed && active && (heldBefore || heldAfter)
    }

    private func gaveUp(_ request: String, _ cause: String) {
        failures = Self.adding(
            "\(request) \(cause) +\(Int(Date().timeIntervalSince(madeAt) * 1000))ms", to: failures
        )
    }

    static func adding(_ failure: String, to failures: [String]) -> [String] {
        Array((failures + [String(failure.prefix(120))]).suffix(4))
    }

    /// Why a response reads as "could not reach the instance", if it does:
    /// the fetch itself failed (`fetch0`, with the error's name and how
    /// long it took against its deadline), or a gateway answered for it.
    static func unreachableCause(
        status: Int, errorCode: String?, fetchError: String?, elapsed: TimeInterval, deadline: TimeInterval
    ) -> String? {
        switch status {
        case 0:
            return "fetch0 \(fetchError ?? "-") \(Int(elapsed * 1000))/\(Int(deadline * 1000))ms"
        case 400 where errorCode == "asr_unavailable", 503 where errorCode == "asr_unavailable":
            return nil
        case 502, 503, 504, 520...599:
            return "http \(status) \(errorCode ?? "-")"
        default:
            return nil
        }
    }

    // MARK: Instance API

    /// `GET /api/asr/status`. An instance without the `consent` field
    /// predates the consent question and is refused (`olderAvibe`).
    func status() async throws -> Status {
        try Self.parseStatus(try await request("GET", "/api/asr/status"))
    }

    static func parseStatus(_ response: Response) throws -> Status {
        try check(response)
        guard let body = response.json else {
            throw Failure.server(response.status, nil)
        }
        guard body.keys.contains("consent") else {
            throw Failure.olderAvibe
        }
        let consent: Status.Consent
        switch body["consent"] as? String {
        case "granted": consent = .granted
        case "declined": consent = .declined
        default: consent = .notAsked
        }
        return Status(available: body["available"] as? Bool == true, consent: consent)
    }

    /// `POST /api/asr/transcribe` with the whole take; the instance relays
    /// it to avibe.bot's transcription and enforces the account's consent.
    func transcribe(audio: Data, name: String, mime: String) async throws -> String {
        let body: [String: Any] = [
            "data": audio.base64EncodedString(),
            "name": name,
            "mime": mime,
            "dictation_id": dictationID,
        ]
        let response = try await request(
            "POST", "/api/asr/transcribe", json: body, deadline: 60
        )
        if response.status == 422, response.errorCode == "transcription_empty" {
            throw Failure.nothingHeard
        }
        try Self.check(response)
        let text = (response.json?["text"] as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !text.isEmpty else {
            throw Failure.nothingHeard
        }
        return text
    }

    /// The main Bot's Session messages path. `GET /api/sessions/main`
    /// creates the Session on first use.
    func mainBotMessagesPath() async throws -> String {
        let main = try await request("GET", "/api/sessions/main")
        try Self.check(main)
        let sessionID = (main.json?["session"] as? [String: Any])?["id"] as? String
            ?? Self.mainSessionID
        return "/api/sessions/"
            + (sessionID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed)
                ?? Self.mainSessionID)
    }

    /// Posts the words as a spoken message. The reply arrives over the
    /// instance's usual notifications.
    ///
    /// An ordinary submission clears the Session's composer draft, which
    /// this message never came from, so an unsent draft is put back: read
    /// before, restored after, only if nobody has written one since.
    func postMessage(
        _ text: String, to sessionPath: String, capture: String?, timing: String? = nil, earlier: String? = nil
    ) async throws {
        let before = try? await request("GET", sessionPath + "/draft")
        let draft = (before?.status == 200 ? before?.json?["text"] as? String : nil) ?? ""
        let response = try await request(
            "POST", sessionPath + "/messages",
            json: ["text": text, "metadata": ["input": "voice"]
                .merging(capture.map { ["capture_attempt": $0] } ?? [:], uniquingKeysWith: { $1 })
                .merging(timing.map { ["capture_timing": $0] } ?? [:], uniquingKeysWith: { $1 })
                .merging(earlier.map { ["capture_earlier": $0] } ?? [:], uniquingKeysWith: { $1 })]
        )
        try Self.check(response)
        guard !draft.isEmpty else { return }
        guard
            let after = try? await request("GET", sessionPath + "/draft"),
            after.status == 200,
            (after.json?["text"] as? String ?? "").isEmpty
        else {
            return
        }
        _ = try? await request(
            "PUT", sessionPath + "/draft",
            json: ["text": draft, "expected_updated_at": after.json?["updated_at"] ?? NSNull()]
        )
    }

    /// The WebView a store transition must wait for.
    var releaseBarrier: WebsiteDataStoreReleaseBarrier? {
        webView.map { WebsiteDataStoreReleaseBarrier(webViews: [$0]) }
    }

    /// Releases the hidden WebView, for good: a request made after it (a
    /// send still under way for a take that was given up) loads nothing and
    /// fails as unreachable. Call when a take is finished.
    func close() {
        closed = true
        webView?.stopLoading()
        webView?.navigationDelegate = nil
        webView = nil
        ready = false
        if loadContinuation != nil { gaveUp(loadingFor, "closed") }
        // The request this load was for was never made.
        loadContinuation?.resume(throwing: Failure.closed)
        loadContinuation = nil
    }

    // MARK: Transport

    struct Response {
        let status: Int
        let json: [String: Any]?
        let redirected: Bool

        var errorCode: String? {
            json?["error"] as? String
        }
    }

    static func check(_ response: Response) throws {
        if response.redirected {
            throw Failure.signIn
        }
        switch response.status {
        case 200..<300:
            return
        case 0:
            throw Failure.offline
        case 401:
            throw Failure.signIn
        case 403:
            switch response.errorCode {
            case "voice_consent_required": throw Failure.consentRequired
            case "remote_access_revoked": throw Failure.signIn
            default: throw Failure.forbidden
            }
        case 400 where response.errorCode == "asr_unavailable",
             503 where response.errorCode == "asr_unavailable":
            throw Failure.unavailable
        case 502, 503, 504, 520...599:
            throw Failure.offline
        default:
            throw Failure.server(response.status, response.errorCode)
        }
    }

    private func request(
        _ method: String,
        _ path: String,
        json: [String: Any]? = nil,
        deadline: TimeInterval = 15
    ) async throws -> Response {
        let label = "\(method) \(path)"
        guard !closed else {
            gaveUp(label, "closed")
            throw Failure.closed
        }
        try await ensureReady(for: label)
        guard !closed else {
            gaveUp(label, "closed")
            throw Failure.closed
        }
        guard
            let webView,
            let current = webView.url,
            WebOrigin(url: current, requiresHTTPS: false) == target.origin
        else {
            gaveUp(label, "nodoc")
            throw Failure.offline
        }
        let startedAt = Date()
        var bodyString: Any = NSNull()
        if let json {
            let data = try JSONSerialization.data(withJSONObject: json)
            bodyString = String(decoding: data, as: UTF8.self)
        }
        let raw: Any?
        do {
            raw = try await webView.callAsyncJavaScript(
                Self.fetchScript,
                arguments: [
                    "method": method,
                    "path": path,
                    "body": bodyString,
                    "deadlineMilliseconds": Int(deadline * 1000),
                ],
                in: nil,
                contentWorld: .defaultClient
            )
        } catch {
            Self.logger.error("fetch script failed: \(String(describing: error), privacy: .public)")
            gaveUp(label, "js \((error as NSError).domain) \((error as NSError).code)")
            throw Failure.offline
        }
        guard
            let string = raw as? String,
            let data = string.data(using: .utf8),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            gaveUp(label, "jsresult")
            throw Failure.offline
        }
        let status = object["status"] as? Int ?? 0
        let redirected = object["type"] as? String == "opaqueredirect"
        var body: [String: Any]?
        if let text = object["body"] as? String, let bodyData = text.data(using: .utf8) {
            body = try? JSONSerialization.jsonObject(with: bodyData) as? [String: Any]
        }
        Self.logger.info("\(method, privacy: .public) \(path, privacy: .public) -> \(status, privacy: .public)")
        if let cause = Self.unreachableCause(
            status: status, errorCode: body?["error"] as? String, fetchError: object["error"] as? String,
            elapsed: Date().timeIntervalSince(startedAt), deadline: deadline
        ) {
            gaveUp(label, cause)
        }
        return Response(status: status, json: body, redirected: redirected)
    }

    /// Same-origin fetch from the instance document. A mutating request
    /// carries the CSRF token `/api/csrf-token` returns, which also sets the
    /// matching cookie. `redirect: "manual"` turns a sign-in redirect into
    /// an opaque redirect instead of following it to avibe.bot.
    private static let fetchScript = """
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), deadlineMilliseconds);
        try {
          const headers = { "Accept": "application/json" };
          if (method !== "GET") {
            const tokenResponse = await fetch("/api/csrf-token", {
              credentials: "same-origin", cache: "no-store", signal: controller.signal
            });
            const token = await tokenResponse.json();
            headers["Content-Type"] = "application/json";
            headers["X-Vibe-CSRF-Token"] = token.csrf_token;
          }
          const response = await fetch(path, {
            method,
            headers,
            body: body === null ? undefined : body,
            credentials: "same-origin",
            cache: "no-store",
            redirect: "manual",
            signal: controller.signal
          });
          const text = response.type === "opaqueredirect" ? "" : await response.text();
          return JSON.stringify({ status: response.status, type: response.type, body: text });
        } catch (error) {
          return JSON.stringify({ status: 0, error: String(error && error.name) });
        } finally {
          clearTimeout(timer);
        }
        """

    /// The document being made ready, shared by requests that overlap it.
    private var readying: Task<Void, Error>?
    /// Closed for good (its take is over, or the account is changing):
    /// nothing more is loaded or run, whatever is still on its way here.
    private var closed = false

    private func ensureReady(for label: String) async throws {
        if ready, webView != nil {
            return
        }
        if let readying {
            return try await readying.value
        }
        let task = Task { try await self.makeReady(for: label) }
        readying = task
        defer { readying = nil }
        try await task.value
    }

    private func makeReady(for label: String) async throws {
        loadingFor = label
        // In front, a load that failed while the listener was held (it was
        // issued, or it ended, in the return's window) is made once more
        // after the gate settles; both waits share one `gateWait`. In the
        // background the gate is always held and nothing is made twice.
        let deadline = ContinuousClock.now + Self.gateWait
        for attempt in 0..<2 {
            let waitedFrom = Date()
            let settled = await Self.settledGate(
                gate: gate, isActive: isActive, sleep: sleep,
                limit: max(deadline - ContinuousClock.now, .zero)
            )
            let waited = Int(Date().timeIntervalSince(waitedFrom) * 1000)
            guard !closed else {
                gaveUp(label, "closed")
                throw Failure.closed
            }
            if settled == .fault {
                gaveUp(label, "direct_fault")
                throw Failure.directFault
            }
            if waited >= 50 { gaveUp(label, "gate \(settled == .held ? "held" : "open") after \(waited)ms") }
            do {
                try await load()
                ready = true
                return
            } catch Failure.offline where Self.loadsAgain(
                attempt: attempt, closed: closed, active: isActive(),
                heldBefore: settled == .held, heldAfter: gate() == .held
            ) {
                continue
            }
        }
    }

    private func load() async throws {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = WKWebsiteDataStore(
            forIdentifier: target.storeIdentifier
        )
        let webView = WKWebView(frame: .zero, configuration: configuration)
        DirectWebViewRegistry.shared.register(webView)
        webView.navigationDelegate = self
        self.webView?.navigationDelegate = nil
        self.webView = webView
        let url = target.origin.url.appending(path: "api/csrf-token")
        try await withCheckedThrowingContinuation { continuation in
            loadContinuation = continuation
            webView.load(URLRequest(
                url: url,
                cachePolicy: .reloadIgnoringLocalCacheData,
                timeoutInterval: 10
            ))
        }
    }

    private func finishLoad(_ result: Result<Void, Error>) {
        guard let continuation = loadContinuation else { return }
        loadContinuation = nil
        continuation.resume(with: result)
    }

    nonisolated func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        Task { @MainActor in
            let origin = webView.url.flatMap { WebOrigin(url: $0, requiresHTTPS: false) }
            if origin == target.origin {
                finishLoad(.success(()))
            } else {
                finishLoad(.failure(Failure.signIn))
            }
        }
    }

    nonisolated func webView(
        _ webView: WKWebView,
        didFail navigation: WKNavigation!,
        withError error: Error
    ) {
        let cause = "load \((error as NSError).domain) \((error as NSError).code)"
        Task { @MainActor in
            if loadContinuation != nil { gaveUp(loadingFor, cause) }
            finishLoad(.failure(Failure.offline))
        }
    }

    nonisolated func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        let cause = "loadp \((error as NSError).domain) \((error as NSError).code)"
        Task { @MainActor in
            if loadContinuation != nil { gaveUp(loadingFor, cause) }
            finishLoad(.failure(Failure.offline))
        }
    }

    nonisolated func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        Task { @MainActor in
            ready = false
            gaveUp(loadingFor, "webcontent")
            finishLoad(.failure(Failure.offline))
        }
    }
}
