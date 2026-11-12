import Foundation
import Network
import Security
import UIKit
import WebKit
import XCTest

private struct InstanceCase: Decodable {
    let id: String
    let question: String
    let mode: String
    let wireProtocol: String
    enum CodingKeys: String, CodingKey { case id, question, mode; case wireProtocol = "protocol" }
    func host(_ role: String) -> String { "\(id)-\(role).webkit-proxy.test" }
}

private struct InstanceError: Error {
    let message: String
    init(_ message: String) { self.message = message }
}

@MainActor
private func instanceControl(_ path: String, _ body: [String: Any]) async throws {
    var request = URLRequest(url: URL(string: "http://127.0.0.1:18090" + path)!)
    request.httpMethod = "POST"
    request.timeoutInterval = 8
    request.httpBody = try JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    let (_, response) = try await URLSession.shared.data(for: request)
    guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw InstanceError("control failed: \(path)") }
}

@MainActor
private final class InstanceStore {
    let uuid = UUID()
    let handle: String
    let dataStore: WKWebsiteDataStore
    var configSeq = 0
    init(_ handle: String) {
        self.handle = handle
        dataStore = WKWebsiteDataStore(forIdentifier: uuid)
    }
    var identity: [String: Any] { ["store":handle, "storeUUID":uuid.uuidString] }
}

// Each pending callback is fenced by its exact navigation/token. A WebKit bridge
// that cannot acknowledge cancellation becomes explicitly unmeasured; it is never
// silently replaced/reset to obtain a successful later probe.
@MainActor
private final class InstanceView: NSObject, WKNavigationDelegate {
    let store: InstanceStore
    let handle: String
    let web: WKWebView
    let allowedHosts: Set<String>
    let anchor: SecCertificate
    let window: UIWindow
    var poisoned = false
    private var nav: WKNavigation?
    private var navReply: CheckedContinuation<[String: Any], Never>?
    private var navTimedOut = false
    private var navTimer: Task<Void, Never>?
    private var jsToken: UUID?
    private var jsReply: CheckedContinuation<[String: Any], Never>?
    private var jsTimer: Task<Void, Never>?

    init(_ store: InstanceStore, handle: String, hosts: Set<String>, anchor: SecCertificate) {
        self.store = store; self.handle = handle; self.allowedHosts = hosts; self.anchor = anchor
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = store.dataStore
        web = WKWebView(frame: CGRect(x:0,y:0,width:390,height:700),configuration:configuration)
        window = UIWindow(frame:web.frame)
        super.init()
        let controller = UIViewController()
        controller.view.addSubview(web)
        window.rootViewController = controller
        window.makeKeyAndVisible()
        web.navigationDelegate = self
    }

    private func missing(_ reason: String) -> [String: Any] {
        ["ok":false,"outcome":"unmeasured","settled":false,"error":reason]
    }

    func abandon() {
        poisoned = true
        web.stopLoading()
        finishNav(missing("cell cancelled"))
        finishJS(missing("cell cancelled"), token:jsToken)
    }

    func close() {
        abandon()
        web.navigationDelegate = nil
        web.removeFromSuperview()
        window.isHidden = true
    }

    func navigate(_ host: String, id: String) async -> [String: Any] {
        if poisoned || Task.isCancelled { return missing("view unavailable") }
        var result: [String: Any] = await withTaskCancellationHandler(operation: {
            await withCheckedContinuation { continuation in
                navReply = continuation; navTimedOut = false
                var request = URLRequest(url: URL(string:"https://\(host)/page?id=\(id)")!)
                request.cachePolicy = .reloadIgnoringLocalCacheData
                nav = web.load(request)
                navTimer = Task { @MainActor [weak self] in
                    do { try await Task.sleep(for:.seconds(15)) } catch { return }
                    guard let self, self.navReply != nil else { return }
                    self.navTimedOut = true
                    self.web.stopLoading()
                    do { try await Task.sleep(for:.seconds(2)) } catch { return }
                    if self.navReply != nil {
                        self.poisoned = true
                        self.finishNav(self.missing("navigation cancellation not acknowledged"))
                    }
                }
            }
        }, onCancel: { Task { @MainActor [weak self] in self?.abandon() } })
        // Native navigation errors have no page payload. Bind them to this
        // invocation only after the identity-fenced navigation callback settles.
        // Successful page IDs still come from the document, never this assignment.
        result["id"] = id
        guard result["ok"] as? Bool == true else { return result }
        return await evaluate("window.probe.page", arguments:[:], cancelID:nil)
    }

    func fetch(_ host: String, id: String) async -> [String: Any] {
        await evaluate("window.instanceProbe.fetch(id, host)", arguments:["id":id,"host":host],cancelID:id)
    }

    private func evaluate(_ expression: String, arguments: [String: Any], cancelID: String?) async -> [String: Any] {
        if poisoned || Task.isCancelled { return missing("view unavailable") }
        return await withTaskCancellationHandler(operation: {
            await withCheckedContinuation { continuation in
                let token = UUID()
                jsReply = continuation; jsToken = token
                jsTimer = Task { @MainActor [weak self] in
                    do { try await Task.sleep(for:.seconds(12)) } catch { return }
                    guard let self, self.jsToken == token else { return }
                    if let cancelID {
                        self.web.callAsyncJavaScript("return JSON.stringify(await window.instanceProbe.cancel(id));",
                            arguments:["id":cancelID],in:nil,in:.page) { _ in }
                    }
                    // The original async body must settle, not just the abort call.
                    do { try await Task.sleep(for:.seconds(2)) } catch { return }
                    if self.jsToken == token {
                        self.poisoned = true
                        self.finishJS(self.missing("JS cancellation not acknowledged"),token:token)
                    }
                }
                web.callAsyncJavaScript("return JSON.stringify(await (" + expression + "));",
                    arguments:arguments,in:nil,in:.page) { [weak self] result in
                    guard let self, self.jsToken == token else { return }
                    do {
                        guard let text = try result.get() as? String,
                              var value = try JSONSerialization.jsonObject(with:Data(text.utf8)) as? [String: Any]
                        else { throw InstanceError("invalid JS object") }
                        if cancelID == nil { value["ok"] = true; value["outcome"] = "routed"; value["settled"] = true }
                        self.finishJS(value,token:token)
                    } catch {
                        self.poisoned = true
                        self.finishJS(self.missing("JS bridge error: \(error)"),token:token)
                    }
                }
            }
        }, onCancel: { Task { @MainActor [weak self] in self?.abandon() } })
    }

    private func finishJS(_ value: [String: Any], token: UUID?) {
        guard let token, token == jsToken else { return }
        jsTimer?.cancel(); jsTimer = nil; jsToken = nil
        let reply = jsReply; jsReply = nil; reply?.resume(returning:value)
    }
    private func finishNav(_ value: [String: Any]) {
        navTimer?.cancel(); navTimer = nil; nav = nil
        let reply = navReply; navReply = nil; reply?.resume(returning:value)
    }
    private func navigationEnded(_ navigation: WKNavigation?, error: Error?) {
        guard let navigation, let nav, navigation === nav, navReply != nil else { return }
        if navTimedOut { finishNav(["ok":false,"outcome":"bounded_failure","settled":true,"error":"navigation deadline"]) }
        else if let error { finishNav(["ok":false,"outcome":"failed","settled":true,"error":String(describing:error)]) }
        else { finishNav(["ok":true]) }
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { navigationEnded(navigation,error:nil) }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { navigationEnded(navigation,error:error) }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { navigationEnded(navigation,error:error) }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { abandon() }
    func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                 completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              allowedHosts.contains(challenge.protectionSpace.host), let trust = challenge.protectionSpace.serverTrust
        else { completionHandler(.cancelAuthenticationChallenge,nil); return }
        guard SecTrustSetPolicies(trust, SecPolicyCreateSSL(true,challenge.protectionSpace.host as CFString)) == errSecSuccess,
              SecTrustSetAnchorCertificates(trust,[anchor] as CFArray) == errSecSuccess,
              SecTrustSetAnchorCertificatesOnly(trust,true) == errSecSuccess,
              SecTrustSetNetworkFetchAllowed(trust,false) == errSecSuccess
        else { completionHandler(.cancelAuthenticationChallenge,nil); return }
        var error: CFError?
        guard SecTrustEvaluateWithError(trust,&error) else { completionHandler(.cancelAuthenticationChallenge,nil); return }
        completionHandler(.useCredential,URLCredential(trust:trust))
    }
}

@MainActor
private final class InstanceCell {
    let c: InstanceCase
    let anchor: SecCertificate
    var stores: [InstanceStore] = []
    var views: [InstanceView] = []
    init(_ c: InstanceCase, anchor: SecCertificate) { self.c = c; self.anchor = anchor }
    func mark(_ step: String, _ fields: [String: Any] = [:]) async throws {
        var payload = fields
        payload["case"] = c.id; payload["step"] = step; payload["extension"] = "instance-v1"
        payload["appPID"] = ProcessInfo.processInfo.processIdentifier
        try await instanceControl("/log",payload)
    }
    func write(_ store: InstanceStore, hosts: [String], port: UInt16) async throws {
        try Task.checkCancellation()
        store.configSeq += 1
        var entry = store.identity
        entry["configSeq"] = store.configSeq; entry["hosts"] = hosts.sorted()
        entry["port"] = port; entry["allowFailover"] = false; entry["phase"] = "start"
        try await mark("config_write",entry)
        if hosts.isEmpty { store.dataStore.proxyConfigurations = [] }
        else {
            var proxy = ProxyConfiguration(httpCONNECTProxy:.hostPort(host:"127.0.0.1",port:NWEndpoint.Port(String(port))!))
            proxy.matchDomains = hosts.sorted(); proxy.allowFailover = false
            store.dataStore.proxyConfigurations = [proxy]
        }
        entry["phase"] = "end"
        try await mark("config_write",entry)
    }
    @discardableResult
    func op(_ view: InstanceView, host: String, label: String, kind: String) async throws -> [String: Any] {
        try Task.checkCancellation()
        let id = "\(c.id)-\(view.handle)-\(label)-\(UUID().uuidString.lowercased())"
        var entry = view.store.identity
        entry["view"] = view.handle; entry["id"] = id; entry["host"] = host
        entry["label"] = label; entry["kind"] = kind; entry["attempt"] = 1
        entry["configSeq"] = view.store.configSeq
        try await mark("op_start",entry)
        let result: [String: Any]
        if kind == "nav" { result = await view.navigate(host,id:id) }
        else { result = await view.fetch(host,id:id) }
        entry["result"] = result
        try await mark("op_end",entry)
        return result
    }
    func probe(_ view: InstanceView, host: String, prefix: String) async throws {
        try await op(view,host:host,label:prefix + "-immediate",kind:"fetch")
        try await op(view,host:host,label:prefix + "-nav",kind:"nav")
        try await op(view,host:host,label:prefix + "-fetch",kind:"fetch")
    }
    func warm(_ view: InstanceView, host: String) async throws {
        try await op(view,host:host,label:"warm-page",kind:"nav")
        let a = try await op(view,host:host,label:"warm-1",kind:"fetch")
        let b = try await op(view,host:host,label:"warm-2",kind:"fetch")
        guard a["ok"] as? Bool == true, b["ok"] as? Bool == true,
              let connection = a["connection"] as? String, connection == b["connection"] as? String,
              let tcp = a["tcpConnection"] as? String, tcp == b["tcpConnection"] as? String
        else { throw InstanceError("warm identity missing") }
        try await mark("warm",["store":view.store.handle,"connection":connection,"tcpConnection":tcp])
    }
    func run() async throws {
        try await instanceControl("/instance/setup",["case":c.id])
        let count = c.question == "q4" ? 2 : 1
        for n in 0..<count { stores.append(InstanceStore("s\(n)")) }
        try await mark("cell_start",["stores":stores.map(\.identity),"protocol":c.wireProtocol,"mode":c.mode,"deadlineSeconds":240])
        let hosts = Set([c.host("control"),c.host("a"),c.host("b")])
        for (n,store) in stores.enumerated() {
            try await write(store,hosts:c.question == "q2" ? [] : [c.host(n == 0 ? "a" : "b")],port:18100)
            views.append(InstanceView(store,handle:"v\(n)",hosts:hosts,anchor:anchor))
        }
        if c.question == "q2" {
            try await warm(views[0],host:c.host("control"))
            try await mark("boundary")
            try await write(stores[0],hosts:[c.host("a")],port:18100)
            try await probe(views[0],host:c.host("a"),prefix:"after")
            try await op(views[0],host:c.host("control"),label:"control-after",kind:"fetch")
        } else if c.question == "q3" {
            // Keep A's view alive while B's view navigates on the same store.
            let second = InstanceView(stores[0],handle:"v1",hosts:hosts,anchor:anchor)
            views.append(second)
            try await op(second,host:c.host("a"),label:"seed-page",kind:"nav")
            try await warm(views[0],host:c.host("a"))
            try await op(views[0],host:c.host("a"),label:"a-before",kind:"fetch")
            try await mark("boundary")
            try await write(stores[0],hosts:[c.host("a"),c.host("b")],port:18100)
            try await probe(second,host:c.host("b"),prefix:"after")
            try await op(views[0],host:c.host("a"),label:"a-after",kind:"fetch")
        } else {
            for (n,view) in views.enumerated() { try await warm(view,host:c.host(n == 0 ? "a" : "b")) }
            try await mark("boundary")
            try await instanceControl("/instance/transition",["case":c.id])
            for (n,store) in stores.enumerated() { try await write(store,hosts:[c.host(n == 0 ? "a" : "b")],port:18101) }
            for (n,view) in views.enumerated() { try await probe(view,host:c.host(n == 0 ? "a" : "b"),prefix:"after") }
        }
        try await mark("cell_done")
    }
    func close() { for view in views { view.close() } }
}

final class InstanceRouteTests: XCTestCase {
    @MainActor
    func testInstanceRoutes() async throws {
        let bundle = Bundle(for:Self.self)
        let cases = try JSONDecoder().decode([InstanceCase].self,from:Data(contentsOf:XCTUnwrap(bundle.url(forResource:"instance-cases",withExtension:"json"))))
        let data = try Data(contentsOf:XCTUnwrap(bundle.url(forResource:"Anchor",withExtension:"der")))
        let anchor = try XCTUnwrap(SecCertificateCreateWithData(nil,data as CFData))
        var setupErrors = 0
        for c in cases {
            let cell = InstanceCell(c,anchor:anchor)
            let work = Task { @MainActor in try await cell.run() }
            let deadline = Task { @MainActor in
                do { try await Task.sleep(for:.seconds(240)) } catch { return }
                work.cancel()
                for view in cell.views { view.abandon() }
            }
            do { try await work.value }
            catch { setupErrors += 1; try? await cell.mark("cell_error",["error":String(describing:error)]) }
            deadline.cancel()
            await deadline.value
            // Stand-in remains intact through the observation-end marker; teardown
            // is test cleanup, not part of recovery, even on an incomplete case.
            do { try await instanceControl("/instance/finish",["case":c.id]) }
            catch { setupErrors += 1; try? await cell.mark("finish_error",["error":String(describing:error)]) }
            cell.close()
        }
        XCTAssertEqual(setupErrors,0,"See per-case partial evidence; no route-success assertion")
    }
}
