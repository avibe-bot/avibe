import CryptoKit
import Foundation
import UserNotifications

struct NativePushPayload: Decodable {
    let title: String
    let body: String
    let url: String
    let tag: String?
    let badgeCount: Int?
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
    private let now: () -> Date

    init(
        keychain: PushKeychainStoring,
        generationStore: PushGenerationStoring,
        now: @escaping () -> Date = Date.init
    ) {
        self.keychain = keychain
        self.generationStore = generationStore
        self.now = now
    }

    func process(
        _ content: UNMutableNotificationContent
    ) -> UNMutableNotificationContent {
        let currentTime = now()
        guard
            let envelope = content.userInfo["avibe"] as? [AnyHashable: Any],
            let kid = envelope["kid"] as? String,
            let ciphertext = envelope["ct"] as? String,
            let ciphertextData = PushBase64URL.decode(ciphertext),
            let record = try? keychain.loadRecord(kid: kid),
            record.state == .active
                || (
                    record.state == .retiring
                        && !record.pendingRevoke
                        && record.retirementDeadline.map {
                            $0 > currentTime
                        } == true
                ),
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
        if let badgeCount = payload.badgeCount {
            content.badge = NSNumber(value: badgeCount)
        }
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
