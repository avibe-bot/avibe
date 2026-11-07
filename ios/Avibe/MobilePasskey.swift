import AuthenticationServices
import Foundation

/// WebAuthn's JSON encoding of binary fields: unpadded base64url.
enum MobileBase64URL {
    static func encode(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    static func decode(_ string: String) -> Data? {
        guard
            !string.isEmpty,
            string.range(of: #"^[A-Za-z0-9_-]+$"#, options: .regularExpression) != nil,
            string.count % 4 != 1
        else {
            return nil
        }
        var base64 = string
            .replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        base64 += String(repeating: "=", count: (4 - base64.count % 4) % 4)
        return Data(base64Encoded: base64)
    }
}

/// The parts of avibe.bot's `PublicKeyCredentialRequestOptionsJSON` the
/// system passkey request needs. The relying party is fixed: native code
/// asserts only for `avibe.bot`, which the app's `webcredentials` associated
/// domain names, whatever the control plane answers.
struct MobilePasskeyRequest: Equatable {
    static let relyingPartyIdentifier = "avibe.bot"

    let challenge: Data
    let allowedCredentialIDs: [Data]

    init(challenge: Data, allowedCredentialIDs: [Data]) {
        self.challenge = challenge
        self.allowedCredentialIDs = allowedCredentialIDs
    }

    init(options: [String: Any]) throws {
        guard options["rpId"] as? String == Self.relyingPartyIdentifier else {
            throw MobileAccountError.passkeyUnavailable
        }
        guard
            let rawChallenge = options["challenge"] as? String,
            let challenge = MobileBase64URL.decode(rawChallenge)
        else {
            throw MobileAccountError.network
        }
        var allowedCredentialIDs: [Data] = []
        if let rawDescriptors = options["allowCredentials"] {
            guard let descriptors = rawDescriptors as? [[String: Any]] else {
                throw MobileAccountError.network
            }
            for descriptor in descriptors {
                guard
                    (descriptor["type"] as? String ?? "public-key") == "public-key",
                    let rawID = descriptor["id"] as? String,
                    let id = MobileBase64URL.decode(rawID)
                else {
                    throw MobileAccountError.network
                }
                allowedCredentialIDs.append(id)
            }
        }
        self.init(
            challenge: challenge,
            allowedCredentialIDs: allowedCredentialIDs
        )
    }
}

struct MobilePasskeyChallenge: Equatable {
    let request: MobilePasskeyRequest
    /// avibe.bot's signed challenge token, returned with the assertion.
    let token: String
}

/// A platform passkey assertion, encoded as WebAuthn's
/// `AuthenticationResponseJSON` for `POST /api/two-factor/verify`.
struct MobilePasskeyAssertion: Equatable {
    let credentialID: Data
    let clientDataJSON: Data
    let authenticatorData: Data
    let signature: Data
    let userHandle: Data?

    var authenticationResponseJSON: [String: Any] {
        let id = MobileBase64URL.encode(credentialID)
        var response: [String: Any] = [
            "clientDataJSON": MobileBase64URL.encode(clientDataJSON),
            "authenticatorData": MobileBase64URL.encode(authenticatorData),
            "signature": MobileBase64URL.encode(signature),
        ]
        if let userHandle, !userHandle.isEmpty {
            response["userHandle"] = MobileBase64URL.encode(userHandle)
        }
        return [
            "id": id,
            "rawId": id,
            "type": "public-key",
            "response": response,
            "clientExtensionResults": [String: Any](),
            "authenticatorAttachment": "platform",
        ]
    }
}

@MainActor
protocol MobilePasskeyAuthenticating: AnyObject {
    /// Throws `passkeyCancelled` when the person dismisses the system sheet
    /// and `passkeyUnavailable` for every other system failure.
    func assert(
        _ request: MobilePasskeyRequest,
        presentationAnchor: ASPresentationAnchor
    ) async throws -> MobilePasskeyAssertion
}

@MainActor
final class MobilePasskeyAuthenticator: NSObject, MobilePasskeyAuthenticating {
    private struct PendingRequest {
        let controller: ASAuthorizationController
        let anchor: ASPresentationAnchor
        let continuation: CheckedContinuation<MobilePasskeyAssertion, Error>
    }

    private var pending: PendingRequest?

    func assert(
        _ request: MobilePasskeyRequest,
        presentationAnchor: ASPresentationAnchor
    ) async throws -> MobilePasskeyAssertion {
        guard pending == nil else {
            throw MobileAccountError.busy
        }
        let provider = ASAuthorizationPlatformPublicKeyCredentialProvider(
            relyingPartyIdentifier: MobilePasskeyRequest.relyingPartyIdentifier
        )
        let assertionRequest = provider.createCredentialAssertionRequest(
            challenge: request.challenge
        )
        assertionRequest.allowedCredentials = request.allowedCredentialIDs.map {
            ASAuthorizationPlatformPublicKeyCredentialDescriptor(credentialID: $0)
        }
        assertionRequest.userVerificationPreference = .required
        let controller = ASAuthorizationController(
            authorizationRequests: [assertionRequest]
        )
        controller.delegate = self
        controller.presentationContextProvider = self
        return try await withCheckedThrowingContinuation { continuation in
            pending = PendingRequest(
                controller: controller,
                anchor: presentationAnchor,
                continuation: continuation
            )
            controller.performRequests()
        }
    }

    static func mappedError(_ error: Error) -> MobileAccountError {
        if let error = error as? ASAuthorizationError, error.code == .canceled {
            return .passkeyCancelled
        }
        return .passkeyUnavailable
    }

    private func finish(
        _ controller: ASAuthorizationController,
        with result: Result<MobilePasskeyAssertion, Error>
    ) {
        guard let pending, pending.controller === controller else {
            return
        }
        self.pending = nil
        pending.continuation.resume(with: result)
    }
}

extension MobilePasskeyAuthenticator: ASAuthorizationControllerDelegate {
    func authorizationController(
        controller: ASAuthorizationController,
        didCompleteWithAuthorization authorization: ASAuthorization
    ) {
        guard
            let credential = authorization.credential
                as? ASAuthorizationPlatformPublicKeyCredentialAssertion
        else {
            finish(controller, with: .failure(MobileAccountError.passkeyUnavailable))
            return
        }
        finish(
            controller,
            with: .success(
                MobilePasskeyAssertion(
                    credentialID: credential.credentialID,
                    clientDataJSON: credential.rawClientDataJSON,
                    authenticatorData: credential.rawAuthenticatorData,
                    signature: credential.signature,
                    userHandle: credential.userID
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

extension MobilePasskeyAuthenticator: ASAuthorizationControllerPresentationContextProviding {
    func presentationAnchor(
        for controller: ASAuthorizationController
    ) -> ASPresentationAnchor {
        guard let pending, pending.controller === controller else {
            return ASPresentationAnchor()
        }
        return pending.anchor
    }
}
