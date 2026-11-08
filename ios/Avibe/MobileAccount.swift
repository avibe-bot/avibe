import AuthenticationServices
import Foundation
import OSLog
import WebKit

struct MobileAccount: Codable, Equatable {
    let id: String
    let email: String
}

struct MobileInstance: Codable, Equatable {
    let id: String
    let name: String
    let host: String
    let url: URL
    let relationship: String
    let ownerEmail: String?
    let available: Bool

    enum CodingKeys: String, CodingKey {
        case id
        case name
        case host
        case url
        case relationship
        case ownerEmail = "owner_email"
        case available
    }
}

struct MobileSessionSnapshot: Codable, Equatable {
    let account: MobileAccount
    let instances: [MobileInstance]
}

enum MobileSecondFactorGate: String, Equatable {
    /// The account has a method; this session must pass it.
    case challenge
    /// Policy requires a method and the account has none yet.
    case enroll
}

enum MobileSecondFactorMethod: String, Equatable {
    case passkey
    case totp
}

/// What `GET /api/mobile/session` answers with `403 two_factor_required`:
/// the email code passed, and this session still owes the second factor.
struct MobileSecondFactorRequirement: Equatable {
    let email: String?
    let gate: MobileSecondFactorGate
    let methods: [MobileSecondFactorMethod]

    func withEmail(fallback: String?) -> MobileSecondFactorRequirement {
        MobileSecondFactorRequirement(
            email: email ?? fallback,
            gate: gate,
            methods: methods
        )
    }
}

enum MobileSessionDiscovery: Equatable {
    case signedIn(MobileSessionSnapshot)
    case secondFactorRequired(MobileSecondFactorRequirement)
}

/// Why Supabase still accepted a password that no longer meets avibe.bot's
/// rules (`weak_password.reasons`).
enum MobileWeakPasswordReason: String, Equatable {
    case length
    case characters
    case pwned
}

/// What a password sign-in found. `weakPasswordReasons` is `nil` unless
/// Supabase reported `weak_password`.
struct MobilePasswordSignInDiscovery: Equatable {
    let discovery: MobileSessionDiscovery
    let weakPasswordReasons: [MobileWeakPasswordReason]?
}

/// `auth.signInWithPassword`'s answer: the session state of `auth.verifyOtp`,
/// plus `weak_password` when Supabase reported one.
struct MobilePasswordSignInResult: Equatable {
    let state: MobileSessionState
    let weakPasswordReasons: [MobileWeakPasswordReason]?

    var jsonObject: [String: Any] {
        var object = state.jsonObject
        if let weakPasswordReasons {
            object["weak_password"] = [
                "reasons": weakPasswordReasons.map(\.rawValue),
            ]
        }
        return object
    }
}

enum MobileSecondFactorCodeMethod: String, Equatable {
    case totp
    case recovery
}

/// The body of `POST /api/two-factor/verify`. Codes come from the bundled
/// page; the passkey token and assertion stay between the transport and
/// native code.
enum MobileSecondFactorProof {
    case code(MobileSecondFactorCodeMethod, String)
    case passkey(token: String, response: [String: Any])

    var jsonObject: [String: Any] {
        switch self {
        case .code(let method, let code):
            return ["method": method.rawValue, "code": code]
        case .passkey(let token, let response):
            return ["method": "passkey", "token": token, "response": response]
        }
    }
}

struct MobileRuntimeContext: Equatable {
    let account: MobileAccount
    let instance: MobileInstance
    fileprivate let generation: Int
    fileprivate let websiteDataStoreIdentifier: UUID
}

enum MobileSessionState: Equatable {
    case signedOut
    case signedIn(
        snapshot: MobileSessionSnapshot,
        selectedInstanceID: String?
    )
    case twoFactorRequired(MobileSecondFactorRequirement)

    var jsonObject: [String: Any] {
        switch self {
        case .signedOut:
            return ["status": "signedOut"]
        case .twoFactorRequired(let requirement):
            return [
                "status": "twoFactorRequired",
                "email": requirement.email ?? NSNull(),
                "gate": requirement.gate.rawValue,
                "methods": requirement.methods.map(\.rawValue),
            ]
        case .signedIn(let snapshot, let selectedInstanceID):
            let account = [
                "id": snapshot.account.id,
                "email": snapshot.account.email,
            ]
            let instances = snapshot.instances.map { instance in
                [
                    "id": instance.id,
                    "name": instance.name,
                    "host": instance.host,
                    "url": instance.url.absoluteString,
                    "relationship": instance.relationship,
                    "owner_email": instance.ownerEmail ?? NSNull(),
                    "available": instance.available,
                ] as [String: Any]
            }
            return [
                "status": "signedIn",
                "account": account,
                "instances": instances,
                "selected_instance_id": selectedInstanceID ?? NSNull(),
            ]
        }
    }
}

struct MobileAccountError: Error, Equatable {
    let code: String
    let message: String
    let retryAfterSeconds: Int?

    init(
        code: String,
        message: String,
        retryAfterSeconds: Int? = nil
    ) {
        self.code = code
        self.message = message
        self.retryAfterSeconds = retryAfterSeconds
    }

    var jsonObject: [String: Any] {
        var object: [String: Any] = [
            "code": code,
            "message": message,
        ]
        if let retryAfterSeconds {
            object["retry_after_seconds"] = retryAfterSeconds
        }
        return object
    }

    static let busy = MobileAccountError(
        code: "busy",
        message: "Another account operation is already running."
    )
    static let forbidden = MobileAccountError(
        code: "forbidden",
        message: "This account operation is not available in the current document."
    )
    static let invalidRequest = MobileAccountError(
        code: "invalid_request",
        message: "The mobile account request is malformed."
    )
    static let network = MobileAccountError(
        code: "network",
        message: "The control plane could not be reached."
    )
    static let signedOut = MobileAccountError(
        code: "signed_out",
        message: "The account session is no longer active."
    )
    static let secondFactorInvalid = MobileAccountError(
        code: "invalid",
        message: "The second factor was not accepted."
    )
    /// A wrong email or password, or an account that never set a password.
    static let invalidCredentials = MobileAccountError(
        code: "invalid_credentials",
        message: "The email or password was not accepted."
    )
    static let emailNotConfirmed = MobileAccountError(
        code: "email_not_confirmed",
        message: "The account's email address is not confirmed."
    )
    static let passkeyCancelled = MobileAccountError(
        code: "passkey_cancelled",
        message: "The passkey request was cancelled."
    )
    static let passkeyUnavailable = MobileAccountError(
        code: "passkey_unavailable",
        message: "No passkey for this account is available on this device."
    )
    static let runtimeUnavailable = MobileAccountError(
        code: "runtime_unavailable",
        message: "No instance is open behind the account page."
    )
}

struct MobileControlPlaneConfiguration: Equatable {
    static let productionURL = URL(string: "https://avibe.bot")!
    let baseURL: URL

    init(
        arguments: [String] = ProcessInfo.processInfo.arguments
    ) {
        #if DEBUG
        if
            let index = arguments.firstIndex(of: "-AvibeControlPlaneURL"),
            arguments.indices.contains(index + 1),
            let override = URL(string: arguments[index + 1]),
            Self.isAllowedDebugURL(override)
        {
            baseURL = override
            return
        }
        #endif
        baseURL = Self.productionURL
    }

    init(testBaseURL: URL) {
        baseURL = testBaseURL
    }

    static func isAllowedDebugURL(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased() else {
            return false
        }
        if scheme == "https" {
            return url.host?.isEmpty == false
        }
        #if DEBUG
        return scheme == "http"
            && ["127.0.0.1", "localhost", "::1"].contains(url.host?.lowercased() ?? "")
        #else
        return false
        #endif
    }
}

struct AccountWebsiteDataStoreOperations {
    let makeDataStore: @MainActor (UUID) -> WKWebsiteDataStore
    let removeIdentifier: @MainActor (UUID) async -> Error?

    static let live = AccountWebsiteDataStoreOperations(
        makeDataStore: { WKWebsiteDataStore(forIdentifier: $0) },
        removeIdentifier: { identifier in
            await withCheckedContinuation { continuation in
                WKWebsiteDataStore.remove(forIdentifier: identifier) {
                    continuation.resume(returning: $0)
                }
            }
        }
    )
}

@MainActor
final class WebsiteDataStoreReleaseBarrier {
    private final class WeakWebViewReference {
        weak var webView: WKWebView?

        init(_ webView: WKWebView) {
            self.webView = webView
        }
    }

    private let references: [WeakWebViewReference]

    init(webViews: [WKWebView]) {
        references = webViews.map(WeakWebViewReference.init)
    }

    var isReleased: Bool {
        references.allSatisfy { $0.webView == nil }
    }

    func waitForRelease() async -> Bool {
        await Self.waitForRelease(of: [self]).isEmpty
    }

    static func waitForRelease(
        of barriers: [WebsiteDataStoreReleaseBarrier]
    ) async -> [WebsiteDataStoreReleaseBarrier] {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(1))
        var unreleased = barriers.filter { !$0.isReleased }
        while !unreleased.isEmpty {
            guard clock.now < deadline else {
                return unreleased
            }
            try? await Task.sleep(for: .milliseconds(10))
            unreleased = barriers.filter { !$0.isReleased }
        }
        return []
    }
}

@MainActor
final class AccountWebsiteDataStore {
    private static let identifierKey = "avibe.mobile.websiteDataStoreIdentifier"
    private static let ownedIdentifiersKey =
        "avibe.mobile.ownedWebsiteDataStoreIdentifiers"
    private static let logger = Logger(
        subsystem: Bundle.main.bundleIdentifier ?? "com.avibe",
        category: "MobileAccount"
    )

    private let defaults: UserDefaults
    private let operations: AccountWebsiteDataStoreOperations
    private(set) var identifier: UUID
    private(set) var dataStore: WKWebsiteDataStore
    private var ownedIdentifiers: Set<UUID>

    init(
        defaults: UserDefaults = .standard,
        operations: AccountWebsiteDataStoreOperations = .live
    ) {
        self.defaults = defaults
        self.operations = operations
        var persistedIdentifiers = Set(
            (defaults.stringArray(forKey: Self.ownedIdentifiersKey) ?? [])
                .compactMap(UUID.init(uuidString:))
        )
        let resolvedIdentifier: UUID
        if
            let rawIdentifier = defaults.string(forKey: Self.identifierKey),
            let savedIdentifier = UUID(uuidString: rawIdentifier)
        {
            resolvedIdentifier = savedIdentifier
        } else {
            resolvedIdentifier = UUID()
        }
        persistedIdentifiers.insert(resolvedIdentifier)
        identifier = resolvedIdentifier
        ownedIdentifiers = persistedIdentifiers
        defaults.set(resolvedIdentifier.uuidString, forKey: Self.identifierKey)
        defaults.set(
            persistedIdentifiers.map(\.uuidString).sorted(),
            forKey: Self.ownedIdentifiersKey
        )
        dataStore = operations.makeDataStore(resolvedIdentifier)
    }

    func rotate(sweepOwnedStores: Bool = true) async {
        let newIdentifier = UUID()
        ownedIdentifiers.insert(newIdentifier)
        identifier = newIdentifier
        defaults.set(newIdentifier.uuidString, forKey: Self.identifierKey)
        persistOwnedIdentifiers()
        dataStore = operations.makeDataStore(newIdentifier)

        await Task.yield()
        if sweepOwnedStores {
            await sweepOrphans()
        }
    }

    func sweepOrphans() async {
        let candidates = ownedIdentifiers.subtracting([identifier])

        for candidate in candidates.sorted(by: {
            $0.uuidString < $1.uuidString
        }) {
            if let error = await operations.removeIdentifier(candidate) {
                Self.logger.error(
                    "Named WebKit store cleanup failed for \(candidate.uuidString, privacy: .public): \(String(describing: error), privacy: .public)"
                )
            } else {
                ownedIdentifiers.remove(candidate)
            }
        }

        persistOwnedIdentifiers()
    }

    private func persistOwnedIdentifiers() {
        defaults.set(
            ownedIdentifiers
                .map(\.uuidString)
                .sorted(),
            forKey: Self.ownedIdentifiersKey
        )
    }
}

@MainActor
protocol MobileAuthTransporting: AnyObject {
    func requestOTP(email: String) async throws -> Int
    func verifyOTP(email: String, code: String) async throws -> MobileSessionDiscovery
    /// Supabase's password grant, then the same callback and session read as
    /// `verifyOTP`. The password is sent only to the grant; it is never
    /// returned, logged, or kept. Throws `signedOut` for the session's `401`.
    func signInWithPassword(
        email: String,
        password: String
    ) async throws -> MobilePasswordSignInDiscovery
    /// `nil` means signed out (`401`).
    func fetchSession() async throws -> MobileSessionDiscovery?
    /// Throws `signedOut` for `401` and `passkeyUnavailable` when the account
    /// has no passkey.
    func passkeyChallenge() async throws -> MobilePasskeyChallenge
    /// Passes the second factor, then reads the session again. Throws
    /// `signedOut` for `401`.
    func verifySecondFactor(
        _ proof: MobileSecondFactorProof
    ) async throws -> MobileSessionDiscovery
    func logout() async throws
    func cancelAll() async
    func invalidate() -> WebsiteDataStoreReleaseBarrier?
}

private struct StoredMobileSelection: Codable, Equatable {
    let accountID: String
    let instanceID: String
    let url: URL
}

@MainActor
final class MobileSessionCoordinator {
    typealias TransportFactory = @MainActor (
        URL,
        WKWebsiteDataStore
    ) -> MobileAuthTransporting

    private static let selectionKey = "avibe.mobile.selection"

    private let defaults: UserDefaults
    private let controlPlane: MobileControlPlaneConfiguration
    private let websiteDataStore: AccountWebsiteDataStore
    private let transportFactory: TransportFactory
    private let passkeyAuthenticator: MobilePasskeyAuthenticating
    private let onOpenRuntime: (MobileRuntimeContext) -> Void
    private let onPrepareAccountTransition: () -> [
        WebsiteDataStoreReleaseBarrier
    ]

    private var transport: MobileAuthTransporting?
    private var snapshot: MobileSessionSnapshot?
    /// The email typed for the sign-in that now owes a second factor, shown
    /// when the backend's answer does not name the account.
    private var secondFactorEmail: String?
    private var generation = 0
    private var phase = Phase.ready
    private var signedOutStoreIsFresh = false
    private var pendingStoreReleaseBarriers: [
        WebsiteDataStoreReleaseBarrier
    ] = []

    private enum Phase: Equatable {
        case ready
        case operation(UUID)
        case resetting(UUID)
    }

    var isAccountSurfaceActive: Bool {
        transport != nil
    }

    init(
        defaults: UserDefaults = .standard,
        controlPlane: MobileControlPlaneConfiguration = MobileControlPlaneConfiguration(),
        websiteDataStore: AccountWebsiteDataStore,
        transportFactory: @escaping TransportFactory = {
            MobileAuthTransport(controlPlaneURL: $0, websiteDataStore: $1)
        },
        passkeyAuthenticator: MobilePasskeyAuthenticating? = nil,
        onOpenRuntime: @escaping (MobileRuntimeContext) -> Void,
        onPrepareAccountTransition: @escaping () -> [
            WebsiteDataStoreReleaseBarrier
        ]
    ) {
        self.defaults = defaults
        self.controlPlane = controlPlane
        self.websiteDataStore = websiteDataStore
        self.transportFactory = transportFactory
        self.passkeyAuthenticator = passkeyAuthenticator
            ?? MobilePasskeyAuthenticator()
        self.onOpenRuntime = onOpenRuntime
        self.onPrepareAccountTransition = onPrepareAccountTransition
        transport = nil
    }

    func activateAccountSurface() {
        guard transport == nil else {
            return
        }
        transport = transportFactory(controlPlane.baseURL, websiteDataStore.dataStore)
    }

    func deactivateAccountSurface() {
        recordReleaseBarrier(transport?.invalidate())
        transport = nil
    }

    func bootstrap() async throws -> MobileSessionState {
        if let snapshot {
            guard phase == .ready else {
                throw MobileAccountError.busy
            }
            return state(for: snapshot)
        }
        if signedOutStoreIsFresh {
            return .signedOut
        }
        return try await runExclusive {
            let requestGeneration = self.generation
            let discovery = try await self.requireTransport().fetchSession()
            try self.requireCurrentGeneration(requestGeneration)
            guard let discovery else {
                self.beginAccountTransition(clearSelection: true)
                await self.replaceWithFreshSignedOutStore()
                return .signedOut
            }
            if self.belongsToAnotherAccount(discovery) {
                self.beginAccountTransition(clearSelection: true)
                await self.replaceWithFreshSignedOutStore()
                return .signedOut
            }
            self.signedOutStoreIsFresh = false
            return self.state(for: discovery)
        }
    }

    func requestOTP(email: String) async throws -> Int {
        let normalized = email.trimmingCharacters(in: .whitespacesAndNewlines)
        guard Self.isValidEmail(normalized) else {
            throw MobileAccountError(
                code: "invalid_email",
                message: "A valid email address is required."
            )
        }
        return try await runExclusive {
            let requestGeneration = self.beginAccountTransition(
                clearSelection: true
            )
            if !self.signedOutStoreIsFresh {
                await self.replaceWithFreshSignedOutStore()
            }
            let cooldown = try await self.requireTransport().requestOTP(email: normalized)
            try self.requireCurrentGeneration(requestGeneration)
            return cooldown
        }
    }

    func verifyOTP(email: String, code: String) async throws -> MobileSessionState {
        let normalizedEmail = email.trimmingCharacters(in: .whitespacesAndNewlines)
        guard Self.isValidEmail(normalizedEmail), code.range(of: #"^\d{6}$"#, options: .regularExpression) != nil else {
            throw MobileAccountError.invalidRequest
        }
        return try await runExclusive {
            let loginGeneration = self.beginAccountTransition(
                clearSelection: true
            )
            do {
                let discovery = try await self.requireTransport().verifyOTP(
                    email: normalizedEmail,
                    code: code
                )
                try self.requireCurrentGeneration(loginGeneration)
                self.clearStoredSelection()
                self.signedOutStoreIsFresh = false
                self.secondFactorEmail = normalizedEmail
                return self.state(for: discovery)
            } catch {
                if self.generation == loginGeneration {
                    await self.replaceWithFreshSignedOutStore()
                }
                throw error
            }
        }
    }

    /// Signs in with the account's avibe.bot password. The answer is the same
    /// as `verifyOTP`'s, so it may owe the second factor. Every failed attempt
    /// rotates to a fresh store; the session's `401` is the explicit
    /// signed-out answer.
    func signInWithPassword(
        email: String,
        password: String
    ) async throws -> MobilePasswordSignInResult {
        let normalizedEmail = email.trimmingCharacters(in: .whitespacesAndNewlines)
        guard Self.isValidEmail(normalizedEmail), !password.isEmpty else {
            throw MobileAccountError.invalidRequest
        }
        return try await runExclusive {
            let loginGeneration = self.beginAccountTransition(
                clearSelection: true
            )
            do {
                let result = try await self.requireTransport().signInWithPassword(
                    email: normalizedEmail,
                    password: password
                )
                try self.requireCurrentGeneration(loginGeneration)
                self.clearStoredSelection()
                self.signedOutStoreIsFresh = false
                self.secondFactorEmail = normalizedEmail
                return MobilePasswordSignInResult(
                    state: self.state(for: result.discovery),
                    weakPasswordReasons: result.weakPasswordReasons
                )
            } catch {
                guard self.generation == loginGeneration else {
                    throw error
                }
                await self.replaceWithFreshSignedOutStore()
                if (error as? MobileAccountError)?.code
                    == MobileAccountError.signedOut.code
                {
                    return MobilePasswordSignInResult(
                        state: .signedOut,
                        weakPasswordReasons: nil
                    )
                }
                throw error
            }
        }
    }

    func refresh() async throws -> MobileSessionState {
        return try await runExclusive {
            let requestGeneration = self.beginAccountTransition(
                clearSelection: false
            )
            let discovery = try await self.requireTransport().fetchSession()
            try self.requireCurrentGeneration(requestGeneration)
            guard let discovery else {
                self.clearStoredSelection()
                await self.replaceWithFreshSignedOutStore()
                return .signedOut
            }
            if self.belongsToAnotherAccount(discovery) {
                self.clearStoredSelection()
                await self.replaceWithFreshSignedOutStore()
                return .signedOut
            }
            self.signedOutStoreIsFresh = false
            return self.state(for: discovery)
        }
    }

    /// Passes an authenticator or recovery code for the signed-in session that
    /// owes the second factor. A rejected code keeps the store: it is the same
    /// first-factor session either way.
    func verifySecondFactor(
        method: MobileSecondFactorCodeMethod,
        code: String
    ) async throws -> MobileSessionState {
        let normalized = code.trimmingCharacters(in: .whitespacesAndNewlines)
        switch method {
        case .totp:
            guard normalized.range(of: #"^\d{6}$"#, options: .regularExpression) != nil else {
                throw MobileAccountError.invalidRequest
            }
        case .recovery:
            guard !normalized.isEmpty, normalized.count <= 32 else {
                throw MobileAccountError.secondFactorInvalid
            }
        }
        return try await runExclusive {
            let requestGeneration = self.generation
            return try await self.completeSecondFactor(
                generation: requestGeneration
            ) {
                try await self.requireTransport().verifySecondFactor(
                    .code(method, normalized)
                )
            }
        }
    }

    /// Confirms with one of the account's passkeys: options from avibe.bot,
    /// the system passkey sheet, then the assertion back to avibe.bot. Neither
    /// side of the exchange reaches the bundled page.
    func verifyPasskey(
        presentationAnchor: ASPresentationAnchor
    ) async throws -> MobileSessionState {
        try await runExclusive {
            let requestGeneration = self.generation
            return try await self.completeSecondFactor(
                generation: requestGeneration
            ) {
                let challenge = try await self.requireTransport().passkeyChallenge()
                try self.requireCurrentGeneration(requestGeneration)
                let assertion = try await self.passkeyAuthenticator.assert(
                    challenge.request,
                    presentationAnchor: presentationAnchor
                )
                try self.requireCurrentGeneration(requestGeneration)
                return try await self.requireTransport().verifySecondFactor(
                    .passkey(
                        token: challenge.token,
                        response: assertion.authenticationResponseJSON
                    )
                )
            }
        }
    }

    func selectInstance(id: String) throws -> MobileInstance {
        guard phase == .ready else {
            throw MobileAccountError.busy
        }
        guard
            let snapshot,
            let instance = snapshot.instances.first(where: { $0.id == id })
        else {
            throw MobileAccountError(
                code: "unauthorized_instance",
                message: "The selected instance is not in the authorized account list."
            )
        }
        guard instance.available else {
            throw MobileAccountError(
                code: "instance_offline",
                message: "The selected instance is currently unavailable."
            )
        }
        guard WebOrigin(url: instance.url) != nil else {
            throw MobileAccountError(
                code: "invalid_instance",
                message: "The selected instance does not have a valid HTTPS URL."
            )
        }
        storeSelection(
            StoredMobileSelection(
                accountID: snapshot.account.id,
                instanceID: instance.id,
                url: instance.url
            )
        )
        deactivateAccountSurface()
        onOpenRuntime(
            MobileRuntimeContext(
                account: snapshot.account,
                instance: instance,
                generation: generation,
                websiteDataStoreIdentifier: websiteDataStore.identifier
            )
        )
        return instance
    }

    func logout() async throws -> MobileSessionState {
        let resetToken = try beginReset()
        defer { finishReset(resetToken) }
        beginAccountTransition(clearSelection: true)
        secondFactorEmail = nil

        if let oldTransport = transport {
            await oldTransport.cancelAll()
            try? await oldTransport.logout()
        }
        deactivateAccountSurface()

        await rotateWebsiteDataStore()
        signedOutStoreIsFresh = true
        activateAccountSurface()
        return .signedOut
    }

    private func completeSecondFactor(
        generation requestGeneration: Int,
        _ operation: () async throws -> MobileSessionDiscovery
    ) async throws -> MobileSessionState {
        do {
            let discovery = try await operation()
            try requireCurrentGeneration(requestGeneration)
            signedOutStoreIsFresh = false
            return state(for: discovery)
        } catch let error as MobileAccountError
            where error.code == MobileAccountError.signedOut.code
        {
            // The first-factor session ended while it owed the second factor:
            // an explicit signed-out answer, like a session read's `401`.
            try requireCurrentGeneration(requestGeneration)
            beginAccountTransition(clearSelection: true)
            await replaceWithFreshSignedOutStore()
            return .signedOut
        }
    }

    private func belongsToAnotherAccount(
        _ discovery: MobileSessionDiscovery
    ) -> Bool {
        guard
            case .signedIn(let fresh) = discovery,
            let selection = storedSelection()
        else {
            return false
        }
        return selection.accountID != fresh.account.id
    }

    private func state(for discovery: MobileSessionDiscovery) -> MobileSessionState {
        switch discovery {
        case .signedIn(let fresh):
            secondFactorEmail = nil
            snapshot = fresh
            return state(for: fresh)
        case .secondFactorRequired(let requirement):
            snapshot = nil
            return .twoFactorRequired(
                requirement.withEmail(fallback: secondFactorEmail)
            )
        }
    }

    private func state(for snapshot: MobileSessionSnapshot) -> MobileSessionState {
        guard
            let selection = storedSelection(),
            selection.accountID == snapshot.account.id,
            let selected = snapshot.instances.first(where: {
                $0.id == selection.instanceID && $0.url == selection.url
            })
        else {
            clearStoredSelection()
            return .signedIn(snapshot: snapshot, selectedInstanceID: nil)
        }
        return .signedIn(snapshot: snapshot, selectedInstanceID: selected.id)
    }

    func restorableRuntime(
        from state: MobileSessionState
    ) -> MobileRuntimeContext? {
        guard
            phase == .ready,
            case .signedIn(let snapshot, let selectedInstanceID) = state,
            self.snapshot == snapshot,
            let selectedInstanceID,
            let selected = snapshot.instances.first(where: {
                $0.id == selectedInstanceID
                    && $0.available
                    && WebOrigin(url: $0.url) != nil
            })
        else {
            return nil
        }
        let runtime = MobileRuntimeContext(
            account: snapshot.account,
            instance: selected,
            generation: generation,
            websiteDataStoreIdentifier: websiteDataStore.identifier
        )
        return isCurrentRuntime(runtime) ? runtime : nil
    }

    func isCurrentRuntime(_ runtime: MobileRuntimeContext) -> Bool {
        guard
            phase == .ready,
            runtime.generation == generation,
            runtime.websiteDataStoreIdentifier == websiteDataStore.identifier,
            let snapshot,
            snapshot.instances.contains(runtime.instance),
            let selection = storedSelection(),
            selection.accountID == snapshot.account.id,
            selection.instanceID == runtime.instance.id,
            selection.url == runtime.instance.url
        else {
            return false
        }
        return true
    }

    private func runExclusive<T>(
        _ operation: () async throws -> T
    ) async throws -> T {
        guard phase == .ready else {
            throw MobileAccountError.busy
        }
        let token = UUID()
        phase = .operation(token)
        defer {
            if phase == .operation(token) {
                phase = .ready
            }
        }
        return try await operation()
    }

    @discardableResult
    private func beginAccountTransition(
        clearSelection: Bool
    ) -> Int {
        generation += 1
        snapshot = nil
        if clearSelection {
            clearStoredSelection()
        }
        pendingStoreReleaseBarriers.append(
            contentsOf: onPrepareAccountTransition()
        )
        // An account page over a live runtime starts no transport; the
        // runtime is released by now, so the transition's requests can run.
        activateAccountSurface()
        return generation
    }

    private func beginReset() throws -> UUID {
        if case .resetting = phase {
            throw MobileAccountError.busy
        }
        let token = UUID()
        phase = .resetting(token)
        return token
    }

    private func finishReset(_ token: UUID) {
        if phase == .resetting(token) {
            phase = .ready
        }
    }

    private func requireCurrentGeneration(_ expected: Int) throws {
        guard generation == expected else {
            throw MobileAccountError(
                code: "stale_session",
                message: "The result belongs to an older account session."
            )
        }
    }

    private func requireTransport() throws -> MobileAuthTransporting {
        guard let transport else {
            throw MobileAccountError(
                code: "inactive",
                message: "The account surface is not active."
            )
        }
        return transport
    }

    private func storedSelection() -> StoredMobileSelection? {
        guard let data = defaults.data(forKey: Self.selectionKey) else {
            return nil
        }
        return try? JSONDecoder().decode(StoredMobileSelection.self, from: data)
    }

    private func storeSelection(_ selection: StoredMobileSelection) {
        guard let data = try? JSONEncoder().encode(selection) else {
            return
        }
        defaults.set(data, forKey: Self.selectionKey)
    }

    private func clearStoredSelection() {
        defaults.removeObject(forKey: Self.selectionKey)
    }

    private func replaceWithFreshSignedOutStore() async {
        deactivateAccountSurface()
        snapshot = nil
        secondFactorEmail = nil
        clearStoredSelection()
        await rotateWebsiteDataStore()
        signedOutStoreIsFresh = true
        activateAccountSurface()
    }

    private func recordReleaseBarrier(
        _ barrier: WebsiteDataStoreReleaseBarrier?
    ) {
        guard let barrier, !barrier.isReleased else {
            return
        }
        pendingStoreReleaseBarriers.append(barrier)
    }

    private func rotateWebsiteDataStore() async {
        let unreleased = await WebsiteDataStoreReleaseBarrier.waitForRelease(
            of: pendingStoreReleaseBarriers
        )
        pendingStoreReleaseBarriers = unreleased
        await websiteDataStore.rotate(
            sweepOwnedStores: unreleased.isEmpty
        )
    }

    private static func isValidEmail(_ email: String) -> Bool {
        email.range(
            of: #"^[^\s@]+@[^\s@]+\.[^\s@]+$"#,
            options: .regularExpression
        ) != nil
    }
}

enum MobileExternalURLPolicy {
    private static let allowedURLs: Set<String> = [
        "https://github.com/avibe-bot/avibe/blob/master/docs/INSTALL_FOR_AI.md",
        "https://github.com/avibe-bot/avibe/blob/master/docs/INSTALL_FOR_AI_ZH.md",
        // Where an account that must turn on two-step verification sets it up.
        "https://avibe.bot/app/account/security",
    ]

    static func allows(_ url: URL) -> Bool {
        allowedURLs.contains(url.absoluteString)
    }
}
