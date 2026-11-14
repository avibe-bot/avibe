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
    /// The named stores WebKit has on disk.
    let listIdentifiers: @MainActor () async -> Set<UUID>

    static let live = AccountWebsiteDataStoreOperations(
        makeDataStore: { WKWebsiteDataStore(forIdentifier: $0) },
        removeIdentifier: { identifier in
            await withCheckedContinuation { continuation in
                WKWebsiteDataStore.remove(forIdentifier: identifier) {
                    continuation.resume(returning: $0)
                }
            }
        },
        listIdentifiers: {
            await withCheckedContinuation { continuation in
                WKWebsiteDataStore.fetchAllDataStoreIdentifiers {
                    continuation.resume(returning: Set($0))
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
    private(set) var ownedIdentifiers: Set<UUID>
    /// Runs first in `rotate()`, while the store that is going away is still
    /// `dataStore`: an account transition. The direct path controller closes
    /// every relay here. It writes no route: the old store keeps its route
    /// and is discarded with it, and the new store starts with none.
    var onWillRotate: (@MainActor () -> Void)?

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
        onWillRotate?()
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

    /// Removes every store this ledger owns except the current one. A store
    /// leaves the ledger only once WebKit no longer lists it, not when its
    /// removal reports success: a store whose network session is still
    /// closing (one released at sign-out) gets its directory back right after
    /// a removal WebKit reported as done. So the sweep that removes a store
    /// keeps it, and a later sweep (the next launch or account change) removes
    /// it again if it came back, or forgets it once it is gone.
    func sweepOrphans() async {
        let candidates = ownedIdentifiers.subtracting([identifier])
        guard !candidates.isEmpty else {
            return
        }
        let listed = await operations.listIdentifiers()

        for candidate in candidates.sorted(by: {
            $0.uuidString < $1.uuidString
        }) {
            guard listed.contains(candidate) else {
                ownedIdentifiers.remove(candidate)
                continue
            }
            if let error = await operations.removeIdentifier(candidate) {
                Self.logger.error(
                    "Named WebKit store cleanup failed for \(candidate.uuidString, privacy: .public): \(String(describing: error), privacy: .public)"
                )
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
    func pushManagementRequest(
        _ request: PushManagementRequest
    ) async throws -> PushManagementResponse
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
    /// When each instance was last opened as the selected runtime, per
    /// account: `[accountID: [instanceID: seconds since 1970]]`.
    private static let instanceUseKey = "avibe.mobile.instanceUse"

    private let defaults: UserDefaults
    private let controlPlane: MobileControlPlaneConfiguration
    private let websiteDataStore: AccountWebsiteDataStore
    private let transportFactory: TransportFactory
    private let passkeyAuthenticator: MobilePasskeyAuthenticating
    private let onOpenRuntime: (MobileRuntimeContext) -> Void
    private let onPrepareAccountTransition: () -> [
        WebsiteDataStoreReleaseBarrier
    ]
    private let onPreparePushLogout: @MainActor () async -> Void

    private var transport: MobileAuthTransporting?
    private var snapshot: MobileSessionSnapshot?
    /// The email typed for the sign-in that now owes a second factor, shown
    /// when the backend's answer does not name the account.
    private var secondFactorEmail: String?
    private var generation = 0
    private var phase = Phase.ready
    /// The account page that currently owns the hidden account transport.
    /// Reopening creates a new lease; an old operation may not release it.
    private var accountSurfaceLeaseID: UUID?
    /// Operation that is allowed to preserve the selected runtime while the
    /// account page refreshes or performs a push-management request.
    private var selectionKeepingOperation: UUID?
    /// The operation that currently retains the transport after its surface
    /// closes. This token is separate from `selectionKeepingOperation`:
    /// selection and transport ownership have different invariants.
    private var transportOperationToken: UUID?
    private var signedOutStoreIsFresh = false
    /// A replacement page must read the known result even while push/store
    /// cleanup suspends. The publication ID prevents an older continuation
    /// from clearing a newer transition's result.
    private var authoritativeTransition: (id: UUID, state: MobileSessionState)?
    private var pendingStoreReleaseBarriers: [
        WebsiteDataStoreReleaseBarrier
    ] = []

    private enum Phase: Equatable {
        case ready
        case operation(UUID)
        case resetting(UUID)
        case pushLogout(UUID?)
    }

    var isAccountSurfaceActive: Bool {
        transport != nil
    }

    var currentAccount: MobileAccount? {
        snapshot?.account
    }

    var currentInstances: [MobileInstance] {
        snapshot?.instances ?? []
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
        ],
        onPreparePushLogout: @escaping @MainActor () async -> Void = {}
    ) {
        self.defaults = defaults
        self.controlPlane = controlPlane
        self.websiteDataStore = websiteDataStore
        self.transportFactory = transportFactory
        self.passkeyAuthenticator = passkeyAuthenticator
            ?? MobilePasskeyAuthenticator()
        self.onOpenRuntime = onOpenRuntime
        self.onPrepareAccountTransition = onPrepareAccountTransition
        self.onPreparePushLogout = onPreparePushLogout
        transport = nil
    }

    func activateAccountSurface() {
        if accountSurfaceLeaseID == nil {
            accountSurfaceLeaseID = UUID()
        }
        ensureTransport()
    }

    func deactivateAccountSurface() {
        accountSurfaceLeaseID = nil
        transportOperationToken = nil
        recordReleaseBarrier(transport?.invalidate())
        transport = nil
    }

    /// The account page is closing. An operation that owns the transport may
    /// finish it; a newly opened page gets a new lease and cancels that release.
    func leaveAccountSurface() {
        accountSurfaceLeaseID = nil
        guard transportOperationToken != nil else {
            deactivateAccountSurface()
            return
        }
    }

    private func ensureTransport() {
        guard transport == nil else {
            return
        }
        transport = transportFactory(controlPlane.baseURL, websiteDataStore.dataStore)
    }

    private func releaseTransportPreservingSurfaceLease() {
        // Store replacement belongs to the operation already in flight. Its
        // owner must follow the replacement transport until that operation
        // finishes; only a new reset may revoke that ownership.
        recordReleaseBarrier(transport?.invalidate())
        transport = nil
    }

    func bootstrap() async throws -> MobileSessionState {
        if let authoritativeTransition {
            return authoritativeTransition.state
        }
        if let snapshot {
            guard keepsSelection else {
                throw MobileAccountError.busy
            }
            return state(for: snapshot)
        }
        if signedOutStoreIsFresh {
            return .signedOut
        }
        if case .resetting = phase, snapshot == nil {
            // logout() has already invalidated the old account and is
            // replacing the page. The replacement must not race the reset's
            // transport operation with a busy error.
            return .signedOut
        }
        return try await runExclusive {
            let requestGeneration = self.generation
            let discovery = try await self.requireTransport().fetchSession()
            try self.requireCurrentGeneration(requestGeneration)
            guard let discovery else {
                try await self.preparePushLogout(
                    publishing: .signedOut, generation: requestGeneration
                )
                let transitionGeneration = self.beginAccountTransition(clearSelection: true)
                try await self.replaceWithFreshSignedOutStore(generation: transitionGeneration)
                return .signedOut
            }
            if self.belongsToAnotherAccount(discovery) {
                try await self.preparePushLogout(
                    publishing: .signedOut, generation: requestGeneration
                )
                let transitionGeneration = self.beginAccountTransition(clearSelection: true)
                try await self.replaceWithFreshSignedOutStore(generation: transitionGeneration)
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
                try await self.replaceWithFreshSignedOutStore(generation: requestGeneration)
            }
            try self.requireCurrentGeneration(requestGeneration)
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
                    try await self.replaceWithFreshSignedOutStore(generation: loginGeneration)
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
                try await self.replaceWithFreshSignedOutStore(generation: loginGeneration)
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
        let keepsRuntime = snapshot != nil
        return try await runExclusive(keepingSelection: keepsRuntime) {
            // Refresh is also called by native runtime consumers after the
            // account page has closed. The operation owns this temporary
            // transport; opening a page is not required to fetch the session.
            self.ensureTransport()
            let previousAccountID = self.snapshot?.account.id
            let requestGeneration = self.generation
            let discovery = try await self.requireTransport().fetchSession()
            try self.requireCurrentGeneration(requestGeneration)
            guard let discovery else {
                try await self.preparePushLogout(
                    publishing: .signedOut, generation: requestGeneration
                )
                let transitionGeneration = self.beginAccountTransition(clearSelection: true)
                try await self.replaceWithFreshSignedOutStore(generation: transitionGeneration)
                return .signedOut
            }
            switch discovery {
            case .signedIn(let fresh):
                if
                    let previousAccountID,
                    previousAccountID != fresh.account.id
                {
                    try await self.preparePushLogout(
                        publishing: .signedOut, generation: requestGeneration
                    )
                    let transitionGeneration = self.beginAccountTransition(clearSelection: true)
                    try await self.replaceWithFreshSignedOutStore(generation: transitionGeneration)
                    return .signedOut
                }
                if self.selectedRuntimeOriginIsMissing(in: fresh) {
                    try await self.preparePushLogout(
                        publishing: .signedIn(
                            snapshot: self.orderedByUse(fresh, current: nil),
                            selectedInstanceID: nil
                        ),
                        generation: requestGeneration
                    )
                    self.beginRuntimeInvalidation(with: fresh)
                }
                self.signedOutStoreIsFresh = false
                return self.state(for: fresh)
            case .secondFactorRequired(let requirement):
                try await self.preparePushLogout(
                    publishing: .twoFactorRequired(
                        requirement.withEmail(fallback: self.secondFactorEmail)
                    ),
                    generation: requestGeneration
                )
                if previousAccountID != nil {
                    // A live account lost its authorization. A cold launch
                    // that already owes a second factor has no runtime to
                    // revoke: keep its saved selection until the verified
                    // account can decide whether it still belongs to it.
                    self.beginAccountTransition(clearSelection: true)
                }
                self.signedOutStoreIsFresh = false
                return self.state(for: discovery)
            }
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
        recordInstanceUse(accountID: snapshot.account.id, instanceID: instance.id)
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
        try await preparePushLogout(publishing: .signedOut, generation: generation)
        let resetToken = try beginReset()
        defer { finishReset(resetToken) }
        let resetGeneration = beginAccountTransition(clearSelection: true)
        secondFactorEmail = nil

        if let oldTransport = transport {
            await oldTransport.cancelAll()
            try requireCurrentGeneration(resetGeneration)
            try? await oldTransport.logout()
            try requireCurrentGeneration(resetGeneration)
        }
        releaseTransportPreservingSurfaceLease()

        try await rotateWebsiteDataStore(generation: resetGeneration)
        signedOutStoreIsFresh = true
        ensureTransport()
        return .signedOut
    }

    func pushManagementRequest(
        _ request: PushManagementRequest
    ) async throws -> PushManagementResponse {
        if case .pushLogout = phase {
            let requestGeneration = generation
            let operationToken = transportOperationToken ?? UUID()
            let ownsTransport = transportOperationToken == nil
            ensureTransport()
            if ownsTransport {
                transportOperationToken = operationToken
            }
            defer {
                if ownsTransport {
                    finishTransportOperation(operationToken)
                }
            }
            let response = try await requireTransport().pushManagementRequest(
                request
            )
            try requireCurrentGeneration(requestGeneration)
            return response
        }
        return try await runExclusive(keepingSelection: true) {
            let requestGeneration = self.generation
            self.ensureTransport()
            let response = try await self.requireTransport().pushManagementRequest(
                request
            )
            try self.requireCurrentGeneration(requestGeneration)
            return response
        }
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
            try await preparePushLogout(publishing: .signedOut, generation: requestGeneration)
            let transitionGeneration = beginAccountTransition(clearSelection: true)
            try await replaceWithFreshSignedOutStore(generation: transitionGeneration)
            return .signedOut
        }
    }

    private func preparePushLogout(
        publishing state: MobileSessionState,
        generation requestGeneration: Int
    ) async throws {
        try requireCurrentGeneration(requestGeneration)
        let publicationID = UUID()
        authoritativeTransition = (publicationID, state)
        defer {
            if authoritativeTransition?.id == publicationID {
                authoritativeTransition = nil
            }
        }
        await preparePushLogout()
        // Push cleanup can be superseded by logout and even a new login.
        // Transport ownership alone does not authorize the old state write.
        try requireCurrentGeneration(requestGeneration)
    }

    private func preparePushLogout() async {
        let resumePhase: UUID?
        switch phase {
        case .ready:
            resumePhase = nil
        case .operation(let token):
            resumePhase = token
        case .resetting, .pushLogout:
            return
        }
        let requestGeneration = generation
        phase = .pushLogout(resumePhase)
        await onPreparePushLogout()
        guard generation == requestGeneration, phase == .pushLogout(resumePhase) else {
            return
        }
        if let resumePhase {
            phase = .operation(resumePhase)
        } else {
            phase = .ready
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

    private func beginRuntimeInvalidation(
        with snapshot: MobileSessionSnapshot
    ) {
        generation += 1
        clearStoredSelection()
        self.snapshot = snapshot
        pendingStoreReleaseBarriers.append(
            contentsOf: onPrepareAccountTransition()
        )
        ensureTransport()
    }

    private func selectedRuntimeOriginIsMissing(
        in fresh: MobileSessionSnapshot
    ) -> Bool {
        guard
            let previous = snapshot,
            previous.account.id == fresh.account.id,
            let selection = storedSelection(),
            selection.accountID == fresh.account.id
        else {
            return false
        }
        return !fresh.instances.contains {
            $0.id == selection.instanceID && $0.url == selection.url
        }
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
            let ordered = orderedByUse(snapshot, current: nil)
            self.snapshot = ordered
            return .signedIn(snapshot: ordered, selectedInstanceID: nil)
        }
        let ordered = orderedByUse(snapshot, current: selected.id)
        self.snapshot = ordered
        return .signedIn(snapshot: ordered, selectedInstanceID: selected.id)
    }

    /// The account's instances as the instance list shows them: the current
    /// one first, then the ones opened on this device, most recent first,
    /// then the rest in the order avibe.bot sent them (pinned, then newest).
    private func orderedByUse(
        _ snapshot: MobileSessionSnapshot,
        current: String?
    ) -> MobileSessionSnapshot {
        let uses = instanceUses(accountID: snapshot.account.id)
        let serverOrder = Dictionary(
            uniqueKeysWithValues: snapshot.instances.enumerated().map {
                ($0.element.id, $0.offset)
            }
        )
        let instances = snapshot.instances.sorted { left, right in
            if (left.id == current) != (right.id == current) {
                return left.id == current
            }
            let leftUse = uses[left.id]
            let rightUse = uses[right.id]
            switch (leftUse, rightUse) {
            case let (leftUse?, rightUse?) where leftUse != rightUse:
                return leftUse > rightUse
            case (_?, nil):
                return true
            case (nil, _?):
                return false
            default:
                return serverOrder[left.id, default: 0] < serverOrder[right.id, default: 0]
            }
        }
        return MobileSessionSnapshot(account: snapshot.account, instances: instances)
    }

    private func instanceUses(accountID: String) -> [String: Double] {
        let all = defaults.dictionary(forKey: Self.instanceUseKey) as? [String: [String: Double]]
        return all?[accountID] ?? [:]
    }

    private func recordInstanceUse(accountID: String, instanceID: String) {
        var all = defaults.dictionary(forKey: Self.instanceUseKey) as? [String: [String: Double]] ?? [:]
        all[accountID, default: [:]][instanceID] = Date().timeIntervalSince1970
        defaults.set(all, forKey: Self.instanceUseKey)
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
        phase == .ready && isSelectedRuntime(runtime)
    }

    /// Whether `runtime` is still this account's selected instance, whatever
    /// ordinary exclusive operation runs meanwhile (a push-management request
    /// keeps the account, the store, and the selection as they are). Once a
    /// sign-out or an account change has begun (push logout, a reset), it is
    /// not: the account is on its way out.
    func isSelectedRuntime(_ runtime: MobileRuntimeContext) -> Bool {
        switch phase {
        case .ready, .operation:
            break
        case .resetting, .pushLogout:
            return false
        }
        guard
            runtime.generation == generation,
            runtime.websiteDataStoreIdentifier == websiteDataStore.identifier,
            let snapshot,
            snapshot.instances.contains(where: {
                $0.id == runtime.instance.id && $0.url == runtime.instance.url
            }),
            let selection = storedSelection(),
            selection.accountID == snapshot.account.id,
            selection.instanceID == runtime.instance.id,
            selection.url == runtime.instance.url
        else {
            return false
        }
        return true
    }

    private func finishTransportOperation(_ token: UUID) {
        guard transportOperationToken == token else {
            return
        }
        transportOperationToken = nil
        if accountSurfaceLeaseID == nil {
            deactivateAccountSurface()
        }
    }

    private func runExclusive<T>(
        keepingSelection: Bool = false,
        _ operation: () async throws -> T
    ) async throws -> T {
        guard phase == .ready else {
            throw MobileAccountError.busy
        }
        let token = UUID()
        phase = .operation(token)
        if keepingSelection {
            selectionKeepingOperation = token
        }
        // Every exclusive operation may lazily acquire or replace transport.
        // Keep its ownership token even if there was no transport on entry.
        transportOperationToken = token
        defer {
            if selectionKeepingOperation == token {
                selectionKeepingOperation = nil
            }
            if transportOperationToken == token {
                finishTransportOperation(token)
            }
            if phase == .operation(token) {
                phase = .ready
            }
        }
        return try await operation()
    }

    /// Whether the signed-in session may be read now: no operation is
    /// running, or only one that keeps the account, its store and the
    /// selection (a push-management request, which the App makes on its own
    /// at launch and in the background). The account page opened over the
    /// runtime reads it, and must not fail on that request.
    private var keepsSelection: Bool {
        switch phase {
        case .ready:
            return true
        case .operation(let token):
            return token == selectionKeepingOperation
        case .resetting, .pushLogout:
            return false
        }
    }

    @discardableResult
    private func beginAccountTransition(
        clearSelection: Bool
    ) -> Int {
        generation += 1
        authoritativeTransition = nil
        snapshot = nil
        if clearSelection {
            clearStoredSelection()
        }
        pendingStoreReleaseBarriers.append(
            contentsOf: onPrepareAccountTransition()
        )
        // An account page over a live runtime starts no transport; the
        // runtime is released by now, so the transition's requests can run.
        ensureTransport()
        return generation
    }

    private func beginReset() throws -> UUID {
        if case .resetting = phase {
            throw MobileAccountError.busy
        }
        let token = UUID()
        phase = .resetting(token)
        // A reset supersedes any older exclusive operation. Its late
        // completion may not release the replacement account transport.
        transportOperationToken = nil
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

    private func replaceWithFreshSignedOutStore(
        generation requestGeneration: Int
    ) async throws {
        try requireCurrentGeneration(requestGeneration)
        let publicationID = UUID()
        authoritativeTransition = (publicationID, .signedOut)
        defer {
            if authoritativeTransition?.id == publicationID {
                authoritativeTransition = nil
            }
        }
        releaseTransportPreservingSurfaceLease()
        snapshot = nil
        secondFactorEmail = nil
        clearStoredSelection()
        try await rotateWebsiteDataStore(generation: requestGeneration)
        signedOutStoreIsFresh = true
        ensureTransport()
    }

    private func recordReleaseBarrier(
        _ barrier: WebsiteDataStoreReleaseBarrier?
    ) {
        guard let barrier, !barrier.isReleased else {
            return
        }
        pendingStoreReleaseBarriers.append(barrier)
    }

    private func rotateWebsiteDataStore(generation requestGeneration: Int) async throws {
        // New releases can be recorded while this wait is suspended. Consume
        // only this batch, then retain both its unreleased views and any new
        // barriers, even if a reset has superseded this operation meanwhile.
        let barriers = pendingStoreReleaseBarriers
        pendingStoreReleaseBarriers = []
        let unreleased = await WebsiteDataStoreReleaseBarrier.waitForRelease(of: barriers)
        pendingStoreReleaseBarriers.append(contentsOf: unreleased)
        try requireCurrentGeneration(requestGeneration)
        await websiteDataStore.rotate(
            sweepOwnedStores: pendingStoreReleaseBarriers.isEmpty
        )
        try requireCurrentGeneration(requestGeneration)
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

    /// Source links on the bundled Open-source licenses page: exactly the
    /// URLs its generated `licenses.json` lists, nothing else on those hosts.
    private static let licenseSourceURLs: Set<String> = {
        guard
            let url = Bundle.main.resourceURL?
                .appendingPathComponent("MobileApp", isDirectory: true)
                .appendingPathComponent("licenses.json"),
            let data = try? Data(contentsOf: url)
        else { return [] }
        return licenseSourceURLs(from: data)
    }()

    static func allows(_ url: URL) -> Bool {
        allows(url, licenseSources: licenseSourceURLs)
    }

    static func allows(_ url: URL, licenseSources: Set<String>) -> Bool {
        allowedURLs.contains(url.absoluteString)
            || licenseSources.contains(url.absoluteString)
    }

    /// The crates.io archives and upstream sources of the page's MPL-2.0 offer.
    static func licenseSourceURLs(from data: Data) -> Set<String> {
        guard
            let object = try? JSONSerialization.jsonObject(with: data),
            let root = object as? [String: Any],
            let sources = root["sources"] as? [[String: Any]]
        else { return [] }
        var urls: [String] = []
        for source in sources {
            if let upstream = source["upstream"] as? String {
                urls.append(upstream)
            }
            for crate in source["crates"] as? [[String: Any]] ?? [] {
                if let registry = crate["registry"] as? String {
                    urls.append(registry)
                }
            }
        }
        return Set(urls.filter { raw in
            guard let url = URL(string: raw), url.scheme == "https", url.user == nil,
                  url.query == nil, url.fragment == nil
            else { return false }
            return url.host == "static.crates.io" || url.host == "github.com"
        })
    }
}
