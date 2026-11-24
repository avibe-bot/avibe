import XCTest
@testable import Avibe

/// The one rebuild rule of the App fixtures: `FixtureBootstrap` decides,
/// from what a bootstrap left, whether it is the stall after a web content
/// launch. These are the two stalls recorded on CI and the states next to
/// them, none of which may rebuild an App.
final class WebKitReadinessTests: XCTestCase {
    private let readiness = Duration.seconds(120)

    private func entry(
        _ seconds: Double,
        _ method: String,
        _ path: String,
        _ status: Int
    ) -> FixtureRequestLog.Entry {
        FixtureRequestLog.Entry(at: .seconds(seconds), method: method, path: path, status: status)
    }

    private func bootstrap(
        elapsed: Double,
        signedIn: Bool = false,
        served: [FixtureRequestLog.Entry]
    ) -> FixtureBootstrap {
        FixtureBootstrap(elapsed: .seconds(elapsed), signedIn: signedIn, served: served)
    }

    func testTheTwoRecordedStallsAreTheStall() {
        // Runs 45318053323 and 45005744290, as their logs have them.
        for (elapsed, launch) in [(120.09273025, 35.587694958), (120.394388667, 38.290254167)] {
            let stalled = bootstrap(
                elapsed: elapsed,
                served: [entry(launch, "GET", "/api/config/auth", 200)]
            )
            XCTAssertTrue(stalled.stalledAfterWebContentLaunch(readiness: readiness), "\(elapsed)")
        }
    }

    func testTheWaitMayBeginAMomentAfterTheAppDoes() {
        let served = [entry(35, "GET", "/api/config/auth", 200)]
        XCTAssertTrue(
            bootstrap(elapsed: 119.2, served: served).stalledAfterWebContentLaunch(readiness: readiness)
        )
        XCTAssertFalse(
            bootstrap(elapsed: 118.9, served: served).stalledAfterWebContentLaunch(readiness: readiness)
        )
    }

    func testAnAppThatWasAnsweredIsNotTheStall() {
        // The session was asked for: refused, failed or answered, that is
        // the bootstrap's own result, at any time.
        for status in [200, 401, 503] {
            let answered = bootstrap(
                elapsed: 120.1,
                served: [
                    entry(35, "GET", "/api/config/auth", 200),
                    entry(119, "GET", "/api/mobile/session", status),
                ]
            )
            XCTAssertFalse(answered.stalledAfterWebContentLaunch(readiness: readiness), "\(status)")
        }
        // The two bootstraps the same logs have beside the stalls: a
        // refused session, settled in seconds.
        let refused = bootstrap(
            elapsed: 4.861987417,
            served: [
                entry(0.974798041, "GET", "/api/config/auth", 200),
                entry(4.860521083, "GET", "/api/mobile/session", 503),
                entry(4.868460708, "GET", "/api/config/auth", 200),
            ]
        )
        XCTAssertFalse(refused.stalledAfterWebContentLaunch(readiness: readiness))
    }

    func testEveryOtherPartOfTheSignatureIsRequired() {
        let document = [entry(35, "GET", "/api/config/auth", 200)]
        // Signed in.
        XCTAssertFalse(
            bootstrap(elapsed: 120.1, signedIn: true, served: document)
                .stalledAfterWebContentLaunch(readiness: readiness)
        )
        // Settled before its readiness deadline: it was told something.
        XCTAssertFalse(
            bootstrap(elapsed: 60, served: document).stalledAfterWebContentLaunch(readiness: readiness)
        )
        // Nothing was served: the process never came up, which is the
        // launch's own timeout and not this stall.
        XCTAssertFalse(
            bootstrap(elapsed: 120.1, served: []).stalledAfterWebContentLaunch(readiness: readiness)
        )
        // The document's request was not answered with 200.
        XCTAssertFalse(
            bootstrap(elapsed: 120.1, served: [entry(35, "GET", "/api/config/auth", 500)])
                .stalledAfterWebContentLaunch(readiness: readiness)
        )
        // Another request only.
        XCTAssertFalse(
            bootstrap(elapsed: 120.1, served: [entry(35, "GET", "/api/other", 200)])
                .stalledAfterWebContentLaunch(readiness: readiness)
        )
        // The deadline is the App's own: the same bootstrap is no stall for
        // an App that waits longer.
        XCTAssertFalse(
            bootstrap(elapsed: 120.1, served: document)
                .stalledAfterWebContentLaunch(readiness: .seconds(180))
        )
    }

    func testTheStallLineNamesTheLaunchWhatWasServedAndWhatWasMissing() {
        let stalled = bootstrap(
            elapsed: 120.5,
            served: [entry(35.5, "GET", "/api/config/auth", 200)]
        )
        XCTAssertEqual(
            stalled.stallEvidence(readiness: readiness),
            "launch=35.5 seconds, settled=120.5 seconds, readiness=120.0 seconds, "
                + "served=[GET /api/config/auth -> 200], missing=[GET /api/mobile/session], account=nil"
        )
    }

    func testTheRequestLogKeepsItsSummaryAndGivesWhatItServed() {
        let log = FixtureRequestLog()
        log.record(
            LocalHTTPRequest(method: "GET", path: "/api/config/auth", queryItems: [], headers: [:], body: Data()),
            status: 200
        )
        log.record(
            LocalHTTPRequest(method: "GET", path: "/api/mobile/session", queryItems: [], headers: [:], body: Data()),
            status: 503
        )
        XCTAssertEqual(log.count, 2)
        XCTAssertEqual(log.served.map(\.method), ["GET", "GET"])
        XCTAssertEqual(log.served.map(\.path), ["/api/config/auth", "/api/mobile/session"])
        XCTAssertEqual(log.served.map(\.status), [200, 503])
        // The line the fixtures print and other tests read is unchanged:
        // `<when> <method> <path> -> <status>`, joined by "; ".
        let summary = log.summary()
        XCTAssertTrue(summary.hasPrefix("requests=["), summary)
        XCTAssertTrue(summary.contains(" GET /api/config/auth -> 200; "), summary)
        XCTAssertTrue(summary.hasSuffix(" GET /api/mobile/session -> 503]"), summary)
        XCTAssertTrue(log.summary(since: 1).hasPrefix("requests=["), log.summary(since: 1))
        XCTAssertFalse(log.summary(since: 1).contains("/api/config/auth"))
    }
}
