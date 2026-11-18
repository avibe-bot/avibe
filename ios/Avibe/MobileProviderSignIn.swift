import AuthenticationServices
import CryptoKit
import Foundation
import Security

/// The other accounts the App signs in with: Google and GitHub in the system sign-in window,
/// Apple in its native sheet (owner, 2026-11-18).
enum MobileSignInProvider: String, CaseIterable {
    case google
    case github
    case apple
}

/// One attempt's PKCE verifier and state: 32 random bytes each, unpadded base64url. They live in
/// native memory only, for the attempt that made them.
struct MobileProviderSecrets: Equatable {
    let verifier: String
    let state: String

    /// `S256`: unpadded base64url of SHA256 over the verifier's ASCII.
    var challenge: String {
        Self.challenge(for: verifier)
    }

    static func challenge(for verifier: String) -> String {
        MobileBase64URL.encode(Data(SHA256.hash(data: Data(verifier.utf8))))
    }

    static func generate() throws -> MobileProviderSecrets {
        MobileProviderSecrets(verifier: try randomToken(), state: try randomToken())
    }

    private static func randomToken() throws -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
            throw MobileAccountError.network
        }
        return MobileBase64URL.encode(Data(bytes))
    }
}

/// What `POST /api/mobile/auth/start` returns: a browser address for Google and GitHub, the
/// server's raw nonce for Apple.
enum MobileProviderStart: Equatable {
    case browser(transactionID: String, url: URL)
    case apple(transactionID: String, nonce: String)
}

/// What Sign in with Apple hands back: the identity token and the one-use authorization code
/// (both UTF-8), and the request's state as Apple returned it.
struct MobileAppleCredential: Equatable {
    let identityToken: String
    let authorizationCode: String
    let state: String?
}

enum MobileAppleNonce {
    /// The `nonce` an Apple ID request carries: lowercase hex SHA256 over the server's raw nonce.
    static func hashed(_ raw: String) -> String {
        SHA256.hash(data: Data(raw.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}

/// What the exchange settles: the session read after it, and whether the backend attested that
/// this transaction created the account (`Avibe-Account-Created: 1`). Absence proves nothing.
struct MobileProviderExchange: Equatable {
    let discovery: MobileSessionDiscovery
    let accountCreated: Bool
}

/// The system sign-in window's address and its one return address, both fixed on the control
/// plane (contract: `_tmp/ios-login/contract-backend.md`).
enum MobileProviderRoute {
    static let browserPath = "/api/mobile/auth/browser"
    static let callbackPath = "/mobile/auth/complete"
    /// The code is GoTrue's opaque PKCE auth code: any nonempty value up to 4 KiB.
    static let maxValueBytes = 4096

    /// The only address the App opens: the control plane's browser route with exactly one
    /// `launch` parameter.
    static func isAllowedBrowserURL(_ url: URL, controlPlane: URL) -> Bool {
        guard
            let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
            let base = URLComponents(url: controlPlane, resolvingAgainstBaseURL: false),
            components.scheme?.lowercased() == base.scheme?.lowercased(),
            components.host?.lowercased() == base.host?.lowercased(),
            components.port == base.port,
            components.user == nil,
            components.password == nil,
            components.fragment == nil,
            components.percentEncodedPath == browserPath,
            let items = components.queryItems,
            items.count == 1,
            items[0].name == "launch",
            let launch = items[0].value,
            isCanonicalToken(launch)
        else {
            return false
        }
        return true
    }

    /// What the return address says. `nil` means the address is not this attempt's callback at
    /// all; anything malformed about a callback is `invalid`.
    enum Callback: Equatable {
        case code(String)
        case cancelled
        case emailUnverified
        case unavailable
        case invalid
    }

    static func callback(_ url: URL, controlPlane: URL, state: String) -> Callback {
        guard
            let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
            let base = URLComponents(url: controlPlane, resolvingAgainstBaseURL: false),
            components.scheme?.lowercased() == "https",
            components.host?.lowercased() == base.host?.lowercased(),
            components.port == nil,
            components.user == nil,
            components.password == nil,
            // The backend may end the address with an empty `#` so the browser drops a fragment
            // Supabase put on an earlier hop; any fragment with content is refused.
            components.fragment == nil || components.fragment == "",
            components.percentEncodedPath == callbackPath,
            let items = components.queryItems
        else {
            return .invalid
        }
        var values: [String: String] = [:]
        for item in items {
            guard
                ["code", "error", "state"].contains(item.name),
                values[item.name] == nil,
                let value = item.value,
                !value.isEmpty,
                value.utf8.count <= maxValueBytes
            else {
                return .invalid
            }
            values[item.name] = value
        }
        guard
            let returned = values["state"],
            constantTimeEqual(returned, state),
            (values["code"] == nil) != (values["error"] == nil)
        else {
            return .invalid
        }
        if let code = values["code"] {
            return .code(code)
        }
        switch values["error"] {
        case "cancelled":
            return .cancelled
        case "provider_email_unverified":
            return .emailUnverified
        case "provider_unavailable":
            return .unavailable
        default:
            return .invalid
        }
    }

    /// A 32-byte random value in canonical unpadded base64url: exactly 43 characters, the last
    /// one carrying no unused bits. The contract's launch ticket, transaction id and nonce.
    static func isCanonicalToken(_ value: String) -> Bool {
        guard
            value.utf8.count == 43,
            value.range(of: #"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$"#, options: .regularExpression) != nil
        else {
            return false
        }
        return true
    }

    static func constantTimeEqual(_ lhs: String, _ rhs: String) -> Bool {
        let left = Array(lhs.utf8)
        let right = Array(rhs.utf8)
        guard left.count == right.count else {
            return false
        }
        var difference: UInt8 = 0
        for index in left.indices {
            difference |= left[index] ^ right[index]
        }
        return difference == 0
    }
}

@MainActor
protocol MobileWebAuthenticating: AnyObject {
    /// Opens `url` in the system sign-in window and returns the address it came back to: an
    /// `https` address on `callbackHost` at `callbackPath`. Throws `providerCancelled` when the
    /// person closes it, `network` otherwise. Cancelling the calling task closes the window.
    func authenticate(
        url: URL,
        callbackHost: String,
        callbackPath: String,
        presentationAnchor: ASPresentationAnchor
    ) async throws -> URL
    /// Closes an open window; its caller gets `providerCancelled`. Nothing open: no effect.
    func cancel()
}

@MainActor
final class MobileWebAuthenticator: NSObject, MobileWebAuthenticating {
    private struct Pending {
        let session: ASWebAuthenticationSession
        let anchor: ASPresentationAnchor
        let continuation: CheckedContinuation<URL, Error>
    }

    private var pending: Pending?

    func authenticate(
        url: URL,
        callbackHost: String,
        callbackPath: String,
        presentationAnchor: ASPresentationAnchor
    ) async throws -> URL {
        guard pending == nil else {
            throw MobileAccountError.busy
        }
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                var session: ASWebAuthenticationSession?
                session = ASWebAuthenticationSession(
                    url: url,
                    callback: .https(host: callbackHost, path: callbackPath)
                ) { [weak self] callbackURL, error in
                    Task { @MainActor in
                        guard let self, let session else { return }
                        if let callbackURL {
                            self.finish(session, with: .success(callbackURL))
                        } else {
                            self.finish(session, with: .failure(Self.mappedError(error)))
                        }
                    }
                }
                guard let session else {
                    continuation.resume(throwing: MobileAccountError.network)
                    return
                }
                // Reuse Safari's Google or GitHub sign-in; the browser leg sets no Avibe session.
                session.prefersEphemeralWebBrowserSession = false
                session.presentationContextProvider = self
                pending = Pending(session: session, anchor: presentationAnchor, continuation: continuation)
                if Task.isCancelled || !session.start() {
                    finish(session, with: .failure(
                        Task.isCancelled ? MobileAccountError.providerCancelled : MobileAccountError.network
                    ))
                }
            }
        } onCancel: {
            Task { @MainActor [weak self] in
                self?.cancel()
            }
        }
    }

    /// A programmatic cancel does not call the session's completion handler, so the attempt
    /// is finished here as well.
    func cancel() {
        guard let pending else { return }
        pending.session.cancel()
        finish(pending.session, with: .failure(MobileAccountError.providerCancelled))
    }

    static func mappedError(_ error: Error?) -> MobileAccountError {
        if let error = error as? ASWebAuthenticationSessionError, error.code == .canceledLogin {
            return .providerCancelled
        }
        return .network
    }

    private func finish(_ session: ASWebAuthenticationSession, with result: Result<URL, Error>) {
        guard let pending, pending.session === session else {
            return
        }
        self.pending = nil
        pending.continuation.resume(with: result)
    }
}

extension MobileWebAuthenticator: ASWebAuthenticationPresentationContextProviding {
    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        guard let pending, pending.session === session else {
            return ASPresentationAnchor()
        }
        return pending.anchor
    }
}

@MainActor
protocol MobileAppleAuthorizing: AnyObject {
    /// Runs the Sign in with Apple sheet for the email scope with `nonce` (already hashed) and
    /// `state`. Throws `providerCancelled` when the person dismisses it, `network` otherwise.
    func authorize(
        nonce: String,
        state: String,
        presentationAnchor: ASPresentationAnchor
    ) async throws -> MobileAppleCredential
    /// Closes an open sheet; its caller gets `providerCancelled`. Nothing open: no effect.
    func cancel()
}

@MainActor
final class MobileAppleAuthorizer: NSObject, MobileAppleAuthorizing {
    private struct Pending {
        let controller: ASAuthorizationController
        let anchor: ASPresentationAnchor
        let continuation: CheckedContinuation<MobileAppleCredential, Error>
    }

    private var pending: Pending?

    func authorize(
        nonce: String,
        state: String,
        presentationAnchor: ASPresentationAnchor
    ) async throws -> MobileAppleCredential {
        guard pending == nil else {
            throw MobileAccountError.busy
        }
        let request = ASAuthorizationAppleIDProvider().createRequest()
        // The name is never sent anywhere, so it is not asked for.
        request.requestedScopes = [.email]
        request.nonce = nonce
        request.state = state
        let controller = ASAuthorizationController(authorizationRequests: [request])
        controller.delegate = self
        controller.presentationContextProvider = self
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                pending = Pending(controller: controller, anchor: presentationAnchor, continuation: continuation)
                if Task.isCancelled {
                    finish(controller, with: .failure(MobileAccountError.providerCancelled))
                    return
                }
                controller.performRequests()
            }
        } onCancel: {
            Task { @MainActor [weak self] in
                self?.cancel()
            }
        }
    }

    func cancel() {
        guard let pending else { return }
        pending.controller.cancel()
        finish(pending.controller, with: .failure(MobileAccountError.providerCancelled))
    }

    static func mappedError(_ error: Error) -> MobileAccountError {
        if let error = error as? ASAuthorizationError, error.code == .canceled {
            return .providerCancelled
        }
        return .network
    }

    private func finish(
        _ controller: ASAuthorizationController,
        with result: Result<MobileAppleCredential, Error>
    ) {
        guard let pending, pending.controller === controller else {
            return
        }
        self.pending = nil
        pending.continuation.resume(with: result)
    }
}

extension MobileAppleAuthorizer: ASAuthorizationControllerDelegate {
    func authorizationController(
        controller: ASAuthorizationController,
        didCompleteWithAuthorization authorization: ASAuthorization
    ) {
        guard
            let credential = authorization.credential as? ASAuthorizationAppleIDCredential,
            let tokenData = credential.identityToken,
            let codeData = credential.authorizationCode,
            (1...16_384).contains(tokenData.count),
            (1...4_096).contains(codeData.count),
            let identityToken = String(data: tokenData, encoding: .utf8),
            let authorizationCode = String(data: codeData, encoding: .utf8)
        else {
            finish(controller, with: .failure(MobileAccountError.network))
            return
        }
        finish(
            controller,
            with: .success(
                MobileAppleCredential(
                    identityToken: identityToken,
                    authorizationCode: authorizationCode,
                    state: credential.state
                )
            )
        )
    }

    func authorizationController(
        controller: ASAuthorizationController,
        didCompleteWithError error: Error
    ) {
        finish(controller, with: .failure(Self.mappedError(error)))
    }
}

extension MobileAppleAuthorizer: ASAuthorizationControllerPresentationContextProviding {
    func presentationAnchor(for controller: ASAuthorizationController) -> ASPresentationAnchor {
        guard let pending, pending.controller === controller else {
            return ASPresentationAnchor()
        }
        return pending.anchor
    }
}
