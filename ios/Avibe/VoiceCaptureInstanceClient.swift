import Foundation
import OSLog
import WebKit

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
    private var webView: WKWebView?
    private var loadContinuation: CheckedContinuation<Void, Error>?
    private var ready = false

    init(target: VoiceCaptureTarget) {
        self.target = target
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
            "dictation_id": "ios-action-\(UUID().uuidString.lowercased())",
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
    func postMessage(_ text: String, to sessionPath: String, capture: String?) async throws {
        let before = try? await request("GET", sessionPath + "/draft")
        let draft = (before?.status == 200 ? before?.json?["text"] as? String : nil) ?? ""
        let response = try await request(
            "POST", sessionPath + "/messages",
            json: ["text": text, "metadata": ["input": "voice"].merging(
                capture.map { ["capture_attempt": $0] } ?? [:], uniquingKeysWith: { $1 }
            )]
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

    /// Releases the hidden WebView. Call when a take is finished.
    func close() {
        webView?.stopLoading()
        webView?.navigationDelegate = nil
        webView = nil
        ready = false
        loadContinuation?.resume(throwing: Failure.offline)
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
        try await ensureReady()
        guard
            let webView,
            let current = webView.url,
            WebOrigin(url: current, requiresHTTPS: false) == target.origin
        else {
            throw Failure.offline
        }
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
            throw Failure.offline
        }
        guard
            let string = raw as? String,
            let data = string.data(using: .utf8),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            throw Failure.offline
        }
        let status = object["status"] as? Int ?? 0
        let redirected = object["type"] as? String == "opaqueredirect"
        var body: [String: Any]?
        if let text = object["body"] as? String, let bodyData = text.data(using: .utf8) {
            body = try? JSONSerialization.jsonObject(with: bodyData) as? [String: Any]
        }
        Self.logger.info("\(method, privacy: .public) \(path, privacy: .public) -> \(status, privacy: .public)")
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

    private func ensureReady() async throws {
        if ready, webView != nil {
            return
        }
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = WKWebsiteDataStore(
            forIdentifier: target.storeIdentifier
        )
        let webView = WKWebView(frame: .zero, configuration: configuration)
        DirectWebViewRegistry.shared.register(webView)
        webView.navigationDelegate = self
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
        ready = true
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
        Task { @MainActor in finishLoad(.failure(Failure.offline)) }
    }

    nonisolated func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        Task { @MainActor in finishLoad(.failure(Failure.offline)) }
    }

    nonisolated func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        Task { @MainActor in
            ready = false
            finishLoad(.failure(Failure.offline))
        }
    }
}
