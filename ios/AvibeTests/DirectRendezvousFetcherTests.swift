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
        let data = try await fetch(in: webView, origin: instance.origin).get()
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
        switch await fetch(in: webView, origin: other) {
        case .success:
            XCTFail("Another origin was asked.")
        case .failure(let error):
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

        let data = try await fetch(in: webView, origin: instance.origin).get()
        XCTAssertEqual(data, Data("{\"version\":1,\"status\":\"unavailable\"}".utf8))
        XCTAssertEqual(instance.rendezvousRequests.count, 1)
        // The page's own script does see its replacement.
        let forged = try await webView.evaluateJavaScript("typeof window.forgedFetch") as? String
        XCTAssertEqual(forged, "function")
    }

    func testAServiceWorkerThatControlsTheDocumentAtAnyMomentIsRefused() async throws {
        let instance = try await InstanceFixture.start(self)
        let webView = try await instance.loadedWebView(path: "/")
        let answer = "{\"version\":1,\"status\":\"unavailable\"}"
        instance.answer = .json(answer)

        // This App's WebViews have no service worker container (the next
        // test), so one is stood in, in the request's own content world:
        // each case makes it take control at another moment of the request.

        // A container nothing controls changes nothing.
        try await standInServiceWorkers(in: webView, takingControl: "never")
        let data = try await fetch(in: webView, origin: instance.origin).get()
        XCTAssertEqual(data, Data(answer.utf8))
        XCTAssertEqual(instance.rendezvousRequests.count, 1)

        // In control before the request: nothing is sent.
        try await standInServiceWorkers(in: webView, takingControl: "before")
        await assertFetch(in: webView, instance, failsWith: .controlled)
        XCTAssertEqual(instance.rendezvousRequests.count, 1)

        // Takes control between the look and the request. The stand-in
        // then lets go again, which a real worker does not do: it shows
        // that the change itself is remembered, not only the last look.
        try await standInServiceWorkers(in: webView, takingControl: "atFetch")
        await assertFetch(in: webView, instance, failsWith: .controlled)

        // Takes control while the request is under way.
        try await standInServiceWorkers(in: webView, takingControl: "afterFetch")
        await assertFetch(in: webView, instance, failsWith: .controlled)

        // Takes control while the answer is read, at its very end.
        try await standInServiceWorkers(in: webView, takingControl: "duringBody")
        await assertFetch(in: webView, instance, failsWith: .controlled)

        // And without any of it, the same WebView is answered again.
        try await standInServiceWorkers(in: webView, takingControl: "never")
        let again = try await fetch(in: webView, origin: instance.origin).get()
        XCTAssertEqual(again, Data(answer.utf8))
    }

    func testThisAppsWebViewsHaveNoServiceWorkerContainer() async throws {
        let instance = try await InstanceFixture.start(self)
        let webView = try await instance.loadedWebView(path: "/")
        let probe = "return {secure: window.isSecureContext === true, container: typeof navigator.serviceWorker};"
        let page = try await webView.callAsyncJavaScript(
            probe,
            arguments: [:],
            in: nil,
            contentWorld: .page
        ) as? [String: Any]
        let client = try await webView.callAsyncJavaScript(
            probe,
            arguments: [:],
            in: nil,
            contentWorld: .defaultClient
        ) as? [String: Any]

        // Only a secure context could have a container at all. If this
        // stand-in for the instance is not one, the test shows nothing.
        guard page?["secure"] as? Bool == true else {
            throw XCTSkip("The local stand-in is not a secure context: no conclusion about service workers.")
        }
        // With no container no worker can control a document, so the
        // refusal above has never met a real one. If this fails, a real
        // worker can exist here: qualify the refusal against one before
        // any build uses the direct path.
        XCTAssertEqual(page?["container"] as? String, "undefined")
        XCTAssertEqual(client?["container"] as? String, "undefined")
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
        let outcome = await fetch(
            in: webView,
            origin: instance.origin,
            deadlineMilliseconds: deadlineMilliseconds
        )
        switch outcome {
        case .success:
            XCTFail("The request succeeded.", file: file, line: line)
        case .failure(let error):
            XCTAssertEqual(error as? DirectRendezvousFetchError, expected, file: file, line: line)
        }
    }

    /// Stands a service worker container in, in the content world the
    /// request runs in, and wraps that world's `fetch` so the container
    /// takes control at the moment `takingControl` names. Page script
    /// cannot do either: it has no access to that world.
    private func standInServiceWorkers(in webView: WKWebView, takingControl moment: String) async throws {
        _ = try await webView.callAsyncJavaScript(
            """
            const container = new EventTarget();
            let current = moment === "before" ? {} : null;
            Object.defineProperty(container, "controller", {get: () => current});
            Object.defineProperty(navigator, "serviceWorker", {value: container, configurable: true});
            const take = (announce) => {
              current = {};
              if (announce) container.dispatchEvent(new Event("controllerchange"));
            };
            const realFetch = window.__avibeTestRealFetch ??= window.fetch.bind(window);
            window.fetch = async (input, init) => {
              if (moment === "atFetch") {
                take(true);
                current = null;
              }
              const response = await realFetch(input, init);
              if (moment === "afterFetch") take(false);
              if (moment !== "duringBody") return response;
              const bytes = new Uint8Array(await response.arrayBuffer());
              let reads = 0;
              return {
                redirected: false,
                url: response.url,
                type: "basic",
                status: response.status,
                body: {
                  getReader: () => ({
                    read: async () => {
                      reads += 1;
                      if (reads === 1) return {done: false, value: bytes};
                      take(false);
                      return {done: true, value: undefined};
                    }
                  })
                }
              };
            };
            return true;
            """,
            arguments: ["moment": moment],
            in: nil,
            contentWorld: .defaultClient
        )
    }

    /// One request, bounded as the transport bounds it: a script that never
    /// answers must fail a test, not hang it.
    private func fetch(
        in webView: WKWebView,
        origin: WebOrigin,
        deadlineMilliseconds: Int = DirectRendezvousFetcher.defaultDeadlineMilliseconds
    ) async -> Result<Data, Error> {
        let client = client
        do {
            let data: Data = try await directIrohDeadline(180) {
                try await directIrohCall(allowed: { true }) {
                    try await DirectRendezvousFetcher.fetch(
                        in: webView,
                        origin: origin,
                        clientEndpointID: client,
                        deadlineMilliseconds: deadlineMilliseconds
                    )
                }
            }
            return .success(data)
        } catch {
            return .failure(error)
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
