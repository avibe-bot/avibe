import Foundation
import Network
@testable import Avibe

/// Test-only schema-2 control client. No WebView, installed runtime or credentials.
enum InteropFault: Error {
    case invalid(String)
    case deadline
}

func interopRequire(_ value: Bool, _ reason: String) throws {
    if !value { throw InteropFault.invalid(reason) }
}

indirect enum InteropJSON: Codable, Equatable, Sendable {
    case object([String: InteropJSON]), array([InteropJSON]), string(String), integer(Int), bool(Bool), null

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Int.self) { self = .integer(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([String: InteropJSON].self) { self = .object(v) }
        else { self = .array(try c.decode([InteropJSON].self)) }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .object(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .string(let v): try c.encode(v)
        case .integer(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }

    var text: String? { if case .string(let v) = self { return v }; return nil }
    var number: Int? { if case .integer(let v) = self { return v }; return nil }
    var list: [InteropJSON]? { if case .array(let v) = self { return v }; return nil }
    var fields: [String: InteropJSON]? { if case .object(let v) = self { return v }; return nil }

    func encoded() throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return try encoder.encode(self)
    }
}

enum InteropWire {
    static let names = ["admitted", "not_admitted", "wrong_identity", "refused_connect", "admission_ends", "sidecar_stops"]
    static let payload = Data("avibe-direct-interop-v2\n".utf8)
    static let limit = 4096

    static func requireEmptyTransportSkip(name: String, completedCases: Int) throws {
        try interopRequire(name == "admitted" && completedCases == 0, "addresses lost after transport coverage began")
    }

    static func negativeObservation(name: String, failure: String?, localStreams: Int,
                                    successfulConnects: Int, payloadStreams: Int, usable: Bool) throws -> [String: InteropJSON] {
        let allowed = name == "wrong_identity" ? ["handshake"] : ["handshake", "closed_by_peer"]
        try interopRequire(["wrong_identity", "not_admitted"].contains(name)
            && allowed.contains(failure ?? "") && localStreams >= 0 && payloadStreams == 0 && !usable,
            "unqualified negative observation")
        if name == "wrong_identity" {
            try interopRequire(successfulConnects == 0 && localStreams == 0, "wrong identity established or opened a stream")
        }
        return ["failure": .string(failure!), "local_streams": .integer(localStreams),
                "payload_streams": .integer(payloadStreams), "usable": .bool(usable)]
    }

    static func message(_ type: String, name: String? = nil) -> [String: InteropJSON] {
        var fields: [String: InteropJSON] = ["type": .string(type), "schema": .integer(2)]
        if let name { fields["name"] = .string(name) }
        return fields
    }

    static func decode(_ line: Data) throws -> [String: InteropJSON] {
        try interopRequire(line.count <= limit && line.last == 10, "oversized or truncated control line")
        // JSONDecoder accepts duplicate object keys. Check every nesting level
        // before decoding, including escaped spellings of the same key.
        var scanner = UniqueKeys(bytes: Array(line))
        try scanner.value(depth: 0)
        scanner.space()
        try interopRequire(scanner.index == scanner.bytes.count, "trailing control bytes")
        let decoded = try JSONDecoder().decode(InteropJSON.self, from: line)
        guard let fields = decoded.fields else { throw InteropFault.invalid("control object required") }
        try interopRequire(fields["schema"] == .integer(2), "wrong schema")
        try interopRequire(["hello", "next", "case", "setup_done", "armed", "triggered", "result", "verdict", "done"].contains(fields["type"]?.text ?? ""), "unknown message")
        return fields
    }

    static func expect(_ fields: [String: InteropJSON], type: String, name: String? = nil) throws {
        try interopRequire(fields["type"] == .string(type) && fields["schema"] == .integer(2), "unexpected control message")
        if let name { try interopRequire(fields["name"] == .string(name), "stale or wrong case") }
        if type == "setup_done" || type == "triggered" || type == "done" {
            let expected: Set<String> = name == nil ? ["type", "schema"] : ["type", "schema", "name"]
            try interopRequire(Set(fields.keys) == expected, "unexpected barrier fields")
        }
    }

    private struct UniqueKeys {
        let bytes: [UInt8]
        var index = 0
        mutating func space() { while index < bytes.count && [9, 10, 13, 32].contains(bytes[index]) { index += 1 } }
        mutating func take(_ byte: UInt8) throws {
            space()
            try interopRequire(index < bytes.count && bytes[index] == byte, "invalid JSON delimiter")
            index += 1
        }
        mutating func string() throws -> String {
            space()
            let start = index
            try take(34)
            while index < bytes.count {
                let byte = bytes[index]
                index += 1
                if byte == 92 { index += 1 }
                else if byte == 34 {
                    return try JSONDecoder().decode(String.self, from: Data(bytes[start..<index]))
                }
            }
            throw InteropFault.invalid("unterminated JSON string")
        }
        mutating func value(depth: Int) throws {
            space()
            try interopRequire(depth < 20 && index < bytes.count, "JSON depth or truncation")
            switch bytes[index] {
            case 123:
                index += 1
                var keys = Set<String>()
                space()
                if index < bytes.count && bytes[index] == 125 { index += 1; return }
                while true {
                    let key = try string()
                    try interopRequire(keys.insert(key).inserted, "duplicate JSON key")
                    try take(58)
                    try value(depth: depth + 1)
                    space()
                    if index < bytes.count && bytes[index] == 125 { index += 1; return }
                    try take(44)
                }
            case 91:
                index += 1
                space()
                if index < bytes.count && bytes[index] == 93 { index += 1; return }
                while true {
                    try value(depth: depth + 1)
                    space()
                    if index < bytes.count && bytes[index] == 93 { index += 1; return }
                    try take(44)
                }
            case 34: _ = try string()
            default:
                let start = index
                while index < bytes.count && ![9, 10, 13, 32, 44, 93, 125].contains(bytes[index]) { index += 1 }
                try interopRequire(index > start, "invalid JSON scalar")
            }
        }
    }
}

struct InteropBudget: Sendable {
    let end: ContinuousClock.Instant
    init(seconds: Int) { end = ContinuousClock.now.advanced(by: .seconds(seconds)) }
    var remaining: Double {
        let c = ContinuousClock.now.duration(to: end).components
        return Double(c.seconds) + Double(c.attoseconds) / 1e18
    }
    func run<T: Sendable>(_ operation: @escaping @Sendable () async throws -> T) async throws -> T {
        let seconds = remaining
        guard seconds > 0 else { throw InteropFault.deadline }
        return try await directIrohDeadline(seconds) {
            try await directIrohCall(allowed: { self.remaining > 0 }, operation)
        }
    }
}

/// A NW state callback may fire more than once; only one resumes its waiter.
private final class InteropOnce: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Void, Error>?
    init(_ continuation: CheckedContinuation<Void, Error>) { self.continuation = continuation }
    func finish(_ result: Result<Void, Error>) {
        let c = lock.withLock { let c = continuation; continuation = nil; return c }
        c?.resume(with: result)
    }
}

actor InteropControl {
    private let connection: NWConnection
    private var buffer = Data()
    init(address: String) throws {
        let pieces = address.split(separator: ":", omittingEmptySubsequences: false)
        guard pieces.count == 2, pieces[0] == "127.0.0.1", let port = UInt16(pieces[1]), port > 0,
              let nwPort = NWEndpoint.Port(rawValue: port) else { throw InteropFault.invalid("control must be loopback TCP") }
        connection = NWConnection(host: "127.0.0.1", port: nwPort, using: .tcp)
    }
    func connect() async throws {
        let c = connection
        try await InteropBudget(seconds: 5).run {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                let once = InteropOnce(continuation)
                c.stateUpdateHandler = { state in
                    switch state {
                    case .ready: once.finish(.success(()))
                    case .failed(let error): once.finish(.failure(error))
                    case .cancelled: once.finish(.failure(DirectIrohError.cancelled))
                    default: break
                    }
                }
                c.start(queue: DispatchQueue(label: "avibe.interop.control"))
            }
        }
    }
    func close() { connection.cancel() }
    func send(_ fields: [String: InteropJSON], budget: InteropBudget) async throws {
        var bytes = try InteropJSON.object(fields).encoded()
        bytes.append(10)
        try interopRequire(bytes.count <= InteropWire.limit, "oversized outgoing control message")
        let data = bytes
        let c = connection
        try await budget.run {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                c.send(content: data, completion: .contentProcessed { error in
                    if let error { continuation.resume(throwing: error) }
                    else { continuation.resume() }
                })
            }
        }
    }
    func receive(budget: InteropBudget) async throws -> [String: InteropJSON] {
        while true {
            if let newline = buffer.firstIndex(of: 10) {
                let line = Data(buffer[...newline])
                buffer.removeSubrange(...newline)
                return try InteropWire.decode(line)
            }
            try interopRequire(buffer.count < InteropWire.limit, "control line too long")
            let c = connection
            let bytes: Data = try await budget.run {
                try await withCheckedThrowingContinuation { continuation in
                    c.receive(minimumIncompleteLength: 1, maximumLength: InteropWire.limit) { data, _, complete, error in
                        if let error { continuation.resume(throwing: error) }
                        else if let data, !data.isEmpty { continuation.resume(returning: data) }
                        else { continuation.resume(throwing: InteropFault.invalid(complete ? "truncated control EOF" : "empty control read")) }
                    }
                }
            }
            buffer.append(bytes)
        }
    }
}
