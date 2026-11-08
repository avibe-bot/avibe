import CryptoKit
import Foundation
import UserNotifications

private struct NativePushPayload: Decodable {
    let title: String
    let body: String
    let url: String
    let tag: String?
    let badgeCount: Int
    let category: String?

    enum CodingKeys: String, CodingKey {
        case title
        case body
        case url
        case tag
        case badgeCount = "badge_count"
        case category
    }
}

struct PushNotificationProcessor {
    private let keychain: PushKeychainStoring
    private let generationStore: PushGenerationStoring

    init(
        keychain: PushKeychainStoring,
        generationStore: PushGenerationStoring
    ) {
        self.keychain = keychain
        self.generationStore = generationStore
    }

    func process(
        _ content: UNMutableNotificationContent
    ) -> UNMutableNotificationContent {
        guard
            let envelope = content.userInfo["avibe"] as? [AnyHashable: Any],
            let kid = envelope["kid"] as? String,
            let ciphertext = envelope["ct"] as? String,
            let ciphertextData = PushBase64URL.decode(ciphertext),
            let record = try? keychain.loadRecord(kid: kid),
            record.state != .retiring || !record.pendingRevoke,
            record.accountGeneration == generationStore.generation,
            let privateKey = try? P256KeyAgreementPrivateKey(
                rawRepresentation: record.privateKey
            ),
            let plaintext = try? WebPushAES128GCM.decrypt(
                ciphertextData,
                privateKey: privateKey.privateKey,
                authSecret: record.authSecret
            ),
            let payload = try? JSONDecoder().decode(
                NativePushPayload.self,
                from: plaintext
            ),
            generationStore.generation == record.accountGeneration
        else {
            return content
        }

        content.title = payload.title
        content.body = payload.body
        content.badge = NSNumber(value: payload.badgeCount)
        content.threadIdentifier = payload.tag ?? ""
        if let category = payload.category {
            content.categoryIdentifier = category
        }

        var userInfo = content.userInfo
        userInfo["url"] = payload.url
        userInfo["instance_origin"] = record.instanceOrigin
        userInfo["account_id"] = record.accountID
        userInfo["kid"] = kid
        content.userInfo = userInfo
        return content
    }
}

private struct P256KeyAgreementPrivateKey {
    let privateKey: CryptoKit.P256.KeyAgreement.PrivateKey

    init(rawRepresentation: Data) throws {
        privateKey = try CryptoKit.P256.KeyAgreement.PrivateKey(
            rawRepresentation: rawRepresentation
        )
    }
}
