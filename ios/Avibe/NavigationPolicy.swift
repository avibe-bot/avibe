import Foundation

enum NavigationDestination: Equatable {
    case instance
    case authentication
    case googleAuthentication
    case external
    case blocked
}

struct NavigationPolicy {
    private static let productionAuthenticationOrigins: Set<WebOrigin> = [
        WebOrigin.parseConfigured("https://avibe.bot"),
        WebOrigin.parseConfigured("https://mctppyrqmrjotgkrwdse.supabase.co"),
        WebOrigin.parseConfigured("https://github.com"),
    ].compactMap { $0 }.reduce(into: []) { result, origin in
        result.insert(origin)
    }

    private static let googleAuthenticationOrigins: Set<WebOrigin> = [
        WebOrigin.parseConfigured("https://accounts.google.com"),
        WebOrigin.parseConfigured("https://accounts.googleusercontent.com"),
    ].compactMap { $0 }.reduce(into: []) { result, origin in
        result.insert(origin)
    }

    let instanceOrigins: Set<WebOrigin>
    let authenticationOrigins: Set<WebOrigin>
    let googleOrigins: Set<WebOrigin>

    init(
        instanceOrigins: Set<WebOrigin>,
        authenticationOrigins: Set<WebOrigin> = Self.productionAuthenticationOrigins,
        googleOrigins: Set<WebOrigin> = Self.googleAuthenticationOrigins
    ) {
        self.instanceOrigins = instanceOrigins
        self.authenticationOrigins = authenticationOrigins
        self.googleOrigins = googleOrigins
    }

    func destination(for url: URL) -> NavigationDestination {
        guard let origin = WebOrigin(url: url, requiresHTTPS: false) else {
            return .blocked
        }
        if instanceOrigins.contains(origin) {
            return .instance
        }
        if authenticationOrigins.contains(origin) {
            return .authentication
        }
        if googleOrigins.contains(origin) {
            return .googleAuthentication
        }
        return origin.scheme == "http" || origin.scheme == "https" ? .external : .blocked
    }
}
