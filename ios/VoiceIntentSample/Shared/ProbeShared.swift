import ActivityKit
import Foundation

/// The Live Activity every probe App (and the helper) shows.
struct ProbeAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        var label: String
    }
    var app: String
}

#if !PROBE_WIDGET
/// Appends one line to Caches/probe.log, which CI collects per App.
enum ProbeLog {
    static func record(_ event: String) {
        guard let url = try? FileManager.default.url(
            for: .cachesDirectory, in: .userDomainMask, appropriateFor: nil, create: true
        ).appending(path: "probe.log") else { return }
        let stamp = ISO8601DateFormatter.string(
            from: Date(), timeZone: .gmt, formatOptions: [.withInternetDateTime, .withFractionalSeconds]
        )
        let line = "\(stamp) [pid \(ProcessInfo.processInfo.processIdentifier)] \(event)\n"
        if let handle = try? FileHandle(forWritingTo: url) {
            handle.seekToEndOfFile()
            handle.write(Data(line.utf8))
            try? handle.close()
        } else {
            try? Data(line.utf8).write(to: url)
        }
        print("PROBE: \(event)")
    }

    /// Domain, code, the four-character OSStatus where there is one, and
    /// the error's own description.
    static func describe(_ error: Error) -> String {
        let ns = error as NSError
        var fourcc = ""
        if ns.code > 0x20202020, ns.code < 0x7f7f7f7f {
            let bytes = withUnsafeBytes(of: UInt32(ns.code).bigEndian, Array.init)
            if bytes.allSatisfy({ $0 >= 0x20 && $0 < 0x7f }) {
                fourcc = " '\(String(decoding: bytes, as: UTF8.self))'"
            }
        }
        return "\(ns.domain) \(ns.code)\(fourcc) \(String(reflecting: error))"
    }
}
#endif
