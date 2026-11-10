import CryptoKit
import Foundation

enum PushCryptoError: Error, Equatable {
    case malformedHeader
    case unsupportedRecordSize
    case invalidPublicKey
    case invalidCiphertext
    case authenticationFailed
    case invalidPadding
}

struct WebPushAES128GCMIntermediate: Equatable {
    let encryptedHeader: Data
    let ciphertext: Data
    let ecdhSecret: Data
    let prkKey: Data
    let ikm: Data
    let prk: Data
    let cek: Data
    let nonce: Data
}

enum PushBase64URL {
    static func encode(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    static func decode(_ value: String) -> Data? {
        guard
            !value.isEmpty,
            value.allSatisfy({ $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-" || $0 == "_") })
        else {
            return nil
        }
        let padding = String(repeating: "=", count: (4 - value.count % 4) % 4)
        return Data(
            base64Encoded: value
                .replacingOccurrences(of: "-", with: "+")
                .replacingOccurrences(of: "_", with: "/")
                + padding
        )
    }
}

enum WebPushAES128GCM {
    private static let authInfoPrefix = Data("WebPush: info\u{0}".utf8)
    private static let contentEncodingLabel =
        Data("Content-Encoding: aes128gcm\u{0}".utf8)
    private static let nonceLabel =
        Data("Content-Encoding: nonce\u{0}".utf8)

    static func decrypt(
        _ body: Data,
        privateKey: P256.KeyAgreement.PrivateKey,
        authSecret: Data
    ) throws -> Data {
        let derived = try derive(
            body,
            privateKey: privateKey,
            authSecret: authSecret
        )
        let sealedBox: AES.GCM.SealedBox
        do {
            sealedBox = try AES.GCM.SealedBox(
                nonce: AES.GCM.Nonce(data: derived.nonce),
                ciphertext: derived.ciphertext.dropLast(16),
                tag: derived.ciphertext.suffix(16)
            )
        } catch {
            throw PushCryptoError.invalidCiphertext
        }

        let plaintext: Data
        do {
            plaintext = try AES.GCM.open(
                sealedBox,
                using: SymmetricKey(data: derived.cek)
            )
        } catch {
            throw PushCryptoError.authenticationFailed
        }
        guard plaintext.last == 2 else {
            throw PushCryptoError.invalidPadding
        }
        return Data(plaintext.dropLast())
    }

    static func derive(
        _ body: Data,
        privateKey: P256.KeyAgreement.PrivateKey,
        authSecret: Data
    ) throws -> WebPushAES128GCMIntermediate {
        guard body.count >= 86, authSecret.count == 16 else {
            throw PushCryptoError.malformedHeader
        }
        let salt = Data(body[0..<16])
        let recordSize = body[16..<20]
            .reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }
        guard recordSize >= 18 else {
            throw PushCryptoError.unsupportedRecordSize
        }
        let keyIDLength = Int(body[20])
        guard keyIDLength == 65, body.count >= 21 + keyIDLength else {
            throw PushCryptoError.malformedHeader
        }

        let encryptedHeader = Data(body[0..<86])
        let senderPublicBytes = Data(body[21..<(21 + keyIDLength)])
        let senderPublic: P256.KeyAgreement.PublicKey
        do {
            senderPublic = try P256.KeyAgreement.PublicKey(
                x963Representation: senderPublicBytes
            )
        } catch {
            throw PushCryptoError.invalidPublicKey
        }
        let ciphertext = Data(body.dropFirst(21 + keyIDLength))
        guard ciphertext.count >= 17 else {
            throw PushCryptoError.invalidCiphertext
        }

        let sharedSecret: Data
        do {
            sharedSecret = try privateKey
                .sharedSecretFromKeyAgreement(with: senderPublic)
                .withUnsafeBytes { Data($0) }
        } catch {
            throw PushCryptoError.authenticationFailed
        }
        let receiverPublic = privateKey.publicKey.x963Representation
        var keyInfo = Data()
        keyInfo.append(authInfoPrefix)
        keyInfo.append(receiverPublic)
        keyInfo.append(senderPublicBytes)

        let prkKey = hkdfExtract(salt: authSecret, input: sharedSecret)
        let ikm = hkdfExpand(prk: prkKey, info: keyInfo, outputCount: 32)
        let prk = hkdfExtract(salt: salt, input: ikm)
        let cek = hkdfExpand(
            prk: prk,
            info: contentEncodingLabel,
            outputCount: 16
        )
        let nonce = hkdfExpand(
            prk: prk,
            info: nonceLabel,
            outputCount: 12
        )

        return WebPushAES128GCMIntermediate(
            encryptedHeader: encryptedHeader,
            ciphertext: ciphertext,
            ecdhSecret: sharedSecret,
            prkKey: prkKey,
            ikm: ikm,
            prk: prk,
            cek: cek,
            nonce: nonce
        )
    }

    private static func hkdfExtract(salt: Data, input: Data) -> Data {
        Data(HMAC<SHA256>.authenticationCode(for: input, using: SymmetricKey(data: salt)))
    }

    private static func hkdfExpand(
        prk: Data,
        info: Data,
        outputCount: Int
    ) -> Data {
        var output = Data()
        var previous = Data()
        var counter: UInt8 = 1
        while output.count < outputCount {
            var input = Data()
            input.append(previous)
            input.append(info)
            input.append(counter)
            previous = Data(
                HMAC<SHA256>.authenticationCode(
                    for: input,
                    using: SymmetricKey(data: prk)
                )
            )
            output.append(previous)
            counter += 1
        }
        return Data(output.prefix(outputCount))
    }
}
