import CryptoKit
import Foundation
import UserNotifications
import XCTest
@testable import Avibe

final class PushNotificationTests: XCTestCase {
    func testMainAppBundleContainsFallbackLocalization() throws {
        let appBundle = try XCTUnwrap(Bundle(identifier: "bot.avibe.ios"))
        let value = appBundle.localizedString(
            forKey: "AVIBE_PUSH_FALLBACK",
            value: nil,
            table: nil
        )

        XCTAssertFalse(value.isEmpty)
        XCTAssertNotEqual(value, "AVIBE_PUSH_FALLBACK")
    }

    func testSharedAccessGroupResolverRejectsUnexpandedOrMissingValues() {
        XCTAssertEqual(
            try? PushKeychainStore.resolveSharedAccessGroup(
                "68B2SQ6YK3.bot.avibe.ios.push"
            ),
            "68B2SQ6YK3.bot.avibe.ios.push"
        )
        XCTAssertThrowsError(
            try PushKeychainStore.resolveSharedAccessGroup(
                "$(AppIdentifierPrefix)bot.avibe.ios.push"
            )
        ) { error in
            XCTAssertEqual(error as? PushKeychainError, .invalidAccessGroup)
        }
        XCTAssertThrowsError(
            try PushKeychainStore.resolveSharedAccessGroup("")
        )
        XCTAssertThrowsError(
            try PushKeychainStore.resolveSharedAccessGroup(nil)
        )
    }

    func testNativeSubscriptionUsesX963PublicKeyEncoding() throws {
        let fixture = try loadNativeSubscriptionFixture()
        let privateKey = try P256.KeyAgreement.PrivateKey(
            rawRepresentation: try XCTUnwrap(
                PushBase64URL.decode(fixture.privateKey)
            )
        )
        let record = PushKeyRecord(
            kid: "native-key-shape",
            instanceOrigin: "https://instance.example",
            accountID: "account-1",
            accountGeneration: 1,
            bindGeneration: 1,
            privateKey: privateKey.rawRepresentation,
            authSecret: Data(repeating: 24, count: 16),
            vapidKeyHash: String(repeating: "a", count: 64),
            state: .active,
            pendingRevoke: false,
            replacementRegistrationIDs: []
        )
        let subscription = try XCTUnwrap(
            record.publicSubscription["keys"] as? [String: Any]
        )
        let encoded = try XCTUnwrap(subscription["p256dh"] as? String)
        let publicKey = try XCTUnwrap(PushBase64URL.decode(encoded))

        XCTAssertEqual(publicKey.count, 65)
        XCTAssertEqual(publicKey.first, 4)
        XCTAssertEqual(encoded, fixture.p256dh)
        XCTAssertNoThrow(
            try P256.KeyAgreement.PublicKey(x963Representation: publicKey)
        )
    }

    func testRFC8291AppendixAIntermediateValuesAndPlaintext() throws {
        let privateKey = try XCTUnwrap(
            PushBase64URL.decode(
                "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
            )
        )
        let authSecret = try XCTUnwrap(
            PushBase64URL.decode("BTBZMqHH6r4Tts7J_aSIgg")
        )
        let encryptedHeader = try XCTUnwrap(
            PushBase64URL.decode(
                "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
            )
        )
        let ciphertext = try XCTUnwrap(
            PushBase64URL.decode(
                "8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ"
            )
        )
        let body = encryptedHeader + ciphertext
        let receiver = try P256.KeyAgreement.PrivateKey(
            rawRepresentation: privateKey
        )
        let derived = try WebPushAES128GCM.derive(
            body,
            privateKey: receiver,
            authSecret: authSecret
        )

        XCTAssertEqual(derived.encryptedHeader, encryptedHeader)
        XCTAssertEqual(derived.ciphertext, ciphertext)
        XCTAssertEqual(
            PushBase64URL.encode(derived.ecdhSecret),
            "kyrL1jIIOHEzg3sM2ZWRHDRB62YACZhhSlknJ672kSs"
        )
        XCTAssertEqual(
            PushBase64URL.encode(derived.prkKey),
            "Snr3JMxaHVDXHWJn5wdC52WjpCtd2EIEGBykDcZW32k"
        )
        XCTAssertEqual(
            PushBase64URL.encode(derived.ikm),
            "S4lYMb_L0FxCeq0WhDx813KgSYqU26kOyzWUdsXYyrg"
        )
        XCTAssertEqual(
            PushBase64URL.encode(derived.prk),
            "09_eUZGrsvxChDCGRCdkLiDXrReGOEVeSCdCcPBSJSc"
        )
        XCTAssertEqual(
            PushBase64URL.encode(derived.cek),
            "oIhVW04MRdy2XN9CiKLxTg"
        )
        XCTAssertEqual(
            PushBase64URL.encode(derived.nonce),
            "4h_95klXJ5E_qnoN"
        )
        XCTAssertEqual(
            try WebPushAES128GCM.decrypt(
                body,
                privateKey: receiver,
                authSecret: authSecret
            ),
            Data("When I grow up, I want to be a watermelon".utf8)
        )
    }

    func testExactRelayEnvelopeIsConsumedByTheNSEPath() throws {
        let receiver = try loadReceiverFixture()
        let envelopeData = try fixtureData(named: "relay-apns-envelope")
        let envelope = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: envelopeData)
                as? [String: Any]
        )
        let avibe = try XCTUnwrap(envelope["avibe"] as? [String: Any])
        let kid = try XCTUnwrap(avibe["kid"] as? String)
        let ciphertext = try XCTUnwrap(avibe["ct"] as? String)
        let ciphertextData = try XCTUnwrap(PushBase64URL.decode(ciphertext))

        XCTAssertEqual(sha256Hex(envelopeData), receiver.relaySHA256)
        XCTAssertEqual(sha256Hex(ciphertextData), receiver.requestBodySHA256)
        XCTAssertEqual(kid, "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

        let keychain = InMemoryPushKeychainStore()
        try keychain.saveRecord(
            PushKeyRecord(
                kid: kid,
                instanceOrigin: receiver.expectedNative.instanceOrigin,
                accountID: receiver.expectedNative.accountID,
                accountGeneration: receiver.expectedNative.bindGeneration,
                bindGeneration: receiver.expectedNative.bindGeneration,
                privateKey: try XCTUnwrap(
                    PushBase64URL.decode(receiver.receiverPrivateKey)
                ),
                authSecret: try XCTUnwrap(
                    PushBase64URL.decode(receiver.authSecret)
                ),
                vapidKeyHash: String(repeating: "a", count: 64),
                state: .active,
                pendingRevoke: false,
                replacementRegistrationIDs: []
            )
        )
        let generationStore = InMemoryPushGenerationStore()
        generationStore.generation = receiver.expectedNative.bindGeneration
        let service = NotificationService(
            keychain: keychain,
            generationStore: generationStore
        )
        let content = UNMutableNotificationContent()
        content.title = "AVIBE_PUSH_FALLBACK"
        content.body = "New Avibe notification"
        content.userInfo = envelope
        let request = UNNotificationRequest(
            identifier: "fixture",
            content: content,
            trigger: nil
        )
        let expectation = expectation(description: "NSE processed content")
        var processed: UNNotificationContent?
        service.didReceive(request) {
            processed = $0
            expectation.fulfill()
        }
        wait(for: [expectation], timeout: 1)

        let result = try XCTUnwrap(processed)
        XCTAssertEqual(result.title, receiver.expected.title)
        XCTAssertEqual(result.body, receiver.expected.body)
        XCTAssertEqual(result.badge?.intValue, receiver.expected.badgeCount)
        XCTAssertEqual(result.threadIdentifier, receiver.expected.tag)
        XCTAssertEqual(
            result.userInfo["url"] as? String,
            receiver.expected.url
        )
        XCTAssertEqual(
            result.userInfo["instance_origin"] as? String,
            receiver.expectedNative.instanceOrigin
        )
        XCTAssertEqual(
            result.userInfo["account_id"] as? String,
            receiver.expectedNative.accountID
        )
        XCTAssertEqual(result.userInfo["kid"] as? String, kid)
    }

    func testNSEKeepsFallbackWhenPrivateKeyDoesNotMatch() throws {
        let receiver = try loadReceiverFixture()
        let result = try processThroughNSE(
            receiver: receiver,
            privateKey: Data(repeating: 1, count: 32),
            generation: receiver.expectedNative.bindGeneration
        )
        assertFallback(result)
    }

    func testNSEKeepsFallbackWhenGenerationDoesNotMatch() throws {
        let receiver = try loadReceiverFixture()
        let result = try processThroughNSE(
            receiver: receiver,
            privateKey: try XCTUnwrap(
                PushBase64URL.decode(receiver.receiverPrivateKey)
            ),
            generation: receiver.expectedNative.bindGeneration + 1
        )
        assertFallback(result)
    }

    func testNSEKeepsFallbackForPendingRegistrationKey() throws {
        let receiver = try loadReceiverFixture()
        let result = try processThroughNSE(
            receiver: receiver,
            privateKey: try XCTUnwrap(
                PushBase64URL.decode(receiver.receiverPrivateKey)
            ),
            generation: receiver.expectedNative.bindGeneration,
            state: .pending,
            pendingRevoke: false
        )
        assertFallback(result)
    }

    func testNSEKeepsFallbackForPendingRetireKey() throws {
        let receiver = try loadReceiverFixture()
        let result = try processThroughNSE(
            receiver: receiver,
            privateKey: try XCTUnwrap(
                PushBase64URL.decode(receiver.receiverPrivateKey)
            ),
            generation: receiver.expectedNative.bindGeneration,
            state: .retiring,
            pendingRevoke: true
        )
        assertFallback(result)
    }
}

private extension PushNotificationTests {
    struct NativeSubscriptionFixture: Decodable {
        let privateKey: String
        let p256dh: String

        enum CodingKeys: String, CodingKey {
            case privateKey = "private_key"
            case p256dh
        }
    }

    struct ReceiverFixture: Decodable {
        struct Native: Decodable {
            let instanceOrigin: String
            let accountID: String
            let bindGeneration: Int64

            enum CodingKeys: String, CodingKey {
                case instanceOrigin = "instance_origin"
                case accountID = "account_id"
                case bindGeneration = "bind_generation"
            }
        }

        struct Expected: Decodable {
            let title: String
            let body: String
            let url: String
            let tag: String
            let badgeCount: Int

            enum CodingKeys: String, CodingKey {
                case title
                case body
                case url
                case tag
                case badgeCount = "badge_count"
            }
        }

        let requestBodySHA256: String
        let relaySHA256: String
        let receiverPrivateKey: String
        let authSecret: String
        let expectedNative: Native
        let expected: Expected

        enum CodingKeys: String, CodingKey {
            case requestBodySHA256 = "request_body_sha256"
            case relaySHA256 = "relay_sha256"
            case receiverPrivateKey = "receiver_private_key"
            case authSecret = "auth_secret"
            case expectedNative = "expected_native"
            case expected
        }
    }

    func fixtureData(named name: String) throws -> Data {
        let url = try XCTUnwrap(
            Bundle(for: PushNotificationTests.self).url(
                forResource: name,
                withExtension: "json"
            )
        )
        return try Data(contentsOf: url)
    }

    func loadReceiverFixture() throws -> ReceiverFixture {
        try JSONDecoder().decode(
            ReceiverFixture.self,
            from: fixtureData(named: "instance-webpush-receiver")
        )
    }

    func loadNativeSubscriptionFixture() throws -> NativeSubscriptionFixture {
        try JSONDecoder().decode(
            NativeSubscriptionFixture.self,
            from: fixtureData(named: "native-subscription-key")
        )
    }

    func processThroughNSE(
        receiver: ReceiverFixture,
        privateKey: Data,
        generation: Int64,
        state: PushKeyRecord.State = .active,
        pendingRevoke: Bool = false
    ) throws -> UNNotificationContent {
        let envelope = try XCTUnwrap(
            try JSONSerialization.jsonObject(
                with: fixtureData(named: "relay-apns-envelope")
            ) as? [String: Any]
        )
        let avibe = try XCTUnwrap(envelope["avibe"] as? [String: Any])
        let kid = try XCTUnwrap(avibe["kid"] as? String)
        let keychain = InMemoryPushKeychainStore()
        try keychain.saveRecord(
            PushKeyRecord(
                kid: kid,
                instanceOrigin: receiver.expectedNative.instanceOrigin,
                accountID: receiver.expectedNative.accountID,
                accountGeneration: receiver.expectedNative.bindGeneration,
                bindGeneration: receiver.expectedNative.bindGeneration,
                privateKey: privateKey,
                authSecret: try XCTUnwrap(
                    PushBase64URL.decode(receiver.authSecret)
                ),
                vapidKeyHash: String(repeating: "a", count: 64),
                state: state,
                pendingRevoke: pendingRevoke,
                replacementRegistrationIDs: []
            )
        )
        let generationStore = InMemoryPushGenerationStore()
        generationStore.generation = generation
        let content = UNMutableNotificationContent()
        content.title = "AVIBE_PUSH_FALLBACK"
        content.body = "New Avibe notification"
        content.userInfo = envelope
        let service = NotificationService(
            keychain: keychain,
            generationStore: generationStore
        )
        let expectation = expectation(description: "NSE preserved fallback")
        var processed: UNNotificationContent?
        service.didReceive(
            UNNotificationRequest(
                identifier: "fixture-fallback",
                content: content,
                trigger: nil
            )
        ) {
            processed = $0
            expectation.fulfill()
        }
        wait(for: [expectation], timeout: 1)
        return try XCTUnwrap(processed)
    }

    func assertFallback(_ result: UNNotificationContent) {
        XCTAssertEqual(result.title, "AVIBE_PUSH_FALLBACK")
        XCTAssertEqual(result.body, "New Avibe notification")
        XCTAssertNil(result.userInfo["url"])
        XCTAssertNil(result.userInfo["account_id"])
    }

    func sha256Hex(_ data: Data) -> String {
        SHA256.hash(data: data)
            .map { String(format: "%02x", $0) }
            .joined()
    }
}
