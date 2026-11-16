import Darwin
import Foundation

// The rendezvous answer and what the App accepts from it
// (docs/plans/remote-direct-rendezvous.md section 5). Everything here is
// pure: no socket, no endpoint, no permission. Nothing in the App calls it
// until a direct transport is registered, and `AvibeApp` registers none.
// Design: docs/plans/remote-direct-ios-transport.md.

/// An iroh endpoint ID in its wire form: an Ed25519 public key, written as
/// 64 lowercase hex characters.
struct DirectEndpointID: Hashable, Sendable {
    static let byteCount = 32

    let bytes: Data

    /// `nil` unless `bytes` holds exactly 32 bytes.
    init?(bytes: Data) {
        guard bytes.count == Self.byteCount else {
            return nil
        }
        self.bytes = Data(bytes)
    }

    /// `nil` unless `hex` is exactly 64 lowercase hex characters, the only
    /// form the rendezvous contract uses.
    init?(hex: String) {
        let characters = Array(hex.utf8)
        guard characters.count == Self.byteCount * 2 else {
            return nil
        }
        var bytes = Data(capacity: Self.byteCount)
        var index = 0
        while index < characters.count {
            guard
                let high = Self.nibble(characters[index]),
                let low = Self.nibble(characters[index + 1])
            else {
                return nil
            }
            bytes.append(high << 4 | low)
            index += 2
        }
        self.bytes = bytes
    }

    var hex: String {
        let digits = Array("0123456789abcdef".utf8)
        var characters: [UInt8] = []
        characters.reserveCapacity(bytes.count * 2)
        for byte in bytes {
            characters.append(digits[Int(byte >> 4)])
            characters.append(digits[Int(byte & 0x0F)])
        }
        return String(decoding: characters, as: UTF8.self)
    }

    private static func nibble(_ character: UInt8) -> UInt8? {
        switch character {
        case UInt8(ascii: "0")...UInt8(ascii: "9"):
            return character - UInt8(ascii: "0")
        case UInt8(ascii: "a")...UInt8(ascii: "f"):
            return character - UInt8(ascii: "a") + 10
        default:
            return nil
        }
    }
}

/// One address of the instance's endpoint that the App may send packets to.
///
/// The App holds the user's Local Network grant, so an answer must not be
/// able to point it at anything but the two classes the contract names: a
/// private IPv4 or unique-local IPv6 address (`lan`), or a global unicast
/// IPv6 address (`ipv6`). Everything else is dropped here, before any
/// endpoint exists: loopback, link-local, unspecified, multicast, shared
/// (CGNAT), documentation, IPv4-mapped, 6to4 and NAT64 forms, public IPv4,
/// names, zones, and an address whose class does not match its `kind`.
struct DirectCandidateAddress: Hashable, Sendable {
    enum Kind: String, Sendable {
        case lan
        case ipv6
    }

    let kind: Kind
    /// The numeric socket address, written again from the parsed bytes:
    /// `a.b.c.d:port` or `[x:x:x:x:x:x:x:x]:port`. What is dialled is what
    /// was checked, never the answer's own text.
    let socketAddress: String

    /// `nil` unless `address` is a numeric socket address of the class its
    /// `kind` names. An unknown kind is skipped, as the contract requires.
    static func admit(kind: String, address: String) -> DirectCandidateAddress? {
        guard
            let kind = Kind(rawValue: kind),
            let parsed = parse(address)
        else {
            return nil
        }
        switch parsed.host {
        case .ipv4(let octets):
            guard kind == .lan, isPrivateIPv4(octets) else {
                return nil
            }
            let host = octets.map { String($0) }.joined(separator: ".")
            return DirectCandidateAddress(
                kind: kind,
                socketAddress: "\(host):\(parsed.port)"
            )
        case .ipv6(let bytes):
            guard let actual = ipv6Kind(bytes), actual == kind else {
                return nil
            }
            var groups: [String] = []
            var index = 0
            while index < 16 {
                let group = UInt16(bytes[index]) << 8 | UInt16(bytes[index + 1])
                groups.append(String(group, radix: 16))
                index += 2
            }
            return DirectCandidateAddress(
                kind: kind,
                socketAddress: "[\(groups.joined(separator: ":"))]:\(parsed.port)"
            )
        }
    }

    private enum Host {
        case ipv4([UInt8])
        case ipv6([UInt8])
    }

    private static func parse(_ address: String) -> (host: Host, port: Int)? {
        // The longest text form of a socket address is well under this.
        guard address.utf8.count <= 64 else {
            return nil
        }
        if address.hasPrefix("[") {
            guard let close = address.range(of: "]:", options: .backwards) else {
                return nil
            }
            let host = String(address[address.index(after: address.startIndex)..<close.lowerBound])
            guard
                let port = parsePort(address[close.upperBound...]),
                let bytes = parseIPv6(host)
            else {
                return nil
            }
            return (.ipv6(bytes), port)
        }
        guard let colon = address.lastIndex(of: ":") else {
            return nil
        }
        guard
            let port = parsePort(address[address.index(after: colon)...]),
            let octets = parseIPv4(address[..<colon])
        else {
            return nil
        }
        return (.ipv4(octets), port)
    }

    private static func parsePort(_ text: Substring) -> Int? {
        guard
            (1...5).contains(text.utf8.count),
            text.utf8.allSatisfy(isDigit),
            // No leading zero: one address has one spelling.
            text.utf8.first != UInt8(ascii: "0"),
            let port = Int(text),
            (1...65_535).contains(port)
        else {
            return nil
        }
        return port
    }

    /// Four decimal octets, with no leading zeros and nothing else.
    private static func parseIPv4(_ text: Substring) -> [UInt8]? {
        let parts = text.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else {
            return nil
        }
        var octets: [UInt8] = []
        for part in parts {
            guard
                (1...3).contains(part.utf8.count),
                part.utf8.allSatisfy(isDigit),
                part.utf8.count == 1 || part.utf8.first != UInt8(ascii: "0"),
                let value = Int(part),
                value <= 255
            else {
                return nil
            }
            octets.append(UInt8(value))
        }
        return octets
    }

    /// The 16 bytes of an IPv6 address in any standard text form. A zone
    /// (`%en0`) is refused: the character check rejects it, and so does
    /// `inet_pton`.
    private static func parseIPv6(_ text: String) -> [UInt8]? {
        guard
            !text.isEmpty,
            text.utf8.allSatisfy({ byte in
                isDigit(byte)
                    || (UInt8(ascii: "a")...UInt8(ascii: "f")).contains(byte)
                    || (UInt8(ascii: "A")...UInt8(ascii: "F")).contains(byte)
                    || byte == UInt8(ascii: ":")
                    || byte == UInt8(ascii: ".")
            })
        else {
            return nil
        }
        var address = in6_addr()
        guard inet_pton(AF_INET6, text, &address) == 1 else {
            return nil
        }
        return withUnsafeBytes(of: address) { Array($0) }
    }

    private static func isDigit(_ byte: UInt8) -> Bool {
        (UInt8(ascii: "0")...UInt8(ascii: "9")).contains(byte)
    }

    /// 10/8, 172.16/12, and 192.168/16.
    private static func isPrivateIPv4(_ octets: [UInt8]) -> Bool {
        octets[0] == 10
            || (octets[0] == 172 && (16...31).contains(octets[1]))
            || (octets[0] == 192 && octets[1] == 168)
    }

    private static func ipv6Kind(_ bytes: [UInt8]) -> Kind? {
        guard bytes.count == 16 else {
            return nil
        }
        // Unique-local, fc00::/7.
        if bytes[0] & 0xFE == 0xFC {
            return .lan
        }
        // Global unicast is 2000::/3. That alone leaves out loopback,
        // unspecified, IPv4-mapped and NAT64 (64:ff9b::/96) forms,
        // link-local, site-local, and multicast.
        guard bytes[0] & 0xE0 == 0x20 else {
            return nil
        }
        // 2001::/23: protocol assignments (Teredo, benchmarking, ORCHID).
        if bytes[0] == 0x20, bytes[1] == 0x01, bytes[2] & 0xFE == 0 {
            return nil
        }
        // 2001:db8::/32 and 3fff::/20: documentation.
        if bytes[0] == 0x20, bytes[1] == 0x01, bytes[2] == 0x0D, bytes[3] == 0xB8 {
            return nil
        }
        if bytes[0] == 0x3F, bytes[1] == 0xFF, bytes[2] & 0xF0 == 0 {
            return nil
        }
        // 2002::/16: 6to4, a public IPv4 address in another form.
        if bytes[0] == 0x20, bytes[1] == 0x02 {
            return nil
        }
        return .ipv6
    }
}

/// A `ready` answer the App accepted: whom to dial, where, and for how long.
struct DirectRendezvousOffer: Equatable, Sendable {
    /// Opaque. Changes when the instance's endpoint key or address set does.
    let generation: String
    /// The instance's endpoint. The App dials this identity and no other.
    let endpointID: DirectEndpointID
    /// At least one and at most eight addresses, each checked by the App.
    let addresses: [DirectCandidateAddress]
    /// How long the answer and the client's admission last, counted from
    /// the moment its request was sent: at most 300 seconds.
    let lifetimeSeconds: Int
}

enum DirectRendezvousAnswer: Equatable, Sendable {
    case ready(DirectRendezvousOffer)
    /// No direct path for now; ask again after this many seconds.
    case unavailable(retryAfterSeconds: Int)
}

/// What an answer must agree with: the instance the user selected and the
/// endpoint ID this launch sent.
struct DirectRendezvousExpectation: Equatable, Sendable {
    let instanceID: String
    let hostname: String
    let clientEndpointID: DirectEndpointID
}

/// Why an answer was not accepted. Every one of them leaves the App on the
/// public origin; none is shown to the user.
enum DirectRendezvousRejection: Error, Equatable {
    case malformed
    case unsupportedVersion
    case wrongInstance
    case wrongHostname
    case wrongProtocol
    case wrongClient
    case invalidEndpoint
    case invalidGeneration
    case invalidLifetime
    case noAdmittedAddress
}

enum DirectRendezvous {
    static let version = 1
    /// The application protocol the instance's endpoint accepts.
    static let alpn = "avibe-direct/1"
    static let path = "/api/direct/rendezvous"
    /// The most addresses the App dials for one answer.
    static let maxAddresses = 8
    /// The entries of an answer's address list that are looked at. The
    /// contract lists at most eight; the margin is for kinds a later
    /// version adds, which this one skips.
    static let maxListedAddresses = 32
    /// The answer's lifetime in the contract. A longer one is cut to this.
    static let maxLifetimeSeconds = 300
    /// The largest answer body that is read.
    static let maxAnswerBytes = 16 * 1024
    static let retryAfterSecondsRange = 30...900
    static let defaultRetryAfterSeconds = 60
    private static let maxGenerationBytes = 128

    /// The request body: the protocol version and this launch's endpoint ID.
    static func requestBody(clientEndpointID: DirectEndpointID) -> String {
        "{\"version\":\(version),\"client_endpoint_id\":\"\(clientEndpointID.hex)\"}"
    }

    /// Reads the answer native code fetched for itself and checks it against
    /// the selected instance, whichever its status. Unknown fields are
    /// ignored; unknown address kinds and malformed address entries are
    /// skipped.
    static func answer(
        from data: Data,
        expecting expectation: DirectRendezvousExpectation
    ) throws -> DirectRendezvousAnswer {
        guard
            data.count <= maxAnswerBytes,
            let wire = try? JSONDecoder().decode(Wire.self, from: data)
        else {
            throw DirectRendezvousRejection.malformed
        }
        guard wire.version == version else {
            throw DirectRendezvousRejection.unsupportedVersion
        }
        guard wire.status == "ready" || wire.status == "unavailable" else {
            throw DirectRendezvousRejection.malformed
        }
        // Either answer speaks for one instance. An answer that names
        // another one, or none, says nothing about the selected instance:
        // it must not bring a path up, and it must not take one down.
        guard
            let instanceID = wire.instanceID,
            let hostname = wire.hostname
        else {
            throw DirectRendezvousRejection.malformed
        }
        guard instanceID == expectation.instanceID else {
            throw DirectRendezvousRejection.wrongInstance
        }
        guard hostname == expectation.hostname else {
            throw DirectRendezvousRejection.wrongHostname
        }
        guard wire.status == "ready" else {
            let requested = wire.retryAfterSeconds ?? defaultRetryAfterSeconds
            return .unavailable(
                retryAfterSeconds: min(
                    max(requested, retryAfterSecondsRange.lowerBound),
                    retryAfterSecondsRange.upperBound
                )
            )
        }
        return .ready(try offer(from: wire, expecting: expectation))
    }

    private static func offer(
        from wire: Wire,
        expecting expectation: DirectRendezvousExpectation
    ) throws -> DirectRendezvousOffer {
        guard
            let generation = wire.generation,
            let issuedAt = wire.issuedAt,
            let expiresAt = wire.expiresAt,
            let alpn = wire.alpn,
            let endpoint = wire.endpoint,
            let client = wire.client
        else {
            throw DirectRendezvousRejection.malformed
        }
        guard alpn == Self.alpn else {
            throw DirectRendezvousRejection.wrongProtocol
        }
        // The admission is for the endpoint this launch sent, or it is not
        // ours to use.
        guard client.endpointID == expectation.clientEndpointID.hex else {
            throw DirectRendezvousRejection.wrongClient
        }
        guard
            !generation.isEmpty,
            generation.utf8.count <= maxGenerationBytes
        else {
            throw DirectRendezvousRejection.invalidGeneration
        }
        guard
            let endpointID = DirectEndpointID(hex: endpoint.id),
            endpointID != expectation.clientEndpointID
        else {
            throw DirectRendezvousRejection.invalidEndpoint
        }
        // The lifetime is the answer's own span, which the caller counts
        // from the moment it sent the request. The phone's wall clock is not
        // compared with the instance's: a wrong clock must neither refuse a
        // fresh answer nor keep a stale one.
        guard
            issuedAt >= 0,
            expiresAt > issuedAt,
            client.admittedUntil > issuedAt
        else {
            throw DirectRendezvousRejection.invalidLifetime
        }
        let lifetime = min(
            expiresAt - issuedAt,
            client.admittedUntil - issuedAt,
            maxLifetimeSeconds
        )

        var addresses: [DirectCandidateAddress] = []
        for entry in endpoint.addresses.prefix(maxListedAddresses) {
            guard
                addresses.count < maxAddresses,
                let kind = entry.kind,
                let address = entry.address,
                let candidate = DirectCandidateAddress.admit(kind: kind, address: address),
                !addresses.contains(where: { $0.socketAddress == candidate.socketAddress })
            else {
                continue
            }
            addresses.append(candidate)
        }
        guard !addresses.isEmpty else {
            throw DirectRendezvousRejection.noAdmittedAddress
        }
        // `endpoint.relay` is not read: this version uses no relay.
        return DirectRendezvousOffer(
            generation: generation,
            endpointID: endpointID,
            addresses: addresses,
            lifetimeSeconds: lifetime
        )
    }

    private struct Wire: Decodable {
        struct Endpoint: Decodable {
            let id: String
            let addresses: [Address]
        }

        /// One entry of the address list. An entry of any other shape is
        /// one this version does not know, so it decodes as empty and is
        /// skipped instead of failing the answer.
        struct Address: Decodable {
            let kind: String?
            let address: String?

            enum CodingKeys: String, CodingKey {
                case kind
                case address
            }

            init(from decoder: Decoder) throws {
                guard let container = try? decoder.container(keyedBy: CodingKeys.self) else {
                    kind = nil
                    address = nil
                    return
                }
                kind = try? container.decode(String.self, forKey: .kind)
                address = try? container.decode(String.self, forKey: .address)
            }
        }

        struct Client: Decodable {
            let endpointID: String
            let admittedUntil: Int

            enum CodingKeys: String, CodingKey {
                case endpointID = "endpoint_id"
                case admittedUntil = "admitted_until"
            }
        }

        let version: Int
        let status: String
        let instanceID: String?
        let hostname: String?
        let generation: String?
        let issuedAt: Int?
        let expiresAt: Int?
        let alpn: String?
        let endpoint: Endpoint?
        let client: Client?
        let retryAfterSeconds: Int?

        enum CodingKeys: String, CodingKey {
            case version
            case status
            case instanceID = "instance_id"
            case hostname
            case generation
            case issuedAt = "issued_at"
            case expiresAt = "expires_at"
            case alpn
            case endpoint
            case client
            case retryAfterSeconds = "retry_after_seconds"
        }
    }
}
