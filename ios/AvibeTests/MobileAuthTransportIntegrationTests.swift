import Network
import WebKit
import XCTest
@testable import Avibe

@MainActor
final class MobileAuthTransportIntegrationTests: XCTestCase {
    private static let fixtureEventTimeout: TimeInterval = 30

    func testFailureMappingIsStageAccurateForEveryOperation() {
        struct MappingCase {
            let name: String
            let operation: MobileAuthTransportOperation
            let response: [String: Any]
            let expectedCode: String
            let expectedRetryAfter: Int?
        }

        let stagesByOperation: [
            MobileAuthTransportOperation: [MobileAuthTransportStage]
        ] = [
            .requestOTP: [.request],
            .verifyOTP: [.verify, .callback, .session],
            .fetchSession: [.session],
            .logout: [.logout],
        ]
        XCTAssertEqual(
            Set(stagesByOperation.keys),
            Set(MobileAuthTransportOperation.allCases)
        )
        XCTAssertEqual(
            Set(stagesByOperation.values.flatMap { $0 }),
            Set(MobileAuthTransportStage.allCases)
        )

        var cases = [
            MappingCase(
                name: "request provider rejection",
                operation: .requestOTP,
                response: [
                    "stage": MobileAuthTransportStage.request.rawValue,
                    "status": 422,
                    "code": "email_address_invalid",
                    "message": "Invalid address",
                ],
                expectedCode: "email_address_invalid",
                expectedRetryAfter: nil
            ),
            MappingCase(
                name: "request provider throttle",
                operation: .requestOTP,
                response: [
                    "stage": MobileAuthTransportStage.request.rawValue,
                    "status": 429,
                    "retryAfter": 23,
                ],
                expectedCode: "rate_limited",
                expectedRetryAfter: 23
            ),
            MappingCase(
                name: "request provider server failure",
                operation: .requestOTP,
                response: [
                    "stage": MobileAuthTransportStage.request.rawValue,
                    "status": 500,
                ],
                expectedCode: "network",
                expectedRetryAfter: nil
            ),
            MappingCase(
                name: "verify rejection",
                operation: .verifyOTP,
                response: [
                    "stage": MobileAuthTransportStage.verify.rawValue,
                    "status": 400,
                ],
                expectedCode: "invalid_or_expired",
                expectedRetryAfter: nil
            ),
            MappingCase(
                name: "verify throttle",
                operation: .verifyOTP,
                response: [
                    "stage": MobileAuthTransportStage.verify.rawValue,
                    "status": 429,
                    "retryAfter": 11,
                ],
                expectedCode: "rate_limited",
                expectedRetryAfter: 11
            ),
            MappingCase(
                name: "verify provider server failure",
                operation: .verifyOTP,
                response: [
                    "stage": MobileAuthTransportStage.verify.rawValue,
                    "status": 500,
                ],
                expectedCode: "network",
                expectedRetryAfter: nil
            ),
            MappingCase(
                name: "callback failure",
                operation: .verifyOTP,
                response: [
                    "stage": MobileAuthTransportStage.callback.rawValue,
                    "status": 502,
                ],
                expectedCode: "network",
                expectedRetryAfter: nil
            ),
            MappingCase(
                name: "post-callback session failure",
                operation: .verifyOTP,
                response: [
                    "stage": MobileAuthTransportStage.session.rawValue,
                    "status": 503,
                ],
                expectedCode: "network",
                expectedRetryAfter: nil
            ),
            MappingCase(
                name: "session refresh failure",
                operation: .fetchSession,
                response: [
                    "stage": MobileAuthTransportStage.session.rawValue,
                    "status": 500,
                ],
                expectedCode: "network",
                expectedRetryAfter: nil
            ),
            MappingCase(
                name: "logout failure",
                operation: .logout,
                response: [
                    "stage": MobileAuthTransportStage.logout.rawValue,
                    "status": 500,
                ],
                expectedCode: "network",
                expectedRetryAfter: nil
            ),
        ]

        for (operation, stages) in stagesByOperation {
            for stage in stages {
                for failure in [
                    "exception",
                    "abort",
                    "timeout",
                    "invalid_response",
                ] {
                    cases.append(
                        MappingCase(
                            name: "\(operation) \(stage.rawValue) \(failure)",
                            operation: operation,
                            response: [
                                "stage": stage.rawValue,
                                "status": 0,
                                "failure": failure,
                            ],
                            expectedCode: "network",
                            expectedRetryAfter: nil
                        )
                    )
                }
            }
        }

        for testCase in cases {
            let error = MobileAuthTransport.mappedError(
                for: testCase.operation,
                response: testCase.response
            )
            XCTAssertEqual(
                error.code,
                testCase.expectedCode,
                testCase.name
            )
            XCTAssertEqual(
                error.retryAfterSeconds,
                testCase.expectedRetryAfter,
                testCase.name
            )
        }
    }

    func testHangingReadinessLoadTimesOut() async throws {
        let requestArrived = expectation(description: "config request arrived")
        let releaseResponse = DispatchSemaphore(value: 0)
        let server = try LocalHTTPServer { request in
            if request.path == "/api/config/auth" {
                requestArrived.fulfill()
                releaseResponse.wait()
                return .json("{}")
            }
            return .status(404)
        }
        let baseURL = try await server.start()
        addTeardownBlock {
            releaseResponse.signal()
            server.stop()
        }
        let transport = MobileAuthTransport(
            controlPlaneURL: baseURL,
            websiteDataStore: .nonPersistent(),
            deadlines: MobileAuthTransportDeadlines(
                readinessMilliseconds: 3_000,
                requestMilliseconds: 500,
                logoutMilliseconds: 500
            )
        )

        let request = Task {
            try await transport.requestOTP(email: "alex@example.com")
        }
        await fulfillment(
            of: [requestArrived],
            timeout: Self.fixtureEventTimeout
        )
        await assertNetworkError(from: request)
    }

    func testHangingSessionReadTimesOut() async throws {
        let state = HTTPFixtureState()
        let sessionJSON = Self.sessionJSON
        let requestArrived = expectation(description: "session request arrived")
        let releaseResponse = DispatchSemaphore(value: 0)
        let server = try LocalHTTPServer { request in
            state.record(request)
            switch request.path {
            case "/api/config/auth":
                return .json(
                    """
                    {
                      "supabase_url": "\(state.baseURL!.absoluteString)",
                      "supabase_anon_key": "fixture-anon"
                    }
                    """
                )
            case "/api/mobile/session":
                requestArrived.fulfill()
                releaseResponse.wait()
                return .json(sessionJSON)
            default:
                return .status(404)
            }
        }
        let baseURL = try await server.start()
        state.baseURL = baseURL
        addTeardownBlock {
            releaseResponse.signal()
            server.stop()
        }
        let transport = MobileAuthTransport(
            controlPlaneURL: baseURL,
            websiteDataStore: .nonPersistent(),
            deadlines: MobileAuthTransportDeadlines(
                readinessMilliseconds: 30_000,
                requestMilliseconds: 500,
                logoutMilliseconds: 500
            )
        )

        let request = Task {
            try await transport.fetchSession()
        }
        await fulfillment(
            of: [requestArrived],
            timeout: Self.fixtureEventTimeout
        )
        await assertNetworkError(from: request)
    }

    func testMalformedSessionPayloadMapsToNetwork() async throws {
        let server = try LocalHTTPServer { request in
            switch request.path {
            case "/api/config/auth":
                return .json("{}")
            case "/api/mobile/session":
                return .json("{}")
            default:
                return .status(404)
            }
        }
        let baseURL = try await server.start()
        addTeardownBlock {
            server.stop()
        }
        let transport = MobileAuthTransport(
            controlPlaneURL: baseURL,
            websiteDataStore: .nonPersistent()
        )

        let request = Task {
            try await transport.fetchSession()
        }
        await assertNetworkError(from: request)
    }

    func testHangingLogoutStillRotatesTheLocalStore() async throws {
        let state = HTTPFixtureState()
        let sessionJSON = Self.sessionJSON
        let deleteArrived = expectation(description: "logout request arrived")
        let releaseResponse = DispatchSemaphore(value: 0)
        let lateResponseSent = FixtureEvent(
            description: "late logout response sent"
        )
        let server = try LocalHTTPServer { request in
            state.record(request)
            switch (request.method, request.path) {
            case ("GET", "/api/config/auth"):
                return .json(
                    """
                    {
                      "supabase_url": "\(state.baseURL!.absoluteString)",
                      "supabase_anon_key": "fixture-anon"
                    }
                    """
                )
            case ("GET", "/api/mobile/session"):
                return .json(sessionJSON)
            case ("DELETE", "/api/mobile/session"):
                deleteArrived.fulfill()
                releaseResponse.wait()
                return .json(
                    "{}",
                    headers: [
                        ("Set-Cookie", "late_logout=old; Path=/; HttpOnly"),
                    ],
                    sentEvent: lateResponseSent
                )
            default:
                return .status(404)
            }
        }
        let baseURL = try await server.start()
        state.baseURL = baseURL
        addTeardownBlock {
            releaseResponse.signal()
            server.stop()
        }

        let defaults = makeDefaults()
        let accountStore = makeRealAccountStore(defaults: defaults)
        let coordinator = MobileSessionCoordinator(
            defaults: defaults,
            controlPlane: MobileControlPlaneConfiguration(testBaseURL: baseURL),
            websiteDataStore: accountStore,
            transportFactory: {
                MobileAuthTransport(
                    controlPlaneURL: $0,
                    websiteDataStore: $1,
                    deadlines: MobileAuthTransportDeadlines(
                        readinessMilliseconds: 30_000,
                        requestMilliseconds: 30_000,
                        logoutMilliseconds: 500
                    )
                )
            },
            onOpenRuntime: { _ in },
            onPrepareAccountTransition: { [] }
        )
        coordinator.activateAccountSurface()
        _ = try await coordinator.bootstrap()
        let oldIdentifier = accountStore.identifier

        let logout = Task {
            try await coordinator.logout()
        }
        await fulfillment(
            of: [deleteArrived],
            timeout: Self.fixtureEventTimeout
        )
        let logoutState = try await logout.value
        XCTAssertEqual(logoutState, .signedOut)
        XCTAssertNotEqual(accountStore.identifier, oldIdentifier)

        releaseResponse.signal()
        await fulfillment(
            of: [lateResponseSent.expectation],
            timeout: Self.fixtureEventTimeout
        )
        let cookies = await allCookies(in: accountStore.dataStore.httpCookieStore)
        XCTAssertNil(cookie(named: "late_logout", in: cookies))
    }

    func testReadinessRejectsCrossOriginRedirect() async throws {
        let captureState = HTTPFixtureState()
        let captureServer = try LocalHTTPServer(address: .ipv6) { request in
            captureState.record(request)
            return .json("{}")
        }
        let captureURL = try await captureServer.start()
        let server = try LocalHTTPServer { request in
            switch request.path {
            case "/api/config/auth":
                return LocalHTTPResponse(
                    status: 302,
                    headers: [
                        (
                            "Location",
                            captureURL.appending(path: "capture").absoluteString
                        ),
                    ],
                    body: Data()
                )
            case "/capture":
                return .json("{}")
            default:
                return .status(404)
            }
        }
        let baseURL = try await server.start()
        addTeardownBlock {
            server.stop()
            captureServer.stop()
        }
        let transport = MobileAuthTransport(
            controlPlaneURL: baseURL,
            websiteDataStore: .nonPersistent()
        )

        do {
            _ = try await transport.requestOTP(email: "alex@example.com")
            XCTFail("Expected the cross-origin readiness redirect to fail")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error.code, "network")
        }
        XCTAssertFalse(
            captureState.requests().contains(where: { $0.path == "/capture" })
        )
    }

    func testCookieHandoffRotationDeletionAndMutationOrigin() async throws {
        let state = HTTPFixtureState()
        let sessionJSON = Self.sessionJSON
        let server = try LocalHTTPServer { request in
            state.record(request)
            switch (request.method, request.path) {
            case ("GET", "/api/config/auth"):
                let baseURL = state.baseURL!
                return .json(
                    """
                    {
                      "supabase_url": "\(baseURL.absoluteString)",
                      "supabase_anon_key": "fixture-anon"
                    }
                    """
                )
            case ("POST", "/auth/v1/otp"):
                return .json("{}", headers: [("Retry-After", "7")])
            case ("POST", "/auth/v1/verify"):
                return .json(
                    """
                    {
                      "access_token": "fixture-access",
                      "refresh_token": "fixture-refresh"
                    }
                    """
                )
            case ("POST", "/auth/callback"):
                return .json(
                    "{}",
                    headers: [
                        ("Set-Cookie", "sb-fixture-auth-token.0=old-a; Path=/; HttpOnly"),
                        ("Set-Cookie", "sb-fixture-auth-token.1=old-b; Path=/; HttpOnly"),
                        ("Set-Cookie", "avibe_session=fixture-session; Path=/; HttpOnly"),
                    ]
                )
            case ("GET", "/api/mobile/session"):
                return .json(
                    sessionJSON,
                    headers: [
                        ("Set-Cookie", "sb-fixture-auth-token.0=new-a; Path=/; HttpOnly"),
                        (
                            "Set-Cookie",
                            "sb-fixture-auth-token.1=; Path=/; Max-Age=0; HttpOnly"
                        ),
                    ]
                )
            case ("DELETE", "/api/mobile/session"):
                return .json("{}")
            default:
                return .status(404)
            }
        }
        let baseURL = try await server.start()
        state.baseURL = baseURL
        addTeardownBlock {
            server.stop()
        }

        let dataStore = WKWebsiteDataStore.nonPersistent()
        let transport = MobileAuthTransport(
            controlPlaneURL: baseURL,
            websiteDataStore: dataStore
        )

        let cooldown = try await transport.requestOTP(email: "alex@example.com")
        XCTAssertEqual(cooldown, 7)
        let snapshot = try await transport.verifyOTP(
            email: "alex@example.com",
            code: "123456"
        )
        XCTAssertEqual(snapshot.account.email, "alex@example.com")

        let cookies = await allCookies(in: dataStore.httpCookieStore)
        XCTAssertEqual(cookie(named: "sb-fixture-auth-token.0", in: cookies)?.value, "new-a")
        XCTAssertNil(cookie(named: "sb-fixture-auth-token.1", in: cookies))
        XCTAssertEqual(cookie(named: "avibe_session", in: cookies)?.value, "fixture-session")

        let sessionRequest = try XCTUnwrap(
            state.requests().last(where: {
                $0.method == "GET" && $0.path == "/api/mobile/session"
            })
        )
        let sessionCookies = sessionRequest.headers["cookie"] ?? ""
        XCTAssertTrue(sessionCookies.contains("sb-fixture-auth-token.0=old-a"))
        XCTAssertTrue(sessionCookies.contains("sb-fixture-auth-token.1=old-b"))
        XCTAssertTrue(sessionCookies.contains("avibe_session=fixture-session"))

        try await transport.logout()
        let logoutRequest = try XCTUnwrap(
            state.requests().last(where: {
                $0.method == "DELETE" && $0.path == "/api/mobile/session"
            })
        )
        XCTAssertEqual(
            logoutRequest.headers["origin"],
            WebOrigin(url: baseURL, requiresHTTPS: false)?.serialized
        )
    }

    func testControlPlaneCookiesDoNotFollowACrossOriginRedirect() async throws {
        let state = HTTPFixtureState()
        let captureState = HTTPFixtureState()
        let captureServer = try LocalHTTPServer(address: .ipv6) { request in
            captureState.record(request)
            return .html("captured")
        }
        let captureURL = try await captureServer.start()
        let server = try LocalHTTPServer { request in
            state.record(request)
            switch request.path {
            case "/seed":
                return .html(
                    "seeded",
                    headers: [
                        ("Set-Cookie", "avibe_session=control-plane; Path=/; HttpOnly"),
                    ]
                )
            case "/redirect":
                return LocalHTTPResponse(
                    status: 302,
                    headers: [
                        (
                            "Location",
                            captureURL.appending(path: "capture").absoluteString
                        ),
                    ],
                    body: Data()
                )
            default:
                return .status(404)
            }
        }
        let baseURL = try await server.start()
        state.baseURL = baseURL
        addTeardownBlock {
            server.stop()
            captureServer.stop()
        }

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let webView = WKWebView(frame: .zero, configuration: configuration)
        let probe = NavigationProbe()
        webView.navigationDelegate = probe

        probe.expectNavigation()
        webView.load(URLRequest(url: baseURL.appending(path: "seed")))
        await fulfillment(
            of: [probe.expectation],
            timeout: Self.fixtureEventTimeout
        )

        probe.expectNavigation()
        webView.load(URLRequest(url: baseURL.appending(path: "redirect")))
        await fulfillment(
            of: [probe.expectation],
            timeout: Self.fixtureEventTimeout
        )

        let capture = try XCTUnwrap(
            captureState.requests().last(where: { $0.path == "/capture" })
        )
        XCTAssertNil(capture.headers["cookie"])
    }

    func testCallbackTokensDoNotFollowACrossOriginRedirect() async throws {
        let state = HTTPFixtureState()
        let captureState = HTTPFixtureState()
        let captureServer = try LocalHTTPServer(address: .ipv6) { request in
            captureState.record(request)
            return .json("{}")
        }
        let captureURL = try await captureServer.start()
        let server = try LocalHTTPServer { request in
            state.record(request)
            switch (request.method, request.path) {
            case ("GET", "/api/config/auth"):
                return .json(
                    """
                    {
                      "supabase_url": "\(state.baseURL!.absoluteString)",
                      "supabase_anon_key": "fixture-anon"
                    }
                    """
                )
            case ("POST", "/auth/v1/verify"):
                return .json(
                    """
                    {
                      "access_token": "fixture-access",
                      "refresh_token": "fixture-refresh"
                    }
                    """
                )
            case ("POST", "/auth/callback"):
                return LocalHTTPResponse(
                    status: 307,
                    headers: [
                        (
                            "Location",
                            captureURL.appending(path: "capture").absoluteString
                        ),
                    ],
                    body: Data()
                )
            default:
                return .status(404)
            }
        }
        let baseURL = try await server.start()
        state.baseURL = baseURL
        addTeardownBlock {
            server.stop()
            captureServer.stop()
        }
        let transport = MobileAuthTransport(
            controlPlaneURL: baseURL,
            websiteDataStore: .nonPersistent()
        )

        let request = Task {
            try await transport.verifyOTP(
                email: "alex@example.com",
                code: "123456"
            )
        }
        await assertNetworkError(from: request)

        let callback = try XCTUnwrap(
            state.requests().last(where: {
                $0.method == "POST" && $0.path == "/auth/callback"
            })
        )
        let callbackBody = try XCTUnwrap(
            String(data: callback.body, encoding: .utf8)
        )
        XCTAssertTrue(callbackBody.contains("fixture-access"))
        XCTAssertTrue(callbackBody.contains("fixture-refresh"))
        XCTAssertFalse(
            captureState.requests().contains(where: { $0.path == "/capture" })
        )
    }

    func testDelayedOldStoreResponseCannotRestoreCookiesAfterRotation() async throws {
        let state = HTTPFixtureState()
        let requestArrived = expectation(description: "old store request arrived")
        let releaseResponse = DispatchSemaphore(value: 0)
        let lateResponseSent = FixtureEvent(
            description: "old store response sent"
        )
        let server = try LocalHTTPServer { request in
            state.record(request)
            if request.path == "/delayed-cookie" {
                requestArrived.fulfill()
                releaseResponse.wait()
                return .html(
                    "late",
                    headers: [
                        ("Set-Cookie", "late_generation=old; Path=/; HttpOnly"),
                    ],
                    sentEvent: lateResponseSent
                )
            }
            return .status(404)
        }
        let baseURL = try await server.start()
        state.baseURL = baseURL
        addTeardownBlock {
            releaseResponse.signal()
            server.stop()
        }

        let defaults = makeDefaults()
        let accountStore = makeRealAccountStore(defaults: defaults)
        await setCookie(
            HTTPCookie(properties: [
                .domain: "127.0.0.1",
                .path: "/",
                .name: "old_generation",
                .value: "present",
            ])!,
            in: accountStore.dataStore.httpCookieStore
        )

        var oldWebView: WKWebView? = {
            let configuration = WKWebViewConfiguration()
            configuration.websiteDataStore = accountStore.dataStore
            return WKWebView(frame: .zero, configuration: configuration)
        }()
        oldWebView?.load(
            URLRequest(url: baseURL.appending(path: "delayed-cookie"))
        )
        await fulfillment(
            of: [requestArrived],
            timeout: Self.fixtureEventTimeout
        )

        oldWebView?.stopLoading()
        oldWebView = nil
        await accountStore.rotate()
        releaseResponse.signal()
        await fulfillment(
            of: [lateResponseSent.expectation],
            timeout: Self.fixtureEventTimeout
        )

        let newCookies = await allCookies(
            in: accountStore.dataStore.httpCookieStore
        )
        XCTAssertNil(cookie(named: "old_generation", in: newCookies))
        XCTAssertNil(cookie(named: "late_generation", in: newCookies))
    }

    func testNamedStoreLedgerNeverRemovesAnUnownedLiveIdentifier() async throws {
        let before = await allNamedDataStoreIdentifiers()
        let defaults = makeDefaults()
        let registry = RealNamedDataStoreRegistry()
        var accountStore: AccountWebsiteDataStore? = AccountWebsiteDataStore(
            defaults: defaults,
            operations: registry.operations
        )
        let unownedIdentifier = UUID()
        registry.trackForCleanup(unownedIdentifier)
        addTeardownBlock {
            let remaining = await registry.removeCreatedIdentifiers()
            XCTAssertTrue(remaining.isEmpty)
        }
        var unownedStore: WKWebsiteDataStore? = WKWebsiteDataStore(
            forIdentifier: unownedIdentifier
        )
        await setCookie(
            HTTPCookie(properties: [
                .domain: "example.test",
                .path: "/",
                .name: "test_owned_unrelated",
                .value: "present",
            ])!,
            in: try XCTUnwrap(unownedStore).httpCookieStore
        )
        let registered = await allNamedDataStoreIdentifiers()
        XCTAssertTrue(registered.contains(unownedIdentifier))

        await accountStore?.rotate()
        await accountStore?.sweepOrphans()

        let afterSweep = await allNamedDataStoreIdentifiers()
        XCTAssertTrue(before.isSubset(of: afterSweep))
        XCTAssertTrue(afterSweep.contains(unownedIdentifier))

        let identifiersCreatedByTest =
            registry.createdIdentifiers
        accountStore = nil
        unownedStore = nil
        await Task.yield()
        let remaining = await registry.removeCreatedIdentifiers()
        XCTAssertTrue(remaining.isEmpty)

        let afterCleanup = await allNamedDataStoreIdentifiers()
        XCTAssertTrue(before.isSubset(of: afterCleanup))
        XCTAssertTrue(
            afterCleanup.isDisjoint(
                with: identifiersCreatedByTest.subtracting(before)
            )
        )
    }

    private static let sessionJSON = """
    {
      "account": {
        "id": "account-1",
        "email": "alex@example.com"
      },
      "instances": [
        {
          "id": "instance-1",
          "name": "Fixture Avibe",
          "host": "fixture-app.avibe.bot",
          "url": "https://fixture-app.avibe.bot",
          "relationship": "owner",
          "owner_email": null,
          "available": true
        }
      ]
    }
    """

    private func makeDefaults() -> UserDefaults {
        let suite = "MobileAuthTransportIntegrationTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        addTeardownBlock {
            defaults.removePersistentDomain(forName: suite)
        }
        return defaults
    }

    private func makeRealAccountStore(
        defaults: UserDefaults
    ) -> AccountWebsiteDataStore {
        let registry = RealNamedDataStoreRegistry()
        addTeardownBlock {
            let remaining = await registry.removeCreatedIdentifiers()
            XCTAssertTrue(remaining.isEmpty)
        }
        return AccountWebsiteDataStore(
            defaults: defaults,
            operations: registry.operations
        )
    }

    private func allNamedDataStoreIdentifiers() async -> Set<UUID> {
        await withCheckedContinuation { continuation in
            WKWebsiteDataStore.fetchAllDataStoreIdentifiers {
                continuation.resume(returning: Set($0))
            }
        }
    }

    private func allCookies(
        in store: WKHTTPCookieStore
    ) async -> [HTTPCookie] {
        await withCheckedContinuation { continuation in
            store.getAllCookies { cookies in
                continuation.resume(returning: cookies)
            }
        }
    }

    private func setCookie(
        _ cookie: HTTPCookie,
        in store: WKHTTPCookieStore
    ) async {
        await withCheckedContinuation { continuation in
            store.setCookie(cookie) {
                continuation.resume()
            }
        }
    }

    private func cookie(
        named name: String,
        in cookies: [HTTPCookie]
    ) -> HTTPCookie? {
        cookies.first(where: { $0.name == name })
    }

    private func assertNetworkError<T>(
        from task: Task<T, Error>,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async {
        let completed = expectation(description: "operation completed")
        var result: Result<T, Error>?
        Task { @MainActor in
            result = await task.result
            completed.fulfill()
        }
        await fulfillment(
            of: [completed],
            timeout: Self.fixtureEventTimeout
        )
        guard let result else {
            task.cancel()
            return
        }
        switch result {
        case .success:
            XCTFail("Expected a network error", file: file, line: line)
        case .failure(let error as MobileAccountError):
            XCTAssertEqual(
                error.code,
                "network",
                file: file,
                line: line
            )
        case .failure(let error):
            XCTFail("Unexpected error: \(error)", file: file, line: line)
        }
    }
}

@MainActor
private final class RealNamedDataStoreRegistry {
    private(set) var createdIdentifiers = Set<UUID>()

    var operations: AccountWebsiteDataStoreOperations {
        AccountWebsiteDataStoreOperations(
            makeDataStore: { [self] identifier in
                createdIdentifiers.insert(identifier)
                return WKWebsiteDataStore(forIdentifier: identifier)
            },
            removeIdentifier: { [self] identifier in
                let error = await AccountWebsiteDataStoreOperations.live
                    .removeIdentifier(identifier)
                if error == nil {
                    createdIdentifiers.remove(identifier)
                }
                return error
            }
        )
    }

    func trackForCleanup(_ identifier: UUID) {
        createdIdentifiers.insert(identifier)
    }

    func removeCreatedIdentifiers() async -> Set<UUID> {
        for attempt in 0..<50 {
            var remaining = Set<UUID>()
            for identifier in createdIdentifiers.sorted(by: {
                $0.uuidString < $1.uuidString
            }) {
                if await AccountWebsiteDataStoreOperations.live
                    .removeIdentifier(identifier) != nil
                {
                    remaining.insert(identifier)
                }
            }
            createdIdentifiers = remaining
            if remaining.isEmpty {
                return []
            }
            if attempt < 49 {
                try? await Task.sleep(for: .milliseconds(100))
            }
        }
        return createdIdentifiers
    }
}

private final class NavigationProbe: NSObject, WKNavigationDelegate {
    private(set) var expectation = XCTestExpectation(
        description: "navigation not configured"
    )

    func expectNavigation() {
        expectation = XCTestExpectation(description: "navigation finished")
    }

    func webView(
        _ webView: WKWebView,
        didFinish navigation: WKNavigation?
    ) {
        expectation.fulfill()
    }

    func webView(
        _ webView: WKWebView,
        didFail navigation: WKNavigation?,
        withError error: Error
    ) {
        expectation.fulfill()
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation?,
        withError error: Error
    ) {
        expectation.fulfill()
    }
}

struct LocalHTTPRequest: Sendable {
    let method: String
    let path: String
    let headers: [String: String]
    let body: Data
}

struct LocalHTTPResponse: Sendable {
    let status: Int
    let headers: [(String, String)]
    let body: Data
    let sentEvent: FixtureEvent?

    init(
        status: Int,
        headers: [(String, String)],
        body: Data,
        sentEvent: FixtureEvent? = nil
    ) {
        self.status = status
        self.headers = headers
        self.body = body
        self.sentEvent = sentEvent
    }

    static func json(
        _ body: String,
        headers: [(String, String)] = [],
        sentEvent: FixtureEvent? = nil
    ) -> LocalHTTPResponse {
        LocalHTTPResponse(
            status: 200,
            headers: [("Content-Type", "application/json")] + headers,
            body: Data(body.utf8),
            sentEvent: sentEvent
        )
    }

    static func html(
        _ body: String,
        headers: [(String, String)] = [],
        sentEvent: FixtureEvent? = nil
    ) -> LocalHTTPResponse {
        LocalHTTPResponse(
            status: 200,
            headers: [("Content-Type", "text/html; charset=utf-8")] + headers,
            body: Data(body.utf8),
            sentEvent: sentEvent
        )
    }

    static func status(_ status: Int) -> LocalHTTPResponse {
        LocalHTTPResponse(
            status: status,
            headers: [],
            body: Data()
        )
    }
}

final class FixtureEvent: @unchecked Sendable {
    let expectation: XCTestExpectation

    init(description: String) {
        expectation = XCTestExpectation(description: description)
    }

    func signal() {
        expectation.fulfill()
    }
}

private final class HTTPFixtureState: @unchecked Sendable {
    private let lock = NSLock()
    private var recordedRequests: [LocalHTTPRequest] = []
    private var storedBaseURL: URL?

    var baseURL: URL? {
        get {
            lock.withLock { storedBaseURL }
        }
        set {
            lock.withLock {
                storedBaseURL = newValue
            }
        }
    }

    func record(_ request: LocalHTTPRequest) {
        lock.withLock {
            recordedRequests.append(request)
        }
    }

    func requests() -> [LocalHTTPRequest] {
        lock.withLock { recordedRequests }
    }
}

final class LocalHTTPServer: @unchecked Sendable {
    enum LoopbackAddress {
        case ipv4
        case ipv6

        var endpointHost: NWEndpoint.Host {
            switch self {
            case .ipv4:
                return "127.0.0.1"
            case .ipv6:
                return "::1"
            }
        }

        var urlHost: String {
            switch self {
            case .ipv4:
                return "127.0.0.1"
            case .ipv6:
                return "[::1]"
            }
        }
    }

    private let listener: NWListener
    private let address: LoopbackAddress
    private let queue = DispatchQueue(label: "bot.avibe.ios.tests.http")
    private let handler: @Sendable (LocalHTTPRequest) -> LocalHTTPResponse

    init(
        address: LoopbackAddress = .ipv4,
        handler: @escaping @Sendable (LocalHTTPRequest) -> LocalHTTPResponse
    ) throws {
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(
            host: address.endpointHost,
            port: .any
        )
        listener = try NWListener(using: parameters, on: .any)
        self.address = address
        self.handler = handler
        listener.newConnectionHandler = { [weak self] connection in
            self?.receive(from: connection, data: Data())
            connection.start(queue: self?.queue ?? .global())
        }
    }

    func start() async throws -> URL {
        try await withCheckedThrowingContinuation { continuation in
            listener.stateUpdateHandler = { [weak self] state in
                guard let self else {
                    return
                }
                switch state {
                case .ready:
                    self.listener.stateUpdateHandler = nil
                    guard let port = self.listener.port?.rawValue else {
                        continuation.resume(
                            throwing: MobileAccountError.network
                        )
                        return
                    }
                    continuation.resume(
                        returning: URL(
                            string: "http://\(self.address.urlHost):\(port)"
                        )!
                    )
                case .failed(let error):
                    self.listener.stateUpdateHandler = nil
                    continuation.resume(throwing: error)
                default:
                    break
                }
            }
            listener.start(queue: queue)
        }
    }

    func stop() {
        listener.cancel()
    }

    private func receive(
        from connection: NWConnection,
        data accumulated: Data
    ) {
        connection.receive(
            minimumIncompleteLength: 1,
            maximumLength: 1_048_576
        ) { [weak self] data, _, isComplete, error in
            guard let self else {
                connection.cancel()
                return
            }
            var received = accumulated
            if let data {
                received.append(data)
            }
            if let request = Self.parseRequest(received) {
                self.send(
                    self.handler(request),
                    through: connection
                )
                return
            }
            if isComplete || error != nil {
                connection.cancel()
                return
            }
            self.receive(from: connection, data: received)
        }
    }

    private func send(
        _ response: LocalHTTPResponse,
        through connection: NWConnection
    ) {
        let reason = response.status == 200
            ? "OK"
            : response.status == 302
                ? "Found"
                : "Not Found"
        var lines = [
            "HTTP/1.1 \(response.status) \(reason)",
            "Content-Length: \(response.body.count)",
            "Connection: close",
        ]
        lines.append(contentsOf: response.headers.map { "\($0.0): \($0.1)" })
        var data = Data((lines.joined(separator: "\r\n") + "\r\n\r\n").utf8)
        data.append(response.body)
        connection.send(content: data, completion: .contentProcessed { _ in
            response.sentEvent?.signal()
            connection.cancel()
        })
    }

    private static func parseRequest(_ data: Data) -> LocalHTTPRequest? {
        let delimiter = Data("\r\n\r\n".utf8)
        guard
            let headerRange = data.range(of: delimiter),
            let headerText = String(
                data: data[..<headerRange.lowerBound],
                encoding: .utf8
            )
        else {
            return nil
        }
        let lines = headerText.split(separator: "\r\n", omittingEmptySubsequences: false)
        guard
            let requestLine = lines.first?.split(separator: " "),
            requestLine.count >= 2
        else {
            return nil
        }
        var headers: [String: String] = [:]
        for line in lines.dropFirst() {
            guard let separator = line.firstIndex(of: ":") else {
                continue
            }
            let name = line[..<separator].trimmingCharacters(in: .whitespaces)
                .lowercased()
            let value = line[line.index(after: separator)...]
                .trimmingCharacters(in: .whitespaces)
            headers[name] = value
        }
        let contentLength = Int(headers["content-length"] ?? "0") ?? 0
        let bodyStart = headerRange.upperBound
        guard data.count >= bodyStart + contentLength else {
            return nil
        }
        let rawTarget = String(requestLine[1])
        let path = URLComponents(string: rawTarget)?.path ?? rawTarget
        return LocalHTTPRequest(
            method: String(requestLine[0]),
            path: path,
            headers: headers,
            body: data.subdata(
                in: bodyStart..<(bodyStart + contentLength)
            )
        )
    }
}
