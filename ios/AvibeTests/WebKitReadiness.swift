import WebKit
import XCTest
@testable import Avibe

/// What these tests wait for when they wait on WebKit or on the App's launch,
/// rather than on their own logic.
///
/// A WebView draws nothing, runs no script and finishes no navigation until
/// WebKit has launched a web content process for it. On the CI simulator
/// (`macos-26`, Xcode 26.6) that launch usually takes about a second, but over
/// 5 s in 22 of 35 suite runs measured for #3418, up to 41 s, and once 66 s.
/// A wait that also covers that launch is keyed on the document being loaded
/// and bounded by `webContentLaunch`; whatever the test checks after it keeps
/// its own short budget, so a page that loads but never reaches the state a
/// test expects still fails within seconds.
enum WebKitReadiness {
    /// Twice the slowest web content launch measured on CI (66 s).
    static let webContentLaunch: Duration = .seconds(120)

    /// The longest one account operation of `MobileAuthTransport` with its
    /// standard deadlines can take before it ends either way: the control
    /// plane page loads within the readiness deadline, then the request runs
    /// within the request deadline. A few seconds more cover the work around
    /// it (the App's launch is one such operation). A test that waits less
    /// gives up before the App itself would.
    static let standardTransportOperation: Duration = .milliseconds(
        MobileAuthTransportDeadlines.standard.readinessMilliseconds
            + MobileAuthTransportDeadlines.standard.requestMilliseconds
    ) + .seconds(5)

    /// `standardTransportOperation` for `XCTestCase.fulfillment(of:timeout:)`:
    /// the budget for a request that has to reach the control plane through
    /// an account transport the App launches for it.
    static var standardTransportOperationSeconds: TimeInterval {
        let components = standardTransportOperation.components
        return TimeInterval(components.seconds)
            + TimeInterval(components.attoseconds) / 1e18
    }

    /// The account transport's deadlines for an App whose tests are not
    /// about the control-plane page's readiness: that page may take as long
    /// as a web content launch on CI, so a slow launch is not mistaken for
    /// the App's own readiness timeout. Requests keep the App's deadlines.
    static let afterWebContentLaunch = MobileAuthTransportDeadlines(
        readinessMilliseconds: Int(webContentLaunch.components.seconds) * 1_000,
        requestMilliseconds: MobileAuthTransportDeadlines.standard.requestMilliseconds,
        logoutMilliseconds: MobileAuthTransportDeadlines.standard.logoutMilliseconds
    )

    /// `standardTransportOperation` for an App whose account transports have
    /// `afterWebContentLaunch`: what one account operation, the App's launch
    /// among them, may take before it ends either way.
    static let launchedTransportOperation: Duration = .milliseconds(
        afterWebContentLaunch.readinessMilliseconds
            + afterWebContentLaunch.requestMilliseconds
    ) + .seconds(5)

    /// `launchedTransportOperation` for `XCTestCase.fulfillment(of:timeout:)`.
    static var launchedTransportOperationSeconds: TimeInterval {
        let components = launchedTransportOperation.components
        return TimeInterval(components.seconds)
            + TimeInterval(components.attoseconds) / 1e18
    }

    /// `webContentLaunch` for `XCTestCase.fulfillment(of:timeout:)`.
    static var webContentLaunchSeconds: TimeInterval {
        TimeInterval(webContentLaunch.components.seconds)
    }

    /// Waits until `webView` shows a loaded document of its own: a web content
    /// process is running, and its document is complete and not the initial
    /// `about:blank`. Script evaluated before then waits for the process, so
    /// each check returns once there is one.
    @MainActor
    static func waitForDocument(
        in webView: WKWebView,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async throws {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: webContentLaunch)
        while clock.now < deadline {
            let loaded = try? await webView.evaluateJavaScript(
                """
                document.readyState === "complete"
                  && location.href !== "about:blank"
                """
            ) as? Bool
            if loaded == true {
                return
            }
            try await Task.sleep(for: .milliseconds(50))
        }
        XCTFail(
            "WebKit did not load the document within \(webContentLaunch).",
            file: file,
            line: line
        )
        throw WebKitReadinessError.documentNotLoaded
    }
    /// A picker route is also the App's bootstrap error fallback. Require
    /// the account and instance snapshot before exercising a signed-in flow.
    /// This observes one bootstrap; it never retries or extends its deadlines.
    @MainActor
    static func waitForSignedInFixture(
        _ app: AppModel,
        accountID: String,
        instanceIDs: Set<String>,
        requests: FixtureRequestLog,
        budget: Duration = standardTransportOperation
    ) async throws {
        let clock = ContinuousClock()
        let started = clock.now
        let deadline = started.advanced(by: budget)
        while case .loading = app.route {
            guard clock.now < deadline else {
                throw FixtureReadinessError(description: fixtureEvidence(
                    app, stage: "bootstrap did not settle", started: started,
                    requests: requests
                ))
            }
            try await Task.sleep(for: .milliseconds(50))
        }
        let evidence = fixtureEvidence(
            app, stage: "bootstrap settled", started: started, requests: requests
        )
        print("[iOS fixture] \(evidence)")
        guard case .onboarding(.instances) = app.route,
              app.mobileSessionCoordinator.currentAccount?.id == accountID,
              Set(app.mobileSessionCoordinator.currentInstances.map(\.id)) == instanceIDs
        else {
            throw FixtureReadinessError(description:
                "\(evidence); expected account=\(accountID), instances=\(instanceIDs.sorted())"
            )
        }
    }

    @MainActor
    private static func fixtureEvidence(
        _ app: AppModel,
        stage: String,
        started: ContinuousClock.Instant,
        requests: FixtureRequestLog
    ) -> String {
        let account = app.mobileSessionCoordinator.currentAccount?.id ?? "nil"
        let instances = app.mobileSessionCoordinator.currentInstances.map(\.id).sorted()
        return "stage=\(stage), elapsed=\(started.duration(to: .now)), "
            + "route=\(app.route), account=\(account), instances=\(instances); "
            + requests.summary()
    }
}

/// Only fixture metadata, never request bodies, headers, query strings or
/// provider error text (the password test deliberately puts secrets in those).
final class FixtureRequestLog: @unchecked Sendable {
    private let lock = NSLock()
    private let started = ContinuousClock.now
    private var entries: [String] = []

    var count: Int { lock.withLock { entries.count } }

    func record(_ request: LocalHTTPRequest, status: Int) {
        lock.withLock {
            entries.append("\(started.duration(to: .now)) \(request.method) \(request.path) -> \(status)")
        }
    }

    func summary(since index: Int = 0) -> String {
        lock.withLock {
            "requests=[" + entries.dropFirst(index).joined(separator: "; ") + "]"
        }
    }

    @MainActor
    func attach(to test: XCTestCase, name: String) {
        let attachment = XCTAttachment(string: summary())
        attachment.name = name
        attachment.lifetime = .keepAlways
        test.add(attachment)
    }
}

struct FixtureReadinessError: Error, CustomStringConvertible {
    let description: String
}

enum WebKitReadinessError: Error {
    case documentNotLoaded
}

/// Draft probe (not for merge), for cold-start PR 3: what WebKit gives back
/// of a live main-document response, and whether a simulated load with that
/// response's headers enforces its CSP.
@MainActor
final class CachedPageProbeTests: XCTestCase {
    private static let body = """
        <!doctype html><html><head><title>probe</title>
        <script>window.inlineRan = true;</script></head>
        <body><p id="x">served bytes</p></body></html>
        """

    func testWhatWebKitKeepsOfTheLiveDocument() async throws {
        let server = try LocalHTTPServer { request in
            LocalHTTPResponse(
                status: 200,
                headers: [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Content-Security-Policy", "default-src 'self'; script-src 'self'"),
                    ("Permissions-Policy", "camera=(), microphone=(self)"),
                    ("Cache-Control", "no-store, private"),
                ],
                body: Data(Self.body.utf8)
            )
        }
        let base = try await server.start()
        defer { server.stop() }

        // Live load: headers from the navigation response, then the archive.
        let recorder = ResponseRecorder()
        let live = WKWebView(frame: .zero)
        live.navigationDelegate = recorder
        live.load(URLRequest(url: base))
        try await WebKitReadiness.waitForDocument(in: live)
        let liveInline = try await live.evaluateJavaScript("window.inlineRan === true") as? Bool
        print("[cache probe] live: inline script ran=\(String(describing: liveInline))")
        print("[cache probe] navigation response headers: \(recorder.headers.keys.sorted())")
        print("[cache probe] CSP from navigation response: \(recorder.headers["Content-Security-Policy"] ?? "none")")

        let archive: Data = try await withCheckedThrowingContinuation { continuation in
            live.createWebArchiveData { continuation.resume(with: $0) }
        }
        let plist = try PropertyListSerialization.propertyList(from: archive, format: nil) as? [String: Any]
        let main = plist?["WebMainResource"] as? [String: Any]
        let data = main?["WebResourceData"] as? Data
        print("[cache probe] archive keys: \(plist?.keys.sorted() ?? []); main keys: \(main?.keys.sorted() ?? [])")
        print("[cache probe] archive main data equals served bytes: \(data == Data(Self.body.utf8)); size=\(data?.count ?? -1) served=\(Data(Self.body.utf8).count)")
        if let responseData = main?["WebResourceResponse"] as? Data,
           let response = try? NSKeyedUnarchiver.unarchivedObject(ofClass: HTTPURLResponse.self, from: responseData) {
            print("[cache probe] archive response headers: \(response.allHeaderFields.keys.map { "\($0)" }.sorted())")
        } else {
            print("[cache probe] archive response: not an HTTPURLResponse archive (\(type(of: main?["WebResourceResponse"])))")
        }

        // Simulated load with the recorded headers and the served bytes.
        for (label, headers) in [("with CSP", recorder.headers), ("without CSP", recorder.headers.filter { $0.key != "Content-Security-Policy" })] {
            let simulated = WKWebView(frame: .zero)
            let response = HTTPURLResponse(url: base, statusCode: 200, httpVersion: "HTTP/1.1", headerFields: headers)!
            simulated.loadSimulatedRequest(URLRequest(url: base), response: response, responseData: Data(Self.body.utf8))
            try await WebKitReadiness.waitForDocument(in: simulated)
            let inline = try await simulated.evaluateJavaScript("window.inlineRan === true") as? Bool
            let origin = try await simulated.evaluateJavaScript("location.origin") as? String
            print("[cache probe] simulated \(label): inline script ran=\(String(describing: inline)), origin=\(origin ?? "?")")
        }
    }
}

@MainActor
private final class ResponseRecorder: NSObject, WKNavigationDelegate {
    var headers: [String: String] = [:]
    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationResponse: WKNavigationResponse,
        decisionHandler: @escaping @MainActor (WKNavigationResponsePolicy) -> Void
    ) {
        if let http = navigationResponse.response as? HTTPURLResponse, navigationResponse.isForMainFrame {
            for (key, value) in http.allHeaderFields {
                headers["\(key)"] = "\(value)"
            }
        }
        decisionHandler(.allow)
    }
}
