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

    /// `webContentLaunch` for `XCTestCase.fulfillment(of:timeout:)`.
    static var webContentLaunchSeconds: TimeInterval {
        TimeInterval(webContentLaunch.components.seconds)
    }

    /// Launches a web content process in `dataStore` before a step whose own
    /// deadline must not also carry WebKit's launch on CI: a WebView loads a
    /// document at `baseURL`'s site and is waited for within
    /// `webContentLaunch`. The caller keeps the returned WebView alive for
    /// the step and releases it before the store is removed. Returns how
    /// long the launch took, for the run's evidence.
    @MainActor
    static func warmWebContent(
        in dataStore: WKWebsiteDataStore,
        baseURL: URL
    ) async throws -> (webView: WKWebView, launch: Duration) {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = dataStore
        let webView = WKWebView(frame: .zero, configuration: configuration)
        let started = ContinuousClock.now
        webView.loadHTMLString("<!doctype html><title>warm</title>", baseURL: baseURL)
        try await waitForDocument(in: webView)
        return (webView, started.duration(to: .now))
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
        requests: FixtureRequestLog
    ) async throws {
        let clock = ContinuousClock()
        let started = clock.now
        let deadline = started.advanced(by: standardTransportOperation)
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

/// Draft probe (not for merge): does a WebView reuse a warm web content
/// process? Prints each WebView's process identifier and launch time.
@MainActor
final class WebContentReuseProbeTests: XCTestCase {
    private func pid(_ webView: WKWebView) -> Int {
        (webView.value(forKey: "_webProcessIdentifier") as? Int) ?? -1
    }

    private func open(_ store: WKWebsiteDataStore, _ label: String) async throws -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = store
        let webView = WKWebView(frame: .zero, configuration: configuration)
        let started = ContinuousClock.now
        webView.loadHTMLString("<!doctype html><title>\(label)</title>", baseURL: URL(string: "http://127.0.0.1:43127/")!)
        try await WebKitReadiness.waitForDocument(in: webView)
        print("[reuse probe] \(label): pid=\(pid(webView)) launch=\(started.duration(to: .now))")
        return webView
    }

    func testWhichWebViewsShareAProcess() async throws {
        for round in 0..<5 {
            let x = UUID(), y = UUID()
            let storeX = WKWebsiteDataStore(forIdentifier: x)
            let storeY = WKWebsiteDataStore(forIdentifier: y)
            var a: WKWebView? = try await open(storeX, "r\(round) A store X")
            let b = try await open(storeX, "r\(round) B store X, A alive")
            let d = try await open(storeY, "r\(round) D store Y, A alive")
            a = nil
            try await Task.sleep(for: .milliseconds(300))
            let c = try await open(storeX, "r\(round) C store X, A released")
            let e = try await open(.nonPersistent(), "r\(round) E non-persistent")
            withExtendedLifetime([b, c, d, e]) {}
            _ = a
        }
    }
}
