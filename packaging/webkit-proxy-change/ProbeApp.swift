import Foundation
import Network
import Security
import UIKit
import WebKit

@main
final class ProbeApp: UIResponder, UIApplicationDelegate {
    var window: UIWindow?

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions options: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        let window = UIWindow(frame: UIScreen.main.bounds)
        window.rootViewController = UIViewController()
        window.rootViewController?.view.backgroundColor = .white
        window.makeKeyAndVisible()
        self.window = window
        if let request = LifecycleRequest(arguments: ProcessInfo.processInfo.arguments) {
            Task { @MainActor in
                await LifecycleRunner(request: request, window: window).run()
            }
        }
        return true
    }
}

private struct LifecycleRequest {
    let caseID: String
    let phase: String
    let storeID: UUID
    let controlURL: URL

    init?(arguments: [String]) {
        func value(_ flag: String) -> String? {
            guard let index = arguments.firstIndex(of: flag), arguments.indices.contains(index + 1) else { return nil }
            return arguments[index + 1]
        }
        guard let caseID = value("--lifecycle-case"),
              ["lifecycle-reload", "lifecycle-recreate", "lifecycle-relaunch"].contains(caseID),
              let phase = value("--lifecycle-phase"),
              ["single", "prepare", "resume"].contains(phase),
              let storeID = value("--lifecycle-store-id").flatMap(UUID.init),
              let controlURL = value("--lifecycle-control-url").flatMap(URL.init(string:))
        else { return nil }
        self.caseID = caseID
        self.phase = phase
        self.storeID = storeID
        self.controlURL = controlURL
    }

    var host: String { "\(caseID).webkit-proxy.test" }
}

@MainActor
private final class LifecycleRunner: NSObject, WKNavigationDelegate {
    private let request: LifecycleRequest
    private let window: UIWindow
    private let controller = UIViewController()
    private let store: WKWebsiteDataStore
    private let anchor: SecCertificate
    private var webView: WKWebView
    private var navigation: CheckedContinuation<Void, Error>?
    private var navigationDeadline: Task<Void, Never>?
    private var javascript: CheckedContinuation<[String: Any], Error>?
    private var javascriptDeadline: Task<Void, Never>?

    init(request: LifecycleRequest, window: UIWindow) {
        self.request = request
        self.window = window
        self.store = WKWebsiteDataStore(forIdentifier: request.storeID)
        let anchorData = (Bundle.main.url(forResource: "Anchor", withExtension: "der"))
            .flatMap { try? Data(contentsOf: $0) } ?? Data()
        self.anchor = SecCertificateCreateWithData(nil, anchorData as CFData)!
        self.webView = WKWebView(
            frame: CGRect(x: 0, y: 0, width: 390, height: 700),
            configuration: Self.configuration(store: store)
        )
        super.init()
        window.rootViewController = controller
        controller.view.addSubview(webView)
        webView.navigationDelegate = self
    }

    func run() async {
        do {
            if request.phase == "resume" {
                try await routeB()
                try await load("\(request.caseID)-route-b")
                let routeBPage = try await page()
                let routeBFetch = try await fetch("\(request.caseID)-route-b-fetch")
                let pageUsesRouteB = usesRouteB(routeBPage)
                let fetchUsesRouteB = usesRouteB(routeBFetch)
                try await record("lifecycle_result", [
                    "routeB": ["page": routeBPage, "fetch": routeBFetch],
                    "newTrafficUsesRouteB": pageUsesRouteB && fetchUsesRouteB,
                    "newTrafficOperationsUseRouteB": ["page": pageUsesRouteB, "fetch": fetchUsesRouteB],
                    "newTrafficRouteOutcome": routeOutcome(pageUsesRouteB, fetchUsesRouteB),
                ])
                try await record("lifecycle_done", ["newTrafficUsesRouteB": pageUsesRouteB && fetchUsesRouteB])
                return
            }

            store.proxyConfigurations = []
            try await load("\(request.caseID)-route-a")
            let routeAPage = try await page()
            let routeAWarmFetch = try await fetch("\(request.caseID)-route-a-fetch")
            guard routeAPage["route"] as? String == "origin",
                  routeAWarmFetch["route"] as? String == "origin",
                  routeAWarmFetch["ok"] as? Bool == true,
                  routeAPage["connection"] as? String == routeAWarmFetch["connection"] as? String
            else { throw LifecycleError.routeAPrecondition }
            let routeA = ["page": routeAPage, "warmFetch": routeAWarmFetch]
            try await record("lifecycle_initial", routeA)

            if request.phase == "prepare" {
                try await record("lifecycle_ready", ["routeA": routeA])
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(3600))
                }
                return
            }

            try await record("lifecycle_before_route_b", ["routeA": routeA])
            try await routeB()
            switch request.caseID {
            case "lifecycle-reload":
                try await reload()
            case "lifecycle-recreate":
                replaceWebView()
                try await load("\(request.caseID)-route-b")
            case "lifecycle-relaunch":
                throw LifecycleError.invalidPhase
            default:
                throw LifecycleError.invalidCase
            }
            let routeBPage = try await page()
            let routeBFetch = try await fetch("\(request.caseID)-route-b-fetch")
            let pageUsesRouteB = usesRouteB(routeBPage)
            let fetchUsesRouteB = usesRouteB(routeBFetch)
            try await record("lifecycle_result", [
                "routeA": routeA,
                "routeB": ["page": routeBPage, "fetch": routeBFetch],
                "newTrafficUsesRouteB": pageUsesRouteB && fetchUsesRouteB,
                "newTrafficOperationsUseRouteB": ["page": pageUsesRouteB, "fetch": fetchUsesRouteB],
                "newTrafficRouteOutcome": routeOutcome(pageUsesRouteB, fetchUsesRouteB),
            ])
            try await record("lifecycle_done", ["newTrafficUsesRouteB": pageUsesRouteB && fetchUsesRouteB])
        } catch {
            try? await record("lifecycle_error", ["error": String(describing: error)])
        }
    }

    private static func configuration(store: WKWebsiteDataStore) -> WKWebViewConfiguration {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = store
        return configuration
    }

    private static func proxy(for host: String) -> ProxyConfiguration {
        var proxy = ProxyConfiguration(httpCONNECTProxy: .hostPort(
            host: "127.0.0.1", port: 18080
        ))
        proxy.matchDomains = [host]
        proxy.allowFailover = false
        return proxy
    }

    private func routeB() async throws {
        store.proxyConfigurations = [Self.proxy(for: request.host)]
        try await record("lifecycle_route_b_installed", ["mode": "proxy", "matchDomain": request.host])
    }

    private func replaceWebView() {
        webView.stopLoading()
        webView.navigationDelegate = nil
        webView.removeFromSuperview()
        webView = WKWebView(
            frame: CGRect(x: 0, y: 0, width: 390, height: 700),
            configuration: Self.configuration(store: store)
        )
        controller.view.addSubview(webView)
        webView.navigationDelegate = self
    }

    private func load(_ id: String) async throws {
        try await withCheckedThrowingContinuation { continuation in
            navigation = continuation
            navigationDeadline = Task { @MainActor [weak self] in
                do { try await Task.sleep(for: .seconds(20)) } catch { return }
                self?.completeNavigation(.failure(LifecycleError.navigationTimeout(id)))
            }
            webView.load(URLRequest(url: URL(string: "https://\(request.host)/page?id=\(id)")!))
        }
    }

    private func reload() async throws {
        try await withCheckedThrowingContinuation { continuation in
            navigation = continuation
            navigationDeadline = Task { @MainActor [weak self] in
                do { try await Task.sleep(for: .seconds(20)) } catch { return }
                self?.completeNavigation(.failure(LifecycleError.navigationTimeout("reload")))
            }
            webView.reload()
        }
    }

    private func page() async throws -> [String: Any] {
        try await evaluate("window.probe.page")
    }

    private func fetch(_ id: String) async throws -> [String: Any] {
        let result = try await evaluate("window.probe.fetch('\(id)')")
        guard result["ok"] as? Bool == true else { throw LifecycleError.fetchFailed }
        return result
    }

    private func evaluate(_ expression: String) async throws -> [String: Any] {
        try await withCheckedThrowingContinuation { continuation in
            javascript = continuation
            javascriptDeadline = Task { @MainActor [weak self] in
                do { try await Task.sleep(for: .seconds(12)) } catch { return }
                self?.completeJavaScript(.failure(LifecycleError.javascriptTimeout))
            }
            webView.callAsyncJavaScript(
                "return JSON.stringify(await (" + expression + "));",
                arguments: [:], in: nil, in: .page
            ) { [weak self] result in
                do {
                    let value = try result.get()
                    guard let text = value as? String,
                          let object = try JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any]
                    else { throw LifecycleError.invalidPage }
                    self?.completeJavaScript(.success(object))
                } catch { self?.completeJavaScript(.failure(error)) }
            }
        }
    }

    private func usesRouteB(_ result: [String: Any]) -> Bool {
        result["route"] as? String == "proxy-backend" && result["proxyConnection"] as? String != nil
    }

    private func routeOutcome(_ pageUsesRouteB: Bool, _ fetchUsesRouteB: Bool) -> String {
        switch (pageUsesRouteB, fetchUsesRouteB) {
        case (true, true): return "all-route-b"
        case (false, false): return "none-route-b"
        default: return "mixed"
        }
    }

    private func record(_ step: String, _ result: [String: Any]) async throws {
        let payload: [String: Any] = [
            "case": request.caseID,
            "step": step,
            "phase": request.phase,
            "appPID": ProcessInfo.processInfo.processIdentifier,
            "result": result,
            "replaySafety": "unmeasured: GET only",
        ]
        var urlRequest = URLRequest(url: request.controlURL.appendingPathComponent("log"))
        urlRequest.httpMethod = "POST"
        urlRequest.httpBody = try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        urlRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (_, response) = try await URLSession.shared.data(for: urlRequest)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            throw LifecycleError.controlFailure
        }
    }

    private func completeNavigation(_ result: Result<Void, Error>) {
        navigationDeadline?.cancel()
        navigationDeadline = nil
        let continuation = navigation
        navigation = nil
        continuation?.resume(with: result)
    }

    private func completeJavaScript(_ result: Result<[String: Any], Error>) {
        javascriptDeadline?.cancel()
        javascriptDeadline = nil
        let continuation = javascript
        javascript = nil
        continuation?.resume(with: result)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        completeNavigation(.success(()))
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        completeNavigation(.failure(error))
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        completeNavigation(.failure(error))
    }

    func webView(
        _ webView: WKWebView,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              challenge.protectionSpace.host.hasSuffix(".webkit-proxy.test"),
              let trust = challenge.protectionSpace.serverTrust
        else { completionHandler(.performDefaultHandling, nil); return }
        SecTrustSetPolicies(trust, SecPolicyCreateSSL(true, challenge.protectionSpace.host as CFString))
        SecTrustSetAnchorCertificates(trust, [anchor] as CFArray)
        SecTrustSetAnchorCertificatesOnly(trust, true)
        var error: CFError?
        guard SecTrustEvaluateWithError(trust, &error) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}

private enum LifecycleError: Error {
    case invalidCase
    case invalidPhase
    case invalidPage
    case fetchFailed
    case routeAPrecondition
    case controlFailure
    case javascriptTimeout
    case navigationTimeout(String)
}
