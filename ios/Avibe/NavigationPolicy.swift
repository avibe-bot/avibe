import Foundation

/// A sign-in with another company's account. The App signs in only with
/// avibe.bot's own account system (email code, password, passkey), so these
/// are intercepted with an explanation that points at the email code.
enum ThirdPartySignIn: Equatable {
    /// Google also rejects sign-in inside embedded browsers.
    case google
    case github
}

/// Voice input's consent marker. An instance page that asks each account before
/// speech goes to a third-party AI service carries
/// `<meta name="avibe-voice-consent" content="1">` (ui/index.html). A page
/// without it predates that question, so the App gives it no microphone and
/// says to update the instance (App Store guideline 5.1.2(i)). A marked page
/// asks for itself, and the App adds no question of its own.
enum VoiceConsentMarker {
    /// Evaluated in the page's main frame when it asks for the microphone.
    static let script = #"document.querySelector('meta[name="avibe-voice-consent"]')?.content === "1""#

    /// Whether the script's result says the page carries the marker. Anything
    /// else, an error or a page that is gone included, reads as not marked.
    static func isPresent(_ result: Any?) -> Bool {
        (result as? Bool) == true
    }
}

enum NavigationDestination: Equatable {
    case instance
    case authentication
    case thirdPartySignIn(ThirdPartySignIn)
    case external
    case browserInternal
}

enum NavigationContext: Equatable {
    case primaryMainFrame
    case popupMainFrame
}

enum NavigationDisposition: Equatable {
    case allow
    case thirdPartySignIn(ThirdPartySignIn)
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
    case thirdPartySignIn(ThirdPartySignIn)
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
        // Supabase custom domain for the same project. It serves Auth on our
        // hostname for networks that reset `*.supabase.co`; the default
        // domain stays valid during and after the switch.
        WebOrigin.parseConfigured("https://auth.avibe.bot"),
    ].compactMap { $0 }.reduce(into: []) { result, origin in
        result.insert(origin)
    }

    private static let googleAuthenticationOrigins: Set<WebOrigin> = [
        WebOrigin.parseConfigured("https://accounts.google.com"),
        WebOrigin.parseConfigured("https://accounts.googleusercontent.com"),
    ].compactMap { $0 }.reduce(into: []) { result, origin in
        result.insert(origin)
    }

    /// GitHub's OAuth authorization endpoints, intercepted only for
    /// avibe.bot's own sign-in. The rest of github.com is an ordinary
    /// external site, including the device-code page (`/login/device`) that
    /// an instance's Copilot sign-in opens.
    private static let githubOrigin = WebOrigin.parseConfigured("https://github.com")
    private static let githubAuthorizationPathPrefix = "/login/oauth/"

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
            return .thirdPartySignIn(.google)
        }
        if origin == Self.githubOrigin, isAvibeGitHubAuthorization(url) {
            return .thirdPartySignIn(.github)
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
        case .thirdPartySignIn(let provider):
            return .thirdPartySignIn(provider)
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
        case .thirdPartySignIn(let provider):
            return .thirdPartySignIn(provider)
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

    /// GitHub authorization for avibe.bot's own sign-in: it returns to an
    /// authentication origin (Supabase's `/auth/v1/callback`). GitHub sign-in
    /// for anything else, such as an MCP server an instance connects to, is
    /// an ordinary external page.
    private func isAvibeGitHubAuthorization(_ url: URL) -> Bool {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            return false
        }
        let path = components.percentEncodedPath.lowercased()
        guard path == "/login/oauth" || path.hasPrefix(Self.githubAuthorizationPathPrefix) else {
            return false
        }
        return components.queryItems?.contains { item in
            guard
                item.name == "redirect_uri",
                let value = item.value,
                let redirect = URL(string: value),
                let redirectOrigin = WebOrigin(url: redirect, requiresHTTPS: false)
            else {
                return false
            }
            return authenticationOrigins.contains(redirectOrigin)
        } == true
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
