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
}

enum WebKitReadinessError: Error {
    case documentNotLoaded
}
