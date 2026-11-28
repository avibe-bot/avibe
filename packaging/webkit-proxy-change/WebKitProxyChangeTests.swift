import Foundation
import Network
import ObjectiveC
import Security
import UIKit
import WebKit
import XCTest

private struct ProbeCase: Decodable {
    let id: String
    let origin: String
    let proxy: String
    let action: String
    let listener: String?
    let allowFailover: Bool?
    /// The page's route a gate cell opens; the root when absent.
    let path: String?
    var host: String { "\(id).webkit-proxy.test" }
}

private struct ProbeError: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}

@MainActor
private func emit(_ fields: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys])
    print("MEASURE " + String(decoding: data, as: UTF8.self))
}

@MainActor
private func control(_ path: String, body: [String: Any]? = nil) async throws -> [String: Any] {
    var request = URLRequest(url: URL(string: "http://127.0.0.1:18090" + path)!)
    request.timeoutInterval = 10
    if let body {
        request.httpMethod = "POST"
        request.httpBody = try JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    }
    let (data, response) = try await URLSession.shared.data(for: request)
    guard let value = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        throw ProbeError("control did not return an object")
    }
    guard (response as? HTTPURLResponse)?.statusCode == 200 else {
        throw ProbeError("control failure: \(value)")
    }
    return value
}

@MainActor
private final class Browser: NSObject, WKNavigationDelegate {
    let fixture: ProbeCase
    let store: WKWebsiteDataStore
    let webView: WKWebView
    private let anchor: SecCertificate
    private var window: UIWindow?
    private var navigation: CheckedContinuation<Void, Error>?
    private var navigationDeadline: Task<Void, Never>?
    private var javascript: CheckedContinuation<[String: Any], Error>?
    private var javascriptDeadline: Task<Void, Never>?
    private(set) var contentTerminations = 0
    var warmedConnection: String?

    init(fixture: ProbeCase, anchor: SecCertificate, initialRoute: String? = nil, userScript: String? = nil) {
        self.fixture = fixture
        self.anchor = anchor
        store = WKWebsiteDataStore(forIdentifier: UUID())
        if let initialRoute {
            store.proxyConfigurations = [Self.proxy(for: fixture.host, mode: initialRoute)]
        }
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = store
        if let userScript {
            // The stand-in for the App's bridge, defined at document start
            // in the main frame, as the App defines its own.
            configuration.userContentController.addUserScript(
                WKUserScript(source: userScript, injectionTime: .atDocumentStart, forMainFrameOnly: true)
            )
        }
        webView = WKWebView(frame: CGRect(x: 0, y: 0, width: 390, height: 700), configuration: configuration)
        super.init()
        webView.navigationDelegate = self
        let controller = UIViewController()
        controller.view.addSubview(webView)
        let window = UIWindow(frame: webView.frame)
        window.rootViewController = controller
        window.makeKeyAndVisible()
        self.window = window
    }

    func load(_ id: String, timeout: Double = 20) async throws {
        try await withCheckedThrowingContinuation { continuation in
            navigation = continuation
            navigationDeadline = Task { @MainActor [weak self] in
                do { try await Task.sleep(for: .seconds(timeout)) } catch { return }
                self?.completeNavigation(.failure(ProbeError("navigation timeout: \(id)")))
            }
            webView.load(URLRequest(url: URL(string: "https://\(fixture.host)/page?id=\(id)")!))
        }
    }

    /// Loads a path of the host: the built page's own routes.
    func open(_ path: String, timeout: Double = 30) async throws {
        try await withCheckedThrowingContinuation { continuation in
            navigation = continuation
            navigationDeadline = Task { @MainActor [weak self] in
                do { try await Task.sleep(for: .seconds(timeout)) } catch { return }
                self?.completeNavigation(.failure(ProbeError("navigation timeout: \(path)")))
            }
            webView.load(URLRequest(url: URL(string: "https://\(fixture.host)\(path)")!))
        }
    }

    /// Hides or shows the WebView's window, as the App leaving and coming
    /// back does to its page. If WebKit does not report that to the page
    /// (`document.visibilityState`), the page is told by a made-up event
    /// instead, and the result says so (`mode`: `window` or `pretended`).
    func setVisible(_ visible: Bool) async -> [String: Any] {
        let wanted = visible ? "visible" : "hidden"
        window?.isHidden = !visible
        var seen = ""
        for _ in 0..<10 {
            try? await Task.sleep(for: .milliseconds(50))
            seen = ((try? await js("window.__gate.snapshot()"))?["visibility"] as? String) ?? ""
            if seen == wanted { return ["ok":true, "mode":"window", "visibility":seen] }
        }
        let pretended = (try? await js("window.__gate.pretend('\(wanted)')")) ?? ["ok":false, "error":"no bridge"]
        return ["ok":pretended["ok"] as? Bool ?? false, "mode":"pretended",
                "visibility":pretended["visibility"] as? String ?? seen]
    }

    func js(_ expression: String) async throws -> [String: Any] {
        try await withCheckedThrowingContinuation { continuation in
            javascript = continuation
            javascriptDeadline = Task { @MainActor [weak self] in
                do { try await Task.sleep(for: .seconds(12)) } catch { return }
                self?.completeJavaScript(.failure(ProbeError("JavaScript evaluation deadline")))
            }
            webView.callAsyncJavaScript(
                "return JSON.stringify(await (" + expression + "));",
                arguments: [:], in: nil, in: .page
            ) { [weak self] result in
                do {
                    let value = try result.get()
                    guard let text = value as? String,
                          let result = try JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any]
                    else { throw ProbeError("JavaScript returned no object") }
                    self?.completeJavaScript(.success(result))
                } catch { self?.completeJavaScript(.failure(error)) }
            }
        }
    }

    private func completeJavaScript(_ result: Result<[String: Any], Error>) {
        javascriptDeadline?.cancel()
        javascriptDeadline = nil
        let continuation = javascript
        javascript = nil
        continuation?.resume(with: result)
    }

    func fetch(_ label: String) async throws -> [String: Any] {
        try await observe(label, expression: "window.probe.fetch('\(fixture.id)-\(label)')")
    }

    @discardableResult
    func observe(_ label: String, expression: String) async throws -> [String: Any] {
        do {
            let result = try await js(expression)
            await record(label, result)
            return result
        } catch {
            await record(label, ["ok":false, "unmeasured":true, "error":String(describing:error)])
            throw error
        }
    }

    func record(_ label: String, _ result: [String: Any]) async {
        let entry: [String: Any] = [
            "case": fixture.id, "step": label, "result": result,
            "os": UIDevice.current.systemVersion, "networkPID": networkPID(),
            "contentTerminations": contentTerminations,
        ]
        emit(entry)
        do { _ = try await control("/log", body: entry) }
        catch { emit(["case":fixture.id, "step":"record_failed", "error":String(describing:error)]) }
    }

    private static func proxy(for host: String, mode: String, allowFailover: Bool = false) -> ProxyConfiguration {
        let port = mode == "absent" ? 18081 : mode == "refuse" ? 18082 : mode == "stall" ? 18083 : 18080
        var proxy = ProxyConfiguration(httpCONNECTProxy: .hostPort(
            host: "127.0.0.1", port: NWEndpoint.Port(rawValue: UInt16(port))!
        ))
        proxy.matchDomains = [mode == "placeholder" ? "never-\(UUID().uuidString.lowercased()).invalid" : host]
        proxy.allowFailover = allowFailover
        return proxy
    }

    func route(_ mode: String, allowFailover: Bool = false) async {
        if mode == "empty" {
            store.proxyConfigurations = []
        } else {
            store.proxyConfigurations = [Self.proxy(for: fixture.host, mode: mode, allowFailover: allowFailover)]
        }
        await record("setter", ["mode":mode, "allowFailover":allowFailover,
              "count":store.proxyConfigurations.count, "networkPID":networkPID(),
              "uptime":ProcessInfo.processInfo.systemUptime])
    }

    // Test-only private introspection. This file is never linked into Avibe.
    // The runner control service verifies the returned PID's executable before killing it.
    func networkPID() -> Int32 {
        let selector = NSSelectorFromString("_networkProcessIdentifier")
        guard store.responds(to: selector),
              let method = class_getInstanceMethod(type(of: store), selector)
        else { return 0 }
        typealias Getter = @convention(c) (AnyObject, Selector) -> Int32
        let getter = unsafeBitCast(method_getImplementation(method), to: Getter.self)
        return getter(store, selector)
    }

    func close() async {
        webView.evaluateJavaScript("window.probe?.cleanup()", completionHandler: nil)
        completeJavaScript(.failure(CancellationError()))
        webView.stopLoading()
        webView.navigationDelegate = nil
        navigationDeadline?.cancel()
        completeNavigation(.failure(CancellationError()))
        window?.isHidden = true
        window?.rootViewController = nil
        window = nil
    }

    private func completeNavigation(_ result: Result<Void, Error>) {
        navigationDeadline?.cancel()
        navigationDeadline = nil
        let continuation = navigation
        navigation = nil
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
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        contentTerminations += 1
        emit(["case":fixture.id, "step":"web_content_terminated", "count":contentTerminations])
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
            emit(["case":fixture.id, "step":"trust_failed", "error":String(describing:error)])
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}

final class WebKitProxyChangeTests: XCTestCase {
    @MainActor
    func testHostLaunch() async throws {
        let health = try await control("/health")
        XCTAssertEqual(health["ok"] as? Bool, true)
        emit(["step":"host_launch", "health":health])
    }

    @MainActor
    func testDirectTLSMinimal() async throws {
        let bundle = Bundle(for: Self.self)
        let cases = try JSONDecoder().decode([ProbeCase].self, from: Data(contentsOf: XCTUnwrap(bundle.url(forResource: "matrix", withExtension: "json"))))
        let fixture = try XCTUnwrap(cases.first { $0.action == "coldBeforeFirstRequest" && $0.origin == "h1" })
        let anchorData = try Data(contentsOf: XCTUnwrap(bundle.url(forResource: "Anchor", withExtension: "der")))
        let anchor = try XCTUnwrap(SecCertificateCreateWithData(nil, anchorData as CFData))
        _ = try await control("/health")
        let browser = Browser(fixture: fixture, anchor: anchor)
        do {
            try await browser.load("\(fixture.id)-direct-minimal")
            let page = try await browser.observe("direct-minimal-page", expression: "window.probe.page")
            let fetch = try await browser.fetch("direct-minimal-fetch")
            guard page["route"] as? String == "origin", fetch["route"] as? String == "origin"
            else { throw ProbeError("minimal direct TLS request did not reach origin: \(page) / \(fetch)") }
        } catch {
            await browser.close()
            throw error
        }
        await browser.close()
    }

    @MainActor
    func testColdMinimal() async throws {
        let bundle = Bundle(for: Self.self)
        let cases = try JSONDecoder().decode([ProbeCase].self, from: Data(contentsOf: XCTUnwrap(bundle.url(forResource: "matrix", withExtension: "json"))))
        let fixture = try XCTUnwrap(cases.first { $0.action == "coldBeforeWebView" && $0.origin == "h1" })
        let anchorData = try Data(contentsOf: XCTUnwrap(bundle.url(forResource: "Anchor", withExtension: "der")))
        let anchor = try XCTUnwrap(SecCertificateCreateWithData(nil, anchorData as CFData))
        _ = try await control("/health")
        let browser = Browser(fixture: fixture, anchor: anchor, initialRoute: "direct")
        do {
            try await browser.load("\(fixture.id)-minimal")
            let page = try await browser.observe("minimal-page", expression: "window.probe.page")
            let fetch = try await browser.fetch("minimal-fetch")
            guard page["route"] as? String == "proxy-backend",
                  fetch["route"] as? String == "proxy-backend"
            else { throw ProbeError("minimal cold proxy request did not reach proxy backend: \(page) / \(fetch)") }
        } catch {
            await browser.close()
            throw error
        }
        await browser.close()
    }

    @MainActor
    func testMatrix() async throws {
        let bundle = Bundle(for: Self.self)
        let cases = try JSONDecoder().decode([ProbeCase].self, from: Data(contentsOf: XCTUnwrap(bundle.url(forResource: "matrix", withExtension: "json"))))
        let anchorData = try Data(contentsOf: XCTUnwrap(bundle.url(forResource: "Anchor", withExtension: "der")))
        let anchor = try XCTUnwrap(SecCertificateCreateWithData(nil, anchorData as CFData))
        _ = try await control("/health")
        emit(["step":"matrix_start", "os":UIDevice.current.systemVersion,
              "system":ProcessInfo.processInfo.operatingSystemVersionString, "cases":cases.count])
        var fixtureFailures = 0
        for fixture in cases {
            let initialRoute = fixture.action == "coldBeforeWebView" ? "direct" : nil
            let browser = Browser(fixture: fixture, anchor: anchor, initialRoute: initialRoute)
            do {
                try await run(fixture, browser)
                await browser.record("case_complete", ["executionComplete":true, "qualification":"not asserted", "replaySafety":"unmeasured: GET only"])
            } catch {
                fixtureFailures += 1
                await browser.record("case_unmeasured", ["ok":false, "error":String(describing:error)])
            }
            await browser.close()
        }
        emit(["step":"matrix_complete", "fixtureFailures":fixtureFailures, "cases":cases.count])
        XCTAssertEqual(fixtureFailures, 0, "Fixture/precondition failures are unmeasured, not proxy conclusions; inspect JSONL")
    }

    /// A route added after the store's first load of the host, as the App's
    /// late first bind would add it: what ends the old connection, and where
    /// the next one goes. Every outcome is an observation; only a missing
    /// precondition fails a cell.
    @MainActor
    func testLateRoutes() async throws {
        let bundle = Bundle(for: Self.self)
        let cases = try JSONDecoder().decode([ProbeCase].self, from: Data(contentsOf: XCTUnwrap(bundle.url(forResource: "late-cases", withExtension: "json"))))
        let anchorData = try Data(contentsOf: XCTUnwrap(bundle.url(forResource: "Anchor", withExtension: "der")))
        let anchor = try XCTUnwrap(SecCertificateCreateWithData(nil, anchorData as CFData))
        _ = try await control("/health")
        emit(["step":"late_start", "os":UIDevice.current.systemVersion,
              "system":ProcessInfo.processInfo.operatingSystemVersionString, "cases":cases.count])
        var fixtureFailures = 0
        let bridge = try String(contentsOf: XCTUnwrap(bundle.url(forResource: "gate-bridge", withExtension: "js")), encoding: .utf8)
        for fixture in cases {
            let browser = Browser(fixture: fixture, anchor: anchor,
                                  userScript: fixture.action.hasPrefix("gate") ? bridge : nil)
            do {
                try await run(fixture, browser)
                await browser.record("case_complete", ["executionComplete":true, "qualification":"not asserted", "replaySafety":"unmeasured: GET only"])
            } catch {
                fixtureFailures += 1
                await browser.record("case_unmeasured", ["ok":false, "error":String(describing:error)])
            }
            await browser.close()
        }
        emit(["step":"late_complete", "fixtureFailures":fixtureFailures, "cases":cases.count])
        XCTAssertEqual(fixtureFailures, 0, "Fixture/precondition failures are unmeasured, not route conclusions; inspect JSONL")
    }

    /// After the old connection was ended or refused: two fetches a second
    /// apart, then, once an EventSource has had time to reconnect by itself,
    /// the old stream if there is one, a new stream and a new WebSocket.
    @MainActor
    private func afterLateRoute(_ browser: Browser, live: Bool) async throws {
        try await afterMutation(browser)
        try await Task.sleep(for: .seconds(4))
        if live {
            _ = try await browser.observe("old-sse-after", expression:"window.probe.streamSnapshot('\(browser.fixture.id)-old-sse')")
        }
        _ = try await browser.fetch("after-reconnect-wait")
        _ = try await browser.observe("rebuilt-sse", expression:"window.probe.openEvents('\(browser.fixture.id)-rebuilt-sse')")
        _ = try await browser.observe("new-websocket", expression:"window.probe.webSocket('\(browser.fixture.id)-new-websocket')")
    }

    @MainActor
    private func warm(_ browser: Browser, viaProxy: Bool) async throws {
        if viaProxy { await browser.route("direct") }
        try await browser.load("\(browser.fixture.id)-navigation")
        _ = try await browser.observe("page", expression:"window.probe.page")
        let first = try await browser.fetch("warm-1")
        let second = try await browser.fetch("warm-2")
        let expectedRoute = viaProxy ? "proxy-backend" : "origin"
        let expectedProtocol = (viaProxy ? browser.fixture.proxy : browser.fixture.origin) == "h2" ? "2.0" : "1.1"
        guard first["ok"] as? Bool == true, second["ok"] as? Bool == true,
              first["route"] as? String == expectedRoute,
              second["route"] as? String == expectedRoute,
              first["protocol"] as? String == expectedProtocol,
              second["protocol"] as? String == expectedProtocol,
              let connection = first["connection"] as? String,
              second["connection"] as? String == connection
        else { throw ProbeError("warm pool/protocol precondition failed: \(first) / \(second)") }
        browser.warmedConnection = connection
        await browser.record("pool_precondition", ["ok":true,"route":expectedRoute,"protocol":expectedProtocol,"connection":connection])
        try await Task.sleep(for: .milliseconds(300))
    }

    @MainActor
    private func establishOldStream(_ browser: Browser) async throws {
        let id = "\(browser.fixture.id)-old-sse"
        let first = try await browser.observe("old-sse-first", expression:"window.probe.openEvents('\(id)')")
        guard first["ok"] as? Bool == true, first["protocol"] as? String == "2.0",
              first["connection"] as? String == browser.warmedConnection,
              first["stream"] as? Int != nil, first["tcpConnection"] as? String != nil
        else { throw ProbeError("old SSE not established on the warmed H2 connection") }
        _ = try await browser.observe("old-sse-before", expression:"window.probe.streamSnapshot('\(id)')")
    }

    @MainActor
    private func afterMutation(_ browser: Browser) async throws {
        _ = try await browser.fetch("after-immediate")
        try await Task.sleep(for: .seconds(1))
        _ = try await browser.fetch("after-settled")
    }

    @MainActor
    private func waitForRequest(_ id: String) async throws {
        for _ in 0..<100 {
            let response = try await control("/seen?id=\(id)")
            if let requests = response["requests"] as? [[String:Any]], !requests.isEmpty { return }
            try await Task.sleep(for: .milliseconds(50))
        }
        throw ProbeError("server never saw \(id)")
    }

    @MainActor
    private func run(_ fixture: ProbeCase, _ browser: Browser) async throws {
        await browser.record("case_start", ["action":fixture.action,"originProtocol":fixture.origin,"proxyProtocol":fixture.proxy,
            "listener":fixture.listener ?? "normal", "allowFailover":fixture.allowFailover ?? false,
            "replaySafety":"unmeasured: GET only"])
        switch fixture.action {
        case "coldBeforeWebView", "coldBeforeFirstRequest":
            if fixture.action == "coldBeforeFirstRequest" { await browser.route("direct") }
            try await browser.load("\(fixture.id)-cold-navigation")
            let page = try await browser.observe("cold-page", expression:"window.probe.page")
            let fetch = try await browser.fetch("cold-fetch")
            let expectedProtocol = fixture.proxy == "h2" ? "2.0" : "1.1"
            guard page["protocol"] as? String == expectedProtocol,
                  fetch["protocol"] as? String == expectedProtocol
            else { throw ProbeError("cold protocol precondition failed: \(page) / \(fetch)") }
        case "idle":
            try await warm(browser, viaProxy:false)
            await browser.route("direct")
            try await afterMutation(browser)
        case "live":
            try await warm(browser, viaProxy:false)
            let id = "\(fixture.id)-old-sse"
            try await establishOldStream(browser)
            await browser.route("direct")
            try await afterMutation(browser)
            _ = try await browser.observe("old-sse-after", expression:"window.probe.streamSnapshot('\(id)')")
            _ = try await browser.observe("old-sse-close", expression:"window.probe.closeEvents('\(id)')")
            _ = try await browser.observe("rebuilt-sse", expression:"window.probe.openEvents('\(fixture.id)-rebuilt-sse')")
            _ = try await browser.observe("new-websocket", expression:"window.probe.webSocket('\(fixture.id)-new-websocket')")
        case "removeOpen", "removeClose":
            try await warm(browser, viaProxy:true)
            await browser.route("empty")
            if fixture.action == "removeClose" {
                let closed = try await control("/close-proxy?host=\(fixture.host)")
                await browser.record("closed-proxy-connections", closed)
                guard (closed["closed"] as? Int ?? 0) > 0 else { throw ProbeError("no proxy connection closed") }
            }
            try await afterMutation(browser)
        case "flightInstall", "flightRemove":
            let remove = fixture.action == "flightRemove"
            try await warm(browser, viaProxy:remove)
            let id = "\(fixture.id)-slow"
            _ = try await browser.observe("slow-start", expression:"window.probe.startSlow('\(id)')")
            try await waitForRequest(id)
            await browser.route(remove ? "empty" : "direct")
            try await afterMutation(browser)
            _ = try await browser.observe("slow-result", expression:"window.probe.finishSlow('\(id)')")
        case "restartEmpty", "restartPlaceholder":
            try await warm(browser, viaProxy:true)
            await browser.route(fixture.action == "restartEmpty" ? "empty" : "placeholder")
            _ = try await browser.fetch("before-restart")
            let oldPID = browser.networkPID()
            guard oldPID > 1 else { throw ProbeError("NetworkProcess PID selector unavailable") }
            let killed = try await control("/restart-network?pid=\(oldPID)")
            await browser.record("network-killed", killed)
            var newPID: Int32 = 0
            var navigationError: String?
            try await Task.sleep(for: .milliseconds(250))
            do {
                try await browser.load("\(fixture.id)-restart-navigation", timeout: 8)
                newPID = browser.networkPID()
            } catch { navigationError = String(describing: error) }
            await browser.record("network-restarted", ["oldPID":oldPID,"newPID":newPID,"changed":newPID > 1 && newPID != oldPID,"navigationError":navigationError ?? ""])
            guard newPID > 1, newPID != oldPID else { throw ProbeError("NetworkProcess restart not demonstrated") }
            _ = try await browser.observe("restart-page", expression:"window.probe.page")
            _ = try await browser.fetch("after-restart")
        case "failure":
            try await browser.load("\(fixture.id)-failure-page")
            let page = try await browser.observe("failure-page", expression:"window.probe.page")
            guard page["route"] as? String == "origin", page["protocol"] as? String == "1.1" else {
                throw ProbeError("failure control initial direct H1 page missing")
            }
            let closed = try await control("/close-origin?host=\(fixture.host)")
            await browser.record("closed-origin-connections", closed)
            guard let tcp = page["tcpConnection"] as? String,
                  (closed["closed"] as? Int ?? 0) > 0,
                  (closed["connections"] as? [String])?.contains(tcp) == true
            else { throw ProbeError("failure control did not close the initial origin socket") }
            let listener = try XCTUnwrap(fixture.listener)
            if listener == "absent" {
                let absent = try await control("/absent-check?host=\(fixture.host)")
                await browser.record("absent-listener", absent)
                guard absent["ok"] as? Bool == true else { throw ProbeError("absent listener precondition failed") }
            }
            await browser.route(listener, allowFailover: fixture.allowFailover ?? false)
            _ = try await browser.fetch("failure-fetch")
            // A failure or old route is an observation, not a failed desired-route assertion.
        case "constantProxy", "constantProxySurviving":
            _ = try await control("/proxy-mode?host=\(fixture.host)&mode=transport")
            try await warm(browser, viaProxy:true)
            try await establishOldStream(browser)
            _ = try await control("/proxy-mode?host=\(fixture.host)&mode=origin")
            if fixture.action == "constantProxy" {
                let closed = try await control("/close-proxy?host=\(fixture.host)")
                await browser.record("closed-proxy-connections", closed)
                guard (closed["closed"] as? Int ?? 0) > 0 else { throw ProbeError("no old proxy socket closed") }
            }
            // Deliberately no WebsiteDataStore setter here.
            try await afterMutation(browser)
            _ = try await browser.observe("old-sse-after", expression:"window.probe.streamSnapshot('\(fixture.id)-old-sse')")
            _ = try await browser.observe("rebuilt-sse", expression:"window.probe.openEvents('\(fixture.id)-new-sse')")
            _ = try await browser.observe("new-websocket", expression:"window.probe.webSocket('\(fixture.id)-new-websocket')")
        case "lateClose", "lateGoaway", "lateCloseLive":
            let live = fixture.action == "lateCloseLive"
            try await warm(browser, viaProxy:false)
            if live { try await establishOldStream(browser) }
            await browser.route("direct")
            // Expected on the old connection (measured: `idle`); kept as
            // this cell's own control.
            let before = try await browser.fetch("late-before-end")
            let path = fixture.action == "lateGoaway" ? "/goaway-origin" : "/close-origin"
            let closed = try await control("\(path)?host=\(fixture.host)")
            await browser.record("ended-origin-connections", closed)
            // The connection that request used is among those ended: a
            // socket for a close, an HTTP/2 session for a GOAWAY.
            let ended = closed["connections"] as? [String] ?? []
            guard (closed["closed"] as? Int ?? 0) > 0,
                  [before["tcpConnection"] as? String, before["connection"] as? String]
                      .compactMap({ $0 }).contains(where: { ended.contains($0) })
            else { throw ProbeError("the connection in use was not ended: \(before) / \(closed)") }
            try await afterLateRoute(browser, live: live)
        case "late421", "late421Live":
            let live = fixture.action == "late421Live"
            try await warm(browser, viaProxy:false)
            if live { try await establishOldStream(browser) }
            await browser.route("direct")
            _ = try await browser.fetch("late-before-421")
            // The origin answers 421 on this path; the proxy's backend
            // answers it normally. The result says which answered, and the
            // fixture log has every attempt.
            _ = try await browser.observe("misdirected",
                expression:"window.probe.fetch('\(fixture.id)-misdirected', '/misdirected')")
            try await afterLateRoute(browser, live: live)
        case "returnClosed", "returnHeld":
            // The App's own route, written before the store's first load of
            // the host: the working CONNECT proxy, with fail-over allowed
            // for an ordinary host and not for a 「仅直连和中继」 one.
            let failover = fixture.allowFailover ?? false
            await browser.route("direct", allowFailover: failover)
            try await browser.load("\(fixture.id)-navigation")
            _ = try await browser.observe("page", expression:"window.probe.page")
            let first = try await browser.fetch("warm-1")
            let second = try await browser.fetch("warm-2")
            guard first["route"] as? String == "proxy-backend", second["route"] as? String == "proxy-backend",
                  let connection = first["connection"] as? String, second["connection"] as? String == connection
            else { throw ProbeError("warm proxy precondition failed: \(first) / \(second)") }
            await browser.record("pool_precondition", ["ok":true, "route":"proxy-backend", "connection":connection])
            // The background: nothing listens on the port, and the tunnels
            // through it are ended.
            let closed = try await control("/proxy-listener?state=closed&host=\(fixture.host)")
            await browser.record("listener-closed", closed)
            // The return. `returnClosed`: the page asks before the port is
            // open again, as a page does on `visibilitychange`.
            // `returnHeld`: it asks nothing until the port is open.
            var failure: Error?
            do {
                guard closed["ok"] as? Bool == true, (closed["closed"] as? Int ?? 0) > 0
                else { throw ProbeError("the port was not closed with its tunnels: \(closed)") }
                if fixture.action == "returnClosed" {
                    _ = try await browser.fetch("while-closed-1")
                    try await Task.sleep(for: .milliseconds(300))
                    _ = try await browser.fetch("while-closed-2")
                }
            } catch { failure = error }
            // Opened again whatever happened above: the next cell needs it.
            let opened = try await control("/proxy-listener?state=open&host=\(fixture.host)")
            await browser.record("listener-open", opened)
            if let failure { throw failure }
            guard opened["ok"] as? Bool == true else { throw ProbeError("the port was not opened again: \(opened)") }
            // The port is open: where do the page's requests go now, at
            // once, after a second, and after five?
            try await afterMutation(browser)
            try await Task.sleep(for: .seconds(4))
            _ = try await browser.fetch("after-5s")
            // New connections of the same page.
            _ = try await browser.observe("new-websocket", expression:"window.probe.webSocket('\(fixture.id)-new-websocket')")
            _ = try await browser.observe("new-sse", expression:"window.probe.openEvents('\(fixture.id)-new-sse')")
            _ = try await browser.observe("new-sse-close", expression:"window.probe.closeEvents('\(fixture.id)-new-sse')")
            // The page loads again, as it does by itself after an upgrade.
            try await browser.load("\(fixture.id)-reload")
            _ = try await browser.observe("reload-page", expression:"window.probe.page")
            _ = try await browser.fetch("after-reload")
            // And left alone for a while: does anything end by itself?
            try await Task.sleep(for: .seconds(20))
            _ = try await browser.fetch("after-idle-20s")
        case "gateReturn", "gateControl", "gateCap", "gateExpired", "gateRedirect":
            // The built page, through the route to the port, signed in.
            await browser.route("direct", allowFailover: fixture.allowFailover ?? true)
            try await browser.open(fixture.path ?? "/")
            try await gateSettle(browser, "loaded")
            let loaded = try await control("/gate-summary?host=\(fixture.host)&since=0")
            await browser.record("before-background", loaded)
            guard (loaded["requests"] as? Int ?? 0) > 0, loaded["origin"] as? Int == 0
            else { throw ProbeError("the page did not load through the port: \(loaded)") }
            // The background. The App's bridge will hold its answer on the
            // return until the port is open (not in the control cell, where
            // it answers at once, as to a page without the hold).
            _ = try await browser.observe("bridge",
                expression: "window.__gate.hold(\(fixture.action == "gateControl" ? "false" : "true"))")
            let hidden = await browser.setVisible(false)
            await browser.record("hidden", hidden)
            let closed = try await control("/proxy-listener?state=closed&host=\(fixture.host)")
            await browser.record("listener-closed", closed)
            var failure: Error?
            do {
                guard closed["ok"] as? Bool == true else { throw ProbeError("the port was not closed: \(closed)") }
                try await Task.sleep(for: .seconds(2))
                if fixture.action == "gateExpired" {
                    let gate1 = try await control("/gate-session?host=\(fixture.host)&state=missing")
                    await browser.record("session", gate1)
                } else if fixture.action == "gateRedirect" {
                    let gate2 = try await control("/gate-session?host=\(fixture.host)&state=redirect")
                    await browser.record("session", gate2)
                }
                let mark = (try await control("/gate-mark"))["count"] as? Int ?? 0
                // The return.
                let shown = await browser.setVisible(true)
                await browser.record("shown", shown)
                try await Task.sleep(for: .milliseconds(400))
                let gate3 = try await control("/gate-summary?host=\(fixture.host)&since=\(mark)")
                await browser.record("after-400ms", gate3)
                if fixture.action == "gateCap" {
                    // No answer from the bridge: the page's own cap ends the
                    // hold, with the port still closed.
                    try await Task.sleep(for: .milliseconds(1_100))
                    let gate4 = try await control("/gate-summary?host=\(fixture.host)&since=\(mark)")
                    await browser.record("after-cap", gate4)
                }
                let opened = try await control("/proxy-listener?state=open&host=\(fixture.host)")
                await browser.record("listener-open", opened)
                guard opened["ok"] as? Bool == true else { throw ProbeError("the port was not opened again: \(opened)") }
                // The port is open: now the bridge answers.
                _ = try await browser.observe("bridge-answers", expression: "window.__gate.hold(false)")
                try await Task.sleep(for: .seconds(8))
                let gate5 = try await control("/gate-summary?host=\(fixture.host)&since=\(mark)")
                await browser.record("after-return", gate5)
                if fixture.action == "gateExpired" || fixture.action == "gateRedirect" {
                    // Run 46133326060: no sign-in within these 8 s. Whether one
                    // comes later, by a read the page makes by itself.
                    _ = try? await browser.observe("page-at-8s", expression: "window.__gate ? window.__gate.snapshot() : ({ok:true, href:location.pathname + location.search, title:document.title, signIn:Boolean(document.getElementById('gate-sign-in'))})")
                    try await Task.sleep(for: .seconds(22))
                    let gate8 = try await control("/gate-summary?host=\(fixture.host)&since=\(mark)")
                    await browser.record("after-30s", gate8)
                }
                _ = try? await browser.observe("page-after", expression: "window.__gate ? window.__gate.snapshot() : ({ok:true, href:location.pathname + location.search, title:document.title, text:(document.body?.innerText ?? '').slice(0, 200), signIn:Boolean(document.getElementById('gate-sign-in'))})")
            } catch { failure = error }
            // Opened again whatever happened above: the next cell needs it.
            let reopened = try await control("/proxy-listener?state=open&host=\(fixture.host)")
            if failure != nil { await browser.record("listener-open", reopened) }
            if let failure { throw failure }
        case "strictUnlisted":
            // 「仅直连和中继」 writes a route with no fail-over that lists
            // only the instance's host. Does WebKit also send that route a
            // host it does not list: a sibling, or the parent domain (as
            // avibe.bot is to alex-app.avibe.bot)? The fixture's listener
            // refuses an unlisted host with 403, as the App's does.
            await browser.route("direct", allowFailover: fixture.allowFailover ?? false)
            try await browser.load("\(fixture.id)-minimal")
            for (label, host) in [("sibling", "unlisted.webkit-proxy.test"), ("parent", "webkit-proxy.test")] {
                let fetching = "(async()=>{const t=performance.now();try{const r=await fetch('https://\(host)/fetch?id=\(fixture.id)-\(label)',{mode:'no-cors',cache:'no-store'});return {ok:true,answered:true,type:r.type,ms:Math.round(performance.now()-t)}}catch(e){return {ok:true,answered:false,error:String(e),ms:Math.round(performance.now()-t)}}})()"
                _ = try? await browser.observe("fetch-\(label)", expression: fetching)
                let socket = "(new Promise(done=>{const t=performance.now();const end=opened=>done({ok:true,opened,ms:Math.round(performance.now()-t)});try{const w=new WebSocket('wss://\(host)/socket?id=\(fixture.id)-\(label)');w.onopen=()=>{w.close();end(true)};w.onerror=()=>end(false);setTimeout(()=>end(null),6000)}catch(e){done({ok:true,opened:false,error:String(e),ms:0})}}))"
                _ = try? await browser.observe("socket-\(label)", expression: socket)
            }
            // The listed host still goes through the port.
            _ = try? await browser.fetch("listed-after")
        case "gateMissing":
            // No session from the first read on.
            let gate6 = try await control("/gate-session?host=\(fixture.host)&state=missing")
            await browser.record("session", gate6)
            await browser.route("direct", allowFailover: fixture.allowFailover ?? true)
            try await browser.open("/")
            try await Task.sleep(for: .seconds(8))
            let gate7 = try await control("/gate-summary?host=\(fixture.host)&since=0")
            await browser.record("after-load", gate7)
            _ = try? await browser.observe("page-after", expression: "window.__gate ? window.__gate.snapshot() : ({ok:true, href:location.pathname + location.search, title:document.title, text:(document.body?.innerText ?? '').slice(0, 200), signIn:Boolean(document.getElementById('gate-sign-in'))})")
        default: throw ProbeError("unknown action")
        }
    }

    /// Waits until the built page has asked the bridge for its status, then
    /// five seconds more for its first requests to end.
    @MainActor
    private func gateSettle(_ browser: Browser, _ label: String) async throws {
        for _ in 0..<60 {
            if let snapshot = try? await browser.js("window.__gate.snapshot()"),
               (snapshot["status"] as? Int ?? 0) > 0 {
                try await Task.sleep(for: .seconds(5))
                _ = try await browser.observe(label, expression: "window.__gate.snapshot()")
                return
            }
            try await Task.sleep(for: .milliseconds(500))
        }
        throw ProbeError("the page never asked the bridge for its status")
    }
}
