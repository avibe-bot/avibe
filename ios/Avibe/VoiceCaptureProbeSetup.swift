#if DEBUG
import AppIntents
import Foundation
import WebKit

// DRAFT-ONLY CI PROBE (not for merge): lets the Simulator CI run the
// background voice intent against a local stand-in instance.

/// Writes a probe target (a stand-in instance on the CI host and a fresh
/// WebKit store) and signs that store in by loading the stand-in's
/// `/probe/login`, which sets a session cookie. The background intent then
/// uses this target instead of the account selection.
@MainActor
enum VoiceCaptureProbeSetup {
    struct Config: Codable {
        let origin: String
        let storeIdentifier: String
    }

    nonisolated static var configURL: URL? {
        try? FileManager.default.url(
            for: .cachesDirectory, in: .userDomainMask,
            appropriateFor: nil, create: true
        ).appending(path: "voice-capture-probe.json")
    }

    nonisolated static func target() -> VoiceCaptureTarget? {
        guard
            let url = configURL,
            let data = try? Data(contentsOf: url),
            let config = try? JSONDecoder().decode(Config.self, from: data),
            let originURL = URL(string: config.origin),
            let origin = WebOrigin(url: originURL, requiresHTTPS: false),
            let identifier = UUID(uuidString: config.storeIdentifier)
        else {
            return nil
        }
        return VoiceCaptureTarget(
            accountID: "probe",
            instanceID: "probe",
            origin: origin,
            storeIdentifier: identifier
        )
    }

    private static var kept: (WKWebView, Delegate)?

    static func runIfRequested() {
        let arguments = ProcessInfo.processInfo.arguments
        guard
            let index = arguments.firstIndex(of: "-voiceCaptureProbeSetup"),
            index + 1 < arguments.count,
            let originURL = URL(string: arguments[index + 1])
        else {
            return
        }
        let identifier = UUID()
        let config = Config(origin: arguments[index + 1], storeIdentifier: identifier.uuidString)
        if let url = configURL, let data = try? JSONEncoder().encode(config) {
            try? data.write(to: url)
        }
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = WKWebsiteDataStore(forIdentifier: identifier)
        let webView = WKWebView(frame: .zero, configuration: configuration)
        let delegate = Delegate()
        webView.navigationDelegate = delegate
        kept = (webView, delegate)
        webView.load(URLRequest(url: originURL.appending(path: "probe/login")))
        VoiceCaptureProbe.record("probe setup: loading login into store \(identifier.uuidString)")
    }

    final class Delegate: NSObject, WKNavigationDelegate {
        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            Task { @MainActor in
                let cookies = await webView.configuration.websiteDataStore
                    .httpCookieStore.allCookies()
                VoiceCaptureProbe.record(
                    "probe setup: login loaded, cookies=\(cookies.map(\.name).sorted())"
                )
            }
        }

        func webView(
            _ webView: WKWebView,
            didFailProvisionalNavigation navigation: WKNavigation!,
            withError error: Error
        ) {
            Task { @MainActor in
                VoiceCaptureProbe.record("probe setup: login failed \(error)")
            }
        }
    }
}

#endif
