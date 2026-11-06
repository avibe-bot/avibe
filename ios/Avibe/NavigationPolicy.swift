import Foundation

enum NavigationDestination: Equatable {
    case instance
    case authentication
    case googleAuthentication
    case external
    case browserInternal
}

enum NavigationContext: Equatable {
    case primaryMainFrame
    case popupMainFrame
}

enum NavigationDisposition: Equatable {
    case allow
    case googleAuthentication
    case external
    case externalApplication
}

enum SubframeNavigationDisposition: Equatable {
    case allow
    case cancel
}

enum NewWindowDisposition: Equatable {
    case childWebView
    case externalApplication
    case googleAuthentication
}

enum DownloadDisposition: Equatable {
    case download
    case externalBrowser
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
            return .browserInternal
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
        return origin.scheme == "http" || origin.scheme == "https"
            ? .external
            : .browserInternal
    }

    func disposition(for url: URL, context: NavigationContext) -> NavigationDisposition {
        if isExternalApplicationURL(url) {
            return .externalApplication
        }
        switch destination(for: url) {
        case .googleAuthentication:
            return .googleAuthentication
        case .external:
            return context == .primaryMainFrame ? .external : .allow
        case .instance, .authentication, .browserInternal:
            return .allow
        }
    }

    func allowsBackForwardNavigation(
        to url: URL,
        context: NavigationContext
    ) -> Bool {
        context != .primaryMainFrame || destination(for: url) == .instance
    }

    func subframeDisposition(for url: URL) -> SubframeNavigationDisposition {
        isExternalApplicationURL(url) ? .cancel : .allow
    }

    func newWindowDisposition(for url: URL) -> NewWindowDisposition {
        if isExternalApplicationURL(url) {
            return .externalApplication
        }
        switch destination(for: url) {
        case .googleAuthentication:
            return .googleAuthentication
        case .instance, .authentication, .external, .browserInternal:
            return .childWebView
        }
    }

    func downloadDisposition(
        for url: URL,
        sourceOrigin: WebOrigin?,
        trustedMainFrame: Bool
    ) -> DownloadDisposition {
        guard trustedMainFrame else {
            return .blocked
        }

        switch url.scheme?.lowercased() {
        case "https":
            return WebOrigin(url: url, requiresHTTPS: false).map(instanceOrigins.contains) == true
                ? .download
                : .externalBrowser
        case "http":
            return .externalBrowser
        case "blob":
            guard
                let embeddedURL = URL(string: String(url.absoluteString.dropFirst("blob:".count))),
                let embeddedOrigin = WebOrigin(url: embeddedURL, requiresHTTPS: false),
                instanceOrigins.contains(embeddedOrigin)
            else {
                return .blocked
            }
            return .download
        case "data":
            return sourceOrigin.map(instanceOrigins.contains) == true ? .download : .blocked
        default:
            return .blocked
        }
    }

    private func isExternalApplicationURL(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased(), !scheme.isEmpty else {
            return false
        }
        return ![
            "http",
            "https",
            "about",
            "blob",
            "data",
            "javascript",
            "file",
        ].contains(scheme)
    }
}
