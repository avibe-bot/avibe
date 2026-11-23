import UIKit
import WebKit
import XCTest
@testable import Avibe

@MainActor
final class ShowShareTests: XCTestCase {
    // These tests protect the native owner boundary: real authenticated WebKit
    // reads, current visible pixels, and one operation's system handoff/lease.
    // The old parser/files-only tests could not detect a broken capture path.
    func testC1OnlyAcceptsTypedPageReferencesAndUUIDv4Operations() throws {
        let params: [String: Any] = [
            "operationId": UUID().uuidString, "kind": "pdf", "sessionId": "ses_中文",
        ]
        XCTAssertEqual(try XCTUnwrap(ShowShareRequest.parse(params)).sessionID, "ses_中文")
        for key in ["url", "path", "html", "rect", "credentials", "bytes"] {
            var invalid = params
            invalid[key] = "untrusted"
            XCTAssertNil(ShowShareRequest.parse(invalid), key)
        }
        for (key, values) in [
            "kind": ["file", "html", "", "PDF"],
            "sessionId": ["", "../page", "a/b", "a\\b", "a\n", String(repeating: "中", count: 67)],
            "operationId": ["bad", "00000000-0000-0000-0000-000000000000", "00000000-0000-4000-0000-000000000000"],
        ] {
            for value in values {
                var invalid = params
                invalid[key] = value
                XCTAssertNil(ShowShareRequest.parse(invalid), "\(key): \(value)")
            }
        }
        for key in params.keys {
            var missing = params
            missing.removeValue(forKey: key)
            XCTAssertNil(ShowShareRequest.parse(missing))
            var wrongType = params
            wrongType[key] = 1
            XCTAssertNil(ShowShareRequest.parse(wrongType))
        }
        XCTAssertEqual(NativeShellMetadata.capabilities["c1.share_draft"], 1)
        XCTAssertEqual(ShowShareCoordinator.capabilities["version"] as? Int, 1)
        XCTAssertEqual(ShowShareCoordinator.capabilities["inbound_kinds"] as? [String], [])
        XCTAssertEqual(ShowShareCoordinator.capabilities["outbound_kinds"] as? [String], ["link", "pdf", "image"])
        XCTAssertEqual(ShowShareCoordinator.capabilities["max_attachments"] as? Int, 1)
        XCTAssertEqual(ShowShareCoordinator.capabilities["supports_pending"] as? Bool, false)
    }

    func testOriginalLinksAndPermissionsSurviveAuthenticatedIsolatedWorldReads() async throws {
        let fixture = try await ShareFixture.make()
        defer { fixture.close() }
        // Page-world replacement must not forge access or replace native fetch.
        _ = try await fixture.webView.evaluateJavaScript("""
            window.fetch = async () => { throw new Error('page fetch must not be used'); };
            window.__avibeShowShare = { operationID: 'forged' };
            """)
        for (visibility, path) in [("private", "/show/ses_visible/"), ("limited", "/p/original-limited/?key=fixture"), ("public", "/p/original-public/")] {
            fixture.state.update {
                $0.visibility = visibility
                $0.activeURL = visibility == "public" ? path : nil
                $0.publicURL = visibility == "limited" ? path : nil
            }
            let state = try await fixture.inspect(kind: .link)
            XCTAssertEqual(state["allowed"] as? Bool, true, visibility)
            XCTAssertEqual(state["url"] as? String, fixture.baseURL.absoluteString + path, visibility)
            XCTAssertEqual(fixture.state.current.visibility, visibility)
        }
        let reads = fixture.state.apiRequests
        XCTAssertEqual(reads.count, 6)
        XCTAssertTrue(reads.allSatisfy { $0.method == "GET" && $0.headers["cookie"]?.contains("fixture_session=read-only") == true })
        XCTAssertEqual(reads.map(\.path), Array(repeating: ["/api/show-pages/ses_visible/access", "/api/show-pages/ses_visible"], count: 3).flatMap { $0 })
        XCTAssertEqual(fixture.state.rejectedWrites, 0)
        XCTAssertEqual(try fixture.exportedFiles(), [])
    }

    func testActualBridgeAdvertisesC1AndRejectsUntrustedParametersAndChildFrames() async throws {
        let fixture = try await ShareFixture.make()
        defer { fixture.close() }
        let result = try await fixture.webView.callAsyncJavaScript("""
            const caps = await window.avibeNative.call('share.capabilities');
            let invalid;
            try {
              await window.avibeNative.call('share.present', {
                operationId, kind: 'image', sessionId: 'ses_visible', url: '/arbitrary'
              });
            } catch (error) { invalid = error.code; }
            const child = document.querySelector('iframe').contentWindow;
            const childReply = await child.webkit.messageHandlers.avibeNative.postMessage({
              method: 'share.present', params: {operationId, kind:'link', sessionId:'ses_visible'}
            });
            return {caps, invalid, childReply};
            """, arguments: ["operationId": UUID().uuidString], in: nil, contentWorld: .page)
        let object = try XCTUnwrap(result as? [String: Any])
        XCTAssertEqual((object["caps"] as? [String: Any])?["outbound_kinds"] as? [String], ["link", "pdf", "image"])
        XCTAssertEqual(object["invalid"] as? String, "invalid_argument")
        let childReply = try XCTUnwrap(object["childReply"] as? [String: Any])
        XCTAssertEqual(childReply["ok"] as? Bool, false)
        XCTAssertEqual((childReply["error"] as? [String: Any])?["code"] as? String, "forbidden")
        XCTAssertTrue(fixture.presenter.activities.isEmpty)
        XCTAssertTrue(fixture.state.apiRequests.isEmpty)
    }

    func testRealPNGAndPDFContainOnlyCurrentVisibleFrameDimensions() async throws {
        let fixture = try await ShareFixture.make()
        defer { fixture.close() }
        let inspected = try await fixture.inspect(kind: .image)
        let rect = try XCTUnwrap(ShowShareCoordinator.captureRect(inspected, in: fixture.webView))
        XCTAssertGreaterThan(rect.width, 100)
        XCTAssertGreaterThan(rect.height, 100)
        XCTAssertLessThan(rect.height, fixture.webView.bounds.height)
        for kind: ShowShareRequest.Kind in [.image, .pdf] {
            let request = fixture.request(kind)
            let result = ShareReplies()
            fixture.coordinator.share(request, from: fixture.webView, origin: fixture.origin, reply: result.receive)
            try await eventually("real \(kind) export prepared") { fixture.presenter.activities.count == (kind == .image ? 1 : 2) || !result.values.isEmpty }
            XCTAssertEqual(result.values, [], "No completion before the system callback")
            let url = try XCTUnwrap(try fixture.exportedFiles().first)
            let bytes = try Data(contentsOf: url)
            if kind == .image {
                XCTAssertEqual(Array(bytes.prefix(8)), [137, 80, 78, 71, 13, 10, 26, 10])
                let image = try XCTUnwrap(UIImage(data: bytes)?.cgImage)
                XCTAssertEqual(CGFloat(image.width) / CGFloat(image.height), rect.width / rect.height, accuracy: 0.02)
                XCTAssertGreaterThan(image.width, 100)
                XCTAssertGreaterThan(image.height, 100)
            } else {
                XCTAssertEqual(String(decoding: bytes.prefix(5), as: UTF8.self), "%PDF-")
                let provider = try XCTUnwrap(CGDataProvider(data: bytes as CFData))
                let pdf = try XCTUnwrap(CGPDFDocument(provider))
                XCTAssertEqual(pdf.numberOfPages, 1)
                let box = try XCTUnwrap(pdf.page(at: 1)).getBoxRect(.mediaBox)
                XCTAssertEqual(box.width, rect.width, accuracy: 1)
                XCTAssertEqual(box.height, rect.height, accuracy: 1)
            }
            try fixture.presenter.finishLast(completed: false)
            XCTAssertEqual(result.values.map(\.status), [.cancelled])
            XCTAssertEqual(try fixture.exportedFiles(), [])
        }
        XCTAssertEqual(fixture.state.rejectedWrites, 0)
    }

    func testCompletedExportKeepsFiveMinuteLeaseAndDuplicateReplaysOneResult() async throws {
        let fixture = try await ShareFixture.make()
        defer { fixture.close() }
        let request = fixture.request(.image)
        let first = ShareReplies(), duplicate = ShareReplies(), replay = ShareReplies()
        fixture.coordinator.share(request, from: fixture.webView, origin: fixture.origin, reply: first.receive)
        fixture.coordinator.share(request, from: fixture.webView, origin: fixture.origin, reply: duplicate.receive)
        try await eventually("one activity") { fixture.presenter.activities.count == 1 || !first.values.isEmpty }
        XCTAssertEqual(fixture.presenter.activities.count, 1)
        let url = try XCTUnwrap(try fixture.exportedFiles().first)
        try fixture.presenter.finishLast(completed: true)
        XCTAssertEqual(first.values.map(\.status), [.completed])
        XCTAssertEqual(first.values, duplicate.values)
        fixture.coordinator.share(request, from: fixture.webView, origin: fixture.origin, reply: replay.receive)
        XCTAssertEqual(replay.values, first.values)
        XCTAssertEqual(fixture.presenter.activities.count, 1)
        fixture.clock.advance(299)
        fixture.coordinator.reconcile()
        XCTAssertTrue(FileManager.default.fileExists(atPath: url.path))
        fixture.clock.advance(1)
        fixture.coordinator.reconcile()
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
        let conflict = ShowShareRequest(operationID: request.operationID, kind: .pdf, sessionID: request.sessionID)
        let conflicted = ShareReplies()
        fixture.coordinator.share(conflict, from: fixture.webView, origin: fixture.origin, reply: conflicted.receive)
        XCTAssertEqual(conflicted.values.first?.error, "operation_conflict")
    }

    func testCancellationAndSystemFailureRemoveExportsImmediately() async throws {
        let fixture = try await ShareFixture.make()
        defer { fixture.close() }
        for failed in [false, true] {
            let results = ShareReplies()
            fixture.coordinator.share(fixture.request(.image), from: fixture.webView, origin: fixture.origin, reply: results.receive)
            let count = failed ? 2 : 1
            try await eventually("prepared export") { fixture.presenter.activities.count == count || !results.values.isEmpty }
            XCTAssertEqual(try fixture.exportedFiles().count, 1)
            try fixture.presenter.finishLast(completed: failed, error: failed ? NSError(domain: "ShowShareTests", code: 1) : nil)
            XCTAssertEqual(results.values.first?.status, failed ? .failed : .cancelled)
            XCTAssertEqual(results.values.first?.error, failed ? "system_share_failed" : nil)
            XCTAssertEqual(try fixture.exportedFiles(), [])
        }
    }

    func testBusyExpiryAndLateCallbackCannotFinishAnotherOperation() async throws {
        let fixture = try await ShareFixture.make()
        defer { fixture.close() }
        let expired = ShareReplies(), busy = ShareReplies()
        fixture.coordinator.share(fixture.request(.image), from: fixture.webView, origin: fixture.origin, reply: expired.receive)
        try await eventually("activity prepared") { fixture.presenter.activities.count == 1 || !expired.values.isEmpty }
        let late = try XCTUnwrap(fixture.presenter.activities.first?.completionWithItemsHandler)
        fixture.coordinator.share(fixture.request(.pdf), from: fixture.webView, origin: fixture.origin, reply: busy.receive)
        XCTAssertEqual(busy.values.first?.error, "busy")
        XCTAssertEqual(fixture.presenter.activities.count, 1)
        fixture.clock.advance(899)
        fixture.coordinator.reconcile()
        XCTAssertEqual(expired.values, [])
        XCTAssertEqual(try fixture.exportedFiles().count, 1)
        fixture.clock.advance(1)
        fixture.coordinator.reconcile()
        XCTAssertEqual(expired.values.map(\.status), [.expired])
        XCTAssertEqual(try fixture.exportedFiles(), [])
        let next = ShareReplies()
        fixture.coordinator.share(fixture.request(.link), from: fixture.webView, origin: fixture.origin, reply: next.receive)
        try await eventually("next activity") { fixture.presenter.activities.count == 2 || !next.values.isEmpty }
        late(nil, true, nil, nil)
        XCTAssertEqual(expired.values.map(\.status), [.expired])
        XCTAssertEqual(next.values, [])
        try fixture.presenter.finishLast(completed: false)
        XCTAssertEqual(next.values.map(\.status), [.cancelled])
    }

    func testLifecycleCancellationInvalidatesPendingAndPresentedOperations() async throws {
        let fixture = try await ShareFixture.make()
        defer { fixture.close() }
        let pending = ShareReplies()
        fixture.coordinator.share(fixture.request(.image), from: fixture.webView, origin: fixture.origin, reply: pending.receive)
        fixture.coordinator.cancel(in: fixture.webView)
        XCTAssertEqual(pending.values.map(\.status), [.cancelled])
        XCTAssertTrue(fixture.presenter.activities.isEmpty)
        let presented = ShareReplies()
        fixture.coordinator.share(fixture.request(.image), from: fixture.webView, origin: fixture.origin, reply: presented.receive)
        try await eventually("presented export") { fixture.presenter.activities.count == 1 || !presented.values.isEmpty }
        let callback = try XCTUnwrap(fixture.presenter.activities.last?.completionWithItemsHandler)
        fixture.coordinator.cancel(in: fixture.webView)
        XCTAssertEqual(presented.values.map(\.status), [.cancelled])
        XCTAssertEqual(try fixture.exportedFiles(), [])
        callback(nil, true, nil, nil)
        XCTAssertEqual(presented.values.map(\.status), [.cancelled])
    }

    func testAccessErrorsAndWrongSessionFailBeforeExportOrPresentation() async throws {
        let fixture = try await ShareFixture.make()
        defer { fixture.close() }
        for mode in 0..<4 {
            fixture.state.update {
                $0.canUse = mode != 0
                $0.accessStatus = mode == 1 ? 503 : 200
                $0.offline = mode == 2
                $0.pageSession = mode == 3 ? "ses_wrong" : "ses_visible"
            }
            let result = ShareReplies()
            fixture.coordinator.share(fixture.request(.image), from: fixture.webView, origin: fixture.origin, reply: result.receive)
            try await eventually("access denial \(mode)") { !result.values.isEmpty }
            XCTAssertEqual(result.values.first?.status, .failed)
            XCTAssertTrue(fixture.presenter.activities.isEmpty)
            XCTAssertEqual(try fixture.exportedFiles(), [])
        }
    }

    func testHiddenOverlaidAndMismatchedFramesAreRejectedInRealDOM() async throws {
        let fixture = try await ShareFixture.make()
        defer { fixture.close() }
        let mutations = [
            "frame.dataset.avibeShowSession = 'ses_wrong'",
            "frame.style.display = 'none'",
            "frame.style.opacity = '0.5'",
            "frame.style.transform = 'translateY(2000px)'",
            "const cover = document.createElement('div'); cover.id = 'cover'; cover.style = 'position:fixed;inset:0;background:white;z-index:100'; document.body.append(cover)",
            "const cover = document.createElement('div'); cover.id = 'cover'; cover.style = 'position:fixed;left:200px;top:180px;width:70px;height:24px;background:white;z-index:100'; document.body.append(cover)",
            "const cover = document.createElement('div'); cover.id = 'cover'; cover.style = 'position:fixed;left:200px;top:180px;width:70px;height:24px;background:white;z-index:100;pointer-events:none'; document.body.append(cover)",
        ]
        for mutation in mutations {
            _ = try await fixture.webView.evaluateJavaScript("""
                document.getElementById('cover')?.remove();
                var frame = document.querySelector('iframe');
                frame.dataset.avibeShowSession = 'ses_visible'; frame.removeAttribute('style');
                { \(mutation); }
                """)
            let result = ShareReplies()
            fixture.coordinator.share(fixture.request(.image), from: fixture.webView, origin: fixture.origin, reply: result.receive)
            try await eventually("DOM rejection: \(mutation)") {
                !result.values.isEmpty || !fixture.presenter.activities.isEmpty
            }
            XCTAssertEqual(result.values.first?.status, .failed, mutation)
            XCTAssertTrue(fixture.presenter.activities.isEmpty, mutation)
            if !fixture.presenter.activities.isEmpty {
                fixture.coordinator.cancel(in: fixture.webView)
                return
            }
        }
        XCTAssertEqual(try fixture.exportedFiles(), [])
    }

    func testCrossOriginFrameAndWrongCurrentRouteAreRejected() async throws {
        let foreign = try LocalHTTPServer { _ in .html("<body>foreign fixture</body>") }
        let foreignURL = try await foreign.start()
        defer { foreign.stop() }
        let fixture = try await ShareFixture.make()
        defer { fixture.close() }
        for url in [fixture.baseURL.appendingPathComponent("show/ses_wrong/"), foreignURL] {
            _ = try await fixture.webView.callAsyncJavaScript("""
                const frame = document.querySelector('iframe');
                await new Promise(resolve => { frame.onload = resolve; frame.src = target; });
                return true;
                """, arguments: ["target": url.absoluteString], in: nil, contentWorld: .page)
            let result = ShareReplies()
            fixture.coordinator.share(fixture.request(.image), from: fixture.webView, origin: fixture.origin, reply: result.receive)
            try await eventually("foreign/wrong route rejected") { !result.values.isEmpty }
            XCTAssertEqual(result.values.first?.status, .failed)
        }
        XCTAssertTrue(fixture.presenter.activities.isEmpty)
        XCTAssertEqual(try fixture.exportedFiles(), [])
    }

    func testLoadingCurrentFrameCannotBeExported() async throws {
        let fixture = try await ShareFixture.make()
        defer { fixture.close() }
        _ = try await fixture.webView.evaluateJavaScript(
            "document.querySelector('iframe').src = '/show/ses_visible/loading'"
        )
        await fulfillment(
            of: [fixture.state.loadingHeaders.expectation],
            timeout: WebKitReadiness.webContentLaunchSeconds
        )
        let result = ShareReplies()
        fixture.coordinator.share(fixture.request(.image), from: fixture.webView, origin: fixture.origin, reply: result.receive)
        try await eventually("loading frame rejected") { !result.values.isEmpty }
        XCTAssertEqual(result.values.first?.status, .failed)
        XCTAssertTrue(fixture.presenter.activities.isEmpty)
        XCTAssertEqual(try fixture.exportedFiles(), [])
    }

    func testPresentationFailureCleansPreparedFileAndAllowsRetryWithNewOperation() async throws {
        let fixture = try await ShareFixture.make()
        defer { fixture.close() }
        fixture.presenter.accepts = false
        let failed = ShareReplies()
        fixture.coordinator.share(fixture.request(.image), from: fixture.webView, origin: fixture.origin, reply: failed.receive)
        try await eventually("presentation failure") { !failed.values.isEmpty }
        XCTAssertEqual(failed.values.first?.error, "presentation_failed")
        XCTAssertEqual(try fixture.exportedFiles(), [])
        fixture.presenter.accepts = true
        let retried = ShareReplies()
        fixture.coordinator.share(fixture.request(.link), from: fixture.webView, origin: fixture.origin, reply: retried.receive)
        try await eventually("retry presented") { fixture.presenter.activities.count == 2 || !retried.values.isEmpty }
        XCTAssertTrue(retried.values.isEmpty)
        try fixture.presenter.finishLast(completed: false)
        XCTAssertEqual(retried.values.map(\.status), [.cancelled])
    }

    func testDefaultPresenterShowsActualSystemActivityController() async throws {
        let fixture = try await ShareFixture.make(systemPresenter: true)
        defer { fixture.close() }
        let result = ShareReplies()
        fixture.coordinator.share(fixture.request(.link), from: fixture.webView, origin: fixture.origin, reply: result.receive)
        try await eventually("system activity controller presented") {
            fixture.controller.presentedViewController is UIActivityViewController || !result.values.isEmpty
        }
        let activity = try XCTUnwrap(fixture.controller.presentedViewController as? UIActivityViewController)
        try await eventually("system activity view visible") { activity.viewIfLoaded?.window != nil && !activity.isBeingPresented }
        XCTAssertFalse(fixture.window.isHidden)
        XCTAssertGreaterThan(activity.view.bounds.width, 0)
        let image = UIGraphicsImageRenderer(bounds: fixture.window.bounds).image { _ in
            fixture.window.drawHierarchy(in: fixture.window.bounds, afterScreenUpdates: true)
        }
        let attachment = XCTAttachment(image: image)
        attachment.name = "Show sharing — actual iOS activity panel"
        attachment.lifetime = .keepAlways
        add(attachment)
        XCTAssertTrue(result.values.isEmpty)
        fixture.coordinator.cancel(in: fixture.webView)
        XCTAssertEqual(result.values.map(\.status), [.cancelled])
    }

    func testTemporaryFilesAreBoundedAndOrphansAreRemovedByAge() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("ShowShareTests-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let files = ShowShareFiles(root: root)
        let request = ShowShareRequest(operationID: UUID(), kind: .image, sessionID: "ses_中文")
        let url = try files.write(Data([1, 2, 3]), request: request)
        XCTAssertEqual(try Data(contentsOf: url), Data([1, 2, 3]))
        XCTAssertFalse(url.path.contains(request.sessionID))
        XCTAssertThrowsError(try files.write(Data(), request: request))
        XCTAssertThrowsError(try files.write(Data(repeating: 0, count: ShowShareFiles.maximumBytes + 1), request: request))
        XCTAssertThrowsError(try files.write(Data([1]), request: .init(operationID: UUID(), kind: .link, sessionID: "ses")))
        try files.removeOrphans()
        XCTAssertTrue(FileManager.default.fileExists(atPath: url.path))
        try files.removeOrphans(asOf: Date().addingTimeInterval(ShowShareFiles.orphanLifetime + 1))
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
        try files.remove(request.operationID)
    }

    private func eventually(_ description: String, condition: @MainActor () -> Bool) async throws {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: WebKitReadiness.webContentLaunch)
        while !condition() {
            guard clock.now < deadline else { throw FixtureReadinessError(description: description) }
            try await Task.sleep(for: .milliseconds(25))
        }
    }
}

@MainActor
private final class ShareClock {
    var date = Date(timeIntervalSince1970: 1_800_000_000)
    func advance(_ seconds: TimeInterval) { date = date.addingTimeInterval(seconds) }
}

@MainActor
private final class ShareReplies {
    var values: [ShowShareResult] = []
    func receive(_ result: ShowShareResult) { values.append(result) }
}

@MainActor
private final class SharePresenter {
    var activities: [UIActivityViewController] = []
    var accepts = true
    func present(_ activity: UIActivityViewController, from webView: WKWebView) -> Bool {
        XCTAssertNotNil(webView.window)
        activities.append(activity)
        return accepts
    }
    func finishLast(completed: Bool, error: Error? = nil) throws {
        let callback = try XCTUnwrap(activities.last?.completionWithItemsHandler)
        callback(nil, completed, nil, error)
    }
}

private final class ShareServerState: @unchecked Sendable {
    struct Values {
        var visibility = "private"
        var activeURL: String?
        var publicURL: String?
        var canUse = true
        var accessStatus = 200
        var offline = false
        var pageSession = "ses_visible"
    }
    private let lock = NSLock()
    let loadingHeaders = FixtureEvent(description: "Current iframe response started but has not finished")
    let loadingRelease = DispatchSemaphore(value: 0)
    private var values = Values()
    private var requests: [LocalHTTPRequest] = []
    var current: Values { lock.withLock { values } }
    var apiRequests: [LocalHTTPRequest] { lock.withLock { requests.filter { $0.path.hasPrefix("/api/") } } }
    var rejectedWrites: Int { lock.withLock { requests.filter { $0.method != "GET" }.count } }
    func update(_ mutation: (inout Values) -> Void) { lock.withLock { mutation(&values) } }

    func respond(_ request: LocalHTTPRequest) -> LocalHTTPResponse {
        let state = lock.withLock {
            requests.append(request)
            return values
        }
        guard request.method == "GET" else { return .status(405) }
        if request.path.hasPrefix("/api/") {
            guard request.headers["cookie"]?.contains("fixture_session=read-only") == true else { return .status(401) }
            if request.path == "/api/show-pages/ses_visible/access" {
                guard state.accessStatus == 200 else { return .status(state.accessStatus) }
                return .json("{\"can_use\":\(state.canUse),\"can_manage\":false}")
            }
            guard request.path == "/api/show-pages/ses_visible" else { return .status(404) }
            var page: [String: Any] = [
                "session_id": state.pageSession, "visibility": state.visibility,
                "offline": state.offline, "title": "Fixture visible page", "share_id": "original-public",
            ]
            if let activeURL = state.activeURL { page["active_url"] = activeURL }
            if let publicURL = state.publicURL { page["public_url"] = publicURL }
            let data = try! JSONSerialization.data(withJSONObject: page)
            return .json(String(decoding: data, as: UTF8.self))
        }
        if request.path == "/show/ses_visible/loading" {
            // LocalHTTPServer sends a prefix then waits off its network queue.
            // Keep the real document loading without sleeps or remote traffic.
            return LocalHTTPResponse(
                status: 200, headers: [("Content-Type", "text/html")],
                body: Data(("<!doctype html><body>loading" + String(repeating: " ", count: 80_000)).utf8),
                headersSentEvent: loadingHeaders, bodyRelease: loadingRelease
            )
        }
        if request.path.hasPrefix("/show/") {
            return .html("""
                <!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">
                <style>html,body{margin:0;background:#14643d;color:white;font:24px sans-serif}main{height:1600px}</style>
                <main><h1 id="visible-marker">CURRENT SHOW</h1><p>Visible fixture content</p>
                <p style="position:absolute;top:1400px">BELOW VIEWPORT — DO NOT EXPORT</p></main>
                """)
        }
        guard request.path == "/workbench" else { return .status(404) }
        return .html("""
            <!doctype html><meta name="viewport" content="width=device-width, initial-scale=1">
            <style>html,body{margin:0;background:#d8e2ea}iframe{position:absolute;left:20px;top:40px;width:240px;height:180px;border:0}</style>
            <iframe data-avibe-show-session="ses_visible" src="/show/ses_visible/"></iframe>
            """, headers: [("Set-Cookie", "fixture_session=read-only; Path=/; HttpOnly; SameSite=Lax")])
    }
}

@MainActor
private final class ShareFixture {
    let state: ShareServerState
    let server: LocalHTTPServer
    let baseURL: URL
    let origin: WebOrigin
    let root: URL
    let clock: ShareClock
    let presenter: SharePresenter
    let coordinator: ShowShareCoordinator
    let webView: WKWebView
    let controller: UIViewController
    let window: UIWindow
    private weak var previousKeyWindow: UIWindow?

    private init(state: ShareServerState, server: LocalHTTPServer, baseURL: URL, systemPresenter: Bool) throws {
        self.state = state
        self.server = server
        self.baseURL = baseURL
        origin = try XCTUnwrap(WebOrigin(url: baseURL, requiresHTTPS: false))
        root = FileManager.default.temporaryDirectory.appendingPathComponent("ShowShareTests-\(UUID())", isDirectory: true)
        let clock = ShareClock(), presenter = SharePresenter()
        self.clock = clock
        self.presenter = presenter
        if systemPresenter {
            coordinator = ShowShareCoordinator(files: ShowShareFiles(root: root), now: { clock.date })
        } else {
            coordinator = ShowShareCoordinator(files: ShowShareFiles(root: root), now: { clock.date }, presentActivity: presenter.present)
        }
        let configuration = NativeBridgeConfiguration.make(
            instanceOrigin: origin, websiteDataStore: .nonPersistent(),
            setOuterScrollLocked: { _, _ in true }, showShare: coordinator
        )
        webView = WKWebView(frame: CGRect(x: 0, y: 0, width: 390, height: 700), configuration: configuration)
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        controller = UIViewController()
        controller.view = webView
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        previousKeyWindow = scene.windows.first(where: \.isKeyWindow)
        window = UIWindow(windowScene: scene)
        window.frame = scene.screen.bounds
        window.rootViewController = controller
        window.makeKeyAndVisible()
        window.layoutIfNeeded()
        XCTAssertFalse(window.isHidden)
        XCTAssertGreaterThan(webView.bounds.width, 0)
        XCTAssertGreaterThan(webView.bounds.height, 0)
    }

    static func make(systemPresenter: Bool = false) async throws -> ShareFixture {
        let state = ShareServerState()
        let server = try LocalHTTPServer(handler: { request in state.respond(request) })
        do {
            let baseURL = try await server.start()
            let fixture = try ShareFixture(state: state, server: server, baseURL: baseURL, systemPresenter: systemPresenter)
            do {
                fixture.webView.load(URLRequest(url: baseURL.appendingPathComponent("workbench")))
                try await WebKitReadiness.waitForDocument(in: fixture.webView)
                let ready = try await fixture.webView.callAsyncJavaScript("""
                    const frame = document.querySelector('iframe');
                    if (frame.contentDocument?.readyState !== 'complete') await new Promise(resolve => frame.addEventListener('load', resolve, {once:true}));
                    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
                    return document.visibilityState === 'visible' && frame.contentDocument?.getElementById('visible-marker')?.textContent === 'CURRENT SHOW';
                    """, arguments: [:], in: nil, contentWorld: .page)
                XCTAssertEqual(ready as? Bool, true)
                return fixture
            } catch {
                fixture.close()
                throw error
            }
        } catch {
            server.stop()
            throw error
        }
    }

    func request(_ kind: ShowShareRequest.Kind) -> ShowShareRequest {
        ShowShareRequest(operationID: UUID(), kind: kind, sessionID: "ses_visible")
    }

    func inspect(kind: ShowShareRequest.Kind) async throws -> [String: Any] {
        let value = try await webView.callAsyncJavaScript(
            ShowShareCoordinator.inspectScript,
            arguments: ["sessionID": "ses_visible", "kind": kind.rawValue, "origin": origin.serialized, "operationID": UUID().uuidString],
            in: nil, contentWorld: .defaultClient
        )
        return try XCTUnwrap(value as? [String: Any])
    }

    func exportedFiles() throws -> [URL] {
        guard FileManager.default.fileExists(atPath: root.path) else { return [] }
        let directories = try FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
        return try directories.flatMap { try FileManager.default.contentsOfDirectory(at: $0, includingPropertiesForKeys: nil) }
    }

    func close() {
        coordinator.cancel(in: webView)
        controller.dismiss(animated: false)
        webView.stopLoading()
        window.isHidden = true
        window.rootViewController = nil
        previousKeyWindow?.makeKeyAndVisible()
        state.loadingRelease.signal()
        server.stop()
        try? FileManager.default.removeItem(at: root)
    }
}
