import Foundation
import WebKit

enum MobileAuthTransportOperation: CaseIterable, Equatable {
    case requestOTP
    case verifyOTP
    case fetchSession
    case logout
}

enum MobileAuthTransportStage: String, CaseIterable, Equatable {
    case request
    case verify
    case callback
    case session
    case logout
}

struct MobileAuthTransportDeadlines: Equatable {
    let readinessMilliseconds: Int
    let requestMilliseconds: Int
    let logoutMilliseconds: Int

    static let standard = MobileAuthTransportDeadlines(
        readinessMilliseconds: 10_000,
        requestMilliseconds: 15_000,
        logoutMilliseconds: 5_000
    )

    static func testing(_ milliseconds: Int) -> MobileAuthTransportDeadlines {
        MobileAuthTransportDeadlines(
            readinessMilliseconds: milliseconds,
            requestMilliseconds: milliseconds,
            logoutMilliseconds: milliseconds
        )
    }
}

@MainActor
final class MobileAuthTransport: NSObject, MobileAuthTransporting {
    private enum LoadState {
        case idle
        case loading
        case ready
        case failed
    }

    private let controlPlaneURL: URL
    private let deadlines: MobileAuthTransportDeadlines
    private let webView: WKWebView
    private var loadState: LoadState = .idle
    private var loadContinuations: [CheckedContinuation<Void, Error>] = []
    private var loadDeadlineTask: Task<Void, Never>?

    init(
        controlPlaneURL: URL,
        websiteDataStore: WKWebsiteDataStore,
        deadlines: MobileAuthTransportDeadlines = .standard
    ) {
        self.controlPlaneURL = controlPlaneURL
        self.deadlines = deadlines
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = websiteDataStore
        webView = WKWebView(frame: .zero, configuration: configuration)
        super.init()
        webView.navigationDelegate = self
    }

    func requestOTP(email: String) async throws -> Int {
        let response = try await evaluateJSON(
            Self.requestOTPScript,
            operation: .requestOTP,
            arguments: ["email": email]
        )
        guard response.boolean("ok") else {
            throw Self.mappedError(for: .requestOTP, response: response)
        }
        return response.integer("retryAfter") ?? 0
    }

    func verifyOTP(
        email: String,
        code: String
    ) async throws -> MobileSessionSnapshot {
        let response = try await evaluateJSON(
            Self.verifyOTPScript,
            operation: .verifyOTP,
            arguments: [
                "email": email,
                "code": code,
            ]
        )
        guard response.boolean("ok") else {
            throw Self.mappedError(for: .verifyOTP, response: response)
        }
        guard let payload = response["session"] as? [String: Any] else {
            throw MobileAccountError.network
        }
        return try decodeSnapshot(payload)
    }

    func fetchSession() async throws -> MobileSessionSnapshot? {
        let response = try await evaluateJSON(
            Self.fetchSessionScript,
            operation: .fetchSession,
            arguments: [:]
        )
        if response.integer("status") == 401 {
            return nil
        }
        guard
            response.boolean("ok"),
            let payload = response["session"] as? [String: Any]
        else {
            throw MobileAccountError.network
        }
        return try decodeSnapshot(payload)
    }

    func logout() async throws {
        let response = try await evaluateJSON(
            Self.logoutScript,
            operation: .logout,
            arguments: [:]
        )
        guard response.boolean("ok") else {
            throw Self.mappedError(for: .logout, response: response)
        }
    }

    func cancelAll() async {
        _ = try? await webView.callAsyncJavaScript(
            """
            (() => {
              const controllers = window.__avibeMobileControllers;
              if (!controllers) return;
              for (const controller of controllers) controller.abort();
              controllers.clear();
            })()
            """,
            arguments: [:],
            in: nil,
            contentWorld: .defaultClient
        )
    }

    func invalidate() {
        loadDeadlineTask?.cancel()
        loadDeadlineTask = nil
        webView.stopLoading()
        webView.navigationDelegate = nil
        loadContinuations.forEach {
            $0.resume(throwing: MobileAccountError.network)
        }
        loadContinuations.removeAll()
        loadState = .failed
    }

    private func ensureReady() async throws {
        switch loadState {
        case .ready:
            return
        case .failed:
            loadState = .idle
        case .idle, .loading:
            break
        }

        try await withCheckedThrowingContinuation { continuation in
            loadContinuations.append(continuation)
            guard loadState == .idle else {
                return
            }
            loadState = .loading
            let configURL = controlPlaneURL.appending(path: "api/config/auth")
            webView.load(
                URLRequest(
                    url: configURL,
                    cachePolicy: .reloadIgnoringLocalCacheData
                )
            )
            startReadinessDeadline()
        }
    }

    private func evaluateJSON(
        _ body: String,
        operation: MobileAuthTransportOperation,
        arguments: [String: Any]
    ) async throws -> [String: Any] {
        do {
            try await ensureReady()
            guard
                let currentURL = webView.url,
                Self.sameOrigin(currentURL, controlPlaneURL)
            else {
                throw MobileAccountError.network
            }
            var boundedArguments = arguments
            boundedArguments["deadlineMilliseconds"] = deadlineMilliseconds(
                for: operation
            )
            let result = try await webView.callAsyncJavaScript(
                body,
                arguments: boundedArguments,
                in: nil,
                contentWorld: .defaultClient
            )
            guard
                let string = result as? String,
                let data = string.data(using: .utf8),
                let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            else {
                throw MobileAccountError.network
            }
            return object
        } catch let error as MobileAccountError {
            throw error
        } catch {
            throw MobileAccountError.network
        }
    }

    private func deadlineMilliseconds(
        for operation: MobileAuthTransportOperation
    ) -> Int {
        switch operation {
        case .logout:
            return deadlines.logoutMilliseconds
        case .requestOTP, .verifyOTP, .fetchSession:
            return deadlines.requestMilliseconds
        }
    }

    private func startReadinessDeadline() {
        loadDeadlineTask?.cancel()
        let milliseconds = deadlines.readinessMilliseconds
        loadDeadlineTask = Task { @MainActor [weak self] in
            try? await Task.sleep(
                nanoseconds: UInt64(max(1, milliseconds)) * 1_000_000
            )
            guard !Task.isCancelled, let self, self.loadState == .loading else {
                return
            }
            self.webView.stopLoading()
            self.finishLoading(with: .failure(MobileAccountError.network))
        }
    }

    private func decodeSnapshot(
        _ object: [String: Any]
    ) throws -> MobileSessionSnapshot {
        let data = try JSONSerialization.data(withJSONObject: object)
        do {
            return try JSONDecoder().decode(MobileSessionSnapshot.self, from: data)
        } catch {
            throw MobileAccountError.network
        }
    }

    static func mappedError(
        for operation: MobileAuthTransportOperation,
        response: [String: Any]
    ) -> MobileAccountError {
        let status = response.integer("status")
        let retryAfter = response.integer("retryAfter")
        let stage = response.string("stage")
        let failure = response.string("failure")

        if failure != nil || status == nil || status == 0 {
            return .network
        }

        if
            status == 429,
            operation == .requestOTP
                || operation == .verifyOTP && stage == MobileAuthTransportStage.verify.rawValue
        {
            return MobileAccountError(
                code: "rate_limited",
                message: response.string("message") ?? "Please wait before trying again.",
                retryAfterSeconds: retryAfter
            )
        }

        switch operation {
        case .requestOTP:
            guard let status, (400..<500).contains(status) else {
                return .network
            }
            return MobileAccountError(
                code: response.string("code") ?? "otp_rejected",
                message: response.string("message")
                    ?? "The verification email could not be sent.",
                retryAfterSeconds: retryAfter
            )
        case .verifyOTP:
            guard
                stage == MobileAuthTransportStage.verify.rawValue,
                let status,
                (400..<500).contains(status)
            else {
                return .network
            }
            return MobileAccountError(
                code: "invalid_or_expired",
                message: response.string("message")
                    ?? "The verification code is invalid or expired.",
                retryAfterSeconds: retryAfter
            )
        case .fetchSession, .logout:
            return .network
        }
    }
}

extension MobileAuthTransport: WKNavigationDelegate {
    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard
            let url = navigationAction.request.url,
            Self.sameOrigin(url, controlPlaneURL)
        else {
            decisionHandler(.cancel)
            finishLoading(with: .failure(MobileAccountError.network))
            return
        }
        decisionHandler(.allow)
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationResponse: WKNavigationResponse,
        decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void
    ) {
        guard
            let url = navigationResponse.response.url,
            Self.sameOrigin(url, controlPlaneURL)
        else {
            decisionHandler(.cancel)
            finishLoading(with: .failure(MobileAccountError.network))
            return
        }
        decisionHandler(.allow)
    }

    func webView(
        _ webView: WKWebView,
        didFinish navigation: WKNavigation?
    ) {
        guard
            let url = webView.url,
            Self.sameOrigin(url, controlPlaneURL)
        else {
            finishLoading(with: .failure(MobileAccountError.network))
            return
        }
        finishLoading(with: .success(()))
    }

    func webView(
        _ webView: WKWebView,
        didFail navigation: WKNavigation?,
        withError error: Error
    ) {
        finishLoading(with: .failure(error))
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation?,
        withError error: Error
    ) {
        finishLoading(with: .failure(error))
    }

    private func finishLoading(with result: Result<Void, Error>) {
        guard loadState == .loading else {
            return
        }
        loadDeadlineTask?.cancel()
        loadDeadlineTask = nil
        switch result {
        case .success:
            loadState = .ready
            loadContinuations.forEach { $0.resume() }
        case .failure(let error):
            loadState = .failed
            loadContinuations.forEach { $0.resume(throwing: error) }
        }
        loadContinuations.removeAll()
    }

    private static func sameOrigin(_ lhs: URL, _ rhs: URL) -> Bool {
        guard
            let lhsComponents = URLComponents(
                url: lhs,
                resolvingAgainstBaseURL: false
            ),
            let rhsComponents = URLComponents(
                url: rhs,
                resolvingAgainstBaseURL: false
            )
        else {
            return false
        }
        return lhsComponents.scheme?.lowercased() == rhsComponents.scheme?.lowercased()
            && lhsComponents.host?.lowercased() == rhsComponents.host?.lowercased()
            && effectivePort(lhsComponents) == effectivePort(rhsComponents)
    }

    private static func effectivePort(_ components: URLComponents) -> Int? {
        if let port = components.port {
            return port
        }
        switch components.scheme?.lowercased() {
        case "http":
            return 80
        case "https":
            return 443
        default:
            return nil
        }
    }

    private static func boundedScript(
        initialStage: MobileAuthTransportStage,
        body: String
    ) -> String {
        """
        const controllers = window.__avibeMobileControllers ??= new Set();
        const controller = new AbortController();
        controllers.add(controller);
        const timeoutMilliseconds = Math.max(1, Number(deadlineMilliseconds) || 1);
        let timedOut = false;
        let stage = "\(initialStage.rawValue)";
        const timeout = window.setTimeout(() => {
          timedOut = true;
          controller.abort();
        }, timeoutMilliseconds);
        const boundedFetch = (input, init = {}) => fetch(input, {
          ...init,
          signal: controller.signal,
          redirect: "error"
        });
        const failed = (
          failure,
          status = 0,
          message = null,
          code = null,
          retryAfter = null
        ) => JSON.stringify({
          ok: false,
          stage,
          status,
          failure,
          code,
          retryAfter,
          message
        });
        try {
        \(body)
        } catch (error) {
          return failed(
            timedOut
              ? "timeout"
              : error?.name === "AbortError"
                ? "abort"
                : "exception",
            0,
            timedOut ? "Request timed out." : "Network unavailable."
          );
        } finally {
          window.clearTimeout(timeout);
          controllers.delete(controller);
        }
        """
    }

    private static let requestOTPScript = boundedScript(
        initialStage: .request,
        body: """
          let config;
          try {
            config = JSON.parse(document.body.textContent || "{}");
          } catch {
            return failed("invalid_response", 0, "Authentication configuration is invalid.");
          }
          if (!config.supabase_url || !config.supabase_anon_key) {
            return failed("configuration", 503, "Authentication is unavailable.");
          }
          const endpoint = new URL("/auth/v1/otp", config.supabase_url);
          endpoint.searchParams.set(
            "redirect_to",
            new URL("/auth/callback", window.location.origin).toString()
          );
          const response = await boundedFetch(endpoint, {
            method: "POST",
            credentials: "omit",
            headers: {
              "apikey": config.supabase_anon_key,
              "Authorization": `Bearer ${config.supabase_anon_key}`,
              "Content-Type": "application/json"
            },
            body: JSON.stringify({email, create_user: true})
          });
          let payload;
          try {
            payload = await response.json();
          } catch {
            return failed(
              "invalid_response",
              response.status,
              "Authentication returned an invalid response."
            );
          }
          return JSON.stringify({
            ok: response.ok,
            stage,
            status: response.status,
            code: payload.code || payload.error_code || payload.error || null,
            retryAfter: Number(response.headers.get("retry-after")) || null,
            message: payload.message || payload.msg || payload.error_description || null
          });
        """
    )

    private static let verifyOTPScript = boundedScript(
        initialStage: .verify,
        body: """
          let config;
          try {
            config = JSON.parse(document.body.textContent || "{}");
          } catch {
            return failed("invalid_response", 0, "Authentication configuration is invalid.");
          }
          if (!config.supabase_url || !config.supabase_anon_key) {
            return failed("configuration", 503, "Authentication is unavailable.");
          }
          const verifyResponse = await boundedFetch(new URL("/auth/v1/verify", config.supabase_url), {
            method: "POST",
            credentials: "omit",
            headers: {
              "apikey": config.supabase_anon_key,
              "Authorization": `Bearer ${config.supabase_anon_key}`,
              "Content-Type": "application/json"
            },
            body: JSON.stringify({email, token: code, type: "email"})
          });
          let verified;
          try {
            verified = await verifyResponse.json();
          } catch {
            return failed(
              "invalid_response",
              verifyResponse.status,
              "Verification returned an invalid response."
            );
          }
          if (!verifyResponse.ok) {
            return JSON.stringify({
              ok: false,
              stage,
              status: verifyResponse.status,
              code: verified.code || verified.error_code || verified.error || null,
              retryAfter: Number(verifyResponse.headers.get("retry-after")) || null,
              message: verified.message || verified.msg || verified.error_description || null
            });
          }
          if (!verified.access_token || !verified.refresh_token) {
            return failed(
              "invalid_response",
              verifyResponse.status,
              "Verification did not return a session."
            );
          }

          stage = "callback";
          const callbackResponse = await boundedFetch(new URL("/auth/callback", window.location.origin), {
            method: "POST",
            credentials: "same-origin",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
              access_token: verified.access_token,
              refresh_token: verified.refresh_token
            })
          });
          if (!callbackResponse.ok) {
            let callbackPayload;
            try {
              callbackPayload = await callbackResponse.json();
            } catch {
              return failed(
                "invalid_response",
                callbackResponse.status,
                "Session callback returned an invalid response."
              );
            }
            return JSON.stringify({
              ok: false,
              stage,
              status: callbackResponse.status,
              code: callbackPayload.code || callbackPayload.error || null,
              message: callbackPayload.message || callbackPayload.error || "Session callback failed."
            });
          }

          stage = "session";
          const sessionResponse = await boundedFetch(new URL("/api/mobile/session", window.location.origin), {
            credentials: "same-origin",
            cache: "no-store"
          });
          let session;
          try {
            session = await sessionResponse.json();
          } catch {
            return failed(
              "invalid_response",
              sessionResponse.status,
              "Session discovery returned an invalid response."
            );
          }
          return JSON.stringify({
            ok: sessionResponse.ok,
            stage,
            status: sessionResponse.status,
            session
          });
        """
    )

    private static let fetchSessionScript = boundedScript(
        initialStage: .session,
        body: """
          const response = await boundedFetch(new URL("/api/mobile/session", window.location.origin), {
            credentials: "same-origin",
            cache: "no-store"
          });
          if (response.status === 401) {
            return JSON.stringify({ok: false, stage, status: 401});
          }
          let session;
          try {
            session = await response.json();
          } catch {
            return failed(
              "invalid_response",
              response.status,
              "Session discovery returned an invalid response."
            );
          }
          return JSON.stringify({ok: response.ok, stage, status: response.status, session});
        """
    )

    private static let logoutScript = boundedScript(
        initialStage: .logout,
        body: """
          const response = await boundedFetch(new URL("/api/mobile/session", window.location.origin), {
            method: "DELETE",
            credentials: "same-origin",
            cache: "no-store"
          });
          let payload;
          try {
            payload = await response.json();
          } catch {
            return failed(
              "invalid_response",
              response.status,
              "Logout returned an invalid response."
            );
          }
          return JSON.stringify({
            ok: response.ok,
            stage,
            status: response.status,
            code: payload.code || payload.error || null,
            message: payload.message || payload.error || null
          });
        """
    )
}

private extension Dictionary where Key == String, Value == Any {
    func boolean(_ key: String) -> Bool {
        self[key] as? Bool == true
    }

    func integer(_ key: String) -> Int? {
        if let value = self[key] as? Int {
            return value
        }
        if let value = self[key] as? NSNumber {
            return value.intValue
        }
        return nil
    }

    func string(_ key: String) -> String? {
        self[key] as? String
    }
}
