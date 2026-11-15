import Network
import WebKit
import XCTest
@testable import Avibe

@MainActor
final class MobileAuthTransportIntegrationTests: XCTestCase {
    /// Each fixture event here (a request reaching the fixture server, a
    /// navigation finishing, an account operation ending) comes after WebKit
    /// launches a web content process for a new WebView, so it is bounded by
    /// that launch (`WebKitReadiness`), which also outlasts the transport's
    /// own standard deadlines.
    private static let fixtureEventTimeout = TimeInterval(
        WebKitReadiness.webContentLaunch.components.seconds
    )

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
            .signInWithPassword: [.token, .callback, .session],
            .fetchSession: [.session],
            .passkeyChallenge: [.challenge],
            .verifySecondFactor: [.secondFactor, .session],
            .logout: [.logout],
            .pushManagement: [.request],
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
        let secondFactorCases: [(
            String,
            MobileAuthTransportOperation,
            MobileAuthTransportStage,
            Int,
            Int?,
            String
        )] = [
            ("account without a passkey", .passkeyChallenge, .challenge, 404, nil, "passkey_unavailable"),
            ("challenge signed out", .passkeyChallenge, .challenge, 401, nil, "signed_out"),
            ("challenge untrusted origin", .passkeyChallenge, .challenge, 403, nil, "network"),
            ("challenge server failure", .passkeyChallenge, .challenge, 500, nil, "network"),
            ("rejected factor", .verifySecondFactor, .secondFactor, 400, nil, "invalid"),
            ("factor throttle", .verifySecondFactor, .secondFactor, 429, 900, "rate_limited"),
            ("factor signed out", .verifySecondFactor, .secondFactor, 401, nil, "signed_out"),
            ("factor unavailable", .verifySecondFactor, .secondFactor, 503, nil, "network"),
            ("factor untrusted origin", .verifySecondFactor, .secondFactor, 403, nil, "network"),
            ("proved session signed out", .verifySecondFactor, .session, 401, nil, "signed_out"),
            ("proved session failure", .verifySecondFactor, .session, 500, nil, "network"),
            ("proved session throttle", .verifySecondFactor, .session, 429, 9, "network"),
        ]
        let passwordCases: [(String, MobileAuthTransportStage, Int, String?, Int?, String)] = [
            ("wrong password", .token, 400, "invalid_credentials", nil, "invalid_credentials"),
            ("unconfirmed email", .token, 400, "email_not_confirmed", nil, "email_not_confirmed"),
            ("unconfirmed code on another status", .token, 422, "email_not_confirmed", nil, "invalid_credentials"),
            ("grant rejection without a code", .token, 400, nil, nil, "invalid_credentials"),
            ("other grant rejection", .token, 422, "validation_failed", nil, "invalid_credentials"),
            ("grant throttle", .token, 429, "over_request_rate_limit", 42, "rate_limited"),
            ("grant throttle without Retry-After", .token, 429, nil, nil, "rate_limited"),
            ("grant server failure", .token, 500, nil, nil, "network"),
            ("password callback failure", .callback, 502, nil, nil, "network"),
            ("password callback rejection", .callback, 401, nil, nil, "network"),
            ("password session signed out", .session, 401, nil, nil, "signed_out"),
            ("password session failure", .session, 503, nil, nil, "network"),
            ("password session throttle", .session, 429, nil, 9, "network"),
        ]
        for (name, stage, status, code, retryAfter, expectedCode) in passwordCases {
            var response: [String: Any] = [
                "stage": stage.rawValue,
                "status": status,
            ]
            response["code"] = code
            response["retryAfter"] = retryAfter
            cases.append(
                MappingCase(
                    name: name,
                    operation: .signInWithPassword,
                    response: response,
                    expectedCode: expectedCode,
                    expectedRetryAfter: expectedCode == "rate_limited" ? retryAfter : nil
                )
            )
        }
        for (name, operation, stage, status, retryAfter, expectedCode) in secondFactorCases {
            var response: [String: Any] = [
                "stage": stage.rawValue,
                "status": status,
            ]
            response["retryAfter"] = retryAfter
            cases.append(
                MappingCase(
                    name: name,
                    operation: operation,
                    response: response,
                    expectedCode: expectedCode,
                    expectedRetryAfter: expectedCode == "rate_limited" ? retryAfter : nil
                )
            )
        }

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
        // A readiness load that reaches the control plane and gets no answer
        // ends as `network` when the readiness deadline passes. On a loaded
        // simulator the WebContent and GPU processes can take longer to launch
        // than that deadline (3 to 12 s in CI), so an attempt can expire
        // before its request is sent. Such an attempt proves nothing; the next
        // one reuses the processes launched meanwhile, within the fixture budget.
        let readinessMilliseconds = 3_000
        let state = HTTPFixtureState()
        let releaseResponse = DispatchSemaphore(value: 0)
        let server = try LocalHTTPServer { request in
            if request.path == "/api/config/auth" {
                state.record(request)
                // Hold the answer; each released handler releases the next.
                releaseResponse.wait()
                releaseResponse.signal()
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
                readinessMilliseconds: readinessMilliseconds,
                requestMilliseconds: 500,
                logoutMilliseconds: 500
            )
        )

        let clock = ContinuousClock()
        let budget = clock.now.advanced(by: .seconds(Self.fixtureEventTimeout))
        var attempts = 0
        repeat {
            attempts += 1
            let started = clock.now
            let request = Task {
                try await transport.requestOTP(email: "alex@example.com")
            }
            await assertNetworkError(from: request)
            // Only the readiness deadline ends a load the server never answers.
            XCTAssertGreaterThanOrEqual(
                clock.now - started,
                .milliseconds(readinessMilliseconds),
                "attempt \(attempts)"
            )
        } while state.requests().isEmpty && clock.now < budget
        XCTAssertFalse(
            state.requests().isEmpty,
            "No readiness request reached the control plane in \(attempts) attempts."
        )
    }

    func testPushManagementPreservesInt64ResponseTokensAcrossTransport() async throws {
        let state = HTTPFixtureState()
        let server = try LocalHTTPServer { request in
            state.record(request)
            switch request.path {
            case "/api/config/auth":
                return .json("{}")
            case "/api/push/test":
                let count = state.requests().filter {
                    $0.path == "/api/push/test"
                }.count
                let generation = count == 1
                    ? "9007199254740993"
                    : "9223372036854775807"
                return .json(
                    #"{"provider_generation":\#(generation)}"#
                )
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
            websiteDataStore: .nonPersistent(),
            deadlines: MobileAuthTransportDeadlines.testing(5_000)
        )
        defer {
            _ = transport.invalidate()
        }

        let expected: [(String, Int64)] = [
            ("9007199254740993", 9_007_199_254_740_993),
            ("9223372036854775807", Int64.max),
        ]
        for (raw, value) in expected {
            let response = try await transport.pushManagementRequest(
                PushManagementRequest(
                    method: "GET",
                    path: "/api/push/test",
                    headers: [:],
                    body: nil
                )
            )
            let generation = try XCTUnwrap(
                response.body["provider_generation"] as? NSNumber
            )
            XCTAssertEqual(generation.int64Value, value, raw)
        }
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

    func testOTPRequestAsksForTheAppsCodeOnlyEmail() async throws {
        let state = HTTPFixtureState()
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
        let transport = MobileAuthTransport(
            controlPlaneURL: baseURL,
            websiteDataStore: .nonPersistent()
        )

        _ = try await transport.requestOTP(email: "alex@example.com")

        let otpRequest = try XCTUnwrap(
            state.requests().first(where: {
                $0.method == "POST" && $0.path == "/auth/v1/otp"
            })
        )
        // Production composes https://avibe.bot/auth/callback?source=ios-app,
        // the exact redirect the Supabase templates answer with a code-only email.
        var expected = try XCTUnwrap(
            URLComponents(url: baseURL, resolvingAgainstBaseURL: false)
        )
        expected.path = "/auth/callback"
        expected.query = "source=ios-app"
        XCTAssertEqual(
            otpRequest.queryItems.filter { $0.name == "redirect_to" }
                .map(\.value),
            [try XCTUnwrap(expected.url).absoluteString]
        )
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
        let discovery = try await transport.verifyOTP(
            email: "alex@example.com",
            code: "123456"
        )
        guard case .signedIn(let snapshot) = discovery else {
            return XCTFail("Expected a signed-in session, got \(discovery)")
        }
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

    func testSecondFactorIsReadAndPassedInsideTheAccountStore() async throws {
        let state = HTTPFixtureState()
        let sessionJSON = Self.sessionJSON
        let owed = """
        {
          "error": "two_factor_required",
          "email": "alex@example.com",
          "gate": "challenge",
          "methods": ["passkey", "security-key", "totp"]
        }
        """
        let attempts = HTTPFixtureState()
        let server = try LocalHTTPServer { request in
            state.record(request)
            let cookies = request.headers["cookie"] ?? ""
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
                return .json(
                    "{}",
                    headers: [
                        ("Set-Cookie", "avibe_session=first-factor; Path=/; HttpOnly"),
                    ]
                )
            case ("GET", "/api/mobile/session"):
                guard cookies.contains("avibe_session=proved") else {
                    return LocalHTTPResponse(
                        status: 403,
                        headers: [("Content-Type", "application/json")],
                        body: Data(owed.utf8)
                    )
                }
                return .json(sessionJSON)
            case ("POST", "/api/two-factor/passkey-challenge"):
                return .json(
                    """
                    {
                      "token": "signed-challenge",
                      "options": {
                        "challenge": "AAEC_-8",
                        "rpId": "avibe.bot",
                        "userVerification": "required",
                        "allowCredentials": [{"id": "Y3JlZC0x", "type": "public-key"}]
                      }
                    }
                    """
                )
            case ("POST", "/api/two-factor/verify"):
                attempts.record(request)
                switch attempts.requests().count {
                case 1:
                    return LocalHTTPResponse(
                        status: 429,
                        headers: [
                            ("Content-Type", "application/json"),
                            ("Retry-After", "900"),
                        ],
                        body: Data(#"{"error":"rate_limited"}"#.utf8)
                    )
                case 2:
                    return LocalHTTPResponse(
                        status: 400,
                        headers: [("Content-Type", "application/json")],
                        body: Data(#"{"error":"invalid"}"#.utf8)
                    )
                default:
                    return .json(
                        #"{"ok":true}"#,
                        headers: [
                            ("Set-Cookie", "avibe_session=proved; Path=/; HttpOnly"),
                        ]
                    )
                }
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
        let expectedRequirement = MobileSecondFactorRequirement(
            email: "alex@example.com",
            gate: .challenge,
            methods: [.passkey, .totp]
        )

        let afterOTP = try await transport.verifyOTP(
            email: "alex@example.com",
            code: "123456"
        )
        XCTAssertEqual(afterOTP, .secondFactorRequired(expectedRequirement))
        let restored = try await transport.fetchSession()
        XCTAssertEqual(restored, .secondFactorRequired(expectedRequirement))

        do {
            _ = try await transport.verifySecondFactor(.code(.totp, "287082"))
            XCTFail("Expected the throttled attempt to fail")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error.code, "rate_limited")
            XCTAssertEqual(error.retryAfterSeconds, 900)
        }
        do {
            _ = try await transport.verifySecondFactor(.code(.recovery, "k7m2p-9xq4r"))
            XCTFail("Expected the rejected code to fail")
        } catch let error as MobileAccountError {
            XCTAssertEqual(error, .secondFactorInvalid)
        }

        let challenge = try await transport.passkeyChallenge()
        XCTAssertEqual(challenge.token, "signed-challenge")
        XCTAssertEqual(challenge.request.challenge, Data([0, 1, 2, 255, 239]))
        XCTAssertEqual(challenge.request.allowedCredentialIDs, [Data("cred-1".utf8)])
        let assertion = MobilePasskeyAssertion(
            credentialID: Data("cred-1".utf8),
            clientDataJSON: Data(#"{"type":"webauthn.get"}"#.utf8),
            authenticatorData: Data([1, 2, 3]),
            signature: Data([4, 5, 6]),
            userHandle: Data("user-alex".utf8)
        )
        let proved = try await transport.verifySecondFactor(
            .passkey(
                token: challenge.token,
                response: assertion.authenticationResponseJSON
            )
        )
        guard case .signedIn(let snapshot) = proved else {
            return XCTFail("Expected the proved session, got \(proved)")
        }
        XCTAssertEqual(snapshot.account.email, "alex@example.com")

        let verifyRequests = attempts.requests()
        XCTAssertEqual(verifyRequests.count, 3)
        let origin = WebOrigin(url: baseURL, requiresHTTPS: false)?.serialized
        for request in verifyRequests {
            XCTAssertEqual(request.headers["origin"], origin)
            XCTAssertEqual(request.headers["content-type"], "application/json")
            XCTAssertTrue(
                (request.headers["cookie"] ?? "").contains("avibe_session=first-factor")
            )
        }
        let bodies = try verifyRequests.map {
            try XCTUnwrap(
                JSONSerialization.jsonObject(with: $0.body) as? [String: Any]
            )
        }
        XCTAssertEqual(bodies[0] as NSDictionary, ["method": "totp", "code": "287082"])
        XCTAssertEqual(bodies[1] as NSDictionary, ["method": "recovery", "code": "k7m2p-9xq4r"])
        XCTAssertEqual(
            bodies[2] as NSDictionary,
            [
                "method": "passkey",
                "token": "signed-challenge",
                "response": [
                    "id": "Y3JlZC0x",
                    "rawId": "Y3JlZC0x",
                    "type": "public-key",
                    "authenticatorAttachment": "platform",
                    "clientExtensionResults": [String: Any](),
                    "response": [
                        "clientDataJSON": "eyJ0eXBlIjoid2ViYXV0aG4uZ2V0In0",
                        "authenticatorData": "AQID",
                        "signature": "BAUG",
                        "userHandle": "dXNlci1hbGV4",
                    ] as [String: Any],
                ] as [String: Any],
            ] as NSDictionary
        )
        let challengeRequest = try XCTUnwrap(
            state.requests().last(where: {
                $0.path == "/api/two-factor/passkey-challenge"
            })
        )
        XCTAssertEqual(challengeRequest.method, "POST")
        XCTAssertEqual(challengeRequest.headers["origin"], origin)

        let cookies = await allCookies(in: dataStore.httpCookieStore)
        XCTAssertEqual(cookie(named: "avibe_session", in: cookies)?.value, "proved")
        let provedSessionRead = try XCTUnwrap(
            state.requests().last(where: {
                $0.method == "GET" && $0.path == "/api/mobile/session"
            })
        )
        XCTAssertTrue(
            (provedSessionRead.headers["cookie"] ?? "").contains("avibe_session=proved")
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

    func testPasswordSignInSendsThePasswordOnlyInTheGrantAndPassesOnTheSecondFactor() async throws {
        let password = "correct horse \"battery\" \(UUID().uuidString)"
        let state = HTTPFixtureState()
        let owed = """
        {
          "error": "two_factor_required",
          "email": "alex@example.com",
          "gate": "challenge",
          "methods": ["totp"]
        }
        """
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
                    """,
                    headers: [
                        ("Set-Cookie", "control_plane=seeded; Path=/; HttpOnly"),
                    ]
                )
            case ("POST", "/auth/v1/token"):
                return .json(
                    """
                    {
                      "access_token": "fixture-access",
                      "refresh_token": "fixture-refresh",
                      "weak_password": {
                        "message": "Password is known to be weak and easy to guess",
                        "reasons": ["pwned", "mystery", "pwned"]
                      }
                    }
                    """
                )
            case ("POST", "/auth/callback"):
                return .json(
                    "{}",
                    headers: [
                        ("Set-Cookie", "avibe_session=first-factor; Path=/; HttpOnly"),
                    ]
                )
            case ("GET", "/api/mobile/session"):
                return LocalHTTPResponse(
                    status: 403,
                    headers: [("Content-Type", "application/json")],
                    body: Data(owed.utf8)
                )
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

        let result = try await transport.signInWithPassword(
            email: "alex@example.com",
            password: password
        )

        // The shared session read reports the second factor unchanged, and
        // the weak-password reasons this app knows come with it.
        XCTAssertEqual(
            result,
            MobilePasswordSignInDiscovery(
                discovery: .secondFactorRequired(
                    MobileSecondFactorRequirement(
                        email: "alex@example.com",
                        gate: .challenge,
                        methods: [.totp]
                    )
                ),
                weakPasswordReasons: [.pwned]
            )
        )
        let requests = state.requests().filter { $0.path != "/favicon.ico" }
        XCTAssertEqual(
            requests.map { "\($0.method) \($0.path)" },
            [
                "GET /api/config/auth",
                "POST /auth/v1/token",
                "POST /auth/callback",
                "GET /api/mobile/session",
            ]
        )
        let grant = requests[1]
        XCTAssertEqual(
            grant.queryItems,
            [URLQueryItem(name: "grant_type", value: "password")]
        )
        XCTAssertEqual(grant.headers["apikey"], "fixture-anon")
        XCTAssertEqual(grant.headers["authorization"], "Bearer fixture-anon")
        XCTAssertEqual(grant.headers["content-type"], "application/json")
        // `credentials: "omit"`: the control-plane cookie stays off the grant.
        XCTAssertNil(grant.headers["cookie"])
        XCTAssertEqual(
            try XCTUnwrap(
                JSONSerialization.jsonObject(with: grant.body) as? [String: String]
            ),
            ["email": "alex@example.com", "password": password]
        )
        let callback = requests[2]
        XCTAssertTrue(
            (callback.headers["cookie"] ?? "").contains("control_plane=seeded")
        )
        XCTAssertEqual(
            try XCTUnwrap(
                JSONSerialization.jsonObject(with: callback.body) as? [String: String]
            ),
            ["access_token": "fixture-access", "refresh_token": "fixture-refresh"]
        )
        XCTAssertTrue(
            (requests[3].headers["cookie"] ?? "").contains("avibe_session=first-factor")
        )

        // Everything the fixture captured, apart from the grant's body, and
        // every rendering of the result are free of the password.
        for (index, request) in requests.enumerated() {
            var captured = [request.method, request.path]
            captured += request.queryItems.map { "\($0.name)=\($0.value ?? "")" }
            captured += request.headers.map { "\($0.key): \($0.value)" }
            if index != 1 {
                captured.append(String(decoding: request.body, as: UTF8.self))
            }
            XCTAssertFalse(
                captured.contains { $0.contains(password) },
                "\(request.method) \(request.path)"
            )
        }
        var dumped = ""
        dump(result, to: &dumped)
        XCTAssertFalse(String(describing: result).contains(password))
        XCTAssertFalse(dumped.contains(password))
    }

    func testPasswordSignInFailuresMapByStageWithoutEchoingThePassword() async throws {
        let password = "hunter2-\(UUID().uuidString)"
        let fixture = PasswordGrantFixture()
        let state = HTTPFixtureState()
        let sessionJSON = Self.sessionJSON
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
            case ("POST", "/auth/v1/token"):
                return fixture.token
            case ("POST", "/auth/callback"):
                return fixture.callback
            case ("GET", "/api/mobile/session"):
                return fixture.session ?? .json(sessionJSON)
            default:
                return .status(404)
            }
        }
        let baseURL = try await server.start()
        state.baseURL = baseURL
        addTeardownBlock {
            server.stop()
        }
        let transport = MobileAuthTransport(
            controlPlaneURL: baseURL,
            websiteDataStore: .nonPersistent()
        )
        let granted = LocalHTTPResponse.json(
            #"{"access_token":"fixture-access","refresh_token":"fixture-refresh"}"#
        )
        func status(
            _ code: Int,
            _ body: String,
            headers: [(String, String)] = []
        ) -> LocalHTTPResponse {
            LocalHTTPResponse(
                status: code,
                headers: [("Content-Type", "application/json")] + headers,
                body: Data(body.utf8)
            )
        }
        struct FailureCase {
            let name: String
            let token: LocalHTTPResponse
            var callback: LocalHTTPResponse = .json("{}")
            var session: LocalHTTPResponse?
            let expectedCode: String
            var expectedRetryAfter: Int?
            var reachesCallback = false
        }
        let cases = [
            // A provider that echoed the password must not carry it further.
            FailureCase(
                name: "current error shape",
                token: status(400, #"{"code":"invalid_credentials","message":"Invalid login credentials for \#(password)"}"#),
                expectedCode: "invalid_credentials"
            ),
            FailureCase(
                name: "older error shape",
                token: status(400, #"{"code":400,"error_code":"email_not_confirmed","msg":"Email not confirmed"}"#),
                expectedCode: "email_not_confirmed"
            ),
            FailureCase(
                name: "OAuth error shape",
                token: status(400, #"{"error":"invalid_grant","error_description":"Invalid login credentials"}"#),
                expectedCode: "invalid_credentials"
            ),
            FailureCase(
                name: "other client error",
                token: status(422, #"{"code":"validation_failed","message":"\#(password)"}"#),
                expectedCode: "invalid_credentials"
            ),
            FailureCase(
                name: "throttle with Retry-After",
                token: status(429, #"{"code":"over_request_rate_limit"}"#, headers: [("Retry-After", "42")]),
                expectedCode: "rate_limited",
                expectedRetryAfter: 42
            ),
            FailureCase(
                name: "throttle without Retry-After",
                token: status(429, "not json"),
                expectedCode: "rate_limited"
            ),
            FailureCase(
                name: "provider failure",
                token: status(503, "{}"),
                expectedCode: "network"
            ),
            FailureCase(
                name: "grant without a session",
                token: .json(#"{"user":{}}"#),
                expectedCode: "network"
            ),
            FailureCase(
                name: "malformed grant",
                token: .html("<p>ok</p>"),
                expectedCode: "network"
            ),
            FailureCase(
                name: "callback failure",
                token: granted,
                callback: status(502, #"{"error":"bad gateway"}"#),
                expectedCode: "network",
                reachesCallback: true
            ),
            FailureCase(
                name: "session signed out",
                token: granted,
                session: status(401, #"{"error":"unauthorized"}"#),
                expectedCode: "signed_out",
                reachesCallback: true
            ),
            FailureCase(
                name: "session failure",
                token: granted,
                session: status(500, "{}"),
                expectedCode: "network",
                reachesCallback: true
            ),
        ]

        // Establish this transport's own document and session before testing
        // provider error mapping. A cold-start failure belongs to this stage,
        // not the first table row. The separate deadline tests stay cold.
        addTeardownBlock { @MainActor [self] in
            let barrier = transport.invalidate()
            let released = await barrier?.waitForRelease() ?? true
            XCTAssertTrue(released, "Password fixture transport still owns a WebView")
            server.requests.attach(to: self, name: "password fixture requests")
        }
        do {
            guard case .signedIn(let snapshot) = try await transport.fetchSession(),
                  snapshot.account.id == "account-1"
            else {
                throw FixtureReadinessError(description: "Unexpected fixture session")
            }
        } catch {
            throw FixtureReadinessError(description:
                "stage=password table prerequisite; \(server.requests.summary()); "
                    + "errorType=\(type(of: error))"
            )
        }
        print("[iOS fixture] stage=password table ready; \(server.requests.summary())")

        for testCase in cases {
            let requestOffset = server.requests.count
            defer {
                print("[iOS fixture] row=\(testCase.name); \(server.requests.summary(since: requestOffset))")
            }
            fixture.set(
                token: testCase.token,
                callback: testCase.callback,
                session: testCase.session
            )
            let callbacksBefore = state.requests().filter {
                $0.path == "/auth/callback"
            }.count
            do {
                _ = try await transport.signInWithPassword(
                    email: "alex@example.com",
                    password: password
                )
                XCTFail("Expected \(testCase.name) to fail")
            } catch let error as MobileAccountError {
                XCTAssertEqual(error.code, testCase.expectedCode, testCase.name)
                XCTAssertEqual(
                    error.retryAfterSeconds,
                    testCase.expectedRetryAfter,
                    testCase.name
                )
                let rendered = [
                    String(describing: error),
                    String(reflecting: error),
                    error.localizedDescription,
                    String(
                        decoding: try JSONSerialization.data(
                            withJSONObject: error.jsonObject
                        ),
                        as: UTF8.self
                    ),
                ]
                XCTAssertFalse(
                    rendered.contains { $0.contains(password) },
                    testCase.name
                )
            }
            let rowRequests = Array(state.requests().dropFirst(requestOffset))
            XCTAssertEqual(
                rowRequests.filter { $0.method == "POST" && $0.path == "/auth/v1/token" }.count,
                1,
                "\(testCase.name) did not reach its grant; \(server.requests.summary(since: requestOffset))"
            )
            let callbacks = state.requests().filter {
                $0.path == "/auth/callback"
            }.count
            XCTAssertEqual(
                callbacks - callbacksBefore,
                testCase.reachesCallback ? 1 : 0,
                testCase.name
            )
        }
    }

    func testPasswordDoesNotFollowACrossOriginGrantRedirect() async throws {
        let password = "hunter2-\(UUID().uuidString)"
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
            case ("POST", "/auth/v1/token"):
                // A 307 would replay the POST body, password included.
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
            try await transport.signInWithPassword(
                email: "alex@example.com",
                password: password
            )
        }
        await assertNetworkError(from: request)

        XCTAssertTrue(
            state.requests().contains(where: {
                $0.method == "POST" && $0.path == "/auth/v1/token"
            })
        )
        XCTAssertFalse(
            state.requests().contains(where: { $0.path == "/auth/callback" })
        )
        XCTAssertTrue(captureState.requests().isEmpty)
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
        let before = await fetchAllNamedDataStoreIdentifiers()
        let defaults = makeDefaults()
        let registry = RealNamedDataStoreRegistry()
        var accountStore: AccountWebsiteDataStore? = AccountWebsiteDataStore(
            defaults: defaults,
            operations: registry.operations
        )
        let unownedIdentifier = UUID()
        addTeardownBlock {
            let remaining = await registry.removeCreatedIdentifiers()
            XCTAssertTrue(remaining.isEmpty)
        }
        // Someone else's store, alive and on disk, with no network session.
        // A session would let WebKit refuse its removal as "in use" and so hide
        // a sweep that wrongly tried; and a released session keeps writing its
        // directory back for a while after the cleanup below removes it (#3418).
        var unownedStore: WKWebsiteDataStore? = registry.makeUnownedStore(
            unownedIdentifier
        )
        await waitUntilListed([unownedIdentifier])
        let registered = await fetchAllNamedDataStoreIdentifiers()
        XCTAssertTrue(registered.contains(unownedIdentifier))

        await accountStore?.rotate()
        await accountStore?.sweepOrphans()

        let afterSweep = await fetchAllNamedDataStoreIdentifiers()
        XCTAssertTrue(before.isSubset(of: afterSweep))
        XCTAssertTrue(afterSweep.contains(unownedIdentifier))

        let identifiersCreatedByTest =
            registry.createdIdentifiers
        accountStore = nil
        unownedStore = nil
        let remaining = await registry.removeCreatedIdentifiers()
        XCTAssertTrue(remaining.isEmpty)

        let afterCleanup = await fetchAllNamedDataStoreIdentifiers()
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

    private func waitUntilListed(
        _ identifiers: Set<UUID>,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(Self.fixtureEventTimeout))
        while !(await fetchAllNamedDataStoreIdentifiers()).isSuperset(of: identifiers) {
            guard clock.now < deadline else {
                XCTFail("WebKit never listed the store", file: file, line: line)
                return
            }
            try? await Task.sleep(for: .milliseconds(20))
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
    private final class WeakStore {
        weak var store: WKWebsiteDataStore?

        init(_ store: WKWebsiteDataStore) {
            self.store = store
        }
    }

    private(set) var createdIdentifiers = Set<UUID>()
    private var stores: [WeakStore] = []

    var operations: AccountWebsiteDataStoreOperations {
        AccountWebsiteDataStoreOperations(
            makeDataStore: { [self] identifier in
                makeStore(identifier)
            },
            removeIdentifier: { [self] identifier in
                let error = await AccountWebsiteDataStoreOperations.live
                    .removeIdentifier(identifier)
                if error == nil {
                    createdIdentifiers.remove(identifier)
                }
                return error
            },
            listIdentifiers: AccountWebsiteDataStoreOperations.live
                .listIdentifiers
        )
    }

    /// A store for another owner than the ledger under test, removed with the
    /// ones it makes.
    func makeUnownedStore(_ identifier: UUID) -> WKWebsiteDataStore {
        makeStore(identifier)
    }

    private func makeStore(_ identifier: UUID) -> WKWebsiteDataStore {
        createdIdentifiers.insert(identifier)
        let store = WKWebsiteDataStore(forIdentifier: identifier)
        stores.append(WeakStore(store))
        return store
    }

    /// Removes every store made here, once WebKit has let go of it: WebKit
    /// finishes creating a new store's directories after it is returned, and
    /// a store removed before that has its directories made again afterwards.
    func removeCreatedIdentifiers() async -> Set<UUID> {
        let clock = ContinuousClock()
        let released = clock.now.advanced(by: .seconds(5))
        while stores.contains(where: { $0.store != nil }), clock.now < released {
            try? await Task.sleep(for: .milliseconds(10))
        }
        for attempt in 0..<50 {
            for identifier in createdIdentifiers.sorted(by: {
                $0.uuidString < $1.uuidString
            }) {
                _ = await AccountWebsiteDataStoreOperations.live
                    .removeIdentifier(identifier)
            }
            let registered = await fetchAllNamedDataStoreIdentifiers()
            createdIdentifiers.formIntersection(registered)
            if createdIdentifiers.isEmpty {
                return []
            }
            if attempt < 49 {
                try? await Task.sleep(for: .milliseconds(100))
            }
        }
        return createdIdentifiers
    }
}

@MainActor
private func fetchAllNamedDataStoreIdentifiers() async -> Set<UUID> {
    await withCheckedContinuation { continuation in
        WKWebsiteDataStore.fetchAllDataStoreIdentifiers {
            continuation.resume(returning: Set($0))
        }
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
    let queryItems: [URLQueryItem]
    let headers: [String: String]
    let body: Data
}

struct LocalHTTPResponse: Sendable {
    let status: Int
    let headers: [(String, String)]
    let body: Data
    let sentEvent: FixtureEvent?
    let headersSentEvent: FixtureEvent?
    let bodyRelease: DispatchSemaphore?

    init(
        status: Int,
        headers: [(String, String)],
        body: Data,
        sentEvent: FixtureEvent? = nil,
        headersSentEvent: FixtureEvent? = nil,
        bodyRelease: DispatchSemaphore? = nil
    ) {
        self.status = status
        self.headers = headers
        self.body = body
        self.sentEvent = sentEvent
        self.headersSentEvent = headersSentEvent
        self.bodyRelease = bodyRelease
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

/// The password grant, callback, and session answers one failure case serves.
private final class PasswordGrantFixture: @unchecked Sendable {
    private let lock = NSLock()
    private var storedToken = LocalHTTPResponse.status(500)
    private var storedCallback = LocalHTTPResponse.json("{}")
    private var storedSession: LocalHTTPResponse?

    var token: LocalHTTPResponse {
        lock.withLock { storedToken }
    }

    var callback: LocalHTTPResponse {
        lock.withLock { storedCallback }
    }

    var session: LocalHTTPResponse? {
        lock.withLock { storedSession }
    }

    func set(
        token: LocalHTTPResponse,
        callback: LocalHTTPResponse,
        session: LocalHTTPResponse?
    ) {
        lock.withLock {
            storedToken = token
            storedCallback = callback
            storedSession = session
        }
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

    let requests = FixtureRequestLog()
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
                let response = self.handler(request)
                self.requests.record(request, status: response.status)
                self.send(response, through: connection)
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
        let headers = Data(
            (lines.joined(separator: "\r\n") + "\r\n\r\n").utf8
        )
        guard let bodyRelease = response.bodyRelease else {
            var data = headers
            data.append(response.body)
            connection.send(content: data, completion: .contentProcessed { _ in
                response.sentEvent?.signal()
                connection.cancel()
            })
            return
        }

        let prefixCount = min(65_536, response.body.count)
        var prefix = headers
        prefix.append(response.body.prefix(prefixCount))
        let remainingBody = Data(response.body.dropFirst(prefixCount))
        connection.send(content: prefix, completion: .contentProcessed { [weak self] error in
            response.headersSentEvent?.signal()
            guard error == nil, let self else {
                connection.cancel()
                return
            }
            DispatchQueue.global(qos: .userInitiated).async {
                bodyRelease.wait()
                self.queue.async {
                    connection.send(
                        content: remainingBody,
                        completion: .contentProcessed { _ in
                            response.sentEvent?.signal()
                            connection.cancel()
                        }
                    )
                }
            }
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
        let target = URLComponents(string: rawTarget)
        return LocalHTTPRequest(
            method: String(requestLine[0]),
            path: target?.path ?? rawTarget,
            queryItems: target?.queryItems ?? [],
            headers: headers,
            body: data.subdata(
                in: bodyStart..<(bodyStart + contentLength)
            )
        )
    }
}
