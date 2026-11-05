import Foundation

struct WebOrigin: Hashable, Codable, Sendable {
    let scheme: String
    let host: String
    let port: Int?

    init?(scheme: String, host: String, port: Int?) {
        let normalizedScheme = scheme.lowercased()
        let normalizedHost = host.lowercased()
        guard !normalizedScheme.isEmpty, !normalizedHost.isEmpty else {
            return nil
        }
        self.scheme = normalizedScheme
        self.host = normalizedHost
        self.port = Self.normalizedPort(port, scheme: normalizedScheme)
    }

    init?(url: URL, requiresHTTPS: Bool = true) {
        guard
            let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
            let scheme = components.scheme?.lowercased(),
            let host = components.host?.lowercased(),
            components.user == nil,
            components.password == nil,
            !host.isEmpty,
            (!requiresHTTPS || scheme == "https")
        else {
            return nil
        }
        guard scheme == "http" || scheme == "https" else {
            return nil
        }
        self.init(scheme: scheme, host: host, port: components.port)
    }

    static func parseConfigured(_ input: String) -> WebOrigin? {
        let trimmed = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed) else {
            return nil
        }
        return WebOrigin(url: url, requiresHTTPS: true)
    }

    var url: URL {
        var components = URLComponents()
        components.scheme = scheme
        components.host = host
        components.port = port
        components.path = "/"
        return components.url!
    }

    private static func normalizedPort(_ port: Int?, scheme: String) -> Int? {
        switch (scheme, port) {
        case ("https", 443), ("http", 80):
            return nil
        default:
            return port
        }
    }
}
