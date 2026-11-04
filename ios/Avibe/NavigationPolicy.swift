import Foundation

enum NavigationDestination: Equatable {
    case instance
    case authentication
    case protectedVault
    case googleAuthentication
    case external
    case blocked
}

enum NavigationContext: Equatable {
    case primaryMainFrame
    case embeddedFrame
    case popupMainFrame
}

enum NavigationDisposition: Equatable {
    case allow
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

    private static let productionProtectedVaultOrigins: Set<WebOrigin> = [
        WebOrigin.parseConfigured("https://sandbox.avibe.bot"),
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
    let protectedVaultOrigins: Set<WebOrigin>
    let googleOrigins: Set<WebOrigin>

    init(
        instanceOrigins: Set<WebOrigin>,
        authenticationOrigins: Set<WebOrigin> = Self.productionAuthenticationOrigins,
        protectedVaultOrigins: Set<WebOrigin> = Self.productionProtectedVaultOrigins,
        googleOrigins: Set<WebOrigin> = Self.googleAuthenticationOrigins
    ) {
        self.instanceOrigins = instanceOrigins
        self.authenticationOrigins = authenticationOrigins
        self.protectedVaultOrigins = protectedVaultOrigins
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
        if protectedVaultOrigins.contains(origin) {
            return .protectedVault
        }
        if googleOrigins.contains(origin) {
            return .googleAuthentication
        }
        return origin.scheme == "http" || origin.scheme == "https" ? .external : .blocked
    }

    func disposition(for url: URL, context: NavigationContext) -> NavigationDisposition {
        if context == .popupMainFrame && isPopupBootstrap(url) {
            return .allow
        }
        switch destination(for: url) {
        case .instance, .authentication:
            return .allow
        case .protectedVault:
            return context == .primaryMainFrame ? .external : .allow
        case .googleAuthentication:
            return .googleAuthentication
        case .external:
            return .external
        case .blocked:
            return .blocked
        }
    }

    func permitsDownload(from url: URL) -> Bool {
        destination(for: url) == .instance
    }

    func isPopupBootstrap(_ url: URL) -> Bool {
        url.absoluteString.caseInsensitiveCompare("about:blank") == .orderedSame
    }
}
