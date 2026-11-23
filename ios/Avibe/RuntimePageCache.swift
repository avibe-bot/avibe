import CryptoKit
import Foundation

/// The last instance document the App showed, kept so a cold start can show
/// it at once (cold-start plan, step 2). Each entry is one real response from
/// the instance: its status, its headers and its bytes, captured by the page
/// fetching its own URL right after a live load, and kept only when its
/// security headers equal the live navigation response's (`capture`). The
/// cache is per account and instance origin, and the whole cache is removed
/// when the account changes or signs out.
struct RuntimeCachedPage: Codable, Equatable {
    let url: URL
    /// Header names as the instance sent them, minus the ones that describe
    /// the transfer rather than the document (see `transferHeaders`).
    let headers: [String: String]
    let body: Data
    let capturedAt: Date
    /// The stored bytes carry `<meta name="avibe-cached-shell" content="1">`:
    /// that Web UI keeps its shell while its instance cannot be reached, so
    /// the App may show it uncovered while connecting. Read from the bytes
    /// when the page is captured; absent (nil) in entries from before.
    var keepsShellUnreachable: Bool? = nil

    /// Whether the App may show this page uncovered before its instance
    /// answers.
    var keepsShell: Bool { keepsShellUnreachable == true }

    func response() -> HTTPURLResponse? {
        HTTPURLResponse(url: url, statusCode: 200, httpVersion: "HTTP/1.1", headerFields: headers)
    }
}

enum RuntimePageCapture {
    /// The headers that decide what the document may do. A captured response
    /// is kept only when each of these is byte-identical to the live
    /// navigation response's (present in both with the same value, or absent
    /// from both).
    static let securityHeaders: Set<String> = [
        "content-security-policy",
        "content-security-policy-report-only",
        "permissions-policy",
        "feature-policy",
        "document-policy",
        "cross-origin-opener-policy",
        "cross-origin-embedder-policy",
        "cross-origin-resource-policy",
        "x-frame-options",
        "x-content-type-options",
        "referrer-policy",
        "strict-transport-security",
        "origin-agent-cluster",
    ]

    /// Headers about how the bytes travelled. `fetch` hands the decoded body
    /// over, so these no longer describe what is stored.
    static let transferHeaders: Set<String> = [
        "content-encoding", "content-length", "transfer-encoding", "connection",
        "keep-alive", "set-cookie",
    ]

    /// Largest document kept; the Web UI's shell is far below it.
    static let maximumBodySize = 2 * 1_024 * 1_024

    /// The entry to keep from a live load, or nil (nothing is cached; the
    /// next live load tries again). `live` is the navigation response's
    /// headers; `status`, `fetched` and `body` are one response to the
    /// page's own fetch of the same URL.
    static func capture(
        url: URL,
        live: [String: String],
        status: Int,
        fetched: [String: String],
        body: Data
    ) -> RuntimeCachedPage? {
        guard
            status == 200,
            !body.isEmpty,
            body.count <= maximumBodySize,
            lowercased(fetched)["content-type"]?.lowercased().hasPrefix("text/html") == true
        else {
            return nil
        }
        let liveHeaders = lowercased(live)
        let fetchedHeaders = lowercased(fetched)
        for name in securityHeaders where liveHeaders[name] != fetchedHeaders[name] {
            return nil
        }
        let kept = fetched.filter { !transferHeaders.contains($0.key.lowercased()) }
        return RuntimeCachedPage(
            url: url, headers: kept, body: body, capturedAt: Date(),
            keepsShellUnreachable: declaresCachedShell(body)
        )
    }

    /// Whether the document declares `<meta name="avibe-cached-shell"
    /// content="1">` (ui/index.html), in either attribute order.
    static func declaresCachedShell(_ body: Data) -> Bool {
        guard let html = String(data: body.prefix(64 * 1_024), encoding: .utf8) else {
            return false
        }
        let pattern = #"<meta\s+(?:name\s*=\s*["']avibe-cached-shell["']\s+content\s*=\s*["']1["']|content\s*=\s*["']1["']\s+name\s*=\s*["']avibe-cached-shell["'])\s*/?>"#
        return html.range(of: pattern, options: [.regularExpression, .caseInsensitive]) != nil
    }

    /// Whether a fresh response to the same URL is still the stored page:
    /// status 200, the same bytes and the same security headers. If not,
    /// the entry is dropped and the next launch loads live.
    static func matches(
        _ page: RuntimeCachedPage,
        status: Int,
        fetched: [String: String],
        body: Data
    ) -> Bool {
        guard status == 200, body == page.body else { return false }
        let stored = lowercased(page.headers)
        let fresh = lowercased(fetched)
        return securityHeaders.allSatisfy { stored[$0] == fresh[$0] }
    }

    private static func lowercased(_ headers: [String: String]) -> [String: String] {
        Dictionary(headers.map { ($0.key.lowercased(), $0.value) }, uniquingKeysWith: { first, _ in first })
    }
}

/// What one runtime's controller may do with the cache: read its page, and
/// store a new one (the App checks the runtime is still current).
struct RuntimePageCacheAccess {
    let load: @MainActor () -> RuntimeCachedPage?
    let store: @MainActor (RuntimeCachedPage) -> Void
    var remove: @MainActor () -> Void = {}
}

/// Files under Application Support, protected until the first unlock after
/// a restart, never backed up.
@MainActor
final class RuntimePageCache {
    /// How long a stored page is used before a launch loads live again.
    static let maximumAge: TimeInterval = 24 * 60 * 60

    static var root: URL {
        FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("RuntimePageCache", isDirectory: true)
    }

    private let directory: URL
    private let fileManager = FileManager.default

    init(directory: URL) {
        self.directory = directory
    }

    func page(accountID: String, origin: String) -> RuntimeCachedPage? {
        guard let data = try? Data(contentsOf: fileURL(accountID: accountID, origin: origin)) else {
            return nil
        }
        return try? JSONDecoder().decode(RuntimeCachedPage.self, from: data)
    }

    func store(_ page: RuntimeCachedPage, accountID: String, origin: String) {
        let url = fileURL(accountID: accountID, origin: origin)
        do {
            try fileManager.createDirectory(
                at: url.deletingLastPathComponent(), withIntermediateDirectories: true
            )
            try JSONEncoder().encode(page).write(
                to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
            )
            var values = URLResourceValues()
            values.isExcludedFromBackup = true
            var root = Self.root
            try? root.setResourceValues(values)
        } catch {
            try? fileManager.removeItem(at: url)
        }
    }

    func removePage(accountID: String, origin: String) {
        try? fileManager.removeItem(at: fileURL(accountID: accountID, origin: origin))
    }

    /// This store's pages: a sign-out or an account change.
    func removeAll() {
        try? fileManager.removeItem(at: directory)
    }

    private func fileURL(accountID: String, origin: String) -> URL {
        directory
            .appendingPathComponent(Self.digest(accountID), isDirectory: true)
            .appendingPathComponent(Self.digest(origin) + ".json")
    }

    private static func digest(_ value: String) -> String {
        SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}
