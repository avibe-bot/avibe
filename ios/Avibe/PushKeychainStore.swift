import CryptoKit
import Foundation
import Security

enum PushKeychainError: Error, Equatable {
    case unavailable(OSStatus)
    case invalidData
    case invalidAccessGroup
}

struct PushInstallationState: Codable, Equatable {
    var installationID: UUID
    var installationSecret: Data
    var accountID: String?
    var bindGeneration: Int64
    var providerGeneration: Int64
    var provider: PushProviderState?
    var providerPatchPending: Bool
    var pendingBulkRevokeThroughGeneration: Int64?

    init(
        installationID: UUID,
        installationSecret: Data,
        accountID: String?,
        bindGeneration: Int64,
        providerGeneration: Int64,
        provider: PushProviderState?,
        providerPatchPending: Bool = false,
        pendingBulkRevokeThroughGeneration: Int64?
    ) {
        self.installationID = installationID
        self.installationSecret = installationSecret
        self.accountID = accountID
        self.bindGeneration = bindGeneration
        self.providerGeneration = providerGeneration
        self.provider = provider
        self.providerPatchPending = providerPatchPending
        self.pendingBulkRevokeThroughGeneration = pendingBulkRevokeThroughGeneration
    }

    private enum CodingKeys: String, CodingKey {
        case installationID
        case installationSecret
        case accountID
        case bindGeneration
        case providerGeneration
        case provider
        case providerPatchPending
        case pendingBulkRevokeThroughGeneration
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        installationID = try container.decode(UUID.self, forKey: .installationID)
        installationSecret = try container.decode(Data.self, forKey: .installationSecret)
        accountID = try container.decodeIfPresent(String.self, forKey: .accountID)
        bindGeneration = try container.decode(Int64.self, forKey: .bindGeneration)
        providerGeneration = try container.decode(Int64.self, forKey: .providerGeneration)
        provider = try container.decodeIfPresent(
            PushProviderState.self,
            forKey: .provider
        )
        providerPatchPending = try container.decodeIfPresent(
            Bool.self,
            forKey: .providerPatchPending
        ) ?? (provider != nil)
        pendingBulkRevokeThroughGeneration = try container.decodeIfPresent(
            Int64.self,
            forKey: .pendingBulkRevokeThroughGeneration
        )
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(installationID, forKey: .installationID)
        try container.encode(installationSecret, forKey: .installationSecret)
        try container.encodeIfPresent(accountID, forKey: .accountID)
        try container.encode(bindGeneration, forKey: .bindGeneration)
        try container.encode(providerGeneration, forKey: .providerGeneration)
        try container.encodeIfPresent(provider, forKey: .provider)
        try container.encode(providerPatchPending, forKey: .providerPatchPending)
        try container.encodeIfPresent(
            pendingBulkRevokeThroughGeneration,
            forKey: .pendingBulkRevokeThroughGeneration
        )
    }
}

struct PushOrphanedInstallation: Codable, Equatable {
    let installationID: UUID
    let installationSecret: Data
    let throughBindGeneration: Int64
}

struct PushProviderState: Codable, Equatable {
    let deviceToken: Data
    let environment: String
    let topic: String
    let generation: Int64
}

struct PushKeyRecord: Codable, Equatable {
    enum State: String, Codable {
        case pending
        case active
        case retiring
    }

    let kid: String
    let instanceOrigin: String
    let accountID: String
    let accountGeneration: Int64
    let bindGeneration: Int64
    let privateKey: Data
    let authSecret: Data
    let vapidKeyHash: String
    var state: State
    var pendingRevoke: Bool
    var replacementRegistrationIDs: [String]

    var publicSubscription: [String: Any] {
        let privateKey = try? P256KeyAgreementPrivateKey(rawRepresentation: privateKey)
        let publicKey = privateKey?.publicKey
        return [
            "endpoint": "https://avibe.bot/api/push/apns/\(kid)",
            "keys": [
                "p256dh": publicKey.map(PushBase64URL.encode) ?? "",
                "auth": PushBase64URL.encode(authSecret),
            ],
        ]
    }
}

private struct P256KeyAgreementPrivateKey {
    let publicKey: Data

    init(rawRepresentation: Data) throws {
        let privateKey = try CryptoKit.P256.KeyAgreement.PrivateKey(
            rawRepresentation: rawRepresentation
        )
        publicKey = privateKey.publicKey.x963Representation
    }
}

protocol PushKeychainStoring: AnyObject {
    func loadInstallation() throws -> PushInstallationState?
    func saveInstallation(_ state: PushInstallationState) throws
    func loadOrphanedInstallations() throws -> [PushOrphanedInstallation]
    func saveOrphanedInstallations(_ installations: [PushOrphanedInstallation]) throws
    func loadRecord(kid: String) throws -> PushKeyRecord?
    func saveRecord(_ record: PushKeyRecord) throws
    func deleteRecord(kid: String) throws
    func listRecords() throws -> [PushKeyRecord]
}

final class PushKeychainStore: PushKeychainStoring {
    static let installationService = "bot.avibe.ios.push.installation"
    static let orphanedInstallationService = "bot.avibe.ios.push.orphaned"
    static let recordService = "bot.avibe.ios.push.record"
    static let accessGroupInfoKey = "AVIBE_PUSH_KEYCHAIN_ACCESS_GROUP"

    private let encoder = JSONEncoder()
    private let decoder = JSONDecoder()
    private let bundle: Bundle

    init(bundle: Bundle = .main) {
        self.bundle = bundle
    }

    func loadInstallation() throws -> PushInstallationState? {
        try load(
            service: Self.installationService,
            account: "installation",
            accessGroup: nil
        )
    }

    func saveInstallation(_ state: PushInstallationState) throws {
        try save(
            state,
            service: Self.installationService,
            account: "installation",
            accessGroup: nil
        )
    }

    func loadOrphanedInstallations() throws -> [PushOrphanedInstallation] {
        try load(
            service: Self.orphanedInstallationService,
            account: "installations",
            accessGroup: nil
        ) ?? []
    }

    func saveOrphanedInstallations(
        _ installations: [PushOrphanedInstallation]
    ) throws {
        try save(
            installations,
            service: Self.orphanedInstallationService,
            account: "installations",
            accessGroup: nil
        )
    }

    func loadRecord(kid: String) throws -> PushKeyRecord? {
        try load(
            service: Self.recordService,
            account: kid,
            accessGroup: sharedAccessGroup()
        )
    }

    func saveRecord(_ record: PushKeyRecord) throws {
        try save(
            record,
            service: Self.recordService,
            account: record.kid,
            accessGroup: sharedAccessGroup()
        )
    }

    func deleteRecord(kid: String) throws {
        let query = baseQuery(
            service: Self.recordService,
            account: kid,
            accessGroup: try sharedAccessGroup()
        )
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw PushKeychainError.unavailable(status)
        }
    }

    func listRecords() throws -> [PushKeyRecord] {
        var query = baseQuery(
            service: Self.recordService,
            account: nil,
            accessGroup: try sharedAccessGroup()
        )
        query[kSecMatchLimit as String] = kSecMatchLimitAll
        query[kSecReturnData as String] = true
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound {
            return []
        }
        guard status == errSecSuccess else {
            throw PushKeychainError.unavailable(status)
        }
        guard let values = result as? [Data] else {
            throw PushKeychainError.invalidData
        }
        return try values.map { try decoder.decode(PushKeyRecord.self, from: $0) }
    }

    static func resolveSharedAccessGroup(_ value: Any?) throws -> String {
        guard
            let value = value as? String,
            !value.isEmpty,
            !value.contains("$("),
            !value.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains)
        else {
            throw PushKeychainError.invalidAccessGroup
        }
        return value
    }

    private func sharedAccessGroup() throws -> String {
        try Self.resolveSharedAccessGroup(
            bundle.object(forInfoDictionaryKey: Self.accessGroupInfoKey)
        )
    }

    private func load<Value: Decodable>(
        service: String,
        account: String,
        accessGroup: String?
    ) throws -> Value? {
        var query = baseQuery(
            service: service,
            account: account,
            accessGroup: accessGroup
        )
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        query[kSecReturnData as String] = true
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound {
            return nil
        }
        guard status == errSecSuccess, let data = result as? Data else {
            throw PushKeychainError.unavailable(status)
        }
        do {
            return try decoder.decode(Value.self, from: data)
        } catch {
            throw PushKeychainError.invalidData
        }
    }

    private func save<Value: Encodable>(
        _ value: Value,
        service: String,
        account: String,
        accessGroup: String?
    ) throws {
        let data: Data
        do {
            data = try encoder.encode(value)
        } catch {
            throw PushKeychainError.invalidData
        }
        let query = baseQuery(
            service: service,
            account: account,
            accessGroup: accessGroup
        )
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        let updateStatus = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if updateStatus == errSecItemNotFound {
            var insert = query
            insert.merge(attributes) { _, new in new }
            let status = SecItemAdd(insert as CFDictionary, nil)
            guard status == errSecSuccess else {
                throw PushKeychainError.unavailable(status)
            }
            return
        }
        guard updateStatus == errSecSuccess else {
            throw PushKeychainError.unavailable(updateStatus)
        }
    }

    private func baseQuery(
        service: String,
        account: String?,
        accessGroup: String?
    ) -> [String: Any] {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
        ]
        if let account {
            query[kSecAttrAccount as String] = account
        }
        if let accessGroup {
            query[kSecAttrAccessGroup as String] = accessGroup
        }
        return query
    }
}

final class InMemoryPushKeychainStore: PushKeychainStoring {
    var installation: PushInstallationState?
    var orphanedInstallations: [PushOrphanedInstallation] = []
    var records: [String: PushKeyRecord] = [:]

    func loadInstallation() throws -> PushInstallationState? {
        installation
    }

    func saveInstallation(_ state: PushInstallationState) throws {
        installation = state
    }

    func loadOrphanedInstallations() throws -> [PushOrphanedInstallation] {
        orphanedInstallations
    }

    func saveOrphanedInstallations(
        _ installations: [PushOrphanedInstallation]
    ) throws {
        orphanedInstallations = installations
    }

    func loadRecord(kid: String) throws -> PushKeyRecord? {
        records[kid]
    }

    func saveRecord(_ record: PushKeyRecord) throws {
        records[record.kid] = record
    }

    func deleteRecord(kid: String) throws {
        records.removeValue(forKey: kid)
    }

    func listRecords() throws -> [PushKeyRecord] {
        records.values.sorted { $0.kid < $1.kid }
    }
}

protocol PushGenerationStoring: AnyObject {
    var generation: Int64 { get set }
}

final class SharedPushGenerationStore: PushGenerationStoring {
    private static let key = "avibe.push.account-generation"
    private let defaults: UserDefaults

    init(defaults: UserDefaults? = UserDefaults(suiteName: "group.bot.avibe.ios")) {
        self.defaults = defaults ?? .standard
    }

    var generation: Int64 {
        get {
            let value = defaults.object(forKey: Self.key) as? NSNumber
            return value?.int64Value ?? 0
        }
        set {
            defaults.set(NSNumber(value: newValue), forKey: Self.key)
        }
    }
}

final class InMemoryPushGenerationStore: PushGenerationStoring {
    var generation: Int64 = 0
}

protocol PushInstallationMarkerStoring: AnyObject {
    var installationID: UUID? { get set }
}

final class UserDefaultsPushInstallationMarkerStore:
    PushInstallationMarkerStoring
{
    private static let key = "avibe.push.installation-marker"
    private let defaults: UserDefaults

    init(
        defaults: UserDefaults? = UserDefaults(
            suiteName: "group.bot.avibe.ios"
        )
    ) {
        self.defaults = defaults ?? .standard
    }

    var installationID: UUID? {
        get {
            guard let value = defaults.string(forKey: Self.key) else {
                return nil
            }
            return UUID(uuidString: value)
        }
        set {
            defaults.set(newValue?.uuidString, forKey: Self.key)
        }
    }
}

final class InMemoryPushInstallationMarkerStore:
    PushInstallationMarkerStoring
{
    var installationID: UUID?
}
