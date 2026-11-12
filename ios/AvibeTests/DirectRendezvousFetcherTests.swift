import WebKit
import XCTest
@testable import Avibe

/// The native rendezvous request, against a real WebView and a local
/// stand-in for the instance.
@MainActor
final class DirectRendezvousFetcherTests: XCTestCase {
    private let client = DirectEndpointID(bytes: Data(repeating: 0x5A, count: 32))!

    func testTheRequestIsTheInstancesOwnAndItsAnswerComesBackUnchanged() async throws {
        let instance = try await InstanceFixture.start(self)
        let webView = try await instance.loadedWebView(path: "/")
        let answer = "{\"version\":1,\"status\":\"unavailable\",\"note\":\"caf\u{e9}\"}"

        // The instance's answer, byte for byte.
        instance.answer = .json(answer)
        let data = try await DirectRendezvousFetcher.fetch(
            in: webView,
            origin: instance.origin,
            clientEndpointID: client
        )
        XCTAssertEqual(data, Data(answer.utf8))
        let request = try XCTUnwrap(instance.rendezvousRequests.last)
        XCTAssertEqual(request.method, "POST")
        XCTAssertEqual(request.headers["x-vibe-csrf-token"], "csrf token/1")
        XCTAssertEqual(request.headers["content-type"], "application/json")
        XCTAssertEqual(request.headers["origin"], instance.origin.serialized)
        XCTAssertEqual(
            request.headers["cookie"]?.contains("vibe_csrf_token=csrf%20token%2F1"),
            true
        )
        XCTAssertEqual(
            String(decoding: request.body, as: UTF8.self),
            DirectRendezvous.requestBody(clientEndpointID: client)
        )

        // A missing or revoked session is the instance's answer to give.
        for status in [401, 403, 400, 500] {
            instance.answer = .status(status)
            await assertFetch(in: webView, instance, failsWith: .refused(status: status))
        }

        // A redirect is refused, wherever it points.
        instance.answer = LocalHTTPResponse(
            status: 302,
            headers: [("Location", "/elsewhere")],
            body: Data()
        )
        await assertFetch(in: webView, instance, failsWith: .failed)
        XCTAssertEqual(instance.requests(path: "/elsewhere").count, 0)

        // An answer larger than any the contract allows.
        instance.answer = .json(
            "{\"padding\":\"" + String(repeating: "a", count: DirectRendezvous.maxAnswerBytes) + "\"}"
        )
        await assertFetch(in: webView, instance, failsWith: .failed)

        // An instance that does not answer: the request ends at its bound.
        let release = DispatchSemaphore(value: 0)
        instance.hold = release
        let started = Date()
        await assertFetch(in: webView, instance, deadlineMilliseconds: 300, failsWith: .failed)
        XCTAssertLessThan(Date().timeIntervalSince(started), 10)
        release.signal()
        instance.hold = nil

        // The WebView is asked only while it shows the instance's origin.
        let asked = instance.rendezvousRequests.count
        let other = try XCTUnwrap(WebOrigin(scheme: "http", host: "127.0.0.1", port: 9))
        do {
            _ = try await DirectRendezvousFetcher.fetch(
                in: webView,
                origin: other,
                clientEndpointID: client
            )
            XCTFail("Another origin was asked.")
        } catch {
            XCTAssertEqual(error as? DirectRendezvousFetchError, .pageNotReady)
        }
        XCTAssertEqual(instance.rendezvousRequests.count, asked)
    }

    func testAPageCannotAnswerInTheInstancesPlaceOrMoveTheRequest() async throws {
        let instance = try await InstanceFixture.start(self)
        // The page replaces `fetch`, adds a `<base>` element, and keeps a
        // forged answer ready: none of it reaches the native request.
        let webView = try await instance.loadedWebView(path: "/tampered")
        instance.answer = .json("{\"version\":1,\"status\":\"unavailable\"}")

        let data = try await DirectRendezvousFetcher.fetch(
            in: webView,
            origin: instance.origin,
            clientEndpointID: client
        )
        XCTAssertEqual(data, Data("{\"version\":1,\"status\":\"unavailable\"}".utf8))
        XCTAssertEqual(instance.rendezvousRequests.count, 1)
        // The page's own script does see its replacement.
        let forged = try await webView.evaluateJavaScript("typeof window.forgedFetch") as? String
        XCTAssertEqual(forged, "function")
    }

    func testNothingIsAskedBeforeTheInstancesDocumentAndItsCookieAreThere() async throws {
        let instance = try await InstanceFixture.start(self)

        // No document yet.
        let blank = instance.makeWebView()
        await assertFetch(in: blank, instance, failsWith: .pageNotReady)

        // A document of the instance that has no CSRF cookie yet.
        let webView = try await instance.loadedWebView(path: "/plain")
        await assertFetch(in: webView, instance, failsWith: .pageNotReady)
        XCTAssertEqual(instance.rendezvousRequests.count, 0)
    }

    // MARK: Helpers

    private func assertFetch(
        in webView: WKWebView,
        _ instance: InstanceFixture,
        deadlineMilliseconds: Int = DirectRendezvousFetcher.defaultDeadlineMilliseconds,
        failsWith expected: DirectRendezvousFetchError,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async {
        do {
            _ = try await DirectRendezvousFetcher.fetch(
                in: webView,
                origin: instance.origin,
                clientEndpointID: client,
                deadlineMilliseconds: deadlineMilliseconds
            )
            XCTFail("The request succeeded.", file: file, line: line)
        } catch {
            XCTAssertEqual(error as? DirectRendezvousFetchError, expected, file: file, line: line)
        }
    }

    /// A local server standing in for one instance's origin.
    private final class InstanceFixture: @unchecked Sendable {
        private let lock = NSLock()
        private var recorded: [LocalHTTPRequest] = []
        private var storedAnswer = LocalHTTPResponse.status(500)
        private var storedHold: DispatchSemaphore?
        private var server: LocalHTTPServer?
        private(set) var origin: WebOrigin!

        /// What the rendezvous route answers.
        var answer: LocalHTTPResponse {
            get { lock.withLock { storedAnswer } }
            set { lock.withLock { storedAnswer = newValue } }
        }

        /// While set, the rendezvous route waits for it before answering.
        var hold: DispatchSemaphore? {
            get { lock.withLock { storedHold } }
            set { lock.withLock { storedHold = newValue } }
        }

        var rendezvousRequests: [LocalHTTPRequest] {
            requests(path: DirectRendezvous.path)
        }

        func requests(path: String) -> [LocalHTTPRequest] {
            lock.withLock { recorded.filter { $0.path == path } }
        }

        @MainActor
        static func start(_ test: XCTestCase) async throws -> InstanceFixture {
            let fixture = InstanceFixture()
            let server = try LocalHTTPServer { [fixture] request in
                fixture.respond(to: request)
            }
            let baseURL = try await server.start()
            fixture.server = server
            fixture.origin = try XCTUnwrap(WebOrigin(url: baseURL, requiresHTTPS: false))
            test.addTeardownBlock {
                // Nothing may be left waiting on the server's queue.
                fixture.hold?.signal()
                server.stop()
            }
            return fixture
        }

        @MainActor
        func makeWebView() -> WKWebView {
            let configuration = WKWebViewConfiguration()
            configuration.websiteDataStore = .nonPersistent()
            return WKWebView(frame: .zero, configuration: configuration)
        }

        @MainActor
        func loadedWebView(path: String) async throws -> WKWebView {
            let webView = makeWebView()
            let url = try XCTUnwrap(URL(string: origin.serialized + path))
            webView.load(URLRequest(url: url))
            try await WebKitReadiness.waitForDocument(in: webView)
            return webView
        }

        private func respond(to request: LocalHTTPRequest) -> LocalHTTPResponse {
            lock.withLock {
                recorded.append(request)
            }
            switch request.path {
            case "/":
                // A cookie value that needs decoding, as the Web UI reads it.
                return .html(
                    "<!doctype html><title>instance</title>",
                    headers: [("Set-Cookie", "vibe_csrf_token=csrf%20token%2F1; Path=/")]
                )
            case "/plain":
                return .html("<!doctype html><title>instance</title>")
            case "/tampered":
                return .html(
                    """
                    <!doctype html>
                    <base href="http://127.0.0.1:9/">
                    <title>instance</title>
                    <script>
                    window.forgedFetch = () => Promise.resolve(
                      new Response('{"version":1,"status":"ready","forged":true}', {status: 200})
                    );
                    window.fetch = window.forgedFetch;
                    </script>
                    """,
                    headers: [("Set-Cookie", "vibe_csrf_token=csrf%20token%2F1; Path=/")]
                )
            case DirectRendezvous.path:
                if let hold {
                    _ = hold.wait(timeout: .now() + 30)
                }
                return answer
            default:
                return .status(404)
            }
        }
    }
}
