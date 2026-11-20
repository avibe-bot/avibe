import WebKit
import XCTest

@testable import Avibe

/// `device.describe`: who this iPhone is, for the selected instance's device
/// list only.
final class DeviceDescriptionTests: XCTestCase {
    private let one = "https://instance-one.example"

    func testTheInstallationIDIsDerivedPerInstance() {
        let secret = Data(0..<32)
        // HMAC-SHA256(secret, "avibe-device:v1:" + instance id), first 16
        // bytes in hex: the value the instance groups this App's sessions by.
        XCTAssertEqual(
            NativeDeviceDescription.installationID(secret: secret, instanceID: "instance-1"),
            "5eeb24aca55730b9f93cc16472af8b16"
        )
        XCTAssertEqual(
            NativeDeviceDescription.installationID(secret: secret, instanceID: "instance-2"),
            "5e344a8ec4b2b5b6e19bd4cd70847b7b"
        )
        XCTAssertNotEqual(
            NativeDeviceDescription.installationID(secret: Data(repeating: 1, count: 32), instanceID: "instance-1"),
            NativeDeviceDescription.installationID(secret: secret, instanceID: "instance-1")
        )
        XCTAssertNil(NativeDeviceDescription.installationID(secret: Data(), instanceID: "instance-1"))
        XCTAssertNil(NativeDeviceDescription.installationID(secret: secret, instanceID: ""))
    }

    func testTheBodyNamesOnlyTheIdentifiersItHas() throws {
        let full = NativeDeviceDescription.body(
            displayName: "iPhone 16 Pro",
            installationID: "abc",
            healthInstallationID: "health-id",
            scheduleDeviceID: "schedule-id",
            appVersion: "1.0 (42)",
            systemVersion: "iOS 26.1"
        )
        XCTAssertEqual(full["kind"] as? String, "iphone_app")
        XCTAssertEqual(full["display_name"] as? String, "iPhone 16 Pro")
        XCTAssertEqual(full["installation_id"] as? String, "abc")
        XCTAssertEqual(full["app_version"] as? String, "1.0 (42)")
        XCTAssertEqual(full["system_version"] as? String, "iOS 26.1")
        XCTAssertEqual(
            full["aliases"] as? [[String: String]],
            [["source": "health", "id": "health-id"], ["source": "schedule", "id": "schedule-id"]]
        )
        XCTAssertEqual(Set(full.keys), ["kind", "display_name", "installation_id", "aliases", "app_version", "system_version"])

        let bare = NativeDeviceDescription.body(
            displayName: "iPhone",
            installationID: nil,
            healthInstallationID: " ",
            scheduleDeviceID: nil,
            appVersion: "1.0 (42)",
            systemVersion: "iOS 26.1"
        )
        XCTAssertNil(bare["installation_id"])
        XCTAssertEqual(bare["aliases"] as? [[String: String]], [])
        XCTAssertTrue(JSONSerialization.isValidJSONObject(full))
    }

    func testTheModelNameComesFromTheHardwareIdentifier() {
        XCTAssertEqual(NativeDeviceDescription.modelName(identifier: "iPhone17,1"), "iPhone 16 Pro")
        XCTAssertEqual(NativeDeviceDescription.modelName(identifier: "iPhone18,4"), "iPhone Air")
        XCTAssertEqual(NativeDeviceDescription.modelName(identifier: "iPhone19,7"), "iPhone 18 Pro Max")
        // A newer iPhone than the table knows, or anything else.
        XCTAssertEqual(NativeDeviceDescription.modelName(identifier: "iPhone99,1"), "iPhone")
        XCTAssertEqual(NativeDeviceDescription.modelName(identifier: ""), "iPhone")
        XCTAssertFalse(NativeDeviceDescription.hardwareIdentifier.isEmpty)
    }

    @MainActor
    func testTheAppDescribesItselfOnlyToTheSelectedInstance() async throws {
        let keychain = InMemoryPushKeychainStore()
        let installationID = UUID()
        keychain.installation = PushInstallationState(
            installationID: installationID,
            installationSecret: Data(0..<32),
            accountID: nil,
            bindGeneration: 0,
            providerGeneration: 0,
            provider: nil,
            pendingBulkRevokeThroughGeneration: nil
        )
        let marker = InMemoryPushInstallationMarkerStore()
        marker.installationID = installationID
        let health = HealthMemoryCredentials()
        health.identity = HealthDeviceIdentity(
            installationID: "health-installation", accountID: "account-1", instanceID: "instance-1",
            instanceName: "One", origin: one, deviceID: "device-1", credential: "memory-only"
        )
        let appModel = try await makeAppModel(keychain: keychain, marker: marker, health: health)

        // No instance open yet.
        XCTAssertNil(appModel.describeDevice(instanceID: "instance-1"))
        _ = try appModel.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await waitUntil { appModel.route.runtime?.instance.id == "instance-1" }
        // Another instance's page gets nothing.
        XCTAssertNil(appModel.describeDevice(instanceID: "instance-2"))

        let body = try XCTUnwrap(appModel.describeDevice(instanceID: "instance-1"))
        XCTAssertEqual(body["kind"] as? String, "iphone_app")
        XCTAssertEqual(body["installation_id"] as? String, "5eeb24aca55730b9f93cc16472af8b16")
        XCTAssertEqual(body["app_version"] as? String, NativeShellMetadata().shellVersion)
        XCTAssertEqual(
            body["system_version"] as? String,
            "\(UIDevice.current.systemName) \(UIDevice.current.systemVersion)"
        )
        XCTAssertEqual(
            body["display_name"] as? String,
            NativeDeviceDescription.modelName(identifier: NativeDeviceDescription.hardwareIdentifier)
        )
        let aliases = try XCTUnwrap(body["aliases"] as? [[String: String]])
        XCTAssertEqual(aliases.first, ["source": "health", "id": "health-installation"])
        XCTAssertEqual(aliases.contains { $0["source"] == "schedule" }, appModel.systemScheduleDeviceID != nil)
        // The raw push installation never leaves the App.
        XCTAssertFalse(String(describing: body).contains(installationID.uuidString.lowercased()))
        XCTAssertFalse(String(describing: body).contains(installationID.uuidString))

        // Health paired with another instance, or on another account: no alias.
        for identity in [
            HealthDeviceIdentity(installationID: "health-installation", accountID: "account-1", instanceID: "instance-2",
                                 instanceName: "Two", origin: "https://instance-two.example", deviceID: "d", credential: "c"),
            HealthDeviceIdentity(installationID: "health-installation", accountID: "account-2", instanceID: "instance-1",
                                 instanceName: "One", origin: one, deviceID: "d", credential: "c"),
        ] {
            health.identity = identity
            let other = try XCTUnwrap(appModel.describeDevice(instanceID: "instance-1"))
            XCTAssertFalse((other["aliases"] as? [[String: String]] ?? []).contains { $0["source"] == "health" })
        }
    }

    /// A cold start opens the last instance before the account check
    /// answers, and keeps it when the check fails: the page there is still
    /// described (only the instance must be the selected one).
    @MainActor
    func testARestoredInstanceIsDescribedBeforeTheAccountCheckConfirms() async throws {
        let keychain = InMemoryPushKeychainStore()
        let marker = InMemoryPushInstallationMarkerStore()
        let suiteName = "DeviceDescriptionTests.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        addTeardownBlock { defaults.removePersistentDomain(forName: suiteName) }
        let first = try await makeAppModel(keychain: keychain, marker: marker, defaults: defaults)
        _ = try first.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await waitUntil { first.route.runtime?.instance.id == "instance-1" }

        let relaunched = try await makeAppModel(
            keychain: keychain, marker: marker, defaults: defaults,
            sessionCheckFails: true, waitForInstances: false
        )
        try await waitUntil { relaunched.route.runtime?.instance.id == "instance-1" }
        XCTAssertNil(relaunched.mobileSessionCoordinator.healthAccountID)
        let body = try XCTUnwrap(relaunched.describeDevice(instanceID: "instance-1"))
        XCTAssertEqual(body["kind"] as? String, "iphone_app")
        XCTAssertNil(relaunched.describeDevice(instanceID: "instance-2"))
    }

    @MainActor
    func testDescribeAnswersOnlyTheInstanceMainFrame() async throws {
        let instance = try XCTUnwrap(WebOrigin.parseConfigured(one))
        var answer: [String: Any]? = ["kind": "iphone_app", "display_name": "iPhone 16 Pro", "aliases": []]
        let webView = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: instance,
                setOuterScrollLocked: { _, _ in false },
                deviceDescription: { answer }
            )
        )
        let probe = DeviceWebViewLoadProbe()
        webView.navigationDelegate = probe
        try await probe.loadHTML(
            """
            <!doctype html>
            <iframe id="child" srcdoc="<p>subframe</p>"></iframe>
            """,
            baseURL: try XCTUnwrap(URL(string: one)),
            in: webView
        )
        let script = """
            const info = await window.avibeNative.call("app.info");
            const described = await window.avibeNative.call("device.describe");
            let invalid;
            try {
              await window.avibeNative.call("device.describe", {name: "x"});
              invalid = "ok";
            } catch (error) {
              invalid = error.code;
            }
            const subframe = await document.querySelector("#child").contentWindow.webkit
              .messageHandlers.avibeNative.postMessage({method: "device.describe"});
            return JSON.stringify({
              capability: info.capabilities.device,
              described,
              invalid,
              subframe: subframe?.error?.code ?? "ok"
            });
            """
        let payload = try await run(script, in: webView)
        XCTAssertEqual(payload["capability"] as? Int, 1)
        XCTAssertEqual((payload["described"] as? [String: Any])?["display_name"] as? String, "iPhone 16 Pro")
        XCTAssertEqual(payload["invalid"] as? String, "invalid_argument")
        XCTAssertEqual(payload["subframe"] as? String, "forbidden")

        // No longer the selected instance.
        answer = nil
        let refused = try await run(
            """
            try {
              await window.avibeNative.call("device.describe");
              return JSON.stringify({code: "ok"});
            } catch (error) {
              return JSON.stringify({code: error.code});
            }
            """,
            in: webView
        )
        XCTAssertEqual(refused["code"] as? String, "forbidden")
    }

    @MainActor
    func testDescribeAsksForAHealthBindingTicketOnlyWhenTheReportDoes() async throws {
        let instance = try XCTUnwrap(WebOrigin.parseConfigured(one))
        // Synthetic: 43 base64url characters.
        let issued = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA-"
        let reportable: [String: Any] = [
            "kind": "iphone_app", "display_name": "iPhone 16 Pro", "aliases": [],
            "installation_id": "5eeb24aca55730b9f93cc16472af8b16",
        ]
        var answer: [String: Any]? = reportable
        var ticket: String? = issued
        var asked = 0
        var duringTheWait: () -> Void = {}
        let webView = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: instance,
                setOuterScrollLocked: { _, _ in false },
                deviceDescription: { answer },
                healthBindingTicket: {
                    asked += 1
                    duringTheWait()
                    return ticket
                }
            )
        )
        let probe = DeviceWebViewLoadProbe()
        webView.navigationDelegate = probe
        try await probe.loadHTML(
            """
            <!doctype html>
            <iframe id="child" srcdoc="<p>subframe</p>"></iframe>
            """,
            baseURL: try XCTUnwrap(URL(string: one)),
            in: webView
        )
        let script = """
            const code = async (params) => {
              try {
                await window.avibeNative.call("device.describe", params);
                return "ok";
              } catch (error) {
                return error.code;
              }
            };
            const plain = await window.avibeNative.call("device.describe");
            const report = await window.avibeNative.call("device.describe", {health_binding: true});
            const subframe = await document.querySelector("#child").contentWindow.webkit
              .messageHandlers.avibeNative.postMessage({method: "device.describe", params: {health_binding: true}});
            return JSON.stringify({
              plain: "health_binding_ticket" in plain,
              ticket: report.health_binding_ticket ?? null,
              name: report.display_name,
              asFalse: await code({health_binding: false}),
              asNumber: await code({health_binding: 1}),
              withMore: await code({health_binding: true, name: "x"}),
              subframe: subframe?.error?.code ?? "ok"
            });
            """
        let payload = try await run(script, in: webView)
        XCTAssertEqual(payload["plain"] as? Bool, false)
        XCTAssertEqual(payload["ticket"] as? String, issued)
        XCTAssertEqual(payload["name"] as? String, "iPhone 16 Pro")
        XCTAssertEqual(payload["asFalse"] as? String, "invalid_argument")
        XCTAssertEqual(payload["asNumber"] as? String, "invalid_argument")
        XCTAssertEqual(payload["withMore"] as? String, "invalid_argument")
        XCTAssertEqual(payload["subframe"] as? String, "forbidden")
        XCTAssertEqual(asked, 1)

        let ask = """
            try {
              const report = await window.avibeNative.call("device.describe", {health_binding: true});
              return JSON.stringify({code: "ok", ticket: "health_binding_ticket" in report});
            } catch (error) {
              return JSON.stringify({code: error.code});
            }
            """
        // Not reportable yet (no installation id): nothing is asked.
        answer = ["kind": "iphone_app", "display_name": "iPhone 16 Pro", "aliases": []]
        let unreportable = try await run(ask, in: webView)
        XCTAssertEqual(unreportable["code"] as? String, "ok")
        XCTAssertEqual(unreportable["ticket"] as? Bool, false)
        XCTAssertEqual(asked, 1)
        answer = reportable

        // No ticket: the property is left out, not null.
        ticket = nil
        let without = try await run(ask, in: webView)
        XCTAssertEqual(without["code"] as? String, "ok")
        XCTAssertEqual(without["ticket"] as? Bool, false)

        // No longer the selected instance once the ticket is back: refused.
        ticket = issued
        duringTheWait = { answer = nil }
        let moved = try await run(ask, in: webView)
        XCTAssertEqual(moved["code"] as? String, "forbidden")
        XCTAssertEqual(asked, 3)
    }

    @MainActor
    private func run(_ script: String, in webView: WKWebView) async throws -> [String: Any] {
        let result = try await webView.callAsyncJavaScript(script, arguments: [:], in: nil, contentWorld: .page)
        let data = try XCTUnwrap((result as? String)?.data(using: .utf8))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    /// An App signed in to `account-1` with instances one and two.
    @MainActor
    private func makeAppModel(
        keychain: InMemoryPushKeychainStore,
        marker: InMemoryPushInstallationMarkerStore,
        health: HealthCredentialStoring = HealthMemoryCredentials(),
        defaults existing: UserDefaults? = nil,
        sessionCheckFails: Bool = false,
        waitForInstances: Bool = true
    ) async throws -> AppModel {
        let server = try LocalHTTPServer { request in
            switch (request.method, request.path) {
            case ("GET", "/api/config/auth"):
                return .json("{}")
            case ("GET", "/api/mobile/session") where sessionCheckFails:
                return .status(503)
            case ("GET", "/api/mobile/session"):
                return .json(
                    """
                    {
                      "account": {"id": "account-1", "email": "max@example.com"},
                      "instances": [
                        {"id": "instance-1", "name": "One", "host": "instance-one.example",
                         "url": "https://instance-one.example", "relationship": "owner",
                         "owner_email": "max@example.com", "available": true},
                        {"id": "instance-2", "name": "Two", "host": "instance-two.example",
                         "url": "https://instance-two.example", "relationship": "owner",
                         "owner_email": "max@example.com", "available": true}
                      ]
                    }
                    """
                )
            default:
                return .status(404)
            }
        }
        let baseURL = try await server.start()
        addTeardownBlock { server.stop() }
        let defaults: UserDefaults
        if let existing {
            defaults = existing
        } else {
            let suiteName = "DeviceDescriptionTests.\(UUID().uuidString)"
            defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
            addTeardownBlock { defaults.removePersistentDomain(forName: suiteName) }
        }
        let scheduleDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("DeviceDescription.\(UUID().uuidString)", isDirectory: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: scheduleDirectory) }
        let appModel = AppModel(
            defaults: defaults,
            controlPlane: MobileControlPlaneConfiguration(testBaseURL: baseURL),
            pushKeychain: keychain,
            pushGenerationStore: InMemoryPushGenerationStore(),
            pushInstallationMarker: marker,
            systemScheduleStore: FileSystemSystemScheduleStore(
                fileURL: scheduleDirectory.appendingPathComponent("ledger.json")
            ),
            healthCredentials: health
        )
        if waitForInstances {
            try await waitUntil {
                if case .onboarding(.instances) = appModel.route { return true }
                return false
            }
        }
        return appModel
    }

    @MainActor
    private func waitUntil(
        timeout: TimeInterval = 5,
        _ condition: @MainActor () -> Bool
    ) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            guard Date() < deadline else {
                XCTFail("Timed out waiting for the condition")
                return
            }
            try await Task.sleep(for: .milliseconds(20))
        }
    }
}

private final class DeviceWebViewLoadProbe: NSObject, WKNavigationDelegate {
    private var continuation: CheckedContinuation<Void, Error>?

    func loadHTML(_ html: String, baseURL: URL, in webView: WKWebView) async throws {
        try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            webView.loadHTMLString(html, baseURL: baseURL)
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation?) {
        continuation?.resume()
        continuation = nil
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation?, withError error: Error) {
        continuation?.resume(throwing: error)
        continuation = nil
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation?,
        withError error: Error
    ) {
        continuation?.resume(throwing: error)
        continuation = nil
    }
}
