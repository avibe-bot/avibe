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
