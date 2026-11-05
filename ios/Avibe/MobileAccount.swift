import Foundation
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

struct MobileRuntimeContext: Equatable {
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

    var jsonObject: [String: Any] {
        switch self {
        case .signedOut:
            return ["status": "signedOut"]
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

@MainActor
final class AccountWebsiteDataStore {
    private static let identifierKey = "avibe.mobile.websiteDataStoreIdentifier"

    private let defaults: UserDefaults
    private(set) var identifier: UUID
    private(set) var dataStore: WKWebsiteDataStore

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        if
            let rawIdentifier = defaults.string(forKey: Self.identifierKey),
            let savedIdentifier = UUID(uuidString: rawIdentifier)
        {
            identifier = savedIdentifier
        } else {
            identifier = UUID()
            defaults.set(identifier.uuidString, forKey: Self.identifierKey)
        }
        dataStore = WKWebsiteDataStore(forIdentifier: identifier)
    }

    func rotate() async {
        let oldIdentifier = identifier
        identifier = UUID()
        defaults.set(identifier.uuidString, forKey: Self.identifierKey)
        dataStore = WKWebsiteDataStore(forIdentifier: identifier)

        await withCheckedContinuation { continuation in
            WKWebsiteDataStore.remove(forIdentifier: oldIdentifier) { _ in
                continuation.resume()
            }
        }
    }
}

@MainActor
protocol MobileAuthTransporting: AnyObject {
    func requestOTP(email: String) async throws -> Int
    func verifyOTP(email: String, code: String) async throws -> MobileSessionSnapshot
    func fetchSession() async throws -> MobileSessionSnapshot?
    func logout() async throws
    func cancelAll() async
    func invalidate()
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
    private let onOpenRuntime: (MobileRuntimeContext) -> Void
    private let onPrepareAccountTransition: () -> Void

    private var transport: MobileAuthTransporting?
    private var snapshot: MobileSessionSnapshot?
    private var generation = 0
    private var phase = Phase.ready
    private var signedOutStoreIsFresh = false

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
        onOpenRuntime: @escaping (MobileRuntimeContext) -> Void,
        onPrepareAccountTransition: @escaping () -> Void
    ) {
        self.defaults = defaults
        self.controlPlane = controlPlane
        self.websiteDataStore = websiteDataStore
        self.transportFactory = transportFactory
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
        transport?.invalidate()
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
            let fresh = try await self.requireTransport().fetchSession()
            try self.requireCurrentGeneration(requestGeneration)
            guard let fresh else {
                self.beginAccountTransition(clearSelection: true)
                await self.replaceWithFreshSignedOutStore()
                return .signedOut
            }
            if
                let selection = self.storedSelection(),
                selection.accountID != fresh.account.id
            {
                self.beginAccountTransition(clearSelection: true)
                await self.replaceWithFreshSignedOutStore()
                return .signedOut
            }
            self.signedOutStoreIsFresh = false
            self.snapshot = fresh
            return self.state(for: fresh)
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
                let fresh = try await self.requireTransport().verifyOTP(
                    email: normalizedEmail,
                    code: code
                )
                try self.requireCurrentGeneration(loginGeneration)
                self.clearStoredSelection()
                self.signedOutStoreIsFresh = false
                self.snapshot = fresh
                return self.state(for: fresh)
            } catch {
                if self.generation == loginGeneration {
                    await self.replaceWithFreshSignedOutStore()
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
            let fresh = try await self.requireTransport().fetchSession()
            try self.requireCurrentGeneration(requestGeneration)
            guard let fresh else {
                self.clearStoredSelection()
                await self.replaceWithFreshSignedOutStore()
                return .signedOut
            }
            if
                let selection = self.storedSelection(),
                selection.accountID != fresh.account.id
            {
                self.clearStoredSelection()
                await self.replaceWithFreshSignedOutStore()
                return .signedOut
            }
            self.signedOutStoreIsFresh = false
            self.snapshot = fresh
            return self.state(for: fresh)
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

        if let oldTransport = transport {
            await oldTransport.cancelAll()
            try? await oldTransport.logout()
            oldTransport.invalidate()
        }
        transport = nil

        await websiteDataStore.rotate()
        signedOutStoreIsFresh = true
        activateAccountSurface()
        return .signedOut
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
        onPrepareAccountTransition()
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
        transport?.invalidate()
        transport = nil
        snapshot = nil
        clearStoredSelection()
        await websiteDataStore.rotate()
        signedOutStoreIsFresh = true
        activateAccountSurface()
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
    ]

    static func allows(_ url: URL) -> Bool {
        allowedURLs.contains(url.absoluteString)
    }
}
