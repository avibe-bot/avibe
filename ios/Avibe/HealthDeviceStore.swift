import Foundation
import Security

struct HealthDeviceIdentity: Codable, Equatable {
    let installationID: String
    let accountID: String
    let instanceID: String
    let instanceName: String
    let origin: String
    let deviceID: String
    let credential: String
}

protocol HealthCredentialStoring {
    func load() throws -> HealthDeviceIdentity?
    func save(_ identity: HealthDeviceIdentity) throws
    func clear() throws
    func installationID() throws -> String
}

extension HealthCredentialStoring {
    func installationID() throws -> String { try load()?.installationID ?? UUID().uuidString.lowercased() }
}

/// App-private, never shared with the notification service or iCloud Keychain.
struct HealthCredentialStore: HealthCredentialStoring {
    private let itemAccess: PushKeychainItemAccessing

    init(itemAccess: PushKeychainItemAccessing = SystemPushKeychainItemAccess()) { self.itemAccess = itemAccess }

    private var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "bot.avibe.ios.health.device",
         kSecAttrAccount as String: "device",
         kSecAttrSynchronizable as String: false]
    }

    func load() throws -> HealthDeviceIdentity? {
        var request = query
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        let (status, value) = itemAccess.copyMatching(request)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = value as? Data else {
            throw HealthSyncError.code("credential_unavailable")
        }
        return try JSONDecoder().decode(HealthDeviceIdentity.self, from: data)
    }

    func save(_ identity: HealthDeviceIdentity) throws {
        let attributes: [String: Any] = [
            kSecValueData as String: try JSONEncoder().encode(identity),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        let status = itemAccess.update(query, attributes: attributes)
        if status == errSecItemNotFound {
            var insert = query
            insert.merge(attributes) { _, new in new }
            guard itemAccess.add(insert) == errSecSuccess else {
                throw HealthSyncError.code("credential_unavailable")
            }
        } else if status != errSecSuccess {
            throw HealthSyncError.code("credential_unavailable")
        }
    }

    func installationID() throws -> String {
        var installationQuery = query
        installationQuery[kSecAttrService as String] = "bot.avibe.ios.health.installation"
        var lookup = installationQuery
        lookup[kSecReturnData as String] = true
        lookup[kSecMatchLimit as String] = kSecMatchLimitOne
        let (status, value) = itemAccess.copyMatching(lookup)
        if status == errSecSuccess, let data = value as? Data,
           let value = String(data: data, encoding: .utf8), UUID(uuidString: value) != nil { return value }
        guard status == errSecItemNotFound else { throw HealthSyncError.code("credential_unavailable") }
        let id = UUID().uuidString.lowercased()
        installationQuery[kSecValueData as String] = Data(id.utf8)
        installationQuery[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        guard itemAccess.add(installationQuery) == errSecSuccess else { throw HealthSyncError.code("credential_unavailable") }
        return id
    }

    func clear() throws {
        let status = itemAccess.delete(query)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw HealthSyncError.code("credential_unavailable")
        }
    }
}

enum HealthSyncError: Error, Equatable {
    case code(String)
    case http(Int, String, TimeInterval?)

    var code: String {
        switch self {
        case .code(let code), .http(_, let code, _): return code
        }
    }
}

struct HealthPendingResult: Codable {
    let id: String
    var claimToken: String
    var outcome: String
    var states: [String: String]
    var error: String?
    var uploadID: String?
    var ready = false
    var acknowledged = false
    var completedAt = Date()
}

struct HealthSyncState: Codable {
    var sequence: Int64 = 0
    var completed: [String: HealthPendingResult] = [:]
    var uploads: [String] = []
    var initialBackfillPending = true
    var suspended = false
    var cleanupPending = false
}

/// Only health sync owns this directory; no payload is placed in app groups.
final class HealthDeviceStore {
    let directory: URL
    private(set) var state: HealthSyncState

    init(directory: URL) throws {
        self.directory = directory
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
        var url = directory
        var resources = URLResourceValues()
        resources.isExcludedFromBackup = true
        try url.setResourceValues(resources)
        let stateURL = directory.appendingPathComponent("state.json")
        if FileManager.default.fileExists(atPath: stateURL.path) {
            state = try JSONDecoder().decode(HealthSyncState.self, from: Data(contentsOf: stateURL))
        } else {
            state = HealthSyncState()
        }
    }

    func update(_ change: (inout HealthSyncState) -> Void) throws {
        var next = state
        change(&next)
        try JSONEncoder().encode(next).write(to: directory.appendingPathComponent("state.json"),
                                            options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        state = next
    }

    func nextSequence() throws -> Int64 {
        guard state.sequence < Int64.max else { throw HealthSyncError.code("sequence_exhausted") }
        try update { $0.sequence += 1 }
        return state.sequence
    }

    func enqueue(_ payload: [String: Any], prepare: (inout HealthSyncState, String) -> Void = { _, _ in }) throws -> String {
        let data = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        guard data.count <= 256 * 1024 else { throw HealthSyncError.code("invalid_payload") }
        let id = UUID().uuidString
        try data.write(to: file(id), options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        try update {
            $0.uploads.append(id)
            prepare(&$0, id)
        }
        return id
    }

    func file(_ id: String) -> URL { directory.appendingPathComponent(id + ".json") }

    func removeUpload(_ id: String, prepare: (inout HealthSyncState) -> Void = { _ in }) throws {
        try update {
            $0.uploads.removeAll { $0 == id }
            prepare(&$0)
        }
        if FileManager.default.fileExists(atPath: file(id).path) {
            try FileManager.default.removeItem(at: file(id))
        }
    }

    func prepareForCleanup() throws {
        try update {
            $0.cleanupPending = true
            $0.uploads.removeAll()
            $0.completed.removeAll()
        }
        for file in try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            where file.lastPathComponent != "state.json" {
            try FileManager.default.removeItem(at: file)
        }
    }

    func clear() throws {
        try FileManager.default.removeItem(at: directory)
        state = HealthSyncState()
    }
}
