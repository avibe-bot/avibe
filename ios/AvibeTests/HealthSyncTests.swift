import Darwin
import Foundation
import Security
import WebKit
import XCTest
@testable import Avibe

/// All credentials are in memory, all files belong to mkdtemp, and HTTP only
/// reaches the existing loopback fixture. No HealthKit store is constructed.
final class HealthSyncTests: XCTestCase {
    // Contract: sequence and pending payloads survive process death together.
    // Regression: an in-memory counter reuses a sequence after relaunch; the
    // aggregation tests cannot exercise the durable device/outbox boundary.
    func testSequenceAndOutboxSurviveReopenAndAreExcludedFromBackup() throws {
        let directory = try temporaryDirectory()
        let store = try HealthDeviceStore(directory: directory)
        XCTAssertEqual(try store.nextSequence(), 1)
        let id = try store.enqueue(["device_seq": 1, "items": [], "failures": []])
        let reopened = try HealthDeviceStore(directory: directory)
        XCTAssertEqual(reopened.state.sequence, 1)
        XCTAssertEqual(reopened.state.uploads, [id])
        XCTAssertTrue(FileManager.default.fileExists(atPath: reopened.file(id).path))
        XCTAssertEqual(
            try directory.resourceValues(forKeys: [.isExcludedFromBackupKey]).isExcludedFromBackup,
            true
        )
        XCTAssertEqual(try reopened.nextSequence(), 2)
        XCTAssertEqual(try HealthDeviceStore(directory: directory).state.sequence, 2)
        try reopened.removeUpload(id)
        XCTAssertTrue(try HealthDeviceStore(directory: directory).state.uploads.isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: reopened.file(id).path))
        try reopened.clear()
        XCTAssertFalse(FileManager.default.fileExists(atPath: directory.path))
        XCTAssertEqual(reopened.state.sequence, 0)
        XCTAssertTrue(reopened.state.completed.isEmpty)
    }

    func testExhaustedSequenceAndOversizedPayloadDoNotMutateDurableState() throws {
        let directory = try temporaryDirectory()
        let store = try HealthDeviceStore(directory: directory)
        try store.update { $0.sequence = Int64.max }
        XCTAssertThrowsError(try store.nextSequence()) {
            XCTAssertEqual($0 as? HealthSyncError, .code("sequence_exhausted"))
        }
        XCTAssertThrowsError(try store.enqueue(["padding": String(repeating: "x", count: 256 * 1024)])) {
            XCTAssertEqual($0 as? HealthSyncError, .code("invalid_payload"))
        }
        let reopened = try HealthDeviceStore(directory: directory)
        XCTAssertEqual(reopened.state.sequence, Int64.max)
        XCTAssertTrue(reopened.state.uploads.isEmpty)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: directory.path), ["state.json"])
    }

    // Contract: a reopened ledger always sees the payload dependency or the
    // consumed ACK, never a completed result detached from an unacked upload.
    // Regression: persisting those transitions in separate writes permits a
    // crash to report success early; the sequence-only test cannot detect it.
    func testEnqueueAndAckPersistTheResultDependencyInTheSameStateTransition() throws {
        let directory = try temporaryDirectory()
        let store = try HealthDeviceStore(directory: directory)
        try store.update {
            $0.completed["request-1"] = pendingResult()
        }
        let id = try store.enqueue(["device_seq": 1, "items": [], "failures": []]) { state, uploadID in
            state.completed["request-1"]?.uploadID = uploadID
            state.completed["request-1"]?.ready = true
            state.completed["request-1"]?.outcome = "succeeded"
        }
        let pending = try HealthDeviceStore(directory: directory)
        XCTAssertEqual(pending.state.uploads, [id])
        XCTAssertEqual(pending.state.completed["request-1"]?.uploadID, id)
        XCTAssertEqual(pending.state.completed["request-1"]?.ready, true)
        try pending.removeUpload(id) { state in
            state.completed["request-1"]?.uploadID = nil
            state.initialBackfillPending = false
        }
        let acknowledged = try HealthDeviceStore(directory: directory)
        XCTAssertTrue(acknowledged.state.uploads.isEmpty)
        XCTAssertNil(acknowledged.state.completed["request-1"]?.uploadID)
        XCTAssertFalse(acknowledged.state.initialBackfillPending)
        XCTAssertEqual(acknowledged.state.completed["request-1"]?.acknowledged, false)
    }

    // Contract: health secrets and the stable installation identifier stay in
    // the app's private, non-synchronizing ThisDeviceOnly items. The push
    // keychain tests do not cover health's independently constructed queries.
    func testCredentialItemsArePrivateDeviceOnlyAndLogoutKeepsOnlyInstallationIdentity() throws {
        let access = HealthMemoryKeychainAccess()
        let store = HealthCredentialStore(itemAccess: access)
        XCTAssertNil(try store.load())
        let installation = try store.installationID()
        XCTAssertNotNil(UUID(uuidString: installation))
        XCTAssertEqual(try store.installationID(), installation)
        let identity = HealthDeviceIdentity(
            installationID: installation, accountID: "account-1", instanceID: "instance-1",
            instanceName: "Fixture", origin: "https://health-fixture.invalid",
            deviceID: "device-1", credential: "memory-only"
        )
        try store.save(identity)
        XCTAssertEqual(try store.load(), identity)
        try store.save(identity)
        try store.clear()
        XCTAssertNil(try store.load())
        XCTAssertEqual(try store.installationID(), installation)
        XCTAssertEqual(Set(access.insertions.compactMap { $0[kSecAttrService as String] as? String }), [
            "bot.avibe.ios.health.device", "bot.avibe.ios.health.installation",
        ])
        for query in access.queries {
            XCTAssertEqual(query[kSecClass as String] as? String, kSecClassGenericPassword as String)
            XCTAssertEqual(query[kSecAttrSynchronizable as String] as? Bool, false)
            XCTAssertNil(query[kSecAttrAccessGroup as String])
            XCTAssertEqual(query[kSecAttrAccount as String] as? String, "device")
        }
        for attributes in access.insertions + access.updates {
            XCTAssertEqual(
                attributes[kSecAttrAccessible as String] as? String,
                kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly as String
            )
        }
        XCTAssertEqual(access.deletions.compactMap { $0[kSecAttrService as String] as? String }, [
            "bot.avibe.ios.health.device",
        ])
    }

    // Contract: only an absolute instance origin and a device-scoped path can
    // receive credentials. Prefix-only checks miss encoded and literal escape.
    func testDeviceTargetsRejectRemoteHTTPOriginComponentsAndEscapingPaths() throws {
        let path = "/api/mobile/device/v1/manifest"
        for origin in [
            "http://example.com", "ftp://example.com", "//example.com",
            "https:///manifest", "https://", "https://user:password@example.com",
            "https://example.com/base", "https://example.com/?token=x",
            "https://example.com/#fragment",
        ] {
            XCTAssertThrowsError(try HealthDeviceClient.url(origin: origin, path: path)) {
                XCTAssertEqual($0 as? HealthSyncError, .code("invalid_target"))
            }
        }
        for invalidPath in [
            "/api/mobile/session", "//other.example/api/mobile/device/v1/manifest",
            "/api/mobile/device/v1/../session", "/api/mobile/device/v1/./manifest",
            "/api/mobile/device/v1/%2e%2e/session",
            "/api/mobile/device/v1/%252e%252e/session",
            "/api/mobile/device/v1/%2f..%2fsession",
            "/api/mobile/device/v1/manifest?cookie=x",
            "/api/mobile/device/v1/manifest#fragment",
            "/api/mobile/device/v1/..\\session",
        ] {
            XCTAssertThrowsError(try HealthDeviceClient.url(origin: "https://example.com", path: invalidPath)) {
                XCTAssertEqual($0 as? HealthSyncError, .code("invalid_target"))
            }
        }
        for origin in ["https://example.com", "https://example.com/", "http://127.0.0.1:12345", "http://localhost:12345"] {
            let url = try HealthDeviceClient.url(origin: origin, path: path)
            XCTAssertEqual(url.path, path)
            XCTAssertNil(url.query)
            XCTAssertNil(url.fragment)
        }
    }

    // Contract: HTTP status and retry policy remain observable to the owner;
    // malformed/unauthorized replies must not become a successful empty result.
    func testDeviceResponsesPreserveUnauthorizedAndRetryAfter() throws {
        let url = URL(string: "http://127.0.0.1/api/mobile/device/v1/manifest")!
        for (status, body, delay, expected) in [
            (401, #"{"error":{"code":"device_unauthorized"}}"#, nil, HealthSyncError.http(401, "device_unauthorized", nil)),
            (429, #"{"code":"rate_limited"}"#, "17", .http(429, "rate_limited", 17)),
            (503, #"{"error":{"code":"retry_later"}}"#, "31", .http(503, "retry_later", 31)),
            (500, "not json", nil, .http(500, "internal", nil)),
        ] as [(Int, String, String?, HealthSyncError)] {
            let response = try XCTUnwrap(HTTPURLResponse(
                url: url, statusCode: status, httpVersion: nil,
                headerFields: delay.map { ["Retry-After": $0] }
            ))
            XCTAssertThrowsError(try HealthDeviceClient.response(data: Data(body.utf8), response: response)) {
                XCTAssertEqual($0 as? HealthSyncError, expected)
            }
        }
        let success = try XCTUnwrap(HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil))
        XCTAssertEqual(
            try HealthDeviceClient.response(data: Data(#"{"accepted_seq":2}"#.utf8), response: success)["accepted_seq"] as? Int,
            2
        )
        XCTAssertThrowsError(try HealthDeviceClient.response(data: Data(), response: nil))
        let now = Date(timeIntervalSince1970: 0)
        XCTAssertEqual(HealthDeviceClient.retryDelay("Thu, 01 Jan 1970 00:01:00 GMT", now: now), 60)
        XCTAssertEqual(HealthDeviceClient.retryDelay("Thu, 01 Jan 1970 00:00:00 GMT", now: now.addingTimeInterval(5)), 0)
        XCTAssertNil(HealthDeviceClient.retryDelay("not-a-date", now: now))
    }

    // Real URLSession/loopback round trip: a protocol fake cannot detect cookie
    // persistence or automatic redirect forwarding of a bearer credential.
    func testDeviceClientUsesJSONBearerWithoutCookiesAndDoesNotFollowRedirects() async throws {
        let forwarded = HealthHTTPRequestRecorder()
        let destination = try LocalHTTPServer { request in
            forwarded.append(request)
            return .json("{}")
        }
        let destinationURL = try await destination.start()
        addTeardownBlock { destination.stop() }
        let recorded = HealthHTTPRequestRecorder()
        let redirectURL = destinationURL.appendingPathComponent("api/mobile/device/v1/manifest").absoluteString
        let server = try LocalHTTPServer { request in
            recorded.append(request)
            if request.path.hasSuffix("/redirect") {
                return LocalHTTPResponse(status: 307, headers: [("Location", redirectURL)], body: Data())
            }
            if request.path.hasSuffix("/pair") {
                return .json(
                    #"{"device_id":"device-1","credential":"memory-only","instance":{"id":"instance-1"}}"#,
                    headers: [("Set-Cookie", "browser_session=fixture; Path=/; HttpOnly")]
                )
            }
            return .json("{}")
        }
        let baseURL = try await server.start()
        addTeardownBlock { server.stop() }
        let client = HealthDeviceClient()
        let pair = try await client.request(
            origin: baseURL.absoluteString, credential: nil, method: "POST",
            path: "/api/mobile/device/v1/pair",
            body: ["pairing_code": "one-use", "installation_id": "installation-1",
                   "platform": "ios", "device_name": "Fixture", "app_version": "1",
                   "metrics": ["steps", "activity", "workouts", "sleep"]]
        )
        XCTAssertEqual(pair["device_id"] as? String, "device-1")
        _ = try await client.request(
            origin: baseURL.absoluteString, credential: "memory-only", method: "PUT",
            path: "/api/mobile/device/v1/manifest",
            body: ["contract": 1, "app_version": "1", "os_version": "fixture",
                   "timezone": "UTC", "metrics": ["steps", "activity", "workouts", "sleep"],
                   "background_delivery": true, "push_wake": false]
        )
        do {
            _ = try await client.request(
                origin: baseURL.absoluteString, credential: "memory-only", method: "PUT",
                path: "/api/mobile/device/v1/redirect", body: [:]
            )
            XCTFail("A redirect must not be treated as an acknowledged device request.")
        } catch {
            XCTAssertEqual(error as? HealthSyncError, .http(307, "internal", nil))
        }
        let requests = recorded.snapshot()
        XCTAssertEqual(requests.map(\.method), ["POST", "PUT", "PUT"])
        XCTAssertEqual(requests.map(\.path), [
            "/api/mobile/device/v1/pair", "/api/mobile/device/v1/manifest", "/api/mobile/device/v1/redirect",
        ])
        XCTAssertTrue(requests.allSatisfy { $0.headers["cookie"] == nil })
        XCTAssertTrue(requests.allSatisfy { $0.headers["content-type"] == "application/json" })
        XCTAssertNil(requests.first?.headers["authorization"])
        XCTAssertEqual(requests.dropFirst().map { $0.headers["authorization"] }, ["Bearer memory-only", "Bearer memory-only"])
        let pairBody = try object(try XCTUnwrap(requests.first).body)
        XCTAssertEqual(pairBody["pairing_code"] as? String, "one-use")
        XCTAssertEqual(pairBody["platform"] as? String, "ios")
        XCTAssertEqual(pairBody["installation_id"] as? String, "installation-1")
        let manifest = try object(requests[1].body)
        XCTAssertEqual(manifest["contract"] as? Int, 1)
        XCTAssertEqual(manifest["metrics"] as? [String], ["steps", "activity", "workouts", "sleep"])
        XCTAssertEqual(manifest["push_wake"] as? Bool, false)
        XCTAssertTrue(forwarded.snapshot().isEmpty)
    }

    // Contract: every instance document, including Show Pages, only gets state.
    // Regression: a legacy read bridge or a child frame bypass exposes health
    // data; parser-only tests do not exercise WebKit's actual frame policy.
    @MainActor
    func testInstanceAndShowPageBridgeOnlyExposeConnectStateAndRejectSubframes() async throws {
        let server = try LocalHTTPServer { _ in
            .html(#"<!doctype html><iframe id="child" srcdoc="<p>child</p>"></iframe>"#)
        }
        let baseURL = try await server.start()
        addTeardownBlock { server.stop() }
        let origin = try XCTUnwrap(WebOrigin(url: baseURL, requiresHTTPS: false))
        var calls: [(String, String, WebOrigin)] = []
        for path in ["session/test", "show/health-fixture"] {
            // Each document starts from about:blank, so readiness cannot
            // accidentally accept the previous page during a navigation.
            let webView = WKWebView(frame: .zero, configuration: NativeBridgeConfiguration.make(
                instanceOrigin: origin,
                websiteDataStore: .nonPersistent(),
                setOuterScrollLocked: { _, _ in false },
                healthConnect: { requestID, code, source in
                    calls.append((requestID, code, source))
                    return "connected"
                }
            ))
            webView.load(URLRequest(url: baseURL.appendingPathComponent(path)))
            try await WebKitReadiness.waitForDocument(in: webView)
            let raw = try await webView.callAsyncJavaScript(
                """
                const info = await window.avibeNative.call("app.info");
                const connected = await window.avibeNative.call("health.connect", {
                  requestId: "request-1", pairingCode: "one-use"
                });
                const unsupported = {};
                for (const method of ["health.read", "health.refresh", "health.status",
                    "health.query", "health.disconnect", "health.revoke", "health.summary",
                    "health.authorize", "health.requestAuthorization", "health.sync", "app.showHealth"]) {
                  try { await window.avibeNative.call(method); unsupported[method] = "ok"; }
                  catch (error) { unsupported[method] = error.code; }
                }
                const invalid = [];
                for (const params of [{}, {requestId:"r"}, {requestId:"r",pairingCode:""},
                    {requestId:"r",pairingCode:"c",metrics:["steps"]}]) {
                  try { await window.avibeNative.call("health.connect", params); invalid.push("ok"); }
                  catch (error) { invalid.push(error.code); }
                }
                const child = await document.querySelector("#child").contentWindow.webkit
                  .messageHandlers.avibeNative.postMessage({
                    method:"health.connect", params:{requestId:"child",pairingCode:"child"}
                  });
                return JSON.stringify({path:location.pathname, capability:info.capabilities.health,
                  connected, unsupported, invalid, child});
                """,
                arguments: [:], in: nil, contentWorld: .page
            )
            let payload = try object(Data(try XCTUnwrap(raw as? String).utf8))
            XCTAssertEqual(payload["path"] as? String, "/" + path)
            XCTAssertEqual(payload["capability"] as? Int, 1)
            let connected = try XCTUnwrap(payload["connected"] as? [String: String])
            XCTAssertEqual(connected, ["state": "connected"])
            let unsupported = try XCTUnwrap(payload["unsupported"] as? [String: String])
            XCTAssertEqual(unsupported.count, 11)
            XCTAssertTrue(unsupported.values.allSatisfy { $0 == "unsupported" })
            XCTAssertEqual(payload["invalid"] as? [String], Array(repeating: "invalid_argument", count: 4))
            let child = try XCTUnwrap(payload["child"] as? [String: Any])
            XCTAssertEqual(child["ok"] as? Bool, false)
            XCTAssertEqual((child["error"] as? [String: Any])?["code"] as? String, "forbidden")
        }
        XCTAssertEqual(calls.count, 2)
        XCTAssertTrue(calls.allSatisfy { $0.0 == "request-1" && $0.1 == "one-use" && $0.2 == origin })
    }

    // Coordinator contracts have no previous coverage: authorization ordering,
    // ACK-gated completion, cancellation and durable deduplication are outside
    // the reader's aggregation and the bridge's parsing responsibilities.
    @MainActor
    func testDecliningNativeConsentDoesNotAuthorizeReadOrPair() async throws {
        let fixture = try coordinatorFixture()
        let operation = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        XCTAssertTrue(fixture.reader.days.isEmpty)
        XCTAssertEqual(fixture.reader.authorizations, 0)
        XCTAssertTrue(fixture.client.calls.isEmpty)
        fixture.coordinator.answerConsent(false)
        let state = await operation.value
        XCTAssertEqual(state, "declined")
        XCTAssertEqual(fixture.reader.authorizations, 0)
        XCTAssertTrue(fixture.reader.days.isEmpty)
        XCTAssertTrue(fixture.client.calls.isEmpty)
        XCTAssertNil(try fixture.credentials.load())
    }

    @MainActor
    func testSuccessWaitsForUploadAckAndReportsOnlyClaimedMetricStates() async throws {
        let fixture = try coordinatorFixture()
        fixture.client.claims = [[claim(token: "claim-1", metrics: ["steps", "sleep"])]]
        let operation = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        try await eventually { fixture.uploader.uploads.count == 1 }
        XCTAssertTrue(fixture.client.results.isEmpty)
        let upload = try XCTUnwrap(fixture.uploader.uploads.first)
        let before = try HealthDeviceStore(directory: fixture.directory)
        XCTAssertEqual(before.state.uploads, [upload.id])
        XCTAssertEqual(before.state.completed["request-1"]?.uploadID, upload.id)
        XCTAssertEqual(before.state.completed["request-1"]?.ready, true)
        await fixture.uploader.acknowledge(upload)
        let state = await operation.value
        XCTAssertEqual(state, "connected")
        try await eventually { !fixture.client.results.isEmpty }
        for result in fixture.client.results {
            XCTAssertEqual(Set(result.keys), ["claim_token", "outcome", "result"])
            XCTAssertEqual(result["outcome"] as? String, "succeeded")
            let envelope = try XCTUnwrap(result["result"] as? [String: Any])
            XCTAssertEqual(Set(envelope.keys), ["metrics"])
            XCTAssertEqual(envelope["metrics"] as? [String: String], ["steps": "empty", "sleep": "empty"])
        }
        XCTAssertEqual(fixture.reader.days, [30])
        let after = try HealthDeviceStore(directory: fixture.directory)
        XCTAssertTrue(after.state.uploads.isEmpty)
        XCTAssertNil(after.state.completed["request-1"]?.uploadID)
        XCTAssertFalse(FileManager.default.fileExists(atPath: upload.file.path))
        XCTAssertEqual(after.state.completed["request-1"]?.acknowledged, true)
    }

    @MainActor
    func testMismatchedUploadAckKeepsTheDurableDependencyUntilAValidAck() async throws {
        let fixture = try coordinatorFixture()
        let operation = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        try await eventually { fixture.uploader.uploads.count == 1 }
        let upload = try XCTUnwrap(fixture.uploader.uploads.first)
        await fixture.uploader.onComplete?(upload.id, .success(["accepted_seq": upload.sequence.int64Value + 1]))
        let state = await operation.value
        XCTAssertEqual(state, "connected")
        XCTAssertTrue(fixture.client.results.isEmpty)
        let retained = try HealthDeviceStore(directory: fixture.directory)
        XCTAssertEqual(retained.state.uploads, [upload.id])
        XCTAssertEqual(retained.state.completed["request-1"]?.uploadID, upload.id)
        XCTAssertTrue(FileManager.default.fileExists(atPath: upload.file.path))
        await fixture.coordinator.resume(foreground: false)
        XCTAssertEqual(fixture.uploader.uploads.map(\.id), [upload.id, upload.id])
        XCTAssertTrue(fixture.client.results.isEmpty)
        await fixture.uploader.acknowledge(upload)
        try await eventually { fixture.client.results.count == 1 }
        XCTAssertEqual(fixture.client.results.first?["outcome"] as? String, "succeeded")
        XCTAssertEqual(fixture.reader.days, [30])
    }

    @MainActor
    func testLogoutClearsCredentialsFilesAndCancelsUnacknowledgedUpload() async throws {
        let fixture = try coordinatorFixture()
        let operation = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        try await eventually { fixture.uploader.uploads.count == 1 }
        let upload = try XCTUnwrap(fixture.uploader.uploads.first)
        XCTAssertNotNil(try fixture.credentials.load())
        let cancellations = fixture.uploader.cancellations
        let state = await operation.value
        XCTAssertEqual(state, "connected")
        XCTAssertTrue(fixture.client.results.isEmpty)
        await fixture.coordinator.logout()
        XCTAssertNil(try fixture.credentials.load())
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.directory.path))
        XCTAssertGreaterThan(fixture.uploader.cancellations, cancellations)
        XCTAssertNil(fixture.reader.observer)
        XCTAssertTrue(fixture.client.results.isEmpty)
        await fixture.uploader.acknowledge(upload)
        await Task.yield()
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.directory.path))
        XCTAssertNil(try fixture.credentials.load())
    }

    @MainActor
    func testLogoutDuringPairDoesNotRestoreCredentialsWhenTheResponseArrives() async throws {
        let fixture = try coordinatorFixture()
        fixture.client.holdPair = true
        let operation = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        try await eventually { fixture.client.pairContinuation != nil }
        await fixture.coordinator.logout()
        fixture.client.releasePair()
        let state = await operation.value
        XCTAssertEqual(state, "failed")
        XCTAssertNil(try fixture.credentials.load())
        XCTAssertEqual(fixture.credentials.saves, 0)
        XCTAssertTrue(fixture.client.calls.contains { $0.method == "DELETE" && $0.credential == "memory-only" })
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.directory.path))
        XCTAssertTrue(fixture.reader.days.isEmpty)
        XCTAssertTrue(fixture.uploader.uploads.isEmpty)
    }

    @MainActor
    func testUnauthorizedDeviceClearsLocalStateWithoutTryingToRevokeAgain() async throws {
        let directory = try temporaryDirectory()
        let store = try HealthDeviceStore(directory: directory)
        _ = try store.enqueue(["device_seq": 1, "items": [], "failures": []])
        let fixture = try coordinatorFixture(directory: directory, savedIdentity: true)
        fixture.client.manifestError = HealthSyncError.http(401, "internal", nil)
        await fixture.coordinator.resume(foreground: false)
        XCTAssertNil(try fixture.credentials.load())
        XCTAssertFalse(FileManager.default.fileExists(atPath: directory.path))
        XCTAssertTrue(fixture.reader.days.isEmpty)
        XCTAssertNil(fixture.reader.observer)
        XCTAssertEqual(fixture.uploader.cancellations, 1)
        XCTAssertFalse(fixture.client.calls.contains { $0.method == "DELETE" })
    }

    @MainActor
    func testUnknownAccountNeverRestoresAndLaterAccountWithoutSelectionRejectsOldCredentials() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        var accountID: String?
        fixture.coordinator.accountIDProvider = { accountID }
        fixture.coordinator.targetProvider = { nil }
        await fixture.coordinator.resume(foreground: false)
        XCTAssertTrue(fixture.client.calls.isEmpty)
        XCTAssertNil(fixture.reader.observer)
        XCTAssertTrue(fixture.reader.days.isEmpty)
        accountID = "account-2"
        await fixture.coordinator.resume(foreground: true)
        XCTAssertNil(try fixture.credentials.load())
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.directory.path))
        XCTAssertTrue(fixture.reader.days.isEmpty)
        XCTAssertFalse(fixture.client.calls.contains { $0.path.hasSuffix("manifest") || $0.path.hasSuffix("claim") })
    }

    @MainActor
    func testBootstrapRevalidatesAlreadyLoadedIdentityWhileEarlierResumeIsSuspended() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        var accountID = "account-1"
        fixture.coordinator.accountIDProvider = { accountID }
        fixture.coordinator.targetProvider = { nil }
        fixture.client.holdManifest = true
        let early = Task { await fixture.coordinator.resume(foreground: false) }
        try await eventually { fixture.client.manifestContinuation != nil }
        accountID = "account-2"
        await fixture.coordinator.resume(foreground: true)
        XCTAssertNil(try fixture.credentials.load())
        XCTAssertNil(fixture.reader.observer)
        XCTAssertTrue(fixture.reader.days.isEmpty)
        fixture.client.releaseManifest()
        await early.value
        XCTAssertTrue(fixture.uploader.uploads.isEmpty)
        XCTAssertFalse(fixture.client.calls.contains { $0.path.hasSuffix("claim") })
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.directory.path))
    }

    @MainActor
    func testFailedLogoutCleanupCannotRestoreEvenForTheSameAccountAfterRelaunch() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        await fixture.coordinator.resume(foreground: false)
        let observer = try XCTUnwrap(fixture.reader.observer)
        await observer()
        let payload = try XCTUnwrap(fixture.uploader.uploads.first).file
        fixture.credentials.clearError = HealthSyncError.code("credential_unavailable")
        await fixture.coordinator.logout()
        XCTAssertNotNil(try fixture.credentials.load())
        XCTAssertFalse(FileManager.default.fileExists(atPath: payload.path))
        XCTAssertTrue(try HealthDeviceStore(directory: fixture.directory).state.cleanupPending)
        let queries = fixture.reader.days.count
        let calls = fixture.client.calls.count
        let restarted = HealthSyncCoordinator(
            credentials: fixture.credentials, reader: fixture.reader, client: fixture.client,
            uploader: fixture.uploader, directory: fixture.directory
        )
        restarted.accountIDProvider = { "account-1" }
        await restarted.resume(foreground: true)
        XCTAssertEqual(fixture.reader.days.count, queries)
        XCTAssertFalse(fixture.client.calls.dropFirst(calls).contains { $0.path.hasSuffix("manifest") || $0.path.hasSuffix("claim") })
        XCTAssertTrue(try HealthDeviceStore(directory: fixture.directory).state.cleanupPending)
        fixture.credentials.clearError = nil
        await restarted.resume(foreground: false)
        XCTAssertNil(try fixture.credentials.load())
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.directory.path))
    }

    @MainActor
    func testUnrecognized422And426StillSuspendInsteadOfRetrying() async throws {
        for status in [422, 426] {
            let fixture = try coordinatorFixture(savedIdentity: true)
            fixture.client.manifestError = HealthSyncError.http(status, "future_server_code", nil)
            await fixture.coordinator.resume(foreground: false)
            let calls = fixture.client.calls.count
            XCTAssertTrue(try HealthDeviceStore(directory: fixture.directory).state.suspended)
            XCTAssertNil(fixture.reader.observer)
            XCTAssertEqual(fixture.coordinator.notice, status == 426 ? "health.update_required" : "health.connection_failed")
            await fixture.coordinator.resume(foreground: true)
            XCTAssertEqual(fixture.client.calls.count, calls)
            XCTAssertTrue(fixture.reader.days.isEmpty)
        }
    }

    @MainActor
    func testRepeatedClaimsUseTheDurableResultWithoutQueryingAgain() async throws {
        let fixture = try coordinatorFixture()
        fixture.uploader.autoAcknowledge = true
        let operation = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        let connected = await operation.value
        XCTAssertEqual(connected, "connected")
        try await eventually { fixture.reader.days == [30] && !fixture.client.results.isEmpty }
        fixture.client.claims = [[claim(token: "claim-2")], [claim(token: "claim-3")]]
        await fixture.coordinator.resume(foreground: true)
        try await eventually { fixture.client.pendingClaims.count == 1 }
        // One routine foreground query is required independently of claims.
        XCTAssertEqual(fixture.reader.days, [30, 8])
        XCTAssertEqual(
            fixture.client.results.compactMap { $0["claim_token"] as? String },
            ["claim-1", "claim-2", "claim-3"]
        )
        XCTAssertEqual(fixture.uploader.uploads.count, 2)
        let reopened = try HealthDeviceStore(directory: fixture.directory)
        XCTAssertEqual(reopened.state.completed["request-1"]?.claimToken, "claim-3")
        XCTAssertEqual(reopened.state.completed["request-1"]?.acknowledged, true)
    }

    @MainActor
    func testObserverChangesDuringAnActiveQueryWaitForAFreshDurablePayload() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        await fixture.coordinator.resume(foreground: false)
        let observer = try XCTUnwrap(fixture.reader.observer)
        fixture.reader.holdQueries = true
        let first = Task { await observer() }
        try await eventually { fixture.reader.days.count == 1 }
        var secondCompleted = false
        let second = Task { await observer(); secondCompleted = true }
        await Task.yield()
        XCTAssertEqual(fixture.reader.days.count, 1)
        XCTAssertFalse(secondCompleted)
        fixture.reader.releaseQuery()
        try await eventually { fixture.reader.days.count == 2 }
        XCTAssertFalse(secondCompleted)
        let firstSnapshot = try HealthDeviceStore(directory: fixture.directory)
        XCTAssertEqual(firstSnapshot.state.uploads.count, 1)
        fixture.reader.releaseQuery()
        await first.value
        await second.value
        let final = try HealthDeviceStore(directory: fixture.directory)
        XCTAssertEqual(final.state.uploads.count, 2)
        XCTAssertEqual(final.state.sequence, 2)
        XCTAssertTrue(secondCompleted)
    }

    @MainActor
    func testRestoreQueriesAnInterruptedReservationAndWaitsForTheNewPayloadAck() async throws {
        let directory = try temporaryDirectory()
        let store = try HealthDeviceStore(directory: directory)
        try store.update { $0.completed["request-1"] = pendingResult() }
        let fixture = try coordinatorFixture(directory: directory, savedIdentity: true)
        await fixture.coordinator.resume(foreground: false)
        XCTAssertEqual(fixture.reader.days, [30])
        XCTAssertTrue(fixture.client.results.isEmpty)
        let upload = try XCTUnwrap(fixture.uploader.uploads.first)
        let pending = try HealthDeviceStore(directory: directory)
        XCTAssertEqual(pending.state.completed["request-1"]?.uploadID, upload.id)
        XCTAssertEqual(pending.state.completed["request-1"]?.ready, true)
        await fixture.uploader.acknowledge(upload)
        try await eventually { fixture.client.results.count == 1 }
        XCTAssertEqual(fixture.client.results.first?["outcome"] as? String, "succeeded")
        XCTAssertEqual(fixture.reader.days, [30])
    }

    @MainActor
    func testRestoreReplaysDurablePayloadWithoutRequeryAndConsumesItsAck() async throws {
        let directory = try temporaryDirectory()
        let store = try HealthDeviceStore(directory: directory)
        _ = try store.nextSequence()
        try store.update { $0.completed["request-1"] = pendingResult() }
        let id = try store.enqueue(["device_seq": 1, "items": [], "failures": []]) { state, uploadID in
            state.completed["request-1"]?.ready = true
            state.completed["request-1"]?.outcome = "succeeded"
            state.completed["request-1"]?.states = ["steps": "empty"]
            state.completed["request-1"]?.error = nil
            state.completed["request-1"]?.uploadID = uploadID
        }
        let fixture = try coordinatorFixture(directory: directory, savedIdentity: true)
        await fixture.coordinator.resume(foreground: false)
        XCTAssertTrue(fixture.reader.days.isEmpty)
        XCTAssertTrue(fixture.client.results.isEmpty)
        XCTAssertEqual(fixture.uploader.uploads.map(\.id), [id])
        await fixture.uploader.acknowledge(try XCTUnwrap(fixture.uploader.uploads.first))
        try await eventually { fixture.client.results.count == 1 }
        XCTAssertEqual(fixture.client.results.first?["outcome"] as? String, "succeeded")
        let reopened = try HealthDeviceStore(directory: directory)
        XCTAssertTrue(reopened.state.uploads.isEmpty)
        XCTAssertNil(reopened.state.completed["request-1"]?.uploadID)
        XCTAssertEqual(reopened.state.completed["request-1"]?.acknowledged, true)
        XCTAssertFalse(FileManager.default.fileExists(atPath: store.file(id).path))
    }

    @MainActor
    func testSetupFailuresRollbackBeforeObserversOrUpload() async throws {
        for status in [500, 426] {
            let fixture = try coordinatorFixture()
            fixture.client.manifestError = HealthSyncError.http(status, "fixture_failure", nil)
            let operation = fixture.connect()
            try await eventually { fixture.coordinator.consent != nil }
            fixture.coordinator.answerConsent(true)
            let result = await operation.value
            XCTAssertEqual(result, "failed")
            XCTAssertNil(fixture.credentials.identity)
            XCTAssertNil(fixture.reader.observer)
            XCTAssertTrue(fixture.reader.days.isEmpty)
            XCTAssertTrue(fixture.uploader.uploads.isEmpty)
            XCTAssertTrue(fixture.client.calls.contains { $0.method == "DELETE" && $0.credential == "memory-only" })
            XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.directory.path))
        }
    }

    @MainActor
    func testExactConnectClaimIsRequiredForCommit() async throws {
        for claims in [[], [["id": "other", "operation": "health.connect", "claim_token": "c", "metrics": ["steps"]]],
                       [["id": "request-1", "operation": "health.refresh", "claim_token": "c", "metrics": ["steps"]]]] {
            let fixture = try coordinatorFixture()
            fixture.client.claims = [claims]
            let operation = fixture.connect()
            try await eventually { fixture.coordinator.consent != nil }
            fixture.coordinator.answerConsent(true)
            let result = await operation.value
            XCTAssertEqual(result, "failed")
            XCTAssertNil(fixture.credentials.identity)
            XCTAssertNil(fixture.reader.observer)
            XCTAssertTrue(fixture.reader.days.isEmpty)
            XCTAssertTrue(fixture.uploader.uploads.isEmpty)
        }
    }

    @MainActor
    func testResumeDoesNotCleanActiveProvisionalConnect() async throws {
        let fixture = try coordinatorFixture()
        fixture.client.claims = []
        let operation = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        try await eventually { !fixture.client.pendingClaims.isEmpty }
        await fixture.coordinator.resume(foreground: false)
        await fixture.coordinator.resume(foreground: true)
        XCTAssertNotNil(fixture.credentials.identity)
        XCTAssertNil(fixture.reader.observer)
        XCTAssertTrue(fixture.reader.days.isEmpty)
        XCTAssertEqual(fixture.client.calls.filter { $0.method == "DELETE" }.count, 0)
        let continuation = fixture.client.pendingClaims.removeFirst()
        continuation.resume(returning: ["requests": [claim(token: "lease")]])
        let result = await operation.value
        XCTAssertEqual(result, "connected")
        try await eventually { !fixture.uploader.uploads.isEmpty }
    }

    @MainActor
    func testConnectedPrecedesUploadAckWithoutPrematureRequestSuccess() async throws {
        let fixture = try coordinatorFixture()
        fixture.reader.holdQueries = true
        let operation = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        let result = await operation.value
        XCTAssertEqual(result, "connected")
        let state = try HealthDeviceStore(directory: fixture.directory).state
        XCTAssertEqual(state.committedDeviceID, "device-1")
        XCTAssertEqual(state.completed["request-1"]?.ready, false)
        XCTAssertTrue(fixture.client.results.isEmpty)
        try await eventually { fixture.reader.days.count == 1 }
        fixture.reader.releaseQuery()
        try await eventually { fixture.uploader.uploads.count == 1 }
        XCTAssertTrue(fixture.client.results.isEmpty)
        await fixture.uploader.acknowledge(fixture.uploader.uploads[0])
        try await eventually { fixture.client.results.count == 1 }
        XCTAssertEqual(fixture.client.results[0]["outcome"] as? String, "succeeded")
    }

    @MainActor
    func testFailedSetupCleanupRetriesAfterRelaunch() async throws {
        let fixture = try coordinatorFixture()
        fixture.client.holdManifest = true
        let operation = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        try await eventually { fixture.client.manifestContinuation != nil }
        // Keychain failure is injected before the failed setup rolls back.
        fixture.credentials.clearError = HealthSyncError.code("credential_unavailable")
        let heldManifest = fixture.client.manifestContinuation
        fixture.client.manifestContinuation = nil
        heldManifest?.resume(throwing: HealthSyncError.http(500, "fixture_failure", nil))
        _ = await operation.value
        XCTAssertTrue(try HealthDeviceStore(directory: fixture.directory).state.cleanupPending)
        fixture.credentials.clearError = nil
        let restarted = HealthSyncCoordinator(credentials: fixture.credentials, reader: fixture.reader,
            client: fixture.client, uploader: fixture.uploader, directory: fixture.directory)
        await restarted.resume(foreground: false)
        XCTAssertNil(fixture.credentials.identity)
        XCTAssertTrue(fixture.reader.days.isEmpty)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.directory.path))
        XCTAssertGreaterThan(fixture.reader.disables, 0)
    }

    @MainActor
    func testUncommittedAndDifferentWebsiteStoreCredentialsFailClosed() async throws {
        for committed in [false, true] {
            let fixture = try coordinatorFixture(savedIdentity: true)
            if committed { fixture.coordinator.websiteDataStoreIDProvider = { UUID() } }
            else { try HealthDeviceStore(directory: fixture.directory).update { $0.committedDeviceID = nil } }
            await fixture.coordinator.resume(foreground: false)
            XCTAssertNil(fixture.credentials.identity)
            XCTAssertTrue(fixture.reader.days.isEmpty)
            XCTAssertNil(fixture.reader.observer)
            XCTAssertTrue(fixture.client.calls.contains { $0.method == "DELETE" })
        }
    }

    func testKeychainReadKeepsProtectionDistinctFromMissingAndOtherFailures() throws {
        let access = HealthMemoryKeychainAccess()
        let credentials = HealthCredentialStore(itemAccess: access)
        XCTAssertNil(try credentials.load())
        for status in [errSecInteractionNotAllowed, errSecNotAvailable] {
            access.readStatus = status
            XCTAssertThrowsError(try credentials.load()) { error in
                guard case HealthCredentialReadError.status(let actual) = error else {
                    return XCTFail("The original Keychain status must survive classification.")
                }
                XCTAssertEqual(actual, status)
            }
        }
    }

    @MainActor
    func testProtectedStartupWaitsForUnlockedAuthoritativeAccountBeforeBindingComparison() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        fixture.credentials.loadError = HealthCredentialReadError.status(errSecInteractionNotAllowed)
        fixture.coordinator.captureStartupReadiness()
        fixture.coordinator.websiteDataStoreIDProvider = { UUID() }
        fixture.coordinator.protectedDataAvailable = { false }
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        await fixture.coordinator.resume(foreground: false)
        XCTAssertTrue(fixture.client.calls.isEmpty)
        XCTAssertNotNil(fixture.credentials.identity)
        fixture.credentials.loadError = nil
        fixture.coordinator.protectedDataAvailable = { true }
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        await fixture.coordinator.resume(foreground: false)
        XCTAssertNil(fixture.credentials.identity)
        XCTAssertTrue(fixture.reader.days.isEmpty)
        XCTAssertTrue(fixture.client.calls.contains { $0.method == "DELETE" })
    }

    @MainActor
    func testPausingSameAccountRetainsBindingAndCatalogRemovalTargetsPairedInstance() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        await fixture.coordinator.resume(foreground: false)
        fixture.coordinator.accountEvent(.authorizationUnavailable)
        XCTAssertNotNil(fixture.credentials.identity)
        XCTAssertNil(fixture.reader.observer)
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: []))
        await fixture.coordinator.resume(foreground: false)
        XCTAssertNotNil(fixture.credentials.identity)
        XCTAssertFalse(fixture.client.calls.contains { $0.method == "DELETE" })
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        await fixture.coordinator.resume(foreground: false)
        XCTAssertNotNil(fixture.credentials.identity)
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["other-instance"]))
        await fixture.coordinator.resume(foreground: false)
        XCTAssertNil(fixture.credentials.identity)
        XCTAssertTrue(fixture.client.calls.contains { $0.method == "DELETE" })
    }

    @MainActor
    func testReadableCorruptStateClearsCapturedCredentialWithoutQuerying() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        try Data("{broken".utf8).write(to: fixture.directory.appendingPathComponent("state.json"))
        await fixture.coordinator.resume(foreground: false)
        XCTAssertNil(fixture.credentials.identity)
        XCTAssertTrue(fixture.reader.days.isEmpty)
        XCTAssertTrue(fixture.client.calls.contains { $0.method == "DELETE" })
    }

    @MainActor
    func testUnknown403RetainsCommittedOutboxAndCredential() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        await fixture.coordinator.resume(foreground: false)
        await fixture.reader.observer?()
        let upload = try XCTUnwrap(fixture.uploader.uploads.first)
        await fixture.uploader.onComplete?(upload.id, .failure(HealthSyncError.http(403, "internal", nil)))
        XCTAssertNotNil(fixture.credentials.identity)
        let state = try HealthDeviceStore(directory: fixture.directory).state
        XCTAssertEqual(state.uploads, [upload.id])
        XCTAssertFalse(state.suspended)
        XCTAssertFalse(fixture.client.calls.contains { $0.method == "DELETE" })
    }

    @MainActor
    func testPendingUploadStaysCommittedBeyondTheBoundedAttemptWithoutSuccess() async throws {
        let fixture = try coordinatorFixture()
        let operation = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        let reply = await operation.value
        XCTAssertEqual(reply, "connected")
        try await eventually { fixture.uploader.uploads.count == 1 }
        try await Task.sleep(for: .seconds(41))
        XCTAssertNotNil(fixture.credentials.identity)
        let state = try HealthDeviceStore(directory: fixture.directory).state
        XCTAssertEqual(state.committedDeviceID, "device-1")
        XCTAssertEqual(state.uploads.count, 1)
        XCTAssertFalse(state.completed["request-1"]?.acknowledged ?? true)
        XCTAssertTrue(fixture.client.results.isEmpty)
    }

    @MainActor
    func testOldSetupFailureCannotClearANewerCommittedConnection() async throws {
        let fixture = try coordinatorFixture()
        fixture.client.holdManifest = true
        let old = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        try await eventually { fixture.client.manifestContinuation != nil }
        let oldManifest = fixture.client.manifestContinuation
        fixture.client.manifestContinuation = nil
        await fixture.coordinator.logout()
        fixture.client.holdManifest = false
        let replacement = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        let connected = await replacement.value
        XCTAssertEqual(connected, "connected")
        oldManifest?.resume(throwing: HealthSyncError.http(401, "device_revoked", nil))
        let failed = await old.value
        XCTAssertEqual(failed, "failed")
        XCTAssertNotNil(fixture.credentials.identity)
        XCTAssertEqual(try HealthDeviceStore(directory: fixture.directory).state.committedDeviceID, "device-1")
        // A stale defer must not release another connect's generation or clear it.
        try await eventually { fixture.uploader.uploads.count == 1 }
    }

    @MainActor
    func testConcurrentResumeAndInitialWorkerReuseTheCommittedRequestPayload() async throws {
        let fixture = try coordinatorFixture()
        fixture.reader.holdQueries = true
        let operation = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        let connected = await operation.value
        XCTAssertEqual(connected, "connected")
        try await eventually { fixture.reader.days.count == 1 }
        let resume = Task { await fixture.coordinator.resume(foreground: false) }
        await Task.yield()
        fixture.reader.releaseQuery()
        await resume.value
        XCTAssertEqual(fixture.reader.days, [30])
        XCTAssertEqual(try HealthDeviceStore(directory: fixture.directory).state.uploads.count, 1)
    }

    @MainActor
    func testColdCleanupRetainsMarkerUntilSystemDeliveryDisableCompletes() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        fixture.reader.disableSucceeds = false
        await fixture.coordinator.logout()
        XCTAssertNil(fixture.credentials.identity)
        XCTAssertTrue(try HealthDeviceStore(directory: fixture.directory).state.cleanupPending)
        XCTAssertGreaterThan(fixture.reader.disables, 0)
        XCTAssertTrue(fixture.reader.days.isEmpty)
        fixture.reader.disableSucceeds = true
        await fixture.coordinator.resume(foreground: false)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.directory.path))
    }

    @MainActor
    func testAcknowledgedUploadSurvivesAccountAuthorizationGatesWithoutRetransmission() async throws {
        for pause in [false, true] {
            let fixture = try coordinatorFixture()
            var accountID: String? = "account-1"
            fixture.coordinator.accountIDProvider = { accountID }
            let operation = fixture.connect()
            try await eventually { fixture.coordinator.consent != nil }
            fixture.coordinator.answerConsent(true)
            let connected = await operation.value
            XCTAssertEqual(connected, "connected")
            try await eventually { fixture.uploader.uploads.count == 1 }
            let upload = fixture.uploader.uploads[0]
            accountID = nil
            if pause { fixture.coordinator.accountEvent(.authorizationUnavailable) }
            await fixture.uploader.acknowledge(upload)
            let acknowledged = try HealthDeviceStore(directory: fixture.directory).state
            XCTAssertTrue(acknowledged.uploads.isEmpty)
            XCTAssertNil(acknowledged.completed["request-1"]?.uploadID)
            XCTAssertFalse(acknowledged.completed["request-1"]?.acknowledged ?? true)
            XCTAssertTrue(fixture.client.results.isEmpty)
            accountID = "account-1"
            if pause {
                fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
            } else { await fixture.coordinator.resume(foreground: false) }
            try await eventually { fixture.client.results.count == 1 }
            XCTAssertEqual(fixture.client.results[0]["outcome"] as? String, "succeeded")
            XCTAssertEqual(fixture.uploader.uploads.count, 1)
        }
    }

    @MainActor
    func testOldConnectionAckCannotConsumeNewConnectionPayloadWithTheSameSequence() async throws {
        let fixture = try coordinatorFixture()
        let first = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        _ = await first.value
        try await eventually { fixture.uploader.uploads.count == 1 }
        let old = fixture.uploader.uploads[0]
        await fixture.coordinator.logout()
        fixture.client.pairedDeviceID = "device-2"
        fixture.client.claims = [[claim(token: "new-lease")]]
        let replacement = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        _ = await replacement.value
        try await eventually { fixture.uploader.uploads.count == 2 }
        let current = fixture.uploader.uploads[1]
        XCTAssertEqual(fixture.credentials.identity?.deviceID, "device-2")
        XCTAssertEqual(old.sequence, current.sequence)
        XCTAssertNotEqual(old.id, current.id)
        await fixture.uploader.acknowledge(old)
        let pending = try HealthDeviceStore(directory: fixture.directory).state
        XCTAssertEqual(pending.uploads, [current.id])
        XCTAssertEqual(pending.completed["request-1"]?.uploadID, current.id)
        XCTAssertTrue(fixture.client.results.isEmpty)
        await fixture.uploader.acknowledge(current)
        try await eventually { fixture.client.results.count == 1 }
        XCTAssertEqual(fixture.client.results[0]["outcome"] as? String, "succeeded")
    }

    @MainActor
    func testAuthenticatedEventRestartsPausedObserversOnlyOnce() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        var accountID: String? = "account-1"
        fixture.coordinator.accountIDProvider = { accountID }
        await fixture.coordinator.resume(foreground: false)
        XCTAssertEqual(fixture.reader.starts, 1)
        accountID = nil
        fixture.coordinator.accountEvent(.authorizationUnavailable)
        XCTAssertNil(fixture.reader.observer)
        accountID = "account-1"
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        try await eventually { fixture.reader.starts == 2 && fixture.client.calls.filter { $0.path.hasSuffix("/manifest") }.count == 2 }
        XCTAssertNotNil(fixture.reader.observer)
        XCTAssertEqual(fixture.reader.starts, 2)
        XCTAssertFalse(fixture.client.calls.contains { $0.method == "DELETE" })
    }

    @MainActor
    func testInvalidatedRuntimeWebViewRejectsHealthConnectAfterAccountTransition() async throws {
        let server = try LocalHTTPServer { _ in .html("<!doctype html><title>Health fixture</title>") }
        let baseURL = try await server.start()
        addTeardownBlock { server.stop() }
        let origin = try XCTUnwrap(WebOrigin(url: baseURL, requiresHTTPS: false))
        var calls = 0
        let controller = WebViewController(instanceOrigin: origin, websiteDataStore: .nonPersistent(),
            healthConnect: { _, _, _ in calls += 1; return "connected" },
            didStartLoading: {}, didFinishLoading: {}, didFailLoading: {})
        controller.loadViewIfNeeded()
        let webView = try XCTUnwrap(controller.view.subviews.compactMap { $0 as? WKWebView }.first)
        webView.load(URLRequest(url: baseURL))
        try await WebKitReadiness.waitForDocument(in: webView)
        let script = "return await window.avibeNative.call('health.connect', {requestId:'request-1', pairingCode:'fixture'});"
        let before = try await webView.callAsyncJavaScript(script, arguments: [:], in: nil, contentWorld: .page)
        XCTAssertEqual((before as? [String: String])?["state"], "connected")
        XCTAssertEqual(calls, 1)
        _ = controller.invalidateForAccountTransition()
        let after = try await webView.callAsyncJavaScript(script, arguments: [:], in: nil, contentWorld: .page)
        XCTAssertEqual((after as? [String: String])?["state"], "failed")
        XCTAssertEqual(calls, 1)
    }

    @MainActor
    func testColdCompletionAccountsForTheDurableHeadWithoutAccountOrKeychain() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        fixture.coordinator.accountIDProvider = { nil }
        fixture.credentials.loadError = HealthCredentialReadError.status(errSecInteractionNotAllowed)
        let upload = try pendingUpload(in: fixture.directory)
        await fixture.uploader.acknowledge(upload)
        let reopened = try HealthDeviceStore(directory: fixture.directory)
        XCTAssertTrue(reopened.state.uploads.isEmpty)
        XCTAssertNil(reopened.state.completed["request-1"]?.uploadID)
        XCTAssertFalse(reopened.state.completed["request-1"]?.acknowledged ?? true)
        XCTAssertFalse(reopened.state.initialBackfillPending)
        XCTAssertFalse(FileManager.default.fileExists(atPath: upload.file.path))
        XCTAssertTrue(fixture.client.calls.isEmpty)
        XCTAssertTrue(fixture.reader.days.isEmpty)
        XCTAssertEqual(fixture.reader.starts, 0)
        XCTAssertTrue(fixture.uploader.uploads.isEmpty)
        fixture.credentials.loadError = nil
        fixture.coordinator.accountIDProvider = { "account-1" }
        await fixture.coordinator.resume(foreground: false)
        try await eventually { fixture.client.results.count == 1 }
        XCTAssertTrue(fixture.uploader.uploads.isEmpty)
        XCTAssertEqual(fixture.client.results[0]["outcome"] as? String, "succeeded")
    }

    @MainActor
    func testColdCompletionBeforeHeldManifestDoesNotRetransmitAfterRestoration() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        let upload = try pendingUpload(in: fixture.directory)
        fixture.client.holdManifest = true
        let restoring = Task { await fixture.coordinator.resume(foreground: false) }
        try await eventually { fixture.client.manifestContinuation != nil }
        await fixture.uploader.acknowledge(upload)
        XCTAssertTrue(try HealthDeviceStore(directory: fixture.directory).state.uploads.isEmpty)
        fixture.client.releaseManifest()
        await restoring.value
        try await eventually { fixture.client.results.count == 1 }
        XCTAssertTrue(fixture.uploader.uploads.isEmpty)
        XCTAssertTrue(fixture.reader.days.isEmpty)
    }

    @MainActor
    func testColdCompletionRejectsUncommittedNonHeadWrongSequenceAnd409() async throws {
        for invalid in ["uncommitted", "cleanup", "nonHead", "sequence", "boolean", "stale"] {
            let fixture = try coordinatorFixture(savedIdentity: true)
            fixture.coordinator.accountIDProvider = { nil }
            let upload = try pendingUpload(in: fixture.directory)
            let persisted = try HealthDeviceStore(directory: fixture.directory)
            if invalid == "uncommitted" { try persisted.update { $0.committedDeviceID = nil } }
            if invalid == "cleanup" { try persisted.update { $0.cleanupPending = true } }
            let id = invalid == "nonHead" ? UUID().uuidString : upload.id
            let result: Result<[String: Any], Error>
            if invalid == "stale" { result = .failure(HealthSyncError.http(409, "stale_seq", nil)) }
            else {
                let accepted: NSNumber = invalid == "boolean" ? NSNumber(value: true)
                    : NSNumber(value: invalid == "sequence" ? 2 : 1)
                result = .success(["accepted_seq": accepted])
            }
            await fixture.uploader.onComplete?(id, result)
            let state = try HealthDeviceStore(directory: fixture.directory).state
            XCTAssertEqual(state.uploads, [upload.id], invalid)
            XCTAssertEqual(state.completed["request-1"]?.uploadID, upload.id, invalid)
            XCTAssertTrue(fixture.client.results.isEmpty, invalid)
            XCTAssertTrue(fixture.reader.days.isEmpty, invalid)
        }
    }

    @MainActor
    func testColdCompletionWithUnavailableStorageKeepsTheOutbox() async throws {
        for blockRead in [true, false] {
            let fixture = try coordinatorFixture(savedIdentity: true)
            fixture.coordinator.accountIDProvider = { nil }
            let upload = try pendingUpload(in: fixture.directory)
            let blocked = blockRead ? upload.file : fixture.directory
            try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: blocked.path)
            await fixture.uploader.acknowledge(upload)
            try FileManager.default.setAttributes([.posixPermissions: blockRead ? 0o600 : 0o700], ofItemAtPath: blocked.path)
            let state = try HealthDeviceStore(directory: fixture.directory).state
            XCTAssertEqual(state.uploads, [upload.id])
            XCTAssertEqual(state.completed["request-1"]?.uploadID, upload.id)
            XCTAssertTrue(FileManager.default.fileExists(atPath: upload.file.path))
            XCTAssertTrue(fixture.client.calls.isEmpty)
        }
    }

    @MainActor
    func testRealUploadDelegateFinishesSystemEventsAfterLocalAckWithoutWaitingForNetwork() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        let upload = try pendingUpload(in: fixture.directory)
        let uploader = HealthBackgroundUploader()
        let coordinator = HealthSyncCoordinator(credentials: fixture.credentials, reader: fixture.reader,
            client: fixture.client, uploader: uploader, directory: fixture.directory)
        coordinator.accountIDProvider = { "account-1" }
        coordinator.websiteDataStoreIDProvider = fixture.coordinator.websiteDataStoreIDProvider
        fixture.client.holdManifest = true
        fixture.client.holdResults = true
        let restoring = Task { await coordinator.resume(foreground: false) }
        try await eventually { fixture.client.manifestContinuation != nil }
        let server = try LocalHTTPServer { _ in .json(#"{"accepted_seq":1}"#) }
        let url = try await server.start()
        addTeardownBlock { server.stop() }
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        var task: URLSessionDataTask!
        let data: Data = try await withCheckedThrowingContinuation { continuation in
            task = session.dataTask(with: url) { data, _, error in
                if let error { continuation.resume(throwing: error) }
                else { continuation.resume(returning: data ?? Data()) }
            }
            task.taskDescription = upload.id
            task.resume()
        }
        var systemFinished = false
        uploader.systemCompletion = {
            XCTAssertTrue((try? HealthDeviceStore(directory: fixture.directory).state.uploads.isEmpty) == true)
            systemFinished = true
        }
        uploader.urlSession(session, dataTask: task, didReceive: data)
        uploader.urlSession(session, task: task, didCompleteWithError: nil)
        uploader.urlSessionDidFinishEvents(forBackgroundURLSession: session)
        // The system barrier completes while the manifest and result delivery are both held.
        try await eventually { systemFinished }
        XCTAssertNotNil(fixture.client.manifestContinuation)
        XCTAssertNil(fixture.client.resultContinuation)
        XCTAssertTrue(fixture.client.results.isEmpty)
        XCTAssertTrue(fixture.uploader.uploads.isEmpty)
        // Releasing the manifest lets the same convergence pass deliver the result once.
        fixture.client.releaseManifest()
        try await eventually { fixture.client.resultContinuation != nil }
        fixture.client.releaseResult()
        await restoring.value
        try await eventually { fixture.client.results.count == 1 }
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(fixture.client.results.count, 1)
        await coordinator.logout()
    }

    @MainActor
    func testColdAuthenticationRestoresOnceAndCoalescesEventsDuringRestore() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        var accountID: String?
        fixture.coordinator.accountIDProvider = { accountID }
        _ = try pendingUpload(in: fixture.directory)
        await fixture.coordinator.resume(foreground: false)
        XCTAssertEqual(fixture.reader.starts, 0)
        fixture.client.holdManifest = true
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        // The synchronous event consumer runs before snapshot publication.
        accountID = "account-1"
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        try await eventually { fixture.client.manifestContinuation != nil }
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        fixture.client.holdManifest = false
        fixture.client.releaseManifest()
        try await eventually { fixture.uploader.uploads.count == 1 }
        XCTAssertEqual(fixture.reader.starts, 1)
        XCTAssertEqual(fixture.client.calls.filter { $0.path.hasSuffix("/manifest") }.count, 1)
    }

    @MainActor
    func testColdAuthenticationRetainsOnePassBehindActiveRestoreAndLogoutRetiresIt() async throws {
        for signOut in [false, true] {
            let fixture = try coordinatorFixture(savedIdentity: true)
            var accountID: String? = "account-1"
            fixture.coordinator.accountIDProvider = { accountID }
            fixture.client.holdManifest = true
            let restoring = Task { await fixture.coordinator.resume(foreground: false) }
            try await eventually { fixture.client.manifestContinuation != nil }
            accountID = nil
            fixture.coordinator.accountEvent(.authorizationUnavailable)
            accountID = "account-1"
            fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
            if signOut {
                accountID = nil
                await fixture.coordinator.logout()
            }
            fixture.client.holdManifest = false
            fixture.client.releaseManifest()
            await restoring.value
            if signOut {
                XCTAssertNil(try fixture.credentials.load())
                XCTAssertNil(fixture.reader.observer)
            } else {
                try await eventually { fixture.reader.starts == 2 }
                XCTAssertNotNil(fixture.reader.observer)
            }
        }
    }

    @MainActor
    func testAuthenticationWaitsForActiveConsentAndCannotReviveCleanup() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        let connecting = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        await Task.yield()
        XCTAssertNotNil(fixture.coordinator.consent)
        XCTAssertEqual(fixture.reader.starts, 0)
        fixture.coordinator.answerConsent(false)
        let declined = await connecting.value
        XCTAssertEqual(declined, "declined")
        try await eventually { fixture.reader.starts == 1 }
        fixture.reader.disableSucceeds = false
        await fixture.coordinator.logout()
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        await Task.yield()
        XCTAssertTrue(try HealthDeviceStore(directory: fixture.directory).state.cleanupPending)
        XCTAssertEqual(fixture.reader.starts, 1)
        XCTAssertNil(fixture.credentials.identity)
        fixture.reader.disableSucceeds = true
        await fixture.coordinator.resume(foreground: false)
        XCTAssertNil(fixture.reader.observer)
        XCTAssertEqual(fixture.reader.starts, 1)
    }

    @MainActor
    func testStaleHTTPResponseNeverSynthesizesAnAckOrSuccessfulResult() async throws {
        let fixture = try coordinatorFixture()
        let connecting = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        _ = await connecting.value
        try await eventually { fixture.uploader.uploads.count == 1 }
        let upload = fixture.uploader.uploads[0]
        let response = try XCTUnwrap(HTTPURLResponse(url: URL(string: fixture.origin.serialized)!,
            statusCode: 409, httpVersion: nil, headerFields: nil))
        do {
            _ = try HealthDeviceClient.response(data: Data(#"{"accepted_seq":1,"error":{"code":"stale_seq"}}"#.utf8),
                response: response)
            XCTFail("409 cannot be an ACK even if its body contains a matching sequence")
        } catch {
            await fixture.uploader.onComplete?(upload.id, .failure(error))
        }
        try await eventually { (try? HealthDeviceStore(directory: fixture.directory).state.uploads.isEmpty) == true }
        // The retired payload's failed result is delivered by the same continuation.
        try await eventually { fixture.client.results.count == 1 }
        await fixture.coordinator.resume(foreground: false)
        XCTAssertEqual(fixture.client.results.count, 1)
        XCTAssertEqual(fixture.client.results[0]["outcome"] as? String, "failed")
        XCTAssertFalse(fixture.client.results.contains { $0["outcome"] as? String == "succeeded" })
    }

    // MARK: Network continuation, cleanup and foreground recovery

    @MainActor
    func testTransientResultFailureRetriesOnceForTheResultAndTheNextPayload() async throws {
        let (fixture, second) = try await acknowledgedFirstOfTwo(
            resultError: HealthSyncError.http(503, "unavailable", 2), foreground: true)
        // Retry-After also holds the next payload; nothing is sent early.
        XCTAssertEqual(fixture.uploader.uploads.count, 1)
        try await eventually(timeout: .seconds(10)) {
            fixture.client.results.count == 2 && fixture.uploader.uploads.count == 2
        }
        XCTAssertEqual(fixture.uploader.uploads[1].id, second)
        XCTAssertEqual(fixture.client.results[1]["outcome"] as? String, "succeeded")
        try await Task.sleep(for: .seconds(1))
        XCTAssertEqual(fixture.client.results.count, 2)
        XCTAssertEqual(fixture.uploader.uploads.count, 2)
        fixture.reader.releaseQuery()
    }

    @MainActor
    func testTransientResultFailureWithAnEmptyOutboxStillRetriesTheResult() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        fixture.client.claims = []
        fixture.reader.holdQueries = true
        _ = try pendingUpload(in: fixture.directory)
        fixture.client.resultErrors = [HealthSyncError.http(429, "rate_limited", 1)]
        _ = Task { await fixture.coordinator.resume(foreground: true) }
        try await eventually { fixture.uploader.uploads.count == 1 }
        await fixture.uploader.acknowledge(fixture.uploader.uploads[0])
        try await eventually { fixture.client.results.count == 1 }
        XCTAssertTrue(try HealthDeviceStore(directory: fixture.directory).state.uploads.isEmpty)
        try await eventually(timeout: .seconds(10)) { fixture.client.results.count == 2 }
        XCTAssertEqual(fixture.client.results[1]["outcome"] as? String, "succeeded")
        XCTAssertEqual(fixture.uploader.uploads.count, 1)
        fixture.reader.releaseQuery()
    }

    // MARK: The account's instance list (synthetic data only)

    @MainActor
    func testAListedOriginOrAnUnknownListKeepsTheConnection() async throws {
        let origin = WebOrigin.parseConfigured("https://health-fixture.invalid")!.serialized
        // Listed at its origin; a list avibe.bot has not confirmed yet.
        let lists: [[String: String]?] = [["instance-1": origin, "instance-2": "https://other.invalid"], nil]
        for list in lists {
            let fixture = try coordinatorFixture(savedIdentity: true)
            fixture.coordinator.instancesProvider = { list }
            await fixture.coordinator.resume(foreground: false)
            XCTAssertNotNil(fixture.credentials.identity)
            XCTAssertNotNil(fixture.reader.observer)
            XCTAssertTrue(fixture.client.calls.contains { $0.path.hasSuffix("/manifest") })
            XCTAssertTrue(fixture.client.calls.allSatisfy { $0.origin == origin })
        }
    }

    @MainActor
    func testAnOriginTheConfirmedListNoLongerHasIsDisconnectedWithoutAnyRequest() async throws {
        let origin = WebOrigin.parseConfigured("https://health-fixture.invalid")!.serialized
        // Renamed (the same instance at another origin), no longer listed,
        // and an account avibe.bot confirms has no instance at all.
        let lists: [[String: String]] = [["instance-1": "https://renamed.invalid"], ["instance-2": origin], [:]]
        for list in lists {
            let fixture = try coordinatorFixture(savedIdentity: true)
            fixture.coordinator.instancesProvider = { list }
            await fixture.coordinator.resume(foreground: true)
            XCTAssertTrue(fixture.client.calls.isEmpty)
            XCTAssertTrue(fixture.uploader.uploads.isEmpty)
            XCTAssertTrue(fixture.reader.days.isEmpty)
            XCTAssertNil(fixture.reader.observer)
            XCTAssertNil(fixture.credentials.identity)
            XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.directory.path))
            XCTAssertNil(fixture.coordinator.notice)
        }
    }

    @MainActor
    func testAHealthKitWakeAfterTheOriginLeftTheListSendsNothing() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        var list: [String: String]? = ["instance-1": fixture.origin.serialized]
        fixture.coordinator.instancesProvider = { list }
        await fixture.coordinator.resume(foreground: false)
        let observer = try XCTUnwrap(fixture.reader.observer)
        XCTAssertFalse(fixture.client.calls.isEmpty)
        let calls = fixture.client.calls.count
        let uploads = fixture.uploader.uploads.count
        let queries = fixture.reader.days.count

        list = ["instance-1": "https://renamed.invalid"]
        await observer()
        try await eventually { fixture.credentials.identity == nil }
        // The background session's own wake after it.
        await fixture.coordinator.resume(foreground: false)
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(fixture.client.calls.count, calls)
        XCTAssertEqual(fixture.uploader.uploads.count, uploads)
        XCTAssertEqual(fixture.reader.days.count, queries)
        XCTAssertFalse(fixture.client.calls.contains { $0.method == "DELETE" })
    }

    @MainActor
    func testTheForegroundPollStopsOnceTheOriginLeftTheList() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        var list: [String: String]? = ["instance-1": fixture.origin.serialized]
        fixture.coordinator.instancesProvider = { list }
        fixture.client.claims = []
        _ = Task { await fixture.coordinator.resume(foreground: true) }
        try await eventually {
            fixture.client.pendingClaims.count == 1 && fixture.uploader.uploads.count == 1
                && fixture.client.calls.contains { $0.path.hasSuffix("/manifest") }
        }
        try await Task.sleep(for: .milliseconds(200))
        let calls = fixture.client.calls.count

        list = ["instance-1": "https://renamed.invalid"]
        fixture.client.releasePending()
        try await eventually { fixture.credentials.identity == nil }
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(fixture.client.calls.count, calls)
        XCTAssertTrue(fixture.client.pendingClaims.isEmpty)
        XCTAssertFalse(fixture.client.calls.contains { $0.method == "DELETE" })
    }

    @MainActor
    func testAnAccountLeftWithNoInstanceEndsAnIdleDeviceWithoutARevoke() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        var list: [String: String]? = ["instance-1": fixture.origin.serialized]
        fixture.coordinator.instancesProvider = { list }
        await fixture.coordinator.resume(foreground: false)
        XCTAssertNotNil(fixture.credentials.identity)
        let calls = fixture.client.calls.count

        // The paired instance was deleted and was the account's only one:
        // avibe.bot answers an empty list, published after the event. Its
        // name may later belong to someone else, so nothing more goes there.
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: []))
        list = [:]
        try await eventually { fixture.credentials.identity == nil }

        fixture.coordinator.accountEvent(.signedOut(explicit: true))
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(fixture.client.calls.count, calls)
        XCTAssertFalse(fixture.client.calls.contains { $0.method == "DELETE" })
    }

    @MainActor
    func testARenameAnsweredWhileIdleEndsTheDeviceBeforeASignOutCouldRevokeIt() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        var list: [String: String]? = ["instance-1": fixture.origin.serialized]
        fixture.coordinator.instancesProvider = { list }
        await fixture.coordinator.resume(foreground: false)
        XCTAssertNotNil(fixture.credentials.identity)
        let calls = fixture.client.calls.count

        // avibe.bot answers with the instance renamed. Its list is published
        // after the event, as MobileSessionCoordinator does; the converged,
        // idle device has nothing else that would run the gate.
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        list = ["instance-1": "https://renamed.invalid"]
        try await eventually { fixture.credentials.identity == nil }

        fixture.coordinator.accountEvent(.signedOut(explicit: true))
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(fixture.client.calls.count, calls)
        XCTAssertFalse(fixture.client.calls.contains { $0.method == "DELETE" })
    }

    @MainActor
    func testReconnectingReplacesADelistedDeviceWithoutRevokingItAtItsOldOrigin() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        let saved = try XCTUnwrap(fixture.credentials.identity)
        fixture.credentials.identity = HealthDeviceIdentity(
            installationID: saved.installationID, accountID: saved.accountID, instanceID: saved.instanceID,
            instanceName: saved.instanceName, origin: "https://old-name.invalid", deviceID: saved.deviceID,
            credential: saved.credential, websiteDataStoreID: saved.websiteDataStoreID
        )
        fixture.coordinator.instancesProvider = { ["instance-1": fixture.origin.serialized] }
        let reconnecting = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        let reply = await reconnecting.value
        XCTAssertEqual(reply, "connected")
        XCTAssertFalse(fixture.client.calls.isEmpty)
        XCTAssertTrue(fixture.client.calls.allSatisfy { $0.origin == fixture.origin.serialized })
        XCTAssertFalse(fixture.client.calls.contains { $0.method == "DELETE" })
    }

    // MARK: The binding ticket for the Devices page (synthetic data only)

    /// A connected fixture in front: its poll is held, its first payload acknowledged.
    @MainActor
    private func connectedInFront(now: @escaping () -> Date = Date.init) async throws -> HealthCoordinatorFixture {
        let directory = try temporaryDirectory()
        let fixture = HealthCoordinatorFixture(directory: directory, savedIdentity: true, now: now)
        addTeardownBlock { @MainActor in
            await fixture.coordinator.logout()
            fixture.client.releasePending()
        }
        fixture.client.claims = []
        fixture.uploader.autoAcknowledge = true
        _ = Task { await fixture.coordinator.resume(foreground: true) }
        try await eventually { fixture.client.pendingClaims.count == 1 && fixture.uploader.uploads.count == 1 }
        return fixture
    }

    @MainActor
    private func ticket(_ fixture: HealthCoordinatorFixture, account: String = "account-1",
                        instance: String = "instance-1", origin: String? = nil) async -> String? {
        await fixture.coordinator.bindingTicket(
            accountID: account, instanceID: instance, origin: origin ?? fixture.origin.serialized
        )
    }

    @MainActor
    func testABindingTicketComesOnlyFromTheConnectedPairingInFront() async throws {
        // In the background: none, and nothing asked.
        let background = try coordinatorFixture(savedIdentity: true)
        await background.coordinator.resume(foreground: false)
        let backgroundTicket = await ticket(background)
        XCTAssertNil(backgroundTicket)
        XCTAssertTrue(background.client.ticketCalls.isEmpty)

        // Not connected: none.
        let unpaired = try coordinatorFixture()
        let unpairedTicket = await ticket(unpaired)
        XCTAssertNil(unpairedTicket)
        XCTAssertTrue(unpaired.client.ticketCalls.isEmpty)

        let fixture = try await connectedInFront()
        // Another account, instance or origin than the pairing's: none.
        let otherAccount = await ticket(fixture, account: "account-2")
        let otherInstance = await ticket(fixture, instance: "instance-2")
        let otherOrigin = await ticket(fixture, origin: "https://other.invalid")
        XCTAssertNil(otherAccount)
        XCTAssertNil(otherInstance)
        XCTAssertNil(otherOrigin)
        XCTAssertTrue(fixture.client.ticketCalls.isEmpty)

        let issued = await ticket(fixture)
        XCTAssertEqual(issued, HealthFakeClient.ticket)
        let call = try XCTUnwrap(fixture.client.ticketCalls.first)
        XCTAssertEqual(call.method, "POST")
        XCTAssertEqual(call.path, "/api/mobile/device/v1/binding-ticket")
        XCTAssertEqual(call.origin, fixture.origin.serialized)
        XCTAssertEqual(call.credential, "memory-only")
        XCTAssertEqual(call.body?.isEmpty, true)
        // At most one every 30 seconds.
        let again = await ticket(fixture)
        XCTAssertNil(again)
        XCTAssertEqual(fixture.client.ticketCalls.count, 1)
    }

    @MainActor
    func testNoTicketIsAskedForAPausedDelistedOrOtherAccountsPairing() async throws {
        let changes: [(HealthCoordinatorFixture) -> Void] = [
            // A second factor is owed: authority paused.
            { $0.coordinator.accountEvent(.authorizationUnavailable) },
            // The account's list has the instance at another origin.
            { $0.coordinator.instancesProvider = { ["instance-1": "https://renamed.invalid"] } },
            // Another account is current.
            { $0.coordinator.accountIDProvider = { "account-2" } },
        ]
        for change in changes {
            let fixture = try await connectedInFront()
            change(fixture)
            let none = await ticket(fixture)
            XCTAssertNil(none)
            XCTAssertTrue(fixture.client.ticketCalls.isEmpty)
        }
    }

    @MainActor
    func testNoTicketAnswerChangesTheHealthConnection() async throws {
        let failures: [Error] = [
            HealthSyncError.http(401, "device_unauthorized", nil),
            // An older tunneled instance's cookie gate: no code.
            HealthSyncError.http(401, "internal", nil),
            HealthSyncError.http(403, "device_revoked", nil),
            HealthSyncError.http(403, "instance_not_eligible", nil),
            HealthSyncError.http(404, "internal", nil),
            HealthSyncError.http(405, "internal", nil),
            HealthSyncError.http(413, "invalid_payload", nil),
            HealthSyncError.http(422, "invalid_payload", nil),
            HealthSyncError.http(426, "contract_unsupported", nil),
            HealthSyncError.http(429, "binding_ticket_rate_limited", 30),
            HealthSyncError.http(503, "private_storage_unavailable", 30),
            NSError(domain: NSURLErrorDomain, code: NSURLErrorNotConnectedToInternet),
        ]
        let malformed: [[String: Any]] = [
            [:],
            ["health_binding_ticket": "short", "expires_at": "2099-01-01T00:00:00Z"],
            HealthFakeClient.issuedTicket(expires: Date().addingTimeInterval(-1)),
        ]
        let answers = failures.map { Result<[String: Any], Error>.failure($0) }
            + malformed.map { Result<[String: Any], Error>.success($0) }
        for answer in answers {
            let fixture = try await connectedInFront()
            fixture.client.ticketAnswers = [answer]
            let none = await ticket(fixture)
            XCTAssertNil(none)
            XCTAssertEqual(fixture.client.ticketCalls.count, 1)
            XCTAssertNotNil(fixture.credentials.identity)
            XCTAssertFalse(try HealthDeviceStore(directory: fixture.directory).state.suspended)
            XCTAssertNil(fixture.coordinator.notice)
            // Sync is neither stopped nor delayed: a HealthKit delivery still uploads at once.
            let uploads = fixture.uploader.uploads.count
            await fixture.reader.observer?()
            try await eventually { fixture.uploader.uploads.count == uploads + 1 }
        }
    }

    @MainActor
    func testAnOlderInstanceIsNotAskedAgainWhileOtherFailuresAreAfterTheirWait() async throws {
        var clock = Date()
        // 404, and an older tunneled instance's code-less 401: not asked again in this process.
        for missing in [HealthSyncError.http(404, "internal", nil), HealthSyncError.http(401, "internal", nil)] {
            let older = try await connectedInFront(now: { clock })
            older.client.ticketAnswers = [.failure(missing)]
            _ = await ticket(older)
            clock = clock.addingTimeInterval(31)
            let later = await ticket(older)
            XCTAssertNil(later)
            XCTAssertEqual(older.client.ticketCalls.count, 1)
        }

        // 503: asked again after 30 seconds.
        let busy = try await connectedInFront(now: { clock })
        busy.client.ticketAnswers = [.failure(HealthSyncError.http(503, "private_storage_unavailable", nil))]
        _ = await ticket(busy)
        clock = clock.addingTimeInterval(31)
        let afterBusy = await ticket(busy)
        XCTAssertEqual(afterBusy, HealthFakeClient.ticket)

        // 429: not before its Retry-After.
        let limited = try await connectedInFront(now: { clock })
        limited.client.ticketAnswers = [.failure(HealthSyncError.http(429, "binding_ticket_rate_limited", 60))]
        _ = await ticket(limited)
        clock = clock.addingTimeInterval(31)
        let tooSoon = await ticket(limited)
        XCTAssertNil(tooSoon)
        XCTAssertEqual(limited.client.ticketCalls.count, 1)
        clock = clock.addingTimeInterval(30)
        let afterLimit = await ticket(limited)
        XCTAssertEqual(afterLimit, HealthFakeClient.ticket)
    }

    @MainActor
    func testATicketIsDroppedWhenItIsLateOrTheConnectionChangedOnTheWay() async throws {
        // Over the budget: the request is cancelled.
        let slow = try await connectedInFront()
        slow.coordinator.bindingTicketBudget = .milliseconds(100)
        slow.client.holdTicket = true
        let late = await ticket(slow)
        XCTAssertNil(late)
        XCTAssertNil(slow.client.ticketContinuation)
        XCTAssertNotNil(slow.credentials.identity)

        // Sent to the background, signed out, or the store changed while it was on its way.
        var binding = UUID(uuidString: "10000000-0000-0000-0000-000000000002")
        let changes: [(HealthCoordinatorFixture) -> Void] = [
            { $0.coordinator.setBackground() },
            { $0.coordinator.accountEvent(.signedOut(explicit: true)) },
            { _ in binding = UUID() },
            // The account's list now has the instance at another origin.
            { $0.coordinator.instancesProvider = { ["instance-1": "https://renamed.invalid"] } },
        ]
        for change in changes {
            let fixture = try await connectedInFront()
            binding = UUID(uuidString: "10000000-0000-0000-0000-000000000002")
            fixture.coordinator.websiteDataStoreIDProvider = { binding }
            fixture.client.holdTicket = true
            let pending = Task { await self.ticket(fixture) }
            try await eventually { fixture.client.ticketContinuation != nil }
            change(fixture)
            fixture.client.releaseTicket()
            let dropped = await pending.value
            XCTAssertNil(dropped)
        }
    }

    func testTheIssueAnswerMustCarryAWellFormedUnexpiredTicket() throws {
        let now = try XCTUnwrap(ISO8601DateFormatter().date(from: "2027-01-15T08:00:00Z"))
        let valid = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJ0123-_6"
        let later = "2027-01-15T08:02:00Z"
        XCTAssertEqual(valid.count, 43)
        XCTAssertEqual(HealthSyncCoordinator.bindingTicket(
            from: ["health_binding_ticket": valid, "expires_at": later], now: now), valid)
        XCTAssertEqual(HealthSyncCoordinator.bindingTicket(
            from: ["health_binding_ticket": valid, "expires_at": "2027-01-15T08:01:00.123Z"], now: now), valid)
        let broken: [[String: Any]] = [
            ["health_binding_ticket": String(valid.dropLast()), "expires_at": later],
            ["health_binding_ticket": valid + "A", "expires_at": later],
            ["health_binding_ticket": String(valid.dropLast()) + "+", "expires_at": later],
            ["health_binding_ticket": String(valid.dropLast()) + "=", "expires_at": later],
            ["health_binding_ticket": String(valid.dropLast()) + "é", "expires_at": later],
            ["health_binding_ticket": 7, "expires_at": later],
            ["health_binding_ticket": valid],
            ["health_binding_ticket": valid, "expires_at": "soon"],
            ["health_binding_ticket": valid, "expires_at": "2027-01-15T08:00:00Z"],
            ["health_binding_ticket": valid, "expires_at": "2027-01-15T07:59:59Z"],
            ["expires_at": later],
        ]
        for answer in broken {
            XCTAssertNil(HealthSyncCoordinator.bindingTicket(from: answer, now: now))
        }
    }

    @MainActor
    func testRevokedSuspendedPausedOrBackgroundContinuationsSendNothingFurther() async throws {
        let (revoked, _) = try await acknowledgedFirstOfTwo(
            resultError: HealthSyncError.http(401, "device_unauthorized", nil), foreground: true)
        let (suspended, _) = try await acknowledgedFirstOfTwo(
            resultError: HealthSyncError.http(426, "contract_unsupported", nil), foreground: true)
        let (paused, pausedSecond) = try await acknowledgedFirstOfTwo(
            resultError: HealthSyncError.http(503, "unavailable", 1), foreground: true)
        paused.coordinator.accountEvent(.authorizationUnavailable)
        let (background, _) = try await acknowledgedFirstOfTwo(
            resultError: HealthSyncError.http(503, "unavailable", 1), foreground: false)
        try await eventually { revoked.credentials.identity == nil }
        // Longer than the five-second minimum retry delay.
        try await Task.sleep(for: .milliseconds(6500))
        for fixture in [revoked, suspended, paused, background] {
            XCTAssertEqual(fixture.client.results.count, 1)
            XCTAssertEqual(fixture.uploader.uploads.count, 1)
        }
        XCTAssertFalse(revoked.client.calls.contains { $0.method == "DELETE" })
        XCTAssertTrue(try HealthDeviceStore(directory: suspended.directory).state.suspended)
        XCTAssertEqual(suspended.coordinator.notice, "health.update_required")

        // Reauthentication restores the paused connection once; it then sends the
        // result and the next payload.
        let manifests = paused.client.calls.filter { $0.path.hasSuffix("/manifest") }.count
        paused.reader.releaseQuery()
        paused.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        try await eventually { paused.client.results.count == 2 && paused.uploader.uploads.count == 2 }
        XCTAssertEqual(paused.uploader.uploads[1].id, pausedSecond)
        XCTAssertEqual(paused.client.calls.filter { $0.path.hasSuffix("/manifest") }.count, manifests + 1)
        for fixture in [revoked, suspended, paused] { fixture.reader.releaseQuery() }
    }

    @MainActor
    func testExplicitCleanupRemovesUndecodableStorageAndAllowsReconnection() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        let payload = fixture.directory.appendingPathComponent(UUID().uuidString + ".json")
        try Data(#"{"device_seq":1}"#.utf8).write(to: payload)
        try Data("{broken".utf8).write(to: fixture.directory.appendingPathComponent("state.json"))
        await fixture.coordinator.resume(foreground: false)
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.directory.path))
        XCTAssertNil(fixture.credentials.identity)
        XCTAssertTrue(fixture.client.calls.contains { $0.method == "DELETE" && $0.credential == "memory-only" })
        XCTAssertNil(fixture.coordinator.notice)
        let reconnecting = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        let reply = await reconnecting.value
        XCTAssertEqual(reply, "connected")
        XCTAssertEqual(try HealthDeviceStore(directory: fixture.directory).state.committedDeviceID, "device-1")
    }

    @MainActor
    func testExplicitCleanupOfUnreadableStorageCompletesOnlyOnceTheDirectoryIsGone() async throws {
        // Removable: cleanup completes without decoding the state.
        let removable = try coordinatorFixture(savedIdentity: true)
        try FileManager.default.setAttributes([.posixPermissions: 0o000],
            ofItemAtPath: removable.directory.appendingPathComponent("state.json").path)
        await removable.coordinator.logout()
        XCTAssertFalse(FileManager.default.fileExists(atPath: removable.directory.path))
        XCTAssertNil(removable.credentials.identity)
        XCTAssertNil(removable.coordinator.notice)

        // Not removable: cleanup stays pending, and the next resume retries it.
        let parent = try temporaryDirectory()
        let directory = parent.appendingPathComponent("HealthSync", isDirectory: true)
        let blocked = try coordinatorFixture(directory: directory, savedIdentity: true)
        try FileManager.default.setAttributes([.posixPermissions: 0o000],
            ofItemAtPath: directory.appendingPathComponent("state.json").path)
        try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: parent.path)
        addTeardownBlock { try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: parent.path) }
        await blocked.coordinator.logout()
        XCTAssertTrue(FileManager.default.fileExists(atPath: directory.path))
        XCTAssertEqual(blocked.coordinator.notice, "health.cleanup_failed")
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: parent.path)
        await blocked.coordinator.resume(foreground: false)
        XCTAssertFalse(FileManager.default.fileExists(atPath: directory.path))
    }

    @MainActor
    func testCleanupOfAnAbsentDirectoryCompletes() async throws {
        let parent = try temporaryDirectory()
        let directory = parent.appendingPathComponent("HealthSync", isDirectory: true)
        let fixture = try coordinatorFixture(directory: directory)
        // The store cannot be created here, so cleanup must confirm absence itself.
        try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: parent.path)
        addTeardownBlock { try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: parent.path) }
        await fixture.coordinator.logout()
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: parent.path)
        XCTAssertFalse(FileManager.default.fileExists(atPath: directory.path))
        XCTAssertNil(fixture.coordinator.notice)
    }

    @MainActor
    func testUnreadableStorageWithoutCleanupIntentIsRetained() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        let payload = fixture.directory.appendingPathComponent(UUID().uuidString + ".json")
        try Data(#"{"device_seq":1}"#.utf8).write(to: payload)
        let state = fixture.directory.appendingPathComponent("state.json")
        try FileManager.default.setAttributes([.posixPermissions: 0o000], ofItemAtPath: state.path)
        await fixture.coordinator.resume(foreground: false)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: state.path)
        XCTAssertTrue(FileManager.default.fileExists(atPath: payload.path))
        XCTAssertNotNil(fixture.credentials.identity)
        XCTAssertFalse(fixture.client.calls.contains { $0.method == "DELETE" })
        XCTAssertEqual(try HealthDeviceStore(directory: fixture.directory).state.committedDeviceID, "device-1")
    }

    @MainActor
    func testALaterResumeKeepsPollingOnlyWhenItCarriesTheRealForegroundState() async throws {
        func claims(_ fixture: HealthCoordinatorFixture) -> Int {
            fixture.client.calls.filter { $0.path.hasSuffix("/requests/claim") }.count
        }
        // A later launch or background-session resume while restoration runs.
        for laterForeground in [true, false] {
            let fixture = try coordinatorFixture(savedIdentity: true)
            fixture.client.claims = []
            fixture.reader.holdQueries = true
            _ = Task { await fixture.coordinator.resume(foreground: true) }
            try await eventually { fixture.client.pendingClaims.count == 1 }
            await fixture.coordinator.resume(foreground: laterForeground)
            let before = claims(fixture)
            fixture.client.releasePending()
            if laterForeground {
                try await eventually { claims(fixture) == before + 1 }
            } else {
                try await Task.sleep(for: .milliseconds(300))
                XCTAssertEqual(claims(fixture), before)
            }
            fixture.reader.releaseQuery()
        }
        // A background launch does not poll.
        let background = try coordinatorFixture(savedIdentity: true)
        background.client.claims = []
        await background.coordinator.resume(foreground: false)
        XCTAssertEqual(claims(background), 0)
    }

    @MainActor
    func testAHeldManifestHoldsTheOutboxOfItsPass() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        _ = try pendingUpload(in: fixture.directory)
        fixture.client.holdManifest = true
        let restoring = Task { await fixture.coordinator.resume(foreground: false) }
        try await eventually { fixture.client.manifestContinuation != nil }
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertTrue(fixture.uploader.uploads.isEmpty)
        fixture.client.holdManifest = false
        fixture.client.releaseManifest()
        await restoring.value
        XCTAssertEqual(fixture.uploader.uploads.count, 1)
    }

    @MainActor
    func testATransientManifestFailureStillDeliversResultsAndRetriesOnceAtTheEndOfThePass() async throws {
        func manifests(_ fixture: HealthCoordinatorFixture) -> Int {
            fixture.client.calls.filter { $0.path.hasSuffix("/manifest") }.count
        }
        let fixture = try coordinatorFixture(savedIdentity: true)
        fixture.client.claims = []
        fixture.reader.holdQueries = true
        try HealthDeviceStore(directory: fixture.directory).update {
            var result = self.pendingResult()
            result.ready = true
            $0.completed[result.id] = result
        }
        // 500 sets no Retry-After, so later device requests in the pass are allowed.
        fixture.client.manifestErrors = [HealthSyncError.http(500, "internal", nil)]
        _ = Task { await fixture.coordinator.resume(foreground: true) }
        try await eventually { fixture.client.results.count == 1 }
        XCTAssertEqual(manifests(fixture), 1)
        // One retry, at the 30-second default delay, resends only the manifest.
        try await eventually(timeout: .seconds(40)) { manifests(fixture) == 2 }
        try await Task.sleep(for: .seconds(1))
        XCTAssertEqual(manifests(fixture), 2)
        XCTAssertEqual(fixture.client.results.count, 1)
        fixture.reader.releaseQuery()
    }

    // MARK: Foreground liveness: one convergence owner

    @MainActor
    func testForegroundRestorationPollsDespiteTheManifestAndRetriesAFailedManifest() async throws {
        func manifests(_ fixture: HealthCoordinatorFixture) -> Int {
            fixture.client.calls.filter { $0.path.hasSuffix("/manifest") }.count
        }
        // A held manifest does not keep claims from starting.
        let held = try coordinatorFixture(savedIdentity: true)
        held.client.claims = []
        held.reader.holdQueries = true
        held.client.holdManifest = true
        _ = Task { await held.coordinator.resume(foreground: true) }
        try await eventually { held.client.manifestContinuation != nil && held.client.pendingClaims.count == 1 }
        held.client.holdManifest = false
        held.client.releaseManifest()

        // A transient manifest failure: claims start after Retry-After and one retry resends it.
        let failing = try coordinatorFixture(savedIdentity: true)
        failing.client.claims = []
        failing.reader.holdQueries = true
        failing.client.manifestErrors = [HealthSyncError.http(503, "unavailable", 1)]
        _ = Task { await failing.coordinator.resume(foreground: true) }
        try await eventually(timeout: .seconds(10)) {
            failing.client.pendingClaims.count == 1 && manifests(failing) == 2
        }
        try await Task.sleep(for: .seconds(1))
        XCTAssertEqual(manifests(failing), 2)
        held.reader.releaseQuery()
        failing.reader.releaseQuery()
    }

    @MainActor
    func testConnectionPollsAfterItsFirstSyncFails() async throws {
        let fixture = try coordinatorFixture()
        // Foreground without a saved device.
        await fixture.coordinator.resume(foreground: true)
        fixture.reader.queryErrors = [NSError(domain: "HealthFixture", code: 1)]
        let connecting = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        let reply = await connecting.value
        XCTAssertEqual(reply, "connected")
        try await eventually { fixture.client.pendingClaims.count == 1 }
        try await eventually {
            (try? HealthDeviceStore(directory: fixture.directory).state.completed["request-1"]?.ready) == true
        }
        XCTAssertEqual(fixture.reader.days, [30, 30])
    }

    @MainActor
    func testARevokedDeviceReconnectedInTheSameForegroundPollsAgain() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        fixture.client.claims = []
        fixture.client.claimErrors = [HealthSyncError.http(401, "device_unauthorized", nil)]
        fixture.reader.holdQueries = true
        _ = Task { await fixture.coordinator.resume(foreground: true) }
        try await eventually { fixture.credentials.identity == nil }
        fixture.reader.holdQueries = false
        fixture.reader.releaseQuery()
        fixture.client.claims = [[claim(token: "claim-2")]]
        let reconnecting = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        let reply = await reconnecting.value
        XCTAssertEqual(reply, "connected")
        try await eventually { fixture.client.pendingClaims.count == 1 }
    }

    @MainActor
    func testATransientUploadFailureWithoutRetryAfterDoesNotRetransmitImmediately() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        fixture.client.claims = []
        fixture.reader.holdQueries = true
        let upload = try pendingUpload(in: fixture.directory)
        _ = Task { await fixture.coordinator.resume(foreground: true) }
        try await eventually { fixture.uploader.uploads.count == 1 }
        await fixture.uploader.onComplete?(upload.id, .failure(HealthSyncError.http(500, "internal", nil)))
        try await Task.sleep(for: .seconds(5))
        XCTAssertEqual(fixture.uploader.uploads.count, 1)
        XCTAssertEqual(try HealthDeviceStore(directory: fixture.directory).state.uploads, [upload.id])
        fixture.reader.releaseQuery()
    }

    @MainActor
    func testColdRestorationWithAnEarlyAckAndObserverSendsTheManifestOnce() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        let upload = try pendingUpload(in: fixture.directory)
        fixture.client.holdManifest = true
        let restoring = Task { await fixture.coordinator.resume(foreground: false) }
        try await eventually { fixture.client.manifestContinuation != nil }
        await fixture.uploader.acknowledge(upload)
        await fixture.reader.observer?()
        fixture.client.holdManifest = false
        fixture.client.releaseManifest()
        await restoring.value
        try await eventually { fixture.client.results.count == 1 }
        XCTAssertEqual(fixture.client.calls.filter { $0.path.hasSuffix("/manifest") }.count, 1)
    }

    @MainActor
    func testPausingDuringAHeldManifestLetsTheNextEpochConvergeAfterReauthentication() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        var accountID: String? = "account-1"
        fixture.coordinator.accountIDProvider = { accountID }
        fixture.client.claims = []
        fixture.reader.holdQueries = true
        fixture.client.holdManifest = true
        _ = Task { await fixture.coordinator.resume(foreground: true) }
        try await eventually { fixture.client.manifestContinuation != nil }
        accountID = nil
        fixture.coordinator.accountEvent(.authorizationUnavailable)
        fixture.client.holdManifest = false
        accountID = "account-1"
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        try await eventually {
            fixture.client.calls.filter { $0.path.hasSuffix("/manifest") }.count == 2
                && fixture.client.calls.filter { $0.path.hasSuffix("/requests/claim") }.count >= 1
                && fixture.reader.starts == 2
        }
        fixture.reader.releaseQuery()
    }

    @MainActor
    func testEveryForegroundEntrySendsTheManifestAgain() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        fixture.client.claims = []
        await fixture.coordinator.resume(foreground: false)
        XCTAssertEqual(fixture.client.calls.filter { $0.path.hasSuffix("/manifest") }.count, 1)
        fixture.reader.holdQueries = true
        _ = Task { await fixture.coordinator.resume(foreground: true) }
        try await eventually { fixture.client.calls.filter { $0.path.hasSuffix("/manifest") }.count == 2 }
        fixture.reader.releaseQuery()
    }

    @MainActor
    func testHealthKitDeliveryConvergesAnUnfinishedRestoration() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        fixture.client.manifestErrors = [NSError(domain: "HealthFixture", code: 2)]
        await fixture.coordinator.resume(foreground: false)
        XCTAssertEqual(fixture.client.calls.filter { $0.path.hasSuffix("/manifest") }.count, 1)
        await fixture.reader.observer?()
        try await eventually { fixture.client.calls.filter { $0.path.hasSuffix("/manifest") }.count == 2 }
    }

    @MainActor
    func testAuthenticationFinishesAStalledRestorationOnlyWhileItIsUnconverged() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        fixture.client.manifestErrors = [NSError(domain: "HealthFixture", code: 3)]
        await fixture.coordinator.resume(foreground: false)
        XCTAssertEqual(fixture.client.calls.filter { $0.path.hasSuffix("/manifest") }.count, 1)
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        try await eventually { fixture.client.calls.filter { $0.path.hasSuffix("/manifest") }.count == 2 }
        let calls = fixture.client.calls.count
        // A converged identity triggers no network.
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(fixture.client.calls.count, calls)
    }

    @MainActor
    func testEnteringTheBackgroundCancelsAPendingRetry() async throws {
        let (fixture, _) = try await acknowledgedFirstOfTwo(
            resultError: HealthSyncError.http(503, "unavailable", 1), foreground: true)
        fixture.coordinator.setBackground()
        // Longer than the five-second minimum retry delay.
        try await Task.sleep(for: .milliseconds(6500))
        XCTAssertEqual(fixture.client.results.count, 1)
        XCTAssertEqual(fixture.uploader.uploads.count, 1)
        fixture.reader.releaseQuery()
    }

    @MainActor
    func testAResumeArrivingDuringRestorationStillConverges() async throws {
        func manifests(_ fixture: HealthCoordinatorFixture) -> Int {
            fixture.client.calls.filter { $0.path.hasSuffix("/manifest") }.count
        }
        let fixture = try coordinatorFixture(savedIdentity: true)
        fixture.client.claims = []
        fixture.reader.holdQueries = true
        fixture.client.holdManifest = true
        let first = Task { await fixture.coordinator.resume(foreground: false) }
        try await eventually { fixture.client.manifestContinuation != nil }
        // A foreground entry while the background restoration still runs.
        await fixture.coordinator.resume(foreground: true)
        fixture.client.holdManifest = false
        fixture.client.releaseManifest()
        try await eventually { manifests(fixture) == 2 && fixture.client.pendingClaims.count == 1 }
        fixture.reader.releaseQuery()
        _ = first
    }

    @MainActor
    func testTheManifestFollowsBackgroundDeliveryEnabledAfterAnEarlyColdAck() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        let upload = try pendingUpload(in: fixture.directory)
        fixture.reader.holdStartObserving = true
        let restoring = Task { await fixture.coordinator.resume(foreground: false) }
        try await eventually { fixture.reader.startObservingHeld }
        // A background-session completion delivered while observers are being registered.
        await fixture.uploader.acknowledge(upload)
        fixture.reader.releaseStartObserving()
        await restoring.value
        let manifests = fixture.client.calls.filter { $0.path.hasSuffix("/manifest") }
        XCTAssertEqual(manifests.last?.body?["background_delivery"] as? Bool, true)
    }

    @MainActor
    func testATransientPollingFailureRetriesThePendingResult() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        fixture.client.claims = []
        try HealthDeviceStore(directory: fixture.directory).update {
            var result = self.pendingResult()
            result.ready = true
            $0.completed[result.id] = result
        }
        // A terminal result failure leaves it unacknowledged without scheduling a retry.
        fixture.client.resultErrors = [HealthSyncError.http(409, "conflict", nil)]
        fixture.reader.queryErrors = [NSError(domain: "HealthFixture", code: 4)]
        await fixture.coordinator.resume(foreground: true)
        try await eventually { fixture.client.results.count == 1 && fixture.client.pendingClaims.count == 1 }
        fixture.client.claimErrors = [HealthSyncError.http(503, "unavailable", 1)]
        fixture.client.releasePending()
        try await eventually(timeout: .seconds(10)) { fixture.client.results.count == 2 }
    }

    @MainActor
    func testATransientHealthKitDeliveryFailureRetriesThePendingResult() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        fixture.client.claims = []
        try HealthDeviceStore(directory: fixture.directory).update {
            var result = self.pendingResult()
            result.ready = true
            $0.completed[result.id] = result
        }
        fixture.client.resultErrors = [HealthSyncError.http(409, "conflict", nil)]
        // The activation refresh fails locally; the delivery fails transiently.
        fixture.reader.queryErrors = [
            NSError(domain: "HealthFixture", code: 5), HealthSyncError.http(503, "unavailable", 1),
        ]
        await fixture.coordinator.resume(foreground: true)
        XCTAssertEqual(fixture.client.results.count, 1)
        await fixture.reader.observer?()
        try await eventually(timeout: .seconds(10)) { fixture.client.results.count == 2 }
    }

    @MainActor
    func testASuspendedDeviceReconnectedInTheSameForegroundPollsAgain() async throws {
        let fixture = try coordinatorFixture(savedIdentity: true)
        fixture.client.claims = []
        fixture.client.manifestErrors = [HealthSyncError.http(422, "invalid_payload", nil)]
        await fixture.coordinator.resume(foreground: true)
        XCTAssertTrue(try HealthDeviceStore(directory: fixture.directory).state.suspended)
        let claimsBefore = fixture.client.pendingClaims.count
        fixture.client.claims = [[claim(token: "claim-2")]]
        let reconnecting = fixture.connect()
        try await eventually { fixture.coordinator.consent != nil }
        fixture.coordinator.answerConsent(true)
        let reply = await reconnecting.value
        XCTAssertEqual(reply, "connected")
        try await eventually { fixture.client.pendingClaims.count == claimsBefore + 1 }
    }

    /// A foreground fixture whose poll loop has claimed a refresh and is suspended in
    /// `synchronize` behind an upload that has not been acknowledged. Whichever sync
    /// (the activation refresh or the claim's) starts first owns the only upload.
    @MainActor
    private func pollLoopWaitingForAnUpload() async throws -> (HealthCoordinatorFixture, HealthFakeUploader.Upload) {
        let fixture = try coordinatorFixture(savedIdentity: true)
        fixture.client.claims = [[[
            "id": "refresh-1", "operation": "health.refresh", "claim_token": "lease-1", "metrics": ["steps"],
        ]]]
        _ = Task { await fixture.coordinator.resume(foreground: true) }
        try await eventually {
            fixture.client.calls.contains { $0.path.hasSuffix("/requests/claim") } && fixture.uploader.uploads.count == 1
        }
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(fixture.uploader.uploads.count, 1)
        return (fixture, fixture.uploader.uploads[0])
    }

    @MainActor
    func testARetiredPollLoopCannotClearTheNextLoopsHandle() async throws {
        let (fixture, waiting) = try await pollLoopWaitingForAnUpload()
        // Background retires loop A while it still waits; foreground starts loop B.
        fixture.coordinator.setBackground()
        await fixture.coordinator.resume(foreground: true)
        try await eventually { fixture.client.pendingClaims.count == 1 }
        // Loop A wakes and exits. Authentication starts a pass only if no loop is tracked.
        await fixture.uploader.acknowledge(waiting)
        try await Task.sleep(for: .milliseconds(300))
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(fixture.client.pendingClaims.count, 1)
    }

    @MainActor
    func testAPausedPollLoopCannotClearTheNextEpochsHandle() async throws {
        let (fixture, _) = try await pollLoopWaitingForAnUpload()
        var accountID: String? = "account-1"
        fixture.coordinator.accountIDProvider = { accountID }
        accountID = nil
        fixture.coordinator.accountEvent(.authorizationUnavailable)
        accountID = "account-1"
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        try await eventually { fixture.client.pendingClaims.count == 1 }
        try await Task.sleep(for: .milliseconds(300))
        fixture.coordinator.accountEvent(.authenticated(accountID: "account-1", instanceIDs: ["instance-1"]))
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(fixture.client.pendingClaims.count, 1)
    }

    /// Restores two queued payloads, acknowledges the first and lets its result
    /// delivery fail once with `resultError`.
    @MainActor
    private func acknowledgedFirstOfTwo(
        resultError: Error, foreground: Bool
    ) async throws -> (HealthCoordinatorFixture, String) {
        let fixture = try coordinatorFixture(savedIdentity: true)
        fixture.client.claims = []
        fixture.reader.holdQueries = true
        let first = try pendingUpload(in: fixture.directory)
        let store = try HealthDeviceStore(directory: fixture.directory)
        _ = try store.nextSequence()
        let second = try store.enqueue(["device_seq": 2, "items": [], "failures": []])
        fixture.client.resultErrors = [resultError]
        if foreground {
            _ = Task { await fixture.coordinator.resume(foreground: true) }
        } else {
            await fixture.coordinator.resume(foreground: false)
        }
        try await eventually { fixture.uploader.uploads.count == 1 }
        XCTAssertEqual(fixture.uploader.uploads[0].id, first.id)
        await fixture.uploader.acknowledge(fixture.uploader.uploads[0])
        try await eventually { fixture.client.results.count == 1 }
        return (fixture, second)
    }

    private func pendingUpload(in directory: URL) throws -> HealthFakeUploader.Upload {
        let store = try HealthDeviceStore(directory: directory)
        _ = try store.nextSequence()
        let id = try store.enqueue(["device_seq": 1, "items": [], "failures": []]) { state, id in
            var result = self.pendingResult()
            result.ready = true
            result.outcome = "succeeded"
            result.error = nil
            result.states = ["steps": "empty"]
            result.uploadID = id
            state.completed[result.id] = result
        }
        return HealthFakeUploader.Upload(id: id, file: store.file(id), sequence: 1)
    }

    private func temporaryDirectory() throws -> URL {
        var template = Array((NSTemporaryDirectory() + "HealthSyncTests.XXXXXX").utf8CString)
        let path = try XCTUnwrap(mkdtemp(&template))
        let directory = URL(fileURLWithPath: String(cString: path), isDirectory: true)
        addTeardownBlock {
            if FileManager.default.fileExists(atPath: directory.path) {
                try FileManager.default.removeItem(at: directory)
            }
        }
        return directory
    }

    private func object(_ data: Data) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func pendingResult() -> HealthPendingResult {
        HealthPendingResult(id: "request-1", claimToken: "claim-1", outcome: "failed",
                            states: ["steps": "internal"], error: "internal", uploadID: nil)
    }

    private func claim(token: String, metrics: [String] = ["steps"]) -> [String: Any] {
        ["id": "request-1", "operation": "health.connect", "claim_token": token,
         "created_at": "2026-11-18T00:00:00Z", "expires_at": "2026-11-19T00:00:00Z",
         "requires_user": true, "metrics": metrics]
    }

    @MainActor
    private func coordinatorFixture(
        directory: URL? = nil, savedIdentity: Bool = false
    ) throws -> HealthCoordinatorFixture {
        let fixture = HealthCoordinatorFixture(directory: try directory ?? temporaryDirectory(), savedIdentity: savedIdentity)
        addTeardownBlock { @MainActor in
            await fixture.coordinator.logout()
            fixture.client.releasePending()
        }
        return fixture
    }

    @MainActor
    private func eventually(
        timeout: Duration = .seconds(5),
        _ condition: () -> Bool, file: StaticString = #filePath, line: UInt = #line
    ) async throws {
        let deadline = ContinuousClock.now.advanced(by: timeout)
        while !condition() {
            guard ContinuousClock.now < deadline else {
                XCTFail("The health synchronization condition did not complete.", file: file, line: line)
                throw HealthSyncTestFailure.timeout
            }
            try await Task.sleep(for: .milliseconds(10))
        }
    }
}

private enum HealthSyncTestFailure: Error { case timeout }

private final class HealthHTTPRequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var requests: [LocalHTTPRequest] = []
    func append(_ request: LocalHTTPRequest) { lock.withLock { requests.append(request) } }
    func snapshot() -> [LocalHTTPRequest] { lock.withLock { requests } }
}

final class HealthMemoryCredentials: HealthCredentialStoring {
    var identity: HealthDeviceIdentity?
    private(set) var saves = 0
    var clearError: Error?
    var loadError: Error?
    func load() throws -> HealthDeviceIdentity? {
        if let loadError { throw loadError }
        return identity
    }
    func save(_ identity: HealthDeviceIdentity) throws { self.identity = identity; saves += 1 }
    func clear() throws {
        if let clearError { throw clearError }
        identity = nil
    }
    func installationID() throws -> String { "10000000-0000-0000-0000-000000000001" }
}

private final class HealthMemoryKeychainAccess: PushKeychainItemAccessing {
    private var items: [String: [String: Any]] = [:]
    var readStatus: OSStatus?
    private(set) var queries: [[String: Any]] = []
    private(set) var insertions: [[String: Any]] = []
    private(set) var updates: [[String: Any]] = []
    private(set) var deletions: [[String: Any]] = []

    private func key(_ query: [String: Any]) -> String {
        (query[kSecAttrService as String] as? String ?? "")
            + "/" + (query[kSecAttrAccount as String] as? String ?? "")
    }
    func copyMatching(_ query: [String: Any]) -> (OSStatus, Any?) {
        queries.append(query)
        if let readStatus { return (readStatus, nil) }
        guard let item = items[key(query)] else { return (errSecItemNotFound, nil) }
        return (errSecSuccess, item[kSecValueData as String])
    }
    func update(_ query: [String: Any], attributes: [String: Any]) -> OSStatus {
        queries.append(query)
        updates.append(attributes)
        let id = key(query)
        guard items[id] != nil else { return errSecItemNotFound }
        items[id]?.merge(attributes) { _, new in new }
        return errSecSuccess
    }
    func add(_ attributes: [String: Any]) -> OSStatus {
        queries.append(attributes)
        insertions.append(attributes)
        let id = key(attributes)
        guard items[id] == nil else { return errSecDuplicateItem }
        items[id] = attributes
        return errSecSuccess
    }
    func delete(_ query: [String: Any]) -> OSStatus {
        queries.append(query)
        deletions.append(query)
        return items.removeValue(forKey: key(query)) == nil ? errSecItemNotFound : errSecSuccess
    }
}

@MainActor
final class HealthFakeReader: HealthDataReading {
    private(set) var authorizations = 0
    private(set) var days: [Int] = []
    private(set) var observer: (() async -> Void)?
    var holdQueries = false
    private var queryContinuation: CheckedContinuation<Void, Never>?
    func releaseQuery() {
        let continuation = queryContinuation
        queryContinuation = nil
        continuation?.resume()
    }
    func authorize() async throws { authorizations += 1 }
    var queryErrors: [Error] = []
    func query(days: Int, now: Date) async throws -> HealthQueryWindow {
        self.days.append(days)
        if !queryErrors.isEmpty { throw queryErrors.removeFirst() }
        if holdQueries { await withCheckedContinuation { queryContinuation = $0 } }
        return HealthQueryWindow(
            computedAt: now, from: "2026-11-18", to: "2026-11-18",
            items: [], failures: [],
            metricStates: ["steps": "empty", "activity": "empty", "workouts": "empty", "sleep": "empty"]
        )
    }
    private(set) var starts = 0
    var holdStartObserving = false
    private var startContinuation: CheckedContinuation<Void, Never>?
    var startObservingHeld: Bool { startContinuation != nil }
    func releaseStartObserving() {
        holdStartObserving = false
        let continuation = startContinuation
        startContinuation = nil
        continuation?.resume()
    }
    func startObserving(onChange: @escaping () async -> Void) async -> Bool {
        starts += 1
        observer = onChange
        if holdStartObserving { await withCheckedContinuation { startContinuation = $0 } }
        return true
    }
    private(set) var disables = 0
    var disableSucceeds = true
    func disableBackgroundDelivery() async -> Bool { disables += 1; stopObserving(); return disableSucceeds }
    func stopObserving() { observer = nil }
}

final class HealthFakeUploader: HealthUploading {
    struct Upload {
        let id: String
        let file: URL
        let sequence: NSNumber
    }
    var onComplete: (@MainActor (String, Result<[String: Any], Error>) async -> Void)?
    var autoAcknowledge = false
    private(set) var uploads: [Upload] = []
    private(set) var cancellations = 0
    func reconnect() {}
    func upload(id: String, file: URL, identity: HealthDeviceIdentity) async throws {
        let payload = try JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any]
        guard let sequence = payload?["device_seq"] as? NSNumber else {
            throw HealthSyncError.code("invalid_payload")
        }
        let upload = Upload(id: id, file: file, sequence: sequence)
        uploads.append(upload)
        if autoAcknowledge {
            await acknowledge(upload)
        }
    }
    func invalidate() {}
    func cancelAll() async { cancellations += 1 }
    @MainActor
    func acknowledge(_ upload: Upload) async {
        await onComplete?(upload.id, .success(["accepted_seq": upload.sequence]))
    }
}

@MainActor
final class HealthFakeClient: HealthDeviceRequesting {
    struct Call {
        let method: String
        let path: String
        let body: [String: Any]?
        let credential: String?
        var origin = ""
    }
    private(set) var calls: [Call] = []
    var pairedDeviceID = "device-1"
    var holdResults = false
    var resultErrors: [Error] = []
    var manifestErrors: [Error] = []
    var claimErrors: [Error] = []
    var resultContinuation: CheckedContinuation<[String: Any], Error>?
    var claims: [[[String: Any]]] = [[[
        "id": "request-1", "operation": "health.connect", "claim_token": "claim-1",
        "metrics": ["steps"], "requires_user": true,
    ]]]
    var holdPair = false
    var holdManifest = false
    var manifestContinuation: CheckedContinuation<[String: Any], Error>?
    var manifestError: Error?
    private(set) var pairContinuation: CheckedContinuation<[String: Any], Error>?
    var pendingClaims: [CheckedContinuation<[String: Any], Error>] = []
    var results: [[String: Any]] {
        calls.filter { $0.path.hasSuffix("/result") }.compactMap(\.body)
    }
    func request(origin: String, credential: String?, method: String,
                 path: String, body: [String: Any]?) async throws -> [String: Any] {
        calls.append(Call(method: method, path: path, body: body, credential: credential, origin: origin))
        if path.hasSuffix("/result"), !resultErrors.isEmpty { throw resultErrors.removeFirst() }
        if path.hasSuffix("/result"), holdResults {
            return try await withCheckedThrowingContinuation { resultContinuation = $0 }
        }
        if path.hasSuffix("/manifest") {
            if !manifestErrors.isEmpty { throw manifestErrors.removeFirst() }
            if let manifestError { throw manifestError }
            if holdManifest {
                // Like URLSession, a held request ends when its task is cancelled.
                return try await withTaskCancellationHandler {
                    try await withCheckedThrowingContinuation { manifestContinuation = $0 }
                } onCancel: {
                    Task { @MainActor [weak self] in self?.cancelHeldManifest() }
                }
            }
        }
        if path.hasSuffix("/pair") {
            if holdPair {
                return try await withCheckedThrowingContinuation { pairContinuation = $0 }
            }
            return pairResponse
        }
        if path.hasSuffix("/binding-ticket") {
            if holdTicket {
                return try await withTaskCancellationHandler {
                    try await withCheckedThrowingContinuation { ticketContinuation = $0 }
                } onCancel: {
                    Task { @MainActor [weak self] in self?.cancelHeldTicket() }
                }
            }
            if !ticketAnswers.isEmpty { return try ticketAnswers.removeFirst().get() }
            return Self.issuedTicket()
        }
        if path.hasSuffix("/requests/claim") {
            if !claimErrors.isEmpty { throw claimErrors.removeFirst() }
            if !claims.isEmpty { return ["requests": claims.removeFirst()] }
            return try await withCheckedThrowingContinuation { pendingClaims.append($0) }
        }
        return [:]
    }
    /// Synthetic: 43 base64url characters. It lasts an hour here, so tests
    /// that move the coordinator's clock past 30-second waits still find it
    /// unexpired.
    static let ticket = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA-"
    static func issuedTicket(expires: Date = Date().addingTimeInterval(3600)) -> [String: Any] {
        ["health_binding_ticket": ticket, "expires_at": ISO8601DateFormatter().string(from: expires)]
    }
    var ticketAnswers: [Result<[String: Any], Error>] = []
    var holdTicket = false
    private(set) var ticketContinuation: CheckedContinuation<[String: Any], Error>?
    var ticketCalls: [Call] { calls.filter { $0.path.hasSuffix("/binding-ticket") } }
    func releaseTicket() {
        let continuation = ticketContinuation
        ticketContinuation = nil
        continuation?.resume(returning: Self.issuedTicket())
    }
    func cancelHeldTicket() {
        let continuation = ticketContinuation
        ticketContinuation = nil
        continuation?.resume(throwing: CancellationError())
    }
    func releasePair() {
        let continuation = pairContinuation
        pairContinuation = nil
        continuation?.resume(returning: pairResponse)
    }
    func releaseManifest() {
        let continuation = manifestContinuation
        manifestContinuation = nil
        continuation?.resume(returning: [:])
    }
    func cancelHeldManifest() {
        let continuation = manifestContinuation
        manifestContinuation = nil
        continuation?.resume(throwing: CancellationError())
    }
    func releasePending() {
        releaseTicket()
        releaseResult()
        releasePair()
        releaseManifest()
        let continuations = pendingClaims
        pendingClaims = []
        for continuation in continuations { continuation.resume(returning: ["requests": []]) }
    }
    func releaseResult() {
        holdResults = false
        let continuation = resultContinuation
        resultContinuation = nil
        continuation?.resume(returning: [:])
    }
    private var pairResponse: [String: Any] {
        ["device_id": pairedDeviceID, "credential": "memory-only", "instance": ["id": "instance-1"]]
    }
}

@MainActor
final class HealthCoordinatorFixture {
    let directory: URL
    let credentials = HealthMemoryCredentials()
    let reader = HealthFakeReader()
    let client = HealthFakeClient()
    let uploader = HealthFakeUploader()
    let coordinator: HealthSyncCoordinator
    let origin: WebOrigin
    init(directory: URL, savedIdentity: Bool, origin originText: String = "https://health-fixture.invalid",
         now: @escaping () -> Date = Date.init) {
        self.directory = directory
        self.origin = WebOrigin.parseConfigured(originText)!
        if savedIdentity {
            credentials.identity = HealthDeviceIdentity(
                installationID: "installation-1", accountID: "account-1", instanceID: "instance-1",
                instanceName: "Fixture", origin: origin.serialized, deviceID: "device-1", credential: "memory-only", websiteDataStoreID: UUID(uuidString: "10000000-0000-0000-0000-000000000002")
            )
        }
        coordinator = HealthSyncCoordinator(
            credentials: credentials, reader: reader, client: client, uploader: uploader, directory: directory, now: now
        )
        let target = HealthConnectionTarget(
            accountID: "account-1", instanceID: "instance-1", name: "Fixture", origin: origin.serialized
        )
        coordinator.targetProvider = { target }
        coordinator.websiteDataStoreIDProvider = { UUID(uuidString: "10000000-0000-0000-0000-000000000002") }
        if savedIdentity {
            let store = try! HealthDeviceStore(directory: directory)
            try! store.update { $0.committedDeviceID = "device-1" }
        }
    }
    func connect() -> Task<String, Never> {
        Task { await coordinator.connect(requestID: "request-1", pairingCode: "one-use", origin: origin) }
    }
}
