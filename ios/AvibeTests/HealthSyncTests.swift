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
        XCTAssertEqual(state, "failed")
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
        await fixture.coordinator.logout()
        let state = await operation.value
        XCTAssertEqual(state, "failed")
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
        XCTAssertEqual(fixture.reader.days, [30])
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
        _ condition: () -> Bool, file: StaticString = #filePath, line: UInt = #line
    ) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(5))
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

private final class HealthMemoryCredentials: HealthCredentialStoring {
    var identity: HealthDeviceIdentity?
    private(set) var saves = 0
    var clearError: Error?
    func load() throws -> HealthDeviceIdentity? { identity }
    func save(_ identity: HealthDeviceIdentity) throws { self.identity = identity; saves += 1 }
    func clear() throws {
        if let clearError { throw clearError }
        identity = nil
    }
    func installationID() throws -> String { "10000000-0000-0000-0000-000000000001" }
}

private final class HealthMemoryKeychainAccess: PushKeychainItemAccessing {
    private var items: [String: [String: Any]] = [:]
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
private final class HealthFakeReader: HealthDataReading {
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
    func query(days: Int, now: Date) async throws -> HealthQueryWindow {
        self.days.append(days)
        if holdQueries { await withCheckedContinuation { queryContinuation = $0 } }
        return HealthQueryWindow(
            computedAt: now, from: "2026-11-18", to: "2026-11-18",
            items: [], failures: [],
            metricStates: ["steps": "empty", "activity": "empty", "workouts": "empty", "sleep": "empty"]
        )
    }
    func startObserving(onChange: @escaping () async -> Void) async -> Bool {
        observer = onChange
        return true
    }
    func stopObserving() { observer = nil }
}

private final class HealthFakeUploader: HealthUploading {
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
    func cancelAll() async { cancellations += 1 }
    @MainActor
    func acknowledge(_ upload: Upload) async {
        await onComplete?(upload.id, .success(["accepted_seq": upload.sequence]))
    }
}

@MainActor
private final class HealthFakeClient: HealthDeviceRequesting {
    struct Call {
        let method: String
        let path: String
        let body: [String: Any]?
        let credential: String?
    }
    private(set) var calls: [Call] = []
    var claims: [[[String: Any]]] = [[[
        "id": "request-1", "operation": "health.connect", "claim_token": "claim-1",
        "metrics": ["steps"], "requires_user": true,
    ]]]
    var holdPair = false
    var holdManifest = false
    private(set) var manifestContinuation: CheckedContinuation<[String: Any], Error>?
    var manifestError: Error?
    private(set) var pairContinuation: CheckedContinuation<[String: Any], Error>?
    private(set) var pendingClaims: [CheckedContinuation<[String: Any], Error>] = []
    var results: [[String: Any]] {
        calls.filter { $0.path.hasSuffix("/result") }.compactMap(\.body)
    }
    func request(origin: String, credential: String?, method: String,
                 path: String, body: [String: Any]?) async throws -> [String: Any] {
        calls.append(Call(method: method, path: path, body: body, credential: credential))
        if path.hasSuffix("/manifest") {
            if let manifestError { throw manifestError }
            if holdManifest { return try await withCheckedThrowingContinuation { manifestContinuation = $0 } }
        }
        if path.hasSuffix("/pair") {
            if holdPair {
                return try await withCheckedThrowingContinuation { pairContinuation = $0 }
            }
            return pairResponse
        }
        if path.hasSuffix("/requests/claim") {
            if !claims.isEmpty { return ["requests": claims.removeFirst()] }
            return try await withCheckedThrowingContinuation { pendingClaims.append($0) }
        }
        return [:]
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
    func releasePending() {
        releasePair()
        releaseManifest()
        let continuations = pendingClaims
        pendingClaims = []
        for continuation in continuations { continuation.resume(returning: ["requests": []]) }
    }
    private var pairResponse: [String: Any] {
        ["device_id": "device-1", "credential": "memory-only", "instance": ["id": "instance-1"]]
    }
}

@MainActor
private final class HealthCoordinatorFixture {
    let directory: URL
    let credentials = HealthMemoryCredentials()
    let reader = HealthFakeReader()
    let client = HealthFakeClient()
    let uploader = HealthFakeUploader()
    let coordinator: HealthSyncCoordinator
    let origin = WebOrigin.parseConfigured("https://health-fixture.invalid")!
    init(directory: URL, savedIdentity: Bool) {
        self.directory = directory
        if savedIdentity {
            credentials.identity = HealthDeviceIdentity(
                installationID: "installation-1", accountID: "account-1", instanceID: "instance-1",
                instanceName: "Fixture", origin: origin.serialized, deviceID: "device-1", credential: "memory-only"
            )
        }
        coordinator = HealthSyncCoordinator(
            credentials: credentials, reader: reader, client: client, uploader: uploader, directory: directory
        )
        let target = HealthConnectionTarget(
            accountID: "account-1", instanceID: "instance-1", name: "Fixture", origin: origin.serialized
        )
        coordinator.targetProvider = { target }
    }
    func connect() -> Task<String, Never> {
        Task { await coordinator.connect(requestID: "request-1", pairingCode: "one-use", origin: origin) }
    }
}
