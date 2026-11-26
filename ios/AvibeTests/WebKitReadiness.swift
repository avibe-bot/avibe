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

    /// The direct path controller's bounds for an App whose tests are not
    /// about the listener wait: an open waits for the listener's first bind
    /// and its check until they end by themselves, serving or failed.
    ///
    /// The App waits one second (`DirectPathTiming.standard`) and then loads
    /// the host on the public path, routes it never on that store, stops
    /// the listener and records nothing: its designed fallback. On the CI
    /// simulator the first bind and its check have taken longer (1.19 s in
    /// run 45784478069, with web content launches of 3 s around it; run
    /// 44934936305 before it), and a test that then expects a routed host
    /// and a selection fails on a path the product never promised it. A
    /// bind that never finishes still ends: its check has `rebindSeconds`,
    /// and the foreground deadline fails it, on record. Every other bound
    /// is the App's. The one-second wait itself is held by the controller's
    /// own tests, and by the fixture test that gives an App no wait at all.
    static let afterASlowFirstBind = DirectPathTiming(
        listenerWaitSeconds: 60,
        rebindSeconds: DirectPathTiming.standard.rebindSeconds,
        foregroundSeconds: DirectPathTiming.standard.foregroundSeconds,
        strictOpenSeconds: DirectPathTiming.standard.strictOpenSeconds,
        retrySeconds: DirectPathTiming.standard.retrySeconds
    )

    /// The App's own clock, with account transports whose control-plane page
    /// may take a web content launch's time on the CI simulator
    /// (`afterWebContentLaunch`), and a direct path controller that waits
    /// for its first bind (`afterASlowFirstBind`): the timing an App fixture
    /// passes so that a slow launch is not the App's readiness timeout, nor
    /// a slow bind its fallback to the public path. A fixture that passes
    /// it bounds its waits on that App with `launchedTransportOperation`.
    /// It rides on the App's existing timing injection (#3973).
    @MainActor
    final class LaunchTolerantTiming: RuntimeOpenTiming {
        private let clock = ContinuousRuntimeOpenTiming()

        init() {
            MainThreadWatchdog.start()
        }

        var now: Duration { clock.now }

        var accountTransportDeadlines: MobileAuthTransportDeadlines {
            WebKitReadiness.afterWebContentLaunch
        }

        var directPathTiming: DirectPathTiming {
            WebKitReadiness.afterASlowFirstBind
        }

        func schedule(
            after delay: Duration,
            _ action: @escaping @MainActor () -> Void
        ) -> any RuntimeOpenTimer {
            clock.schedule(after: delay, action)
        }
    }

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
        MainThreadWatchdog.start()
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
    ///
    /// When the bootstrap settled on another state, the error carries what
    /// it left (`FixtureReadinessError.bootstrap`). A caller may build its
    /// App once more for exactly one such state, a bootstrap that stalled
    /// after its web content launch (`FixtureBootstrap.stalledAfterWebContentLaunch`),
    /// and for no other: `DirectPathAppTests.makeApp` does. `requestsSince`
    /// is the log's count when this App was made, for a log that an earlier
    /// App wrote to: what the bootstrap left is then this App's requests
    /// only.
    @MainActor
    static func waitForSignedInFixture(
        _ app: AppModel,
        accountID: String,
        instanceIDs: Set<String>,
        requests: FixtureRequestLog,
        requestsSince: Int = 0,
        budget: Duration = standardTransportOperation,
        recordsFailure: Bool = true,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async throws {
        // `recordsFailure: false` is for the one test that expects this to
        // throw and reads the error itself.
        func thrown(_ what: String, _ error: FixtureReadinessError) -> Error {
            recordsFailure ? recordThrown(what, error, file: file, line: line) : error
        }
        MainThreadWatchdog.start()
        let clock = ContinuousClock()
        let started = clock.now
        let began = requests.elapsed
        let deadline = started.advanced(by: budget)
        while case .loading = app.route {
            guard clock.now < deadline else {
                throw thrown(
                    "The signed-in fixture did not settle",
                    FixtureReadinessError(description: fixtureEvidence(
                        app, stage: "bootstrap did not settle", started: started,
                        requests: requests
                    ))
                )
            }
            try await Task.sleep(for: .milliseconds(50))
        }
        let settled = started.duration(to: clock.now)
        let evidence = fixtureEvidence(
            app, stage: "bootstrap settled", started: started, requests: requests
        )
        print("[iOS fixture] \(evidence)")
        guard case .onboarding(.instances) = app.route,
              app.mobileSessionCoordinator.currentAccount?.id == accountID,
              Set(app.mobileSessionCoordinator.currentInstances.map(\.id)) == instanceIDs
        else {
            throw thrown(
                "The signed-in fixture settled on another state",
                FixtureReadinessError(
                    description:
                        "\(evidence); expected account=\(accountID), instances=\(instanceIDs.sorted())",
                    bootstrap: FixtureBootstrap(
                        elapsed: settled,
                        signedIn: app.mobileSessionCoordinator.currentAccount != nil,
                        began: began,
                        served: requests.served(since: requestsSince)
                    )
                )
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
    /// One request the fixture answered: when, from the log's start, and
    /// with what.
    struct Entry: Equatable, CustomStringConvertible {
        let at: Duration
        let method: String
        let path: String
        let status: Int

        var description: String {
            "\(at) \(method) \(path) -> \(status)"
        }
    }

    private let lock = NSLock()
    private let started = ContinuousClock.now
    private var entries: [Entry] = []

    var count: Int { lock.withLock { entries.count } }

    /// The log's own time now: what an entry recorded now would have as
    /// its `at`.
    var elapsed: Duration { started.duration(to: .now) }

    /// The requests answered so far, in order, from the `index`-th on: an
    /// App made when the log's `count` was `index` has these and no
    /// earlier App's.
    func served(since index: Int = 0) -> [Entry] {
        lock.withLock { Array(entries.dropFirst(index)) }
    }

    func record(_ request: LocalHTTPRequest, status: Int) {
        lock.withLock {
            entries.append(
                Entry(at: started.duration(to: .now), method: request.method, path: request.path, status: status)
            )
        }
    }

    func summary(since index: Int = 0) -> String {
        lock.withLock {
            "requests=[" + entries.dropFirst(index).map(\.description).joined(separator: "; ") + "]"
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

/// Records the error a shared test helper is about to throw, and returns it.
///
/// On the CI simulator XCTest has reported errors thrown by these tests as
/// `caught error: "InvalidTransition { phase: idle targetPhase: failed(deinit) }"`
/// (a wait that timed out and a page load that failed, so far). The text
/// matches a private type of Apple's Gestures framework; why it is printed in
/// place of the test's own error is not known. The failure recorded here is
/// what names the real one in the log. Only for a throw no caller expects.
func recordThrown(
    _ what: String,
    _ error: Error,
    file: StaticString = #filePath,
    line: UInt = #line
) -> Error {
    XCTFail("\(what): \(String(reflecting: error))", file: file, line: line)
    return error
}

struct FixtureReadinessError: Error, CustomStringConvertible {
    let description: String
    /// What the bootstrap left, when it settled on another state than the
    /// signed-in one.
    var bootstrap: FixtureBootstrap? = nil
}

/// A fixture App's bootstrap that settled without the signed-in state, as
/// far as the one rebuild rule needs it.
struct FixtureBootstrap: Equatable {
    /// How long the App took to settle, from when the wait for it began.
    let elapsed: Duration
    /// The App has an account.
    let signedIn: Bool
    /// The request log's time when the wait began, which `elapsed` counts
    /// from; the entries' times are the log's.
    let began: Duration
    /// What the fixture's control plane answered this App, in order: not
    /// what it answered an App made before it on the same log.
    let served: [FixtureRequestLog.Entry]

    /// Whether this is the one stall a caller may rebuild its App for, once.
    ///
    /// Seen on the CI simulator (runs 45318053323 and 45005744290): the
    /// account transport's web content process took 35 to 37 s to launch
    /// and then never finished its first document. The fixture answered
    /// that document's request, `GET /api/config/auth`, the moment the
    /// process came up; nothing followed; and the App gave up at its own
    /// readiness deadline, signed out. Why such a process stays silent is
    /// not known. A longer wait does not help: it had been silent for more
    /// than 80 s. A new App makes a new WebView; that this is enough has
    /// not been seen yet, and the rebuild's two printed lines are how it
    /// will be.
    ///
    /// Exactly that, and nothing near it:
    /// - the App is signed out;
    /// - it settled at its readiness deadline (`readiness`, to within a
    ///   second: the wait begins a moment after the App does), not sooner.
    ///   An App that settled sooner was told something;
    /// - `/api/config/auth` was answered with `200`;
    /// - the session was never asked for. A session request that was
    ///   answered, refused or failed is the bootstrap's own result.
    func stalledAfterWebContentLaunch(readiness: Duration) -> Bool {
        !signedIn
            && elapsed + .seconds(1) >= readiness
            && served.contains { $0.method == "GET" && $0.path == Self.documentPath && $0.status == 200 }
            && !served.contains { $0.path == Self.sessionPath }
    }

    /// The stall in one line, for the log: when the document's request was
    /// answered (the launch, counted like `settled` from when the wait
    /// began), what was served, and what never was.
    func stallEvidence(readiness: Duration) -> String {
        let launched = served.first { $0.path == Self.documentPath }.map { "\($0.at - began)" } ?? "never"
        let answered = served.map { "\($0.method) \($0.path) -> \($0.status)" }.joined(separator: "; ")
        return "launch=\(launched), settled=\(elapsed), readiness=\(readiness), "
            + "served=[\(answered)], missing=[GET \(Self.sessionPath)], account=nil"
    }

    static let documentPath = "/api/config/auth"
    static let sessionPath = "/api/mobile/session"
}

enum WebKitReadinessError: Error {
    case documentNotLoaded
}

/// Prints every time the main thread does not answer for over a second, with
/// the test running when it stopped answering: `[main-thread watchdog] held
/// over 1 s in <test>` at once, again every 10 s while it lasts, and
/// `[main-thread watchdog] blocked 2.4 s, from <test> to <test>` when it
/// ends. A hold that never ends is still in the log.
///
/// Every App fixture's WebView callbacks, timers and deadlines run on the
/// main thread, so a hold there looks like a page that never loads (the
/// 120 s bootstrap stall of run 45564932372). This shows any hold in the run
/// log. It starts with the first App fixture (`LaunchTolerantTiming`,
/// `waitForSignedInFixture`, `waitForDocument`) and runs for the rest of the
/// bundle. It only reports; it fails nothing.
final class MainThreadWatchdog: NSObject, XCTestObservation, @unchecked Sendable {
    static let threshold: TimeInterval = 1

    private static let shared = MainThreadWatchdog()
    private let lock = NSLock()
    private var started = false
    private var currentTest = "(before the first test)"

    /// Idempotent; call from the main thread.
    @MainActor
    static func start() {
        shared.startOnce()
    }

    @MainActor
    private func startOnce() {
        lock.lock()
        let first = !started
        started = true
        lock.unlock()
        guard first else {
            return
        }
        // Started from inside a test, whose start was already announced.
        setTest("(the test that started the watchdog)")
        XCTestObservationCenter.shared.addTestObserver(self)
        let thread = Thread { [self] in self.watch() }
        thread.name = "main-thread watchdog"
        thread.qualityOfService = .userInitiated
        thread.start()
    }

    func testCaseWillStart(_ testCase: XCTestCase) {
        setTest(testCase.name)
    }

    func testCaseDidFinish(_ testCase: XCTestCase) {
        setTest("(after \(testCase.name))")
    }

    private func setTest(_ name: String) {
        lock.lock()
        currentTest = name
        lock.unlock()
    }

    private func test() -> String {
        lock.lock()
        defer { lock.unlock() }
        return currentTest
    }

    private func watch() {
        while true {
            let sent = Date()
            let answered = DispatchSemaphore(value: 0)
            DispatchQueue.main.async { answered.signal() }
            if answered.wait(timeout: .now() + Self.threshold) == .timedOut {
                let from = test()
                print("[main-thread watchdog] held over 1 s in \(from)")
                while answered.wait(timeout: .now() + 10) == .timedOut {
                    print(
                        "[main-thread watchdog] still held after "
                            + "\(Int(Date().timeIntervalSince(sent))) s, now in \(test())"
                    )
                }
                let seconds = Date().timeIntervalSince(sent)
                print(
                    "[main-thread watchdog] blocked \(String(format: "%.1f", seconds)) s, "
                        + "from \(from) to \(test())"
                )
            }
            Thread.sleep(forTimeInterval: 0.25)
        }
    }
}
