import Combine
import Network
import SwiftUI
import UIKit
import UserNotifications
import WebKit
import XCTest
@testable import Avibe

/// The direct path inside the App's own composition: `AppModel`, the
/// runtime controller it registers, and the bridge a page talks to.
final class DirectPathAppTests: XCTestCase {
    private let instance = WebOrigin.parseConfigured(
        "https://instance-one.example"
    )!

    // MARK: App composition controls

    /// The compatibility path with no registered transport: no `direct`
    /// capability, `direct.status` unsupported, no event, WebKit's
    /// `proxyConfigurations` setter never called on any store, and no
    /// listening socket. The registered App's positive control is covered by
    /// `testWithATransportTheRouteIsWrittenBeforeTheFirstLoadAndTheStatusReachesThePage`.
    @MainActor
    func testWithoutATransportHasNoDirectCapabilityNoListenerAndNoRouteWrite() async throws {
        try assertSocketScanSeesListeners()
        let routeWrites = try ProxyConfigurationWrites.count()
        var listening: Set<ListeningSocket> = []

        func assertUntouched(_ step: String) throws {
            XCTAssertEqual(
                try ProxyConfigurationWrites.count(),
                routeWrites,
                step
            )
            XCTAssertEqual(try newListeners(since: listening), [], step)
        }

        let app = try await makeApp(directTransport: nil) {
            listening = try listeningSockets()
        }
        XCTAssertNil(app.directStatusProvider)
        let firstStore = app.runtimeWebsiteDataStore
        try assertUntouched("launched and signed in")

        // A runtime opens at once, as before: there is no route to prepare.
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        guard case .browser(let runtime) = app.route else {
            return XCTFail("The runtime did not open at once.")
        }
        let page = try await showRuntimePage(app, runtime)
        let surface = try await page.directSurface()
        XCTAssertEqual(surface, #"{"capability":null,"status":"unsupported"}"#)
        try assertUntouched("a runtime opened and its page asked")

        NotificationCenter.default.post(
            name: UIApplication.didEnterBackgroundNotification,
            object: nil
        )
        try assertUntouched("background")
        NotificationCenter.default.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        try assertUntouched("foreground")
        try await Task.sleep(for: .milliseconds(200))
        let changes = try await page.changes()
        XCTAssertEqual(changes, "[]")
        page.close()

        _ = try await select("instance-2", in: app)
        try assertUntouched("the instance was switched")

        // Signing out rotates the account's store.
        _ = try await app.mobileSessionCoordinator.logout()
        trackStore(of: app)
        XCTAssertFalse(app.runtimeWebsiteDataStore === firstStore)
        try assertUntouched("signed out and the store rotated")
        try await Task.sleep(for: .milliseconds(200))
        try assertUntouched("afterwards")

        XCTAssertEqual(firstStore.proxyConfigurations.count, 0)
        XCTAssertEqual(app.runtimeWebsiteDataStore.proxyConfigurations.count, 0)
        // Nothing of the listener fault exists either: no marker in the
        // App's (isolated) preferences, and no fault state.
        XCTAssertFalse(app.directListenerFault)
        XCTAssertNil(
            try XCTUnwrap(appDefaults).object(forKey: UserDefaultsDirectFaultMarker.key)
        )
    }

    // MARK: The listener fault

    @MainActor
    func testWithoutATransportTheFaultMarkerIsNeverReadOrWritten() async throws {
        let marker = FaultMarkerSpy(marked: true)
        let app = try await makeApp(directTransport: nil, directFaultMarker: marker)

        _ = try await select("instance-1", in: app)

        XCTAssertNil(app.directStatusProvider)
        XCTAssertEqual(marker.takes, 0)
        XCTAssertEqual(marker.marks, 0)
        XCTAssertTrue(marker.isMarked)
    }

    /// The launch after a fault is decided in `init`, before any route or
    /// first load: no controller, so no capability, no listener, no route
    /// write, and a runtime that opens at once. It consumes the marker, so
    /// the launch after it uses the transport again.
    @MainActor
    func testTheLaunchAfterAFaultUsesNoListenerAndTheOneAfterThatUsesTheTransport() async throws {
        try assertSocketScanSeesListeners()
        let marker = FaultMarkerSpy(marked: true)
        let transport = StandInDirectTransport()
        let routeWrites = try ProxyConfigurationWrites.count()
        var listening: Set<ListeningSocket> = []
        let bypassed = try await makeApp(
            directTransport: transport,
            directFaultMarker: marker
        ) {
            listening = try listeningSockets()
        }

        XCTAssertNil(bypassed.directStatusProvider)
        XCTAssertEqual(marker.takes, 1)
        XCTAssertFalse(marker.isMarked)
        _ = try bypassed.mobileSessionCoordinator.selectInstance(id: "instance-1")
        guard case .browser = bypassed.route else {
            return XCTFail("The runtime did not open at once.")
        }
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites)
        XCTAssertEqual(try newListeners(since: listening), [])
        XCTAssertEqual(transport.selections.count, 0)

        // The next launch, with the same preferences, uses the transport.
        let next = try await makeApp(
            directTransport: transport,
            directFaultMarker: marker
        )
        XCTAssertNotNil(next.directStatusProvider)
        XCTAssertEqual(marker.takes, 2)
        _ = try await select("instance-1", in: next)
        XCTAssertEqual(transport.selections.count, 1)
    }

    func testTheFaultMarkerIsOneVersionedKeyAndAnyStoredValueCounts() throws {
        let suiteName = "DirectPathAppTests.marker.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let marker = UserDefaultsDirectFaultMarker(defaults: defaults)

        XCTAssertFalse(marker.take())
        marker.mark()
        XCTAssertEqual(defaults.object(forKey: UserDefaultsDirectFaultMarker.key) as? Int, 1)
        XCTAssertTrue(marker.take())
        XCTAssertNil(defaults.object(forKey: UserDefaultsDirectFaultMarker.key))
        XCTAssertFalse(marker.take())

        // A value this version cannot read still means one launch without
        // the listener, and is cleared.
        defaults.set("unreadable", forKey: UserDefaultsDirectFaultMarker.key)
        XCTAssertTrue(marker.take())
        XCTAssertNil(defaults.object(forKey: UserDefaultsDirectFaultMarker.key))
    }

    @MainActor
    func testAListenerFaultCoversOnlyARoutedRuntimeOnceAndIsMarkedOnce() async throws {
        let marker = FaultMarkerSpy(marked: false)
        let timing = RuntimeOpenManualTiming()
        let transport = StandInDirectTransport()
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(
            directTransport: transport,
            directFaultMarker: marker,
            runtimeOpenTiming: timing
        ) {
            listening = try listeningSockets()
        }
        let first = try await select("instance-1", in: app)
        let socket = try XCTUnwrap(try newListeners(since: listening).first)
        let port = try XCTUnwrap(NWEndpoint.Port(rawValue: socket.port))
        XCTAssertFalse(app.directFaultCovers(first))
        var shownWhileMarking: [Bool] = []
        // The App owns this spy; its observer must not retain the App back.
        marker.whileMarking = { [weak app] in
            guard let app else {
                return XCTFail("The App was released before the fault was marked")
            }
            shownWhileMarking.append(app.directListenerFault)
        }
        addTeardownBlock { @MainActor [weak app] in
            XCTAssertNil(app, "The fault-marker observer must not keep the fixture App alive")
        }

        // The App's port is taken while it is in the background: the return
        // cannot re-bind it, which is the listener fault.
        NotificationCenter.default.post(
            name: UIApplication.didEnterBackgroundNotification,
            object: nil
        )
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: socket.port,
            reuseAddress: true
        )
        defer { squatter.close() }
        NotificationCenter.default.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        try await eventually { app.directListenerFault }
        // The marker was written before the fault was shown.
        XCTAssertEqual(shownWhileMarking, [false])
        // Retry does not rebuild under the reopen message, and the runtime
        // that was open keeps its WebView under it.
        XCTAssertEqual(app.directLoadGate(for: first), .fault)
        XCTAssertFalse(app.mayReload(first))
        XCTAssertFalse(app.authorizeReload(of: first))
        XCTAssertFalse(app.presentsDirectFaultOnly(first))
        // Another return does not report it again.
        NotificationCenter.default.post(
            name: UIApplication.didEnterBackgroundNotification,
            object: nil
        )
        NotificationCenter.default.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        try await Task.sleep(for: .milliseconds(300))

        XCTAssertTrue(app.directListenerFault)
        XCTAssertEqual(marker.marks, 1)
        XCTAssertTrue(app.directFaultCovers(first))
        // A page behind the fault gets the bridge's `failed`, never a
        // status that reads as ready.
        let page = try await showRuntimePage(app, first)
        let surface = try await page.directSurface()
        XCTAssertEqual(surface, #"{"capability":1,"status":"failed"}"#)
        page.close()

        // An instance the fault left unrouted opens and is not covered: an
        // ordinary page, with its WebView and its Retry.
        let second = try await select("instance-2", in: app)
        XCTAssertFalse(app.directFaultCovers(second))
        XCTAssertFalse(app.presentsDirectFaultOnly(second))
        XCTAssertTrue(app.mayReload(second))
        XCTAssertTrue(app.directListenerFault)
        XCTAssertEqual(marker.marks, 1)

        // The routed instance opened again under the fault: the reopen
        // message alone, with no WebView made for it and no Retry.
        let reopened = try await select("instance-1", in: app)
        XCTAssertTrue(app.directFaultCovers(reopened))
        XCTAssertTrue(app.presentsDirectFaultOnly(reopened))
        XCTAssertFalse(app.mayReload(reopened))
        XCTAssertEqual(marker.marks, 1)
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .failed)
        XCTAssertNil(app.runtimeOpenAttempt?.deadline)

        // The process-terminal fault has no later callback to end a new
        // attempt. Its automatic timer is already retired, so advancing far
        // past the ordinary budget cannot replace the reopen message.
        timing.advance(by: .seconds(120))
        XCTAssertEqual(app.route, .browser(reopened))
        XCTAssertTrue(app.presentsDirectFaultOnly(reopened))
        XCTAssertNil(app.runtimeOpenNotice)
    }

    // MARK: The creation and reload boundaries

    private func enterBackground() {
        NotificationCenter.default.post(
            name: UIApplication.didEnterBackgroundNotification,
            object: nil
        )
    }

    private func enterForeground() {
        NotificationCenter.default.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
    }

    /// Lets the main actor run what is already scheduled, then a little
    /// longer: for checks that something did not happen.
    @MainActor
    private func settle() async throws {
        for _ in 0..<200 {
            await Task.yield()
        }
        try await Task.sleep(for: .milliseconds(200))
    }

    /// A routed instance while the listener is closed for the background or
    /// being bound again: Retry is not allowed and an open makes no browser,
    /// both decided at once and by no wait. The held open stays on the
    /// loading route; when the return settles, it becomes the browser if
    /// the listener serves, or the reopen message alone if it failed.
    @MainActor
    func testARoutedOpenAndRetryAreHeldUntilTheReturnSettles() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let runtime = try await select("instance-1", in: app)
        XCTAssertEqual(app.directLoadGate(for: runtime), .open)
        XCTAssertTrue(app.mayReload(runtime))

        enterBackground()
        XCTAssertEqual(app.directLoadGate(for: runtime), .held)
        XCTAssertFalse(app.mayReload(runtime))

        // The instance is opened again (a push tap): nothing is created
        // against the closed port.
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await settle()
        XCTAssertEqual(app.route, .loading)

        // The return starts the re-bind; in that same turn, still held.
        enterForeground()
        XCTAssertEqual(app.route, .loading)
        XCTAssertFalse(app.mayReload(runtime))

        let reopened = try await waitForBrowser("instance-1", in: app)
        if app.directListenerFault {
            XCTAssertTrue(app.presentsDirectFaultOnly(reopened))
            XCTAssertFalse(app.mayReload(reopened))
        } else {
            XCTAssertEqual(app.directLoadGate(for: reopened), .open)
            XCTAssertFalse(app.presentsDirectFaultOnly(reopened))
            XCTAssertTrue(app.mayReload(reopened))
        }
    }

    /// A held open belongs to the runtime identity it was made under: an
    /// open that replaces it, or a sign-out, ends it, and the return then
    /// creates nothing for it.
    @MainActor
    func testAHeldOpenIsEndedByANewerOpenOrASignOut() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let first = try await select("instance-1", in: app)

        enterBackground()
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await settle()
        XCTAssertEqual(app.route, .loading)

        // A newer open, of an instance with no route on this store: it
        // loads on WebKit's normal path at once, background or not.
        let second = try await select("instance-2", in: app)
        XCTAssertEqual(app.directLoadGate(for: second), .open)
        enterForeground()
        try await eventually { app.directLoadGate(for: first) != .held }
        try await settle()
        XCTAssertEqual(app.route, .browser(second))

        // Held again, then signed out: the return leaves the account page.
        guard !app.directListenerFault else {
            return
        }
        enterBackground()
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await settle()
        XCTAssertEqual(app.route, .loading)
        _ = try await app.mobileSessionCoordinator.logout()
        trackStore(of: app)
        // The sign-out itself ends the held open: the spinner does not wait
        // for the return.
        XCTAssertNil(app.route.runtime)
        XCTAssertNotEqual(app.route, .loading)
        enterForeground()
        try await settle()
        XCTAssertNil(app.route.runtime)
        XCTAssertNotEqual(app.route, .loading)
    }

    /// A held open whose return fails: the loading route ends as the reopen
    /// message alone, with no WebView and no Retry, the fault is marked
    /// once, and an instance without a route still opens as an ordinary
    /// page.
    @MainActor
    func testAHeldOpenEndsAsTheReopenMessageWhenTheReBindFails() async throws {
        let marker = FaultMarkerSpy(marked: false)
        let transport = StandInDirectTransport()
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(directTransport: transport, directFaultMarker: marker) {
            listening = try listeningSockets()
        }
        _ = try await select("instance-1", in: app)
        let socket = try XCTUnwrap(try newListeners(since: listening).first)
        let port = try XCTUnwrap(NWEndpoint.Port(rawValue: socket.port))

        enterBackground()
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: socket.port,
            reuseAddress: true
        )
        defer { squatter.close() }
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await settle()
        XCTAssertEqual(app.route, .loading)

        enterForeground()
        try await eventually { app.directListenerFault }
        let runtime = try await waitForBrowser("instance-1", in: app)
        XCTAssertTrue(app.presentsDirectFaultOnly(runtime))
        XCTAssertEqual(app.directLoadGate(for: runtime), .fault)
        XCTAssertFalse(app.mayReload(runtime))
        XCTAssertEqual(marker.marks, 1)

        let second = try await select("instance-2", in: app)
        XCTAssertFalse(app.presentsDirectFaultOnly(second))
        XCTAssertTrue(app.mayReload(second))
        XCTAssertEqual(marker.marks, 1)
    }

    @MainActor
    func testListenerRebindFailureDoesNotFailAnUnroutedRuntimeAttempt() async throws {
        let marker = FaultMarkerSpy(marked: false)
        let transport = StandInDirectTransport()
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(
            directTransport: transport,
            directFaultMarker: marker
        ) {
            listening = try listeningSockets()
        }
        _ = try await select("instance-1", in: app)
        let socket = try XCTUnwrap(try newListeners(since: listening).first)
        let port = try XCTUnwrap(NWEndpoint.Port(rawValue: socket.port))

        enterBackground()
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: socket.port,
            reuseAddress: true
        )
        defer { squatter.close() }

        let ordinary = try await select("instance-2", in: app)
        let attemptID = try XCTUnwrap(app.runtimeOpenAttempt?.id)
        XCTAssertFalse(app.directFaultCovers(ordinary))

        enterForeground()
        try await eventually { app.directListenerFault }
        XCTAssertEqual(app.route, .browser(ordinary))
        XCTAssertEqual(app.runtimeOpenAttempt?.id, attemptID)
        XCTAssertNotEqual(app.runtimeOpenAttempt?.phase, .failed)
        XCTAssertEqual(marker.marks, 1)
    }

    /// Without a transport nothing is gated: Retry reloads at once, and the
    /// runtime's WebView is given no gate at all, as before.
    @MainActor
    func testTrustedRuntimeScheduleConfirmsCreatesOnceAndDetachesOnLogout() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let platform = RuntimeSystemSchedulePlatform()
        let app = try await makeApp(
            directTransport: nil,
            systemSchedulePlatform: platform,
            systemScheduleNow: { now }
        )
        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        defer { page.close() }

        let capabilities = try jsonObject(
            try await page.systemScheduleCapabilities()
        )
        XCTAssertEqual(capabilities["contract"] as? Int, 1)
        XCTAssertEqual(
            capabilities["supportedKinds"] as? [String],
            ["alarm"]
        )

        let first = Task { @MainActor in
            try await page.systemScheduleRequest(
                scheduleParams(
                    requestID: "request-1",
                    operationID: "operation-1",
                    now: now
                )
            )
        }
        try await eventually {
            app.systemScheduleConfirmation?.request.operationID == "operation-1"
        }
        let confirmation = try XCTUnwrap(app.systemScheduleConfirmation)
        XCTAssertEqual(confirmation.request.kind, .alarm)
        XCTAssertEqual(confirmation.request.title, "Take medicine")
        XCTAssertEqual(confirmation.request.instanceID, "instance-1")
        XCTAssertEqual(confirmation.request.sessionID, "session-1")
        XCTAssertEqual(confirmation.instanceName, "One")
        XCTAssertFalse(confirmation.isSubmitting)

        app.confirmSystemSchedule(id: confirmation.id)
        let receipt = try jsonObject(try await first.value)
        XCTAssertEqual(receipt["state"] as? String, "scheduled")
        XCTAssertEqual(receipt["authorization"] as? String, "authorized")
        XCTAssertEqual(receipt["systemIdentifier"] as? String, "system-1")
        XCTAssertEqual(platform.scheduleCalls, 1)
        XCTAssertEqual(platform.systemCreationCalls, 1)

        // A bridge retry uses the stored operation result. It does not show a
        // second native confirmation or create another system item.
        let retry = try jsonObject(
            try await page.systemScheduleRequest(
                scheduleParams(
                    requestID: "request-2",
                    operationID: "operation-1",
                    now: now
                )
            )
        )
        XCTAssertEqual(retry["requestId"] as? String, "request-2")
        XCTAssertEqual(retry["state"] as? String, "scheduled")
        XCTAssertNil(app.systemScheduleConfirmation)
        XCTAssertEqual(platform.scheduleCalls, 1)
        XCTAssertEqual(platform.systemCreationCalls, 1)

        let store = try XCTUnwrap(appScheduleStore)
        let owner = try XCTUnwrap(
            store.managementOwner(accountID: "account-1")
        )
        XCTAssertEqual(
            store.record(
                operationID: "operation-1",
                instanceID: "instance-1",
                owner: owner
            )?.lifecycle,
            .managedScheduled
        )
        _ = try await app.mobileSessionCoordinator.logout()
        trackStore(of: app)
        XCTAssertEqual(
            store.record(
                operationID: "operation-1",
                instanceID: "instance-1",
                owner: owner
            )?.lifecycle,
            .detachedRetained
        )
        XCTAssertEqual(platform.cancelCalls, 0)
    }

    @MainActor
    func testDeferredColdLogoutRemainsUnmanagedThroughAppRecoveryAndAccountReopen() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("ColdLogoutAppTests.\(UUID().uuidString)", isDirectory: true)
        let url = directory.appendingPathComponent("ledger.json")
        let initial = FileSystemSystemScheduleStore(fileURL: url)
        let owner = try XCTUnwrap(initial.managementOwner(accountID: "account-1"))
        let request = try XCTUnwrap(SystemScheduleRequest.parse(
            scheduleParams(requestID: "cold-create", operationID: "cold-created", now: now)))
        let platform = RuntimeSystemSchedulePlatform()
        let previous = SystemScheduleCoordinator(platform: platform, store: initial, now: { now })
        let context = SystemScheduleContext(owner: owner, instanceID: "instance-1",
            origin: instance.serialized, runtimeIdentity: 1, navigationEpoch: 1)
        let created = await previous.schedule(request, context: context,
            creationFence: { nil }, postWriteFence: { nil })
        XCTAssertEqual(created.state, .scheduled)
        var failing = true
        let app = try await makeApp(directTransport: nil, systemSchedulePlatform: platform,
            systemScheduleNow: { now },
            systemScheduleCheckpoint: {
                if failing && $0 == .beforeColdOpenSync { throw CocoaError(.fileWriteUnknown) }
            },
            systemScheduleFileURL: url, launchesWithInstanceOneSelected: true)
        try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
            if case .loading = app.route { return false }
            return true
        }
        let store = try XCTUnwrap(appScheduleStore)
        XCTAssertEqual(store.persistenceState, .writeFailed)
        XCTAssertNil(app.systemScheduleDeviceID)
        let signedOut = try await app.mobileSessionCoordinator.logout()
        XCTAssertEqual(signedOut, .signedOut)
        XCTAssertNil(app.mobileSessionCoordinator.currentAccount)
        XCTAssertEqual(store.persistenceState, .writeFailed)
        XCTAssertFalse(store.isActive(owner: owner))
        XCTAssertNil(app.systemScheduleConfirmation)
        XCTAssertNil(app.captureSystemScheduleAdmission(origin: instance))
        XCTAssertEqual(platform.cancelCalls, 0)
        failing = false
        enterForeground()
        try await eventually {
            store.record(operationID: request.operationID,
                         instanceID: request.instanceID, owner: owner)?.lifecycle == .detachedRetained
        }
        _ = try await app.mobileSessionCoordinator.refresh()
        let sameAccountOwner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        XCTAssertNotEqual(sameAccountOwner, owner)
        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        let rejected = try jsonObject(try await page.systemScheduleCancel(
            requestID: "same-account-cancel", operationID: request.operationID, kind: "alarm"))
        XCTAssertEqual((rejected["error"] as? [String: Any])?["code"] as? String, "item_not_found")
        page.close()
        _ = try await app.mobileSessionCoordinator.logout()
        trackStore(of: app)

        let next = try await makeApp(directTransport: nil, sessionResponse: {
            .json("""
                {"account":{"id":"account-2","email":"other@example.com"},"instances":[
                  {"id":"instance-1","name":"One","host":"instance-one.example",
                   "url":"https://instance-one.example","relationship":"owner",
                   "owner_email":"other@example.com","available":true}]}
                """)
        }, systemSchedulePlatform: platform, systemScheduleNow: { now },
            systemScheduleFileURL: url, launchesWithInstanceOneSelected: true,
            beforeTheAppExists: {
                self.appDefaults?.removeObject(forKey: Self.selectionKey)
            })
        try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
            next.mobileSessionCoordinator.currentAccount?.id == "account-2"
        }
        let reopened = try XCTUnwrap(appScheduleStore)
        XCTAssertFalse(reopened.isActive(owner: owner))
        XCTAssertFalse(reopened.isActive(owner: sameAccountOwner))
        let nextRuntime = try await select("instance-1", in: next)
        let nextPage = try await showRuntimePage(next, nextRuntime)
        defer { nextPage.close() }
        let cancel = try jsonObject(try await nextPage.systemScheduleCancel(
            requestID: "new-account-cancel", operationID: request.operationID, kind: "alarm"))
        XCTAssertEqual((cancel["error"] as? [String: Any])?["code"] as? String, "item_not_found")
        let replay = try jsonObject(try await nextPage.systemScheduleRequest(
            scheduleParams(requestID: "new-account-replay", operationID: request.operationID, now: now)))
        XCTAssertEqual(replay["state"] as? String, "scheduled")
        XCTAssertNil(next.systemScheduleConfirmation)
        XCTAssertEqual(reopened.record(operationID: request.operationID,
            instanceID: request.instanceID, owner: owner)?.lifecycle, .detachedRetained)
        XCTAssertEqual(platform.systemCreationCalls, 1)
        XCTAssertEqual(platform.cancelCalls, 0)
    }

    @MainActor
    func testCompletedDeletionWithFailedReadbackAndDetachCannotAuthorizeCachedAccount() async throws {
        // Contract: a confirmed account deletion is not the recoverable
        // failed-signout exception. The existing deletion test covers only
        // a successful fallback; this uses the actual bundled click and
        // native completion navigation while both follow-up paths fail.
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let session = SessionAnswerOverride()
        let platform = RuntimeSystemSchedulePlatform()
        var failing = false
        var failedWrites = 0
        let app = try await makeApp(
            directTransport: nil,
            sessionResponse: { session.response },
            systemSchedulePlatform: platform,
            systemScheduleNow: { now },
            systemScheduleCheckpoint: {
                if failing && $0 == .beforeFileSync {
                    failedWrites += 1
                    throw CocoaError(.fileWriteUnknown)
                }
            }
        )
        let store = try XCTUnwrap(appScheduleStore)
        let oldOwner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        app.showAccountRoot()
        let controller = MobileOnboardingViewController(
            coordinator: app.mobileSessionCoordinator,
            startScreen: .account
        )
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 844))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        defer {
            window.isHidden = true
            window.rootViewController = nil
        }
        controller.loadViewIfNeeded()
        let webView = try XCTUnwrap(controller.view.subviews.compactMap { $0 as? WKWebView }.first)
        try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
            (try? await webView.evaluateJavaScript(
                "Array.from(document.querySelectorAll('button')).some(b => b.textContent.trim() === 'Account and data' && !b.disabled)"
            ) as? Bool) == true
        }
        let openedAccountData = try await webView.evaluateJavaScript("""
            (() => {
              const button = Array.from(document.querySelectorAll('button'))
                .find(b => b.textContent.trim() === 'Account and data' && !b.disabled);
              button?.click();
              return Boolean(button);
            })()
            """) as? Bool
        XCTAssertEqual(openedAccountData, true)
        try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
            (try? await webView.evaluateJavaScript(
                "Array.from(document.querySelectorAll('button')).some(b => b.textContent.trim() === 'Delete account' && !b.disabled)"
            ) as? Bool) == true
        }
        let clicked = try await webView.evaluateJavaScript("""
            (() => {
              const button = Array.from(document.querySelectorAll('button'))
                .find(b => b.textContent.trim() === 'Delete account' && !b.disabled);
              button?.click();
              return Boolean(button);
            })()
            """) as? Bool
        XCTAssertEqual(clicked, true)
        try await eventually { controller.presentedViewController != nil }
        let deletion = try XCTUnwrap(
            (controller.presentedViewController as? UINavigationController)?
                .topViewController as? AvibePageViewController
        )
        session.replace(with: .status(500))
        failing = true
        // Simulate only the server's completion navigation, through the
        // real native deletion-page policy. No real account is deleted.
        deletion.webView.load(URLRequest(
            url: app.mobileSessionCoordinator.controlPlaneURL
                .appendingPathComponent("account/delete/done")
        ))
        try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
            failedWrites > 0 && controller.presentedViewController == nil
        }
        try await eventually {
            (try? await webView.evaluateJavaScript(
                """
                Array.from(document.querySelectorAll('button')).some(b =>
                  ['Delete account', 'Sign in / Create account'].includes(b.textContent.trim()) && !b.disabled)
                """
            ) as? Bool) == true
        }
        XCTAssertFalse(store.isActive(owner: oldOwner))
        XCTAssertNil(app.mobileSessionCoordinator.currentAccount)
        print("[deletion boundary] completion=true readback=500 detach=write_failed cachedAccount=\(app.mobileSessionCoordinator.currentAccount?.id ?? "nil")")
        failing = false
        let selected = try await webView.callAsyncJavaScript(
            """
            try {
              await window.avibeMobile.call('session.selectInstance', {id: 'instance-1'});
              return 'selected';
            } catch (error) { return error.code; }
            """,
            arguments: [:], in: nil, contentWorld: .page
        ) as? String
        print("[deletion boundary] reopen_without_new_auth=\(selected ?? "nil")")
        XCTAssertNotEqual(selected, "selected")
        if case .browser(let runtime) = app.route {
            let page = try await showRuntimePage(app, runtime)
            defer { page.close() }
            let admission = app.captureSystemScheduleAdmission(origin: instance)
            if case .authorized(let context) = admission {
                print("[deletion boundary] authorizedCachedAccount=\(context.owner.accountID) oldEpoch=\(context.owner == oldOwner) persistence=\(store.persistenceState)")
            }
            XCTAssertNil(admission, "Confirmed deletion must not recover schedule authority from the cached account without authentication.")
        } else {
            XCTAssertNotEqual(selected, "selected")
        }
        XCTAssertEqual(platform.systemCreationCalls, 0)
        XCTAssertEqual(platform.cancelCalls, 0)
    }

    @MainActor
    func testDetachReopenRequiresRealAuthenticationAcrossEveryRestartOutcome() async throws {
        // Distinct from the core/store test: every reconstructed AppModel
        // goes through real MobileAuthTransport discovery, never an injected
        // accountID. This expands the original interrupted-logout regression.
        // Route B accepts loss of uncommitted process-local intent, followed
        // by verified same-account authentication. The original stronger-
        // contract red assertions/log remain preserved in the matter.
        for durable in [false, true] {
            for authentication in ["same", "different", "signedOut", "secondFactor", "network"] {
                let label = "\(durable ? "durable" : "not-durable")/\(authentication)"
                let now = Date(timeIntervalSince1970: 1_800_000_000)
                let url = FileManager.default.temporaryDirectory
                    .appendingPathComponent("DetachAuthenticationTests.\(UUID().uuidString)", isDirectory: true)
                    .appendingPathComponent("ledger.json")
                let seed = FileSystemSystemScheduleStore(fileURL: url)
                let owner = try XCTUnwrap(seed.managementOwner(accountID: "account-1"))
                let request = try XCTUnwrap(SystemScheduleRequest.parse(
                    scheduleParams(requestID: "seed", operationID: "retained", now: now)))
                let platform = RuntimeSystemSchedulePlatform()
                let previous = SystemScheduleCoordinator(platform: platform, store: seed, now: { now })
                let created = await previous.schedule(request,
                    context: SystemScheduleContext(owner: owner, instanceID: "instance-1",
                        origin: instance.serialized, runtimeIdentity: 1, navigationEpoch: 1),
                    creationFence: { nil }, postWriteFence: { nil })
                XCTAssertEqual(created.state, .scheduled, label)
                let departing = try await makeApp(directTransport: nil,
                    systemSchedulePlatform: platform, systemScheduleNow: { now },
                    systemScheduleCheckpoint: {
                        if !durable && $0 == .beforeColdOpenSync { throw CocoaError(.fileWriteUnknown) }
                    }, systemScheduleFileURL: url, launchesWithInstanceOneSelected: true)
                try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
                    if case .loading = departing.route { return false }
                    return true
                }
                let logout = try await departing.mobileSessionCoordinator.logout()
                XCTAssertEqual(logout, .signedOut, label)
                XCTAssertNil(departing.mobileSessionCoordinator.currentAccount, label)
                XCTAssertFalse(try XCTUnwrap(appScheduleStore).isActive(owner: owner), label)
                let answers = SessionAnswerOverride()
                switch authentication {
                case "different":
                    answers.replace(with: .json("""
                        {"account":{"id":"account-2","email":"other@example.com"},"instances":[
                        {"id":"instance-1","name":"One","host":"instance-one.example",
                         "url":"https://instance-one.example","relationship":"owner",
                         "owner_email":"other@example.com","available":true}]}
                        """))
                case "signedOut": answers.replace(with: .status(401))
                case "secondFactor":
                    answers.replace(with: LocalHTTPResponse(status: 403,
                        headers: [("Content-Type", "application/json")],
                        body: Data("""
                            {"error":"two_factor_required","email":"max@example.com",
                             "gate":"challenge","methods":["totp"]}
                            """.utf8)))
                case "network": answers.replace(with: .status(500))
                default: break
                }
                let reopened = try await makeApp(directTransport: nil,
                    sessionResponse: { answers.response }, systemSchedulePlatform: platform,
                    systemScheduleNow: { now }, systemScheduleFileURL: url,
                    launchesWithInstanceOneSelected: true, beforeTheAppExists: {
                        self.appDefaults?.removeObject(forKey: Self.selectionKey)
                    })
                try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
                    if case .loading = reopened.route { return false }
                    return true
                }
                let store = try XCTUnwrap(appScheduleStore)
                if authentication == "same" || authentication == "different" {
                    XCTAssertEqual(reopened.mobileSessionCoordinator.currentAccount?.id,
                        authentication == "same" ? "account-1" : "account-2", label)
                    let runtime = try await select("instance-1", in: reopened)
                    let page = try await showRuntimePage(reopened, runtime)
                    let admission = try XCTUnwrap(reopened.captureSystemScheduleAdmission(origin: instance), label)
                    if case .authorized(let context) = admission {
                        XCTAssertEqual(context.owner == owner, !durable && authentication == "same", label)
                    } else {
                        XCTFail("Authenticated recovered runtime must be admitted: \(label)")
                    }
                    page.close()
                } else {
                    XCTAssertNil(reopened.mobileSessionCoordinator.currentAccount, label)
                    XCTAssertThrowsError(try reopened.mobileSessionCoordinator.selectInstance(id: "instance-1"), label)
                    XCTAssertNil(reopened.captureSystemScheduleAdmission(origin: instance), label)
                }
                let retained = durable || authentication == "different"
                XCTAssertEqual(store.record(operationID: request.operationID,
                    instanceID: request.instanceID, owner: owner)?.lifecycle,
                    retained ? .detachedRetained : .managedScheduled, label)
                XCTAssertEqual(platform.cancelCalls, 0, label)
                XCTAssertEqual(platform.systemCreationCalls, 1, label)
                print("[detach authentication] \(label) account=\(reopened.mobileSessionCoordinator.currentAccount?.id ?? "nil") oldActive=\(store.isActive(owner: owner))")
            }
        }
    }

    @MainActor
    func testActualAccountAndSheetSignoutFinishWhileScheduleWriteFails() async throws {
        // Real RootView routing and rebuilt bundled button clicks, including
        // replacement of the sheet during immediate Runtime revocation.
        for sheet in [false, true] {
            var failing = false
            let platform = RuntimeSystemSchedulePlatform()
            let app = try await makeApp(directTransport: nil, systemSchedulePlatform: platform,
                systemScheduleCheckpoint: {
                    if failing && $0 == .beforeFileSync { throw CocoaError(.fileWriteUnknown) }
                })
            let store = try XCTUnwrap(appScheduleStore)
            let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
            if sheet { _ = try await select("instance-1", in: app) }
            let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
            let window = UIWindow(windowScene: scene)
            window.frame = scene.screen.bounds
            window.rootViewController = UIHostingController(rootView: RootView().environmentObject(app))
            window.makeKeyAndVisible()
            defer { window.isHidden = true; window.rootViewController = nil }
            app.showAccountRoot()
            var account: WKWebView?
            try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
                for page in self.bundledWebViews(in: window) {
                    if (try? await page.evaluateJavaScript(
                        "Array.from(document.querySelectorAll('button')).some(b => b.textContent.trim() === 'Sign out on this device' && !b.disabled)"
                    ) as? Bool) == true {
                        account = page
                        return true
                    }
                }
                return false
            }
            failing = true
            _ = try await XCTUnwrap(account).evaluateJavaScript("""
                Array.from(document.querySelectorAll('button'))
                  .find(b => b.textContent.trim() === 'Sign out on this device').click()
                """)
            try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
                for page in self.bundledWebViews(in: window) {
                    if (try? await page.evaluateJavaScript(
                        "document.body.textContent.includes('Sign in / Create account')"
                    ) as? Bool) == true { return true }
                }
                return false
            }
            trackStore(of: app)
            XCTAssertNil(app.mobileSessionCoordinator.currentAccount)
            XCTAssertNil(app.route.runtime)
            XCTAssertNil(app.systemScheduleConfirmation)
            XCTAssertFalse(store.isActive(owner: owner))
            XCTAssertNil(store.managementOwner(accountID: "account-1"))
            XCTAssertNil(store.managementOwner(accountID: "account-2"))
            XCTAssertEqual(store.persistenceState, .writeFailed)
            failing = false
            XCTAssertTrue(store.retryCommit())
            XCTAssertFalse(store.isActive(owner: owner))
            XCTAssertEqual(platform.cancelCalls, 0)
            print("[account signout click] sheet=\(sheet) welcome=true account=nil detachRecovered=true")
        }
    }

    @MainActor
    func testBundledColdFaultShowsLocalRecordsAndRetryRecoversDiscovery() async throws {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("LocalRecordsPresentationTests.\(UUID().uuidString)", isDirectory: true)
            .appendingPathComponent("ledger.json")
        let seed = FileSystemSystemScheduleStore(fileURL: url)
        _ = try XCTUnwrap(seed.managementOwner(accountID: "account-1"))
        var failing = true
        let app = try await makeApp(directTransport: nil, systemScheduleCheckpoint: {
            if failing && $0 == .beforeColdOpenSync { throw CocoaError(.fileWriteUnknown) }
        }, systemScheduleFileURL: url, launchesWithInstanceOneSelected: true)
        try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
            if case .loading = app.route { return false }
            return true
        }
        let controller = MobileOnboardingViewController(coordinator: app.mobileSessionCoordinator)
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 844))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        defer { window.isHidden = true; window.rootViewController = nil }
        controller.loadViewIfNeeded()
        let webView = try XCTUnwrap(controller.view.subviews.compactMap { $0 as? WKWebView }.first)
        let message = "Avibe couldn't read or save alarm and reminder records on this iPhone. Try again."
        func showsLocalError() async -> Bool {
            (try? await webView.callAsyncJavaScript(
                "return document.body.textContent.includes(message);",
                arguments: ["message": message], in: nil, contentWorld: .page) as? Bool) == true
        }
        try await eventually(timeout: WebKitReadiness.standardTransportOperation) { await showsLocalError() }
        let retry = """
            Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === 'Retry').click()
            """
        _ = try await webView.evaluateJavaScript(retry)
        try await eventually(timeout: WebKitReadiness.standardTransportOperation) { await showsLocalError() }
        XCTAssertNil(app.mobileSessionCoordinator.currentAccount)
        XCTAssertNil(app.captureSystemScheduleAdmission(origin: instance))
        failing = false
        _ = try await webView.evaluateJavaScript(retry)
        try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
            let hasInstance = (try? await webView.evaluateJavaScript(
                "document.body.textContent.includes('One')") as? Bool) == true
            return app.mobileSessionCoordinator.currentAccount?.id == "account-1" && hasInstance
        }
        let remainsInError = await showsLocalError()
        XCTAssertFalse(remainsInError)
    }

    @MainActor
    private func bundledWebViews(in view: UIView) -> [WKWebView] {
        let own = (view as? WKWebView).map { [$0] } ?? []
        return own.filter { MobileBundleSchemeHandler.isTrustedBundleURL($0.url) }
            + view.subviews.flatMap { bundledWebViews(in: $0) }
    }

    @MainActor
    func testColdBootstrapFailurePreservesScheduleOwnerAndManagedRecord() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let request = try XCTUnwrap(
            SystemScheduleRequest.parse(
                scheduleParams(
                    requestID: "request-bootstrap-failure",
                    operationID: "operation-bootstrap-failure",
                    now: now
                )
            )
        )
        var owner: SystemScheduleManagementOwner?
        var store: FileSystemSystemScheduleStore?
        let app = try await makeApp(
            directTransport: nil,
            sessionResponse: { .status(500) },
            systemScheduleNow: { now },
            launchesWithInstanceOneSelected: true,
            whenTheAppExists: { _ in
                guard let seeded = self.appScheduleStore else {
                    XCTFail("App schedule store was not created before bootstrap.")
                    return
                }
                guard let seededOwner = seeded.managementOwner(
                    accountID: "account-1"
                ) else {
                    XCTFail("Could not seed the schedule management owner.")
                    return
                }
                XCTAssertTrue(
                    seeded.save(
                        StoredSystemSchedule(
                            operationID: request.operationID,
                            owner: seededOwner,
                            intent: StoredSystemScheduleIntent(request: request),
                            systemIdentifier: "system-1",
                            authorization: .authorized,
                            lifecycle: .managedScheduled,
                            effectMayExist: true,
                            generation: 2,
                            createState: .scheduled,
                            lastError: nil,
                            updatedAt: now
                        )
                    )
                )
                owner = seededOwner
                store = seeded
            }
        )
        try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
            if case .loading = app.route { return false }
            return true
        }

        let persistedOwner = try XCTUnwrap(owner)
        let persistedStore = try XCTUnwrap(store)
        XCTAssertNil(app.mobileSessionCoordinator.currentAccount)
        XCTAssertTrue(isOnInstancePicker(app))
        XCTAssertTrue(persistedStore.isActive(owner: persistedOwner))
        XCTAssertEqual(
            persistedStore.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: persistedOwner
            )?.lifecycle,
            .managedScheduled
        )
    }

    @MainActor
    func testNativeCleanupRetriesOnLaunchForegroundAndBridgeClaims() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let platform = RuntimeSystemSchedulePlatform()
        let launchRequest = try XCTUnwrap(
            SystemScheduleRequest.parse(
                scheduleParams(
                    requestID: "request-launch-cleanup",
                    operationID: "operation-launch-cleanup",
                    now: now
                )
            )
        )
        let retainedRequest = try XCTUnwrap(
            SystemScheduleRequest.parse(
                scheduleParams(
                    requestID: "request-retained",
                    operationID: "operation-retained",
                    now: now
                )
            )
        )
        var seededOwner: SystemScheduleManagementOwner?
        let app = try await makeApp(
            directTransport: nil,
            systemSchedulePlatform: platform,
            systemScheduleNow: { now },
            beforeTheAppExists: { [self] in
                let store = try XCTUnwrap(appScheduleStore)
                let owner = try XCTUnwrap(
                    store.managementOwner(accountID: "account-1")
                )
                seededOwner = owner
                XCTAssertTrue(
                    store.save(
                        cleanupPendingRecord(
                            request: launchRequest,
                            owner: owner,
                            systemIdentifier: "system-launch",
                            updatedAt: now
                        )
                    )
                )
                XCTAssertTrue(
                    store.save(
                        StoredSystemSchedule(
                            operationID: retainedRequest.operationID,
                            owner: owner,
                            intent: StoredSystemScheduleIntent(
                                request: retainedRequest
                            ),
                            systemIdentifier: "system-retained",
                            authorization: .authorized,
                            lifecycle: .detachedRetained,
                            effectMayExist: true,
                            generation: 1,
                            createState: .scheduled,
                            lastError: nil,
                            updatedAt: now
                        )
                    )
                )
            }
        )
        let store = try XCTUnwrap(appScheduleStore)
        let owner = try XCTUnwrap(seededOwner)

        // E2: local cleanup, not a Runtime compensation generation, owns
        // crash recovery on launch and must never touch retained items.
        try await eventually { platform.cancelCalls == 1 }
        XCTAssertEqual(
            store.record(
                operationID: launchRequest.operationID,
                instanceID: launchRequest.instanceID,
                owner: owner
            )?.lifecycle,
            .closedRemoved
        )
        XCTAssertEqual(
            store.record(
                operationID: retainedRequest.operationID,
                instanceID: retainedRequest.instanceID,
                owner: owner
            )?.lifecycle,
            .detachedRetained
        )

        let foregroundRequest = try XCTUnwrap(
            SystemScheduleRequest.parse(
                scheduleParams(
                    requestID: "request-foreground-cleanup",
                    operationID: "operation-foreground-cleanup",
                    now: now
                )
            )
        )
        XCTAssertTrue(
            store.save(
                cleanupPendingRecord(
                    request: foregroundRequest,
                    owner: owner,
                    systemIdentifier: "system-foreground",
                    updatedAt: now.addingTimeInterval(1)
                )
            )
        )
        enterBackground()
        enterForeground()
        try await eventually { platform.cancelCalls == 2 }
        XCTAssertEqual(
            store.record(
                operationID: foregroundRequest.operationID,
                instanceID: foregroundRequest.instanceID,
                owner: owner
            )?.lifecycle,
            .closedRemoved
        )

        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        defer { page.close() }
        let bridgeRequest = try XCTUnwrap(
            SystemScheduleRequest.parse(
                scheduleParams(
                    requestID: "request-bridge-cleanup",
                    operationID: "operation-bridge-cleanup",
                    now: now
                )
            )
        )
        XCTAssertTrue(
            store.save(
                cleanupPendingRecord(
                    request: bridgeRequest,
                    owner: owner,
                    systemIdentifier: "system-bridge",
                    updatedAt: now.addingTimeInterval(2)
                )
            )
        )
        let unknownCancel = try jsonObject(
            try await page.systemScheduleCancel(
                requestID: "cancel-unknown-after-cleanup",
                operationID: "operation-unknown",
                kind: "alarm"
            )
        )
        XCTAssertEqual(unknownCancel["state"] as? String, "failed")
        try await eventually { platform.cancelCalls == 3 }
        XCTAssertEqual(
            store.record(
                operationID: bridgeRequest.operationID,
                instanceID: bridgeRequest.instanceID,
                owner: owner
            )?.lifecycle,
            .closedRemoved
        )
        XCTAssertEqual(
            store.record(
                operationID: retainedRequest.operationID,
                instanceID: retainedRequest.instanceID,
                owner: owner
            )?.lifecycle,
            .detachedRetained
        )
    }

    @MainActor
    func testPendingScheduleRetryUsesOperationIntentInsteadOfRequestID() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let platform = RuntimeSystemSchedulePlatform()
        let app = try await makeApp(
            directTransport: nil,
            systemSchedulePlatform: platform,
            systemScheduleNow: { now }
        )
        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        defer { page.close() }

        let first = Task { @MainActor in
            try await page.systemScheduleRequest(
                scheduleParams(
                    requestID: "request-original",
                    operationID: "operation-retry",
                    now: now
                )
            )
        }
        try await eventually { app.systemScheduleConfirmation != nil }
        let retry = Task { @MainActor in
            try await page.systemScheduleRequest(
                scheduleParams(
                    requestID: "request-retry",
                    operationID: "operation-retry",
                    now: now
                )
            )
        }
        try await settle()
        XCTAssertEqual(platform.scheduleCalls, 0)
        XCTAssertEqual(
            app.systemScheduleConfirmation?.request.operationID,
            "operation-retry"
        )

        let confirmation = try XCTUnwrap(app.systemScheduleConfirmation)
        app.denySystemSchedule(id: confirmation.id)
        let firstResult = try jsonObject(try await first.value)
        let retryResult = try jsonObject(try await retry.value)
        XCTAssertEqual(firstResult["state"] as? String, "user_denied")
        XCTAssertEqual(retryResult["requestId"] as? String, "request-retry")
        XCTAssertEqual(retryResult["state"] as? String, "user_denied")
    }

    @MainActor
    func testRuntimeCancelClosesPendingAndSubmittedConfirmation() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let platform = RuntimeSystemSchedulePlatform()
        let app = try await makeApp(
            directTransport: nil,
            systemSchedulePlatform: platform,
            systemScheduleNow: { now }
        )
        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        defer {
            platform.releasePermission()
            page.close()
        }

        let pendingCreate = Task { @MainActor in
            try await page.systemScheduleRequest(
                scheduleParams(
                    requestID: "request-pending-cancel",
                    operationID: "operation-pending-cancel",
                    now: now
                )
            )
        }
        try await eventually { app.systemScheduleConfirmation != nil }
        let pendingCancel = try jsonObject(
            try await page.systemScheduleCancel(
                requestID: "cancel-pending",
                operationID: "operation-pending-cancel",
                kind: "alarm"
            )
        )
        XCTAssertEqual(pendingCancel["state"] as? String, "cancelled")
        let pendingCreateResult = try jsonObject(try await pendingCreate.value)
        XCTAssertEqual(pendingCreateResult["state"] as? String, "cancelled")
        XCTAssertNil(app.systemScheduleConfirmation)
        XCTAssertEqual(platform.scheduleCalls, 0)

        platform.holdBeforeSystemCreation = true
        let submittedCreate = Task { @MainActor in
            try await page.systemScheduleRequest(
                scheduleParams(
                    requestID: "request-submitted-cancel",
                    operationID: "operation-submitted-cancel",
                    now: now
                )
            )
        }
        try await eventually { app.systemScheduleConfirmation != nil }
        let submittedConfirmation = try XCTUnwrap(
            app.systemScheduleConfirmation
        )
        app.confirmSystemSchedule(id: submittedConfirmation.id)
        try await eventually { platform.isWaitingForPermission }
        let submittedCancel = Task { @MainActor in
            try await page.systemScheduleCancel(
                requestID: "cancel-submitted",
                operationID: "operation-submitted-cancel",
                kind: "alarm"
            )
        }
        try await eventually { app.systemScheduleConfirmation == nil }
        platform.releasePermission()
        let submittedCreateResult = try jsonObject(try await submittedCreate.value)
        let submittedCancelResult = try jsonObject(try await submittedCancel.value)
        XCTAssertEqual(submittedCreateResult["state"] as? String, "cancelled")
        XCTAssertEqual(submittedCancelResult["state"] as? String, "cancelled")
        XCTAssertEqual(platform.systemCreationCalls, 0)
    }

    @MainActor
    func testCrossOperationCommitFailureCannotSwallowCapturedOrBridgeCancelControl() async throws {
        for route in ["captured", "bridge"] {
            let now = Date(timeIntervalSince1970: 1_800_000_000)
            var commits = 0
            var failAt = Int.max
            let platform = RuntimeSystemSchedulePlatform()
            platform.holdBeforeSystemCreation = true
            let app = try await makeApp(directTransport: nil, systemSchedulePlatform: platform,
                systemScheduleNow: { now }, systemScheduleCheckpoint: {
                    if $0 == .beforeDirectorySync {
                        commits += 1
                        if commits >= failAt { throw CocoaError(.fileWriteUnknown) }
                    }
                })
            let runtime = try await select("instance-1", in: app)
            let page = try await showRuntimePage(app, runtime)
            defer { platform.releasePermission(); page.close() }
            let origin = try XCTUnwrap(WebOrigin(url: runtime.instance.url))
            let admission = try XCTUnwrap(app.captureSystemScheduleAdmission(origin: origin))
            guard case .authorized(let context) = admission else {
                return XCTFail("fixture must capture the actual document authority")
            }
            let create = Task { @MainActor in
                try await page.systemScheduleRequest(scheduleParams(
                    requestID: "create-A", operationID: "operation-A", now: now))
            }
            try await eventually { app.systemScheduleConfirmation != nil }
            app.confirmSystemSchedule(id: try XCTUnwrap(app.systemScheduleConfirmation).id)
            try await eventually { platform.isWaitingForPermission }
            let otherRequest = try XCTUnwrap(SystemScheduleRequest.parse(scheduleParams(
                requestID: "create-B", operationID: "operation-B", now: now)))
            var otherRecord = cleanupPendingRecord(request: otherRequest, owner: context.owner,
                systemIdentifier: "system-B", updatedAt: now)
            otherRecord.lifecycle = .managedScheduled
            otherRecord.createState = .scheduled
            XCTAssertTrue(try XCTUnwrap(appScheduleStore).save(otherRecord))
            // B's cleanup intent commits; removal succeeds; its terminal
            // rename becomes visible but every following directory sync fails.
            failAt = commits + 2
            let otherCancel = await app.cancelSystemSchedule(.init(requestID: "cancel-B",
                operationID: "operation-B", kind: .alarm, purpose: .user), admission: admission)
            XCTAssertEqual((otherCancel?["error"] as? [String: Any])?["code"] as? String,
                           "local_state_write_failed")
            XCTAssertEqual(platform.cancelIdentifiers, ["system-B"])
            let stale = SystemScheduleContext(owner: context.owner, instanceID: context.instanceID,
                origin: context.origin, runtimeIdentity: context.runtimeIdentity,
                navigationEpoch: context.navigationEpoch + 1, webViewIdentity: context.webViewIdentity)
            let rejected = await app.cancelSystemSchedule(.init(requestID: "stale-cancel",
                operationID: "operation-A", kind: .alarm, purpose: .user), admission: .authorized(stale))
            XCTAssertEqual((rejected?["error"] as? [String: Any])?["code"] as? String,
                           "local_state_write_failed")
            XCTAssertEqual(platform.systemCreationCalls, 0)
            XCTAssertNotNil(app.systemScheduleConfirmation)
            XCTAssertNil(app.captureSystemScheduleAdmission(
                origin: try XCTUnwrap(WebOrigin.parseConfigured("https://untrusted.example"))))
            var cancelReturned = false
            let cancel = Task { @MainActor () throws -> [String: Any] in
                let receipt: [String: Any]
                if route == "captured" {
                    let result = await app.cancelSystemSchedule(.init(requestID: "cancel-A",
                        operationID: "operation-A", kind: .alarm, purpose: .user), admission: admission)
                    receipt = try XCTUnwrap(result)
                } else {
                    receipt = try jsonObject(try await page.systemScheduleCancel(
                        requestID: "cancel-A", operationID: "operation-A", kind: "alarm"))
                }
                cancelReturned = true
                return receipt
            }
            try await eventually { cancelReturned || app.systemScheduleConfirmation == nil }
            XCTAssertNil(app.systemScheduleConfirmation, route)
            platform.releasePermission()
            let createReceipt = try jsonObject(try await create.value)
            let cancelReceipt = try await cancel.value
            XCTAssertEqual(createReceipt["requestId"] as? String, "create-A")
            XCTAssertEqual(cancelReceipt["requestId"] as? String, "cancel-A")
            for receipt in [createReceipt, cancelReceipt] {
                XCTAssertEqual((receipt["error"] as? [String: Any])?["code"] as? String,
                               "local_state_write_failed", route)
                XCTAssertEqual(receipt["systemIdentifier"] as? String, "system-1")
            }
            XCTAssertEqual(platform.systemCreationCalls, 0, route)
            XCTAssertEqual(platform.cancelIdentifiers, ["system-B"], route)
            failAt = Int.max
            let recovered = await app.cancelSystemSchedule(.init(requestID: "recovered-cancel",
                operationID: "operation-A", kind: .alarm, purpose: .user), admission: admission)
            XCTAssertEqual(recovered?["state"] as? String, "cancelled")
            XCTAssertEqual(platform.systemCreationCalls, 0)
        }
    }

    @MainActor
    func testTwoBridgeCancelsDismissInFlightConfirmationBeforeOSCompletion() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let platform = RuntimeSystemSchedulePlatform()
        platform.holdAfterSystemCreation = true
        let entered = expectation(description: "OS create issued")
        platform.onCreationSuspended = { entered.fulfill() }
        let app = try await makeApp(directTransport: nil, systemSchedulePlatform: platform,
                                    systemScheduleNow: { now })
        let runtime = try await select("instance-1", in: app)
        let cancelsAdmitted = expectation(description: "two bridge cancel deliveries")
        cancelsAdmitted.expectedFulfillmentCount = 2
        let controller = try makeRuntimeController(app, runtime, onScheduleDelivery: { _ in
            if platform.isWaitingAfterSystemCreation { cancelsAdmitted.fulfill() }
        })
        app.registerRuntimeController(controller, for: runtime)
        let page = try RuntimePage(controller: controller)
        defer { platform.releaseCreationResult(); page.close() }
        try await page.load(RuntimePage.recordingDocument, baseURL: runtime.instance.url)
        let create = Task { @MainActor in
            try await page.systemScheduleRequest(scheduleParams(
                requestID: "create-two-cancels", operationID: "two-cancels", now: now))
        }
        try await eventually { app.systemScheduleConfirmation != nil }
        app.confirmSystemSchedule(id: try XCTUnwrap(app.systemScheduleConfirmation).id)
        await fulfillment(of: [entered], timeout: 3)
        var completed: [String] = []
        let cancels = ["cancel-one", "cancel-two"].map { id in
            Task { @MainActor in
                let result = try await page.systemScheduleCancel(requestID: id, operationID: "two-cancels", kind: "alarm")
                completed.append(id)
                return result
            }
        }
        await fulfillment(of: [cancelsAdmitted], timeout: 3)
        XCTAssertNil(app.systemScheduleConfirmation)
        XCTAssertTrue(completed.isEmpty)
        XCTAssertEqual(platform.cancelCalls, 0)
        platform.releaseCreationResult()
        let created = try jsonObject(try await create.value)
        XCTAssertEqual(created["state"] as? String, "cancelled")
        for (index, cancel) in cancels.enumerated() {
            let receipt = try jsonObject(try await cancel.value)
            XCTAssertEqual(receipt["requestId"] as? String, ["cancel-one", "cancel-two"][index])
            XCTAssertEqual(receipt["state"] as? String, "cancelled")
        }
        XCTAssertEqual(completed.count, 2)
        XCTAssertEqual(Set(completed), ["cancel-one", "cancel-two"])
        XCTAssertEqual(platform.cancelCalls, 1)
        XCTAssertEqual(platform.systemCreationCalls, 1)
    }

    @MainActor
    func testCancelAfterSystemWriteReportsCleanupFailureIndependently() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let platform = RuntimeSystemSchedulePlatform()
        platform.holdAfterSystemCreation = true
        platform.cancelResult = .failure(
            .failed(
                code: "cleanup_failed",
                message: "Cleanup failed.",
                systemIdentifier: "system-1",
                effectMayExist: true
            )
        )
        let app = try await makeApp(
            directTransport: nil,
            systemSchedulePlatform: platform,
            systemScheduleNow: { now }
        )
        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        defer {
            platform.releaseCreationResult()
            page.close()
        }

        let create = Task { @MainActor in
            try await page.systemScheduleRequest(
                scheduleParams(
                    requestID: "request-cleanup-failure",
                    operationID: "operation-cleanup-failure",
                    now: now
                )
            )
        }
        try await eventually { app.systemScheduleConfirmation != nil }
        let confirmation = try XCTUnwrap(app.systemScheduleConfirmation)
        app.confirmSystemSchedule(id: confirmation.id)
        try await eventually { platform.isWaitingAfterSystemCreation }

        let cancel = Task { @MainActor in
            try await page.systemScheduleCancel(
                requestID: "cancel-cleanup-failure",
                operationID: "operation-cleanup-failure",
                kind: "alarm"
            )
        }
        try await eventually { app.systemScheduleConfirmation == nil }
        platform.releaseCreationResult()

        let createResult = try jsonObject(try await create.value)
        let cancelResult = try jsonObject(try await cancel.value)
        XCTAssertEqual(createResult["state"] as? String, "cancelled")
        XCTAssertEqual(cancelResult["state"] as? String, "failed")
        XCTAssertEqual(
            (cancelResult["error"] as? [String: Any])?["code"] as? String,
            "cleanup_failed"
        )
        XCTAssertEqual(cancelResult["systemIdentifier"] as? String, "system-1")
        XCTAssertEqual(platform.systemCreationCalls, 1)
        XCTAssertEqual(platform.cancelCalls, 1)
    }

    @MainActor
    func testScheduleDenialAndExpiredConfirmationNeverCallTheSystem() async throws {
        var now = Date(timeIntervalSince1970: 1_800_000_000)
        let platform = RuntimeSystemSchedulePlatform()
        let app = try await makeApp(
            directTransport: nil,
            systemSchedulePlatform: platform,
            systemScheduleNow: { now }
        )
        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        defer { page.close() }

        let deniedRequest = Task { @MainActor in
            try await page.systemScheduleRequest(
                scheduleParams(
                    requestID: "request-denied",
                    operationID: "operation-denied",
                    now: now
                )
            )
        }
        try await eventually { app.systemScheduleConfirmation != nil }
        let deniedConfirmation = try XCTUnwrap(
            app.systemScheduleConfirmation
        )
        app.denySystemSchedule(id: deniedConfirmation.id)
        let denied = try jsonObject(try await deniedRequest.value)
        XCTAssertEqual(denied["state"] as? String, "user_denied")
        XCTAssertEqual(platform.scheduleCalls, 0)

        let expiredRequest = Task { @MainActor in
            try await page.systemScheduleRequest(
                scheduleParams(
                    requestID: "request-expired",
                    operationID: "operation-expired",
                    now: now
                )
            )
        }
        try await eventually {
            app.systemScheduleConfirmation?.request.operationID
                == "operation-expired"
        }
        let expiredConfirmation = try XCTUnwrap(
            app.systemScheduleConfirmation
        )
        now = now.addingTimeInterval(301)
        app.confirmSystemSchedule(id: expiredConfirmation.id)
        let expired = try jsonObject(try await expiredRequest.value)
        XCTAssertEqual(expired["state"] as? String, "expired")
        XCTAssertEqual(platform.scheduleCalls, 0)
    }

    @MainActor
    func testEverySynchronousTerminalKeepsWriteFailureThroughAppModelRetryAndCancel() async throws {
        for scenario in ["denial", "entry-expiry", "entry-fire", "timer", "confirm-expiry", "confirm-fire", "confirm-stale", "invalidation"] {
            let initialNow = Date(timeIntervalSince1970: 1_800_000_000)
            var now = initialNow
            var commits = 0
            var failFromCommit = Int.max
            let platform = RuntimeSystemSchedulePlatform()
            let app = try await makeApp(directTransport: nil, systemSchedulePlatform: platform,
                systemScheduleNow: { now }, systemScheduleCheckpoint: { phase in
                    if phase == .beforeDirectorySync {
                        commits += 1
                        if commits >= failFromCommit { throw CocoaError(.fileWriteUnknown) }
                    }
                })
            let runtime = try await select("instance-1", in: app)
            let page = try await showRuntimePage(app, runtime)
            defer { page.close() }
            let origin = try XCTUnwrap(WebOrigin(url: runtime.instance.url))
            let admission = try XCTUnwrap(app.captureSystemScheduleAdmission(origin: origin))
            let identifier = try XCTUnwrap(app.systemScheduleDeviceID)
            var params = scheduleParams(requestID: "first-\(scenario)", operationID: "terminal-\(scenario)", now: now)
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if scenario == "entry-expiry" { params["expiresAt"] = formatter.string(from: now.addingTimeInterval(-1)) }
            if scenario == "entry-fire" { params["fireAt"] = formatter.string(from: now.addingTimeInterval(-1)) }
            if scenario == "timer" { params["expiresAt"] = formatter.string(from: now.addingTimeInterval(0.1)) }
            if scenario == "confirm-fire" { params["fireAt"] = formatter.string(from: now.addingTimeInterval(1)) }
            let original = try XCTUnwrap(SystemScheduleRequest.parse(params))
            // Reservation succeeds; its following terminal write becomes
            // visible but unacknowledged. Fail every subsequent commit too.
            if scenario.hasPrefix("entry-") || scenario == "timer" {
                failFromCommit = commits + 2
            }
            let request = Task { @MainActor in
                await app.requestSystemSchedule(original, admission: admission)
            }
            if !scenario.hasPrefix("entry-") && scenario != "timer" {
                try await eventually { app.systemScheduleConfirmation != nil }
                let confirmation = try XCTUnwrap(app.systemScheduleConfirmation)
                failFromCommit = commits + 1
                switch scenario {
                case "denial": app.denySystemSchedule(id: confirmation.id)
                case "confirm-expiry":
                    now = now.addingTimeInterval(301)
                    app.confirmSystemSchedule(id: confirmation.id)
                case "confirm-fire":
                    now = now.addingTimeInterval(2)
                    app.confirmSystemSchedule(id: confirmation.id)
                case "confirm-stale":
                    try page.beginPrimaryProvisionalNavigation()
                    app.confirmSystemSchedule(id: confirmation.id)
                default: app.showAccount()
                }
            }
            let first = await request.value
            XCTAssertEqual((first["error"] as? [String: Any])?["code"] as? String,
                           "local_state_write_failed", scenario)
            let systemIdentifier = try XCTUnwrap(first["systemIdentifier"] as? String)
            XCTAssertEqual(app.systemScheduleDeviceID, identifier)
            params["requestId"] = "retry-\(scenario)"
            let retry = await app.requestSystemSchedule(try XCTUnwrap(SystemScheduleRequest.parse(params)), admission: admission)
            XCTAssertEqual(retry["requestId"] as? String, "retry-\(scenario)")
            XCTAssertEqual((retry["error"] as? [String: Any])?["code"] as? String,
                           "local_state_write_failed", scenario)
            let cancel = await app.cancelSystemSchedule(.init(requestID: "cancel-\(scenario)",
                operationID: original.operationID, kind: .alarm, purpose: .user), admission: admission)
            XCTAssertEqual((cancel?["error"] as? [String: Any])?["code"] as? String,
                           "local_state_write_failed", scenario)
            XCTAssertEqual(cancel?["systemIdentifier"] as? String, systemIdentifier)
            if scenario != "confirm-stale" && scenario != "invalidation" {
                let recaptured = try XCTUnwrap(app.captureSystemScheduleAdmission(origin: origin))
                guard case .localStateWriteFailed = recaptured else {
                    return XCTFail("admission must retain the typed commit failure")
                }
                let admittedRetry = await app.requestSystemSchedule(original, admission: recaptured)
                XCTAssertEqual((admittedRetry["error"] as? [String: Any])?["code"] as? String,
                               "local_state_write_failed", scenario)
                let admittedCancel = await app.cancelSystemSchedule(.init(requestID: "admitted-cancel",
                    operationID: original.operationID, kind: .alarm, purpose: .user), admission: recaptured)
                XCTAssertEqual(admittedCancel?["systemIdentifier"] as? String, systemIdentifier)
                let failedInfo = try jsonObject(try await page.systemScheduleOutcome(method: "app.info", params: [:]))
                let failedReceipt = failedInfo["receipt"] as? [String: Any]
                XCTAssertEqual((failedReceipt?["device"] as? [String: Any])?["id"] as? String, identifier)
            }
            failFromCommit = Int.max
            if scenario != "confirm-stale" && scenario != "invalidation" {
                let recovered = await app.requestSystemSchedule(original, admission: admission)
                XCTAssertNotEqual((recovered["error"] as? [String: Any])?["code"] as? String,
                                  "local_state_write_failed", scenario)
                let info = try jsonObject(try await page.systemScheduleOutcome(method: "app.info", params: [:]))
                let receipt = info["receipt"] as? [String: Any]
                XCTAssertEqual((receipt?["device"] as? [String: Any])?["id"] as? String, identifier)
            }
            XCTAssertEqual(platform.scheduleCalls, 0, scenario)
            XCTAssertEqual(platform.cancelCalls, 0, scenario)
            XCTAssertNil(app.systemScheduleConfirmation)
        }
    }

    @MainActor
    func testCorruptSystemScheduleStoreReturnsTheLocalStateErrorThroughTheBridge() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let corrupt = Data("not-json".utf8)
        let platform = RuntimeSystemSchedulePlatform()
        let app = try await makeApp(
            directTransport: nil,
            systemSchedulePlatform: platform,
            systemScheduleNow: { now }
        )
        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        defer { page.close() }
        try corrupt.write(to: XCTUnwrap(appScheduleLedgerURL))

        let create = try jsonObject(
            try await page.systemScheduleRequest(
                scheduleParams(
                    requestID: "request-corrupt-store",
                    operationID: "operation-corrupt-store",
                    now: now
                )
            )
        )
        XCTAssertEqual(create["state"] as? String, "failed")
        XCTAssertEqual(
            (create["error"] as? [String: Any])?["code"] as? String,
            "local_state_unavailable"
        )
        XCTAssertEqual(
            (create["error"] as? [String: Any])?["message"] as? String,
            "Avibe can't read its saved alarm and reminder records on this iPhone. Update Avibe, then try again."
        )

        let cancel = try jsonObject(
            try await page.systemScheduleCancel(
                requestID: "cancel-corrupt-store",
                operationID: "operation-corrupt-store",
                kind: "alarm"
            )
        )
        XCTAssertEqual(cancel["state"] as? String, "failed")
        XCTAssertEqual(
            (cancel["error"] as? [String: Any])?["code"] as? String,
            "local_state_unavailable"
        )
        XCTAssertEqual(
            (cancel["error"] as? [String: Any])?["message"] as? String,
            "Avibe can't read its saved alarm and reminder records on this iPhone. Update Avibe, then try again."
        )
        XCTAssertEqual(
            try Data(contentsOf: XCTUnwrap(appScheduleLedgerURL)),
            corrupt
        )
        XCTAssertEqual(platform.scheduleCalls, 0)
        XCTAssertEqual(platform.cancelCalls, 0)
    }

    @MainActor
    func testExpiredCreatingUnknownRecoversBeforeTheAppExpiryGuard() async throws {
        let requestTime = Date(timeIntervalSince1970: 1_800_000_000)
        var now = requestTime
        let platform = RuntimeSystemSchedulePlatform()
        platform.existenceResult = .exists(.authorized)
        let app = try await makeApp(
            directTransport: nil,
            systemSchedulePlatform: platform,
            systemScheduleNow: { now }
        )
        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        defer { page.close() }
        let params = scheduleParams(
            requestID: "request-expired-recovery",
            operationID: "operation-expired-recovery",
            now: requestTime
        )
        let request = try XCTUnwrap(SystemScheduleRequest.parse(params))
        let store = try XCTUnwrap(appScheduleStore)
        let owner = try XCTUnwrap(
            store.managementOwner(accountID: "account-1")
        )
        let identifier = platform.systemIdentifier(for: request, owner: owner)
        XCTAssertTrue(
            store.save(
                StoredSystemSchedule(
                    operationID: request.operationID,
                    owner: owner,
                    intent: StoredSystemScheduleIntent(request: request),
                    systemIdentifier: identifier,
                    authorization: .authorized,
                    lifecycle: .creatingUnknown,
                    effectMayExist: true,
                    generation: 2,
                    createState: nil,
                    lastError: nil,
                    updatedAt: requestTime
                )
            )
        )
        now = requestTime.addingTimeInterval(301)

        let receipt = try jsonObject(
            try await page.systemScheduleRequest(params)
        )

        XCTAssertEqual(receipt["state"] as? String, "expired")
        XCTAssertNil(app.systemScheduleConfirmation)
        XCTAssertEqual(platform.scheduleCalls, 0)
        XCTAssertEqual(platform.systemCreationCalls, 0)
        XCTAssertEqual(platform.cancelCalls, 1)
        XCTAssertEqual(
            store.record(
                operationID: request.operationID,
                instanceID: request.instanceID,
                owner: owner
            )?.lifecycle,
            .closedRemoved
        )
    }

    @MainActor
    func testClaimedCreateKeepsDeviceResultAfterDeadlineCrosses() async throws {
        var now = Date(timeIntervalSince1970: 1_800_000_000)
        let platform = RuntimeSystemSchedulePlatform()
        platform.holdAfterSystemCreation = true
        let app = try await makeApp(
            directTransport: nil,
            systemSchedulePlatform: platform,
            systemScheduleNow: { now }
        )
        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        defer {
            platform.releaseCreationResult()
            page.close()
        }

        let request = Task { @MainActor in
            try await page.systemScheduleRequest(
                scheduleParams(
                    requestID: "request-delayed-result",
                    operationID: "operation-delayed-result",
                    now: now
                )
            )
        }
        try await eventually { app.systemScheduleConfirmation != nil }
        let confirmation = try XCTUnwrap(app.systemScheduleConfirmation)
        app.confirmSystemSchedule(id: confirmation.id)
        try await eventually { platform.isWaitingAfterSystemCreation }
        XCTAssertEqual(platform.systemCreationCalls, 1)

        now = now.addingTimeInterval(301)
        platform.releaseCreationResult()
        let receipt = try jsonObject(try await request.value)

        XCTAssertEqual(receipt["state"] as? String, "scheduled")
        XCTAssertEqual(platform.cancelCalls, 0)
    }

    @MainActor
    func testLogoutDuringClaimedCreateDetachesThenCleansUpLateOSSuccessLocally() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let platform = RuntimeSystemSchedulePlatform()
        platform.holdAfterSystemCreation = true
        let app = try await makeApp(
            directTransport: nil,
            systemSchedulePlatform: platform,
            systemScheduleNow: { now }
        )
        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        defer {
            platform.releaseCreationResult()
            page.close()
        }

        let request = Task { @MainActor in
            try await page.systemScheduleRequest(
                scheduleParams(
                    requestID: "request-logout-race",
                    operationID: "operation-logout-race",
                    now: now
                )
            )
        }
        try await eventually { app.systemScheduleConfirmation != nil }
        let confirmation = try XCTUnwrap(app.systemScheduleConfirmation)
        app.confirmSystemSchedule(id: confirmation.id)
        try await eventually { platform.isWaitingAfterSystemCreation }

        let store = try XCTUnwrap(appScheduleStore)
        let owner = try XCTUnwrap(
            store.managementOwner(accountID: "account-1")
        )
        _ = try await app.mobileSessionCoordinator.logout()
        XCTAssertEqual(
            store.record(
                operationID: "operation-logout-race",
                instanceID: "instance-1",
                owner: owner
            )?.lifecycle,
            .detachedCleanupPending
        )

        platform.releaseCreationResult()
        let receipt = try jsonObject(try await request.value)
        XCTAssertEqual(receipt["state"] as? String, "stale_context")
        XCTAssertEqual(platform.cancelCalls, 1)
        XCTAssertEqual(
            store.record(
                operationID: "operation-logout-race",
                instanceID: "instance-1",
                owner: owner
            )?.lifecycle,
            .detachedCleaned
        )
        trackStore(of: app)
    }

    @MainActor
    func testInstanceSwitchEndsPendingScheduleWithoutCallingTheSystem() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let platform = RuntimeSystemSchedulePlatform()
        let app = try await makeApp(
            directTransport: nil,
            systemSchedulePlatform: platform,
            systemScheduleNow: { now }
        )
        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        defer { page.close() }

        let request = Task { @MainActor in
            try await page.systemScheduleRequest(
                scheduleParams(
                    requestID: "request-stale",
                    operationID: "operation-stale",
                    now: now
                )
            )
        }
        try await eventually { app.systemScheduleConfirmation != nil }

        _ = try await select("instance-2", in: app)
        let receipt = try jsonObject(try await request.value)
        XCTAssertEqual(receipt["state"] as? String, "stale_context")
        XCTAssertEqual(
            (receipt["error"] as? [String: Any])?["code"] as? String,
            "stale_context"
        )
        XCTAssertNil(app.systemScheduleConfirmation)
        XCTAssertEqual(platform.scheduleCalls, 0)
        XCTAssertEqual(platform.systemCreationCalls, 0)
    }

    // F2: an admitted cancel cannot sample replacement document authority
    // after unrelated local cleanup suspends it. Earlier tests only navigated
    // after confirmation, missing this entry window.
    @MainActor
    func testCancelDoesNotAdoptDocumentAuthorityAfterCleanupSuspension() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let platform = RuntimeSystemSchedulePlatform()
        let app = try await makeApp(
            directTransport: nil, systemSchedulePlatform: platform, systemScheduleNow: { now }
        )
        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        defer { platform.releaseCancel(); page.close() }
        let origin = try XCTUnwrap(WebOrigin(url: runtime.instance.url))
        let store = try XCTUnwrap(appScheduleStore)
        let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
        let target = try XCTUnwrap(SystemScheduleRequest.parse(scheduleParams(
            requestID: "target-create", operationID: "target", now: now
        )))
        let blocker = try XCTUnwrap(SystemScheduleRequest.parse(scheduleParams(
            requestID: "blocker-create", operationID: "blocker", now: now
        )))
        XCTAssertTrue(store.save(cleanupPendingRecord(request: blocker, owner: owner,
            systemIdentifier: "cleanup-blocker", updatedAt: now)))
        var targetRecord = cleanupPendingRecord(request: target, owner: owner,
            systemIdentifier: "target-item", updatedAt: now)
        targetRecord.lifecycle = .managedScheduled
        targetRecord.createState = .scheduled
        XCTAssertTrue(store.save(targetRecord))
        let entered = expectation(description: "cleanup suspended")
        platform.holdCancelIdentifier = "cleanup-blocker"
        platform.onCancelSuspended = { entered.fulfill() }
        let admission = try XCTUnwrap(app.captureSystemScheduleAdmission(origin: origin))
        let cleanup = Task { @MainActor in
            await app.cancelSystemSchedule(SystemScheduleCancelRequest(
                requestID: "blocker-cancel", operationID: "blocker", kind: .alarm, purpose: .user
            ), admission: admission)
        }
        await fulfillment(of: [entered], timeout: 3)
        try await page.load("<!doctype html><title>Replacement</title>", baseURL: runtime.instance.url,
                            forwardingNativeNavigation: true)
        // This is the same frozen admission the bridge carries across its Task
        // boundary; no call is allowed to resolve it against the new document.
        let result = await app.cancelSystemSchedule(SystemScheduleCancelRequest(
            requestID: "old-cancel", operationID: "target", kind: .alarm, purpose: .user
        ), admission: admission)
        platform.releaseCancel()
        _ = await cleanup.value
        XCTAssertFalse(platform.cancelIdentifiers.contains("target-item"))
        XCTAssertNotEqual(result?["state"] as? String, "cancelled")
        let createResult = await app.requestSystemSchedule(target, admission: admission)
        XCTAssertEqual(createResult["state"] as? String, "stale_context")
        XCTAssertNil(app.systemScheduleConfirmation)
        XCTAssertEqual(platform.scheduleCalls, 0)
        guard case .authorized(let oldContext) = admission,
              case .authorized(let newContext) = app.captureSystemScheduleAdmission(origin: origin) else {
            return XCTFail("the new same-origin document must have its own valid authority")
        }
        XCTAssertNotEqual(oldContext.navigationEpoch, newContext.navigationEpoch)
        XCTAssertEqual(store.record(operationID: "target", instanceID: "instance-1", owner: owner)?.lifecycle,
                       .managedScheduled)
    }

    // Matrix 5: actual WebKit delivery invokes admission outside any Swift
    // Task. Reopen the same Runtime synchronously before that admission returns;
    // both operations must receive, and reject, the original snapshot.
    @MainActor
    func testBridgeFreezesAuthorityBeforeItsTaskForCreateAndCancel() async throws {
        for action in ["create", "cancel"] {
            let now = Date(timeIntervalSince1970: 1_800_000_000)
            let platform = RuntimeSystemSchedulePlatform()
            let app = try await makeApp(directTransport: nil, systemSchedulePlatform: platform,
                                        systemScheduleNow: { now })
            let runtime = try await select("instance-1", in: app)
            let originalIdentity = app.runtimeIdentity
            var captures = 0
            var capturedAdmission: SystemScheduleAdmission?
            var deliveries = 0
            let controller = try makeRuntimeController(app, runtime, afterScheduleAdmission: { admission in
                captures += 1
                capturedAdmission = admission
                withUnsafeCurrentTask { XCTAssertNil($0, "admission must precede NativeBridge's Task") }
                guard case .authorized(let context) = admission else {
                    XCTFail("trusted primary source must be admitted")
                    return
                }
                XCTAssertEqual(context.runtimeIdentity, originalIdentity)
                app.openSelectedInstance(path: "/")
                XCTAssertNotEqual(app.runtimeIdentity, originalIdentity)
            }, onScheduleDelivery: { admission in
                deliveries += 1
                guard case .authorized(let context) = admission else {
                    XCTFail("delivery must carry the original admission")
                    return
                }
                XCTAssertEqual(context.runtimeIdentity, originalIdentity)
                XCTAssertNotEqual(context.runtimeIdentity, app.runtimeIdentity)
            })
            app.registerRuntimeController(controller, for: runtime)
            let page = try RuntimePage(controller: controller)
            defer { page.close() }
            try await page.load(RuntimePage.recordingDocument, baseURL: runtime.instance.url)
            let params = scheduleParams(requestID: "old-\(action)", operationID: "target", now: now)
            let request = try XCTUnwrap(SystemScheduleRequest.parse(params))
            let store = try XCTUnwrap(appScheduleStore)
            let owner = try XCTUnwrap(store.managementOwner(accountID: "account-1"))
            if action == "cancel" {
                var record = cleanupPendingRecord(request: request, owner: owner,
                    systemIdentifier: "must-remain", updatedAt: now)
                record.lifecycle = .managedScheduled
                record.createState = .scheduled
                XCTAssertTrue(store.save(record))
            }
            let payload: [String: Any] = action == "create" ? params : [
                "requestId": "old-cancel", "operationId": "target", "kind": "alarm", "purpose": "user"
            ]
            let bridgeResult = try jsonObject(try await page.systemScheduleOutcome(
                method: action == "create" ? "systemSchedule.request" : "systemSchedule.cancel", params: payload))
            // Reopening retires the old factory as well. It may refuse delivery
            // before AppModel, but may never resolve into the replacement owner.
            if let receipt = bridgeResult["receipt"] as? [String: Any] {
                XCTAssertEqual(action, "create")
                XCTAssertEqual(receipt["state"] as? String, "stale_context")
            } else {
                XCTAssertEqual(bridgeResult["errorCode"] as? String, "forbidden")
            }
            XCTAssertEqual(captures, 1)
            XCTAssertLessThanOrEqual(deliveries, 1)
            let originalAdmission = try XCTUnwrap(capturedAdmission)
            let deliveredCreate = await app.requestSystemSchedule(request, admission: originalAdmission)
            XCTAssertEqual(deliveredCreate["state"] as? String, "stale_context")
            let deliveredCancel = await app.cancelSystemSchedule(.init(requestID: "old-cancel",
                operationID: "target", kind: .alarm, purpose: .user), admission: originalAdmission)
            XCTAssertNil(deliveredCancel)
            XCTAssertEqual(platform.scheduleCalls, 0)
            XCTAssertEqual(platform.cancelCalls, 0)
            XCTAssertNil(app.systemScheduleConfirmation)
            if action == "cancel" {
                XCTAssertEqual(store.record(operationID: "target", instanceID: "instance-1", owner: owner)?.lifecycle,
                               .managedScheduled)
            }
        }
    }

    @MainActor
    func testProvisionalNavigationRevokesScheduleAuthorityBeforeURLChanges() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let app = try await makeApp(
            directTransport: nil,
            systemSchedulePlatform: RuntimeSystemSchedulePlatform(),
            systemScheduleNow: { now }
        )
        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        defer { page.close() }
        let origin = try XCTUnwrap(WebOrigin(url: runtime.instance.url))

        XCTAssertTrue(page.hasTrustedScheduleSource(origin: origin))
        try page.beginPrimaryProvisionalNavigation()
        XCTAssertFalse(page.hasTrustedScheduleSource(origin: origin))
    }

    @MainActor
    func testSameOriginReturnDoesNotReviveARequestRevokedAtProvisionalStart() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let platform = RuntimeSystemSchedulePlatform()
        let app = try await makeApp(
            directTransport: nil,
            systemSchedulePlatform: platform,
            systemScheduleNow: { now }
        )
        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        defer { page.close() }

        let request = Task { @MainActor in
            try await page.systemScheduleRequest(
                scheduleParams(
                    requestID: "request-same-origin-return",
                    operationID: "operation-same-origin-return",
                    now: now
                )
            )
        }
        try await eventually { app.systemScheduleConfirmation != nil }
        let revokedConfirmation = try XCTUnwrap(app.systemScheduleConfirmation)

        try await page.load(
            "<!doctype html><title>Trusted again</title>",
            baseURL: runtime.instance.url
        )
        let receipt = try jsonObject(try await request.value)
        XCTAssertEqual(receipt["state"] as? String, "stale_context")
        XCTAssertNil(app.systemScheduleConfirmation)

        app.confirmSystemSchedule(id: revokedConfirmation.id)
        XCTAssertEqual(platform.scheduleCalls, 0)
        XCTAssertEqual(platform.systemCreationCalls, 0)
    }

    @MainActor
    func testPrimaryNavigationAwayRevokesPendingScheduleAuthority() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let platform = RuntimeSystemSchedulePlatform()
        let app = try await makeApp(
            directTransport: nil,
            systemSchedulePlatform: platform,
            systemScheduleNow: { now }
        )
        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        defer { page.close() }

        let request = Task { @MainActor in
            try await page.systemScheduleRequest(
                scheduleParams(
                    requestID: "request-navigation",
                    operationID: "operation-navigation",
                    now: now
                )
            )
        }
        try await eventually { app.systemScheduleConfirmation != nil }
        let confirmation = try XCTUnwrap(app.systemScheduleConfirmation)
        try await page.load(
            "<!doctype html><title>Elsewhere</title>",
            baseURL: URL(string: "https://example.com")!
        )

        app.confirmSystemSchedule(id: confirmation.id)
        try await eventually { app.systemScheduleConfirmation == nil }
        _ = await request.result
        XCTAssertEqual(platform.scheduleCalls, 0)
        XCTAssertEqual(platform.systemCreationCalls, 0)
    }

    @MainActor
    func testLatePermissionReturnCannotCreateForAReplacedRuntime() async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let platform = RuntimeSystemSchedulePlatform()
        platform.holdBeforeSystemCreation = true
        let app = try await makeApp(
            directTransport: nil,
            systemSchedulePlatform: platform,
            systemScheduleNow: { now }
        )
        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime)
        defer {
            platform.releasePermission()
            page.close()
        }

        let request = Task { @MainActor in
            try await page.systemScheduleRequest(
                scheduleParams(
                    requestID: "request-late",
                    operationID: "operation-late",
                    now: now
                )
            )
        }
        try await eventually { app.systemScheduleConfirmation != nil }
        let confirmation = try XCTUnwrap(app.systemScheduleConfirmation)
        app.confirmSystemSchedule(id: confirmation.id)
        try await eventually { platform.isWaitingForPermission }
        XCTAssertEqual(platform.scheduleCalls, 1)
        XCTAssertEqual(platform.systemCreationCalls, 0)

        let store = try XCTUnwrap(appScheduleStore)
        let owner = try XCTUnwrap(
            store.managementOwner(accountID: "account-1")
        )
        _ = try await select("instance-2", in: app)
        XCTAssertNil(app.systemScheduleConfirmation)
        platform.releasePermission()
        let receipt = try jsonObject(try await request.value)
        XCTAssertEqual(receipt["state"] as? String, "stale_context")
        try await settle()
        XCTAssertEqual(platform.systemCreationCalls, 0)
        XCTAssertEqual(
            store.record(
                operationID: "operation-late",
                instanceID: "instance-1",
                owner: owner
            )?.lifecycle,
            .closedNoItem
        )
    }

    @MainActor
    func testWithoutATransportNothingIsGated() async throws {
        let app = try await makeApp(directTransport: nil)
        let runtime = try await select("instance-1", in: app)
        XCTAssertTrue(app.mayReload(runtime))
        XCTAssertTrue(app.authorizeReload(of: runtime))
        XCTAssertEqual(app.directLoadGate(for: runtime), .open)
        XCTAssertNil(app.directLoadGateProvider(for: runtime))
        XCTAssertFalse(app.presentsDirectFaultOnly(runtime))
        enterBackground()
        XCTAssertTrue(app.mayReload(runtime))
        enterForeground()
        XCTAssertTrue(app.mayReload(runtime))
    }

    @MainActor
    func testRuntimeOpenBudgetShowsLongWaitThenTimesOutToThePickerAndCanRetry() async throws {
        let timing = RuntimeOpenManualTiming()
        let notifications = NotificationCenter()
        let app = try await makeApp(
            directTransport: nil,
            runtimeOpenTiming: timing,
            runtimeOpenNotificationCenter: notifications
        )
        let storeIdentifier = app.websiteDataStore.identifier
        let runtime = try await select("instance-1", in: app)
        let attempt = try XCTUnwrap(app.runtimeOpenAttempt)

        XCTAssertEqual(attempt.id, app.runtimeIdentity)
        XCTAssertEqual(attempt.runtime, runtime)
        XCTAssertEqual(attempt.phase, .connecting)
        XCTAssertFalse(attempt.isTakingLong)

        timing.advance(by: .seconds(8))
        XCTAssertTrue(try XCTUnwrap(app.runtimeOpenAttempt).isTakingLong)
        XCTAssertEqual(app.route, .browser(runtime))

        // The budget is WebKit's own 60-second request timeout.
        timing.advance(by: .seconds(51))
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertEqual(app.runtimeOpenAttempt?.id, attempt.id)

        timing.advance(by: .seconds(1))
        XCTAssertTrue(isOnInstancePicker(app))
        XCTAssertNil(app.runtimeOpenAttempt)
        XCTAssertEqual(app.mobileSessionCoordinator.currentAccount?.id, "account-1")
        XCTAssertTrue(app.mobileSessionCoordinator.isSelectedRuntime(runtime))
        XCTAssertEqual(app.websiteDataStore.identifier, storeIdentifier)
        XCTAssertEqual(app.runtimeOpenNotice?.instanceName, "One")
        XCTAssertEqual(app.runtimeOpenNotice?.lastPhase, .connecting)
        XCTAssertEqual(app.lastRuntimeOpenTermination?.reason, .timedOut)

        // The page's current block opens the kept selection again.
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        let retry = try XCTUnwrap(app.runtimeOpenAttempt)
        XCTAssertGreaterThan(retry.id, attempt.id)
        XCTAssertEqual(retry.phase, .connecting)
        XCTAssertNil(app.runtimeOpenNotice)
    }

    /// WebKit is suspended in the background, so a slow open there is not
    /// failing: the budget is charged only for foreground time. The return
    /// schedules the remainder, and the long-wait message keeps its place.
    @MainActor
    func testRuntimeOpenBudgetIsNotChargedWhileTheAppIsInTheBackground() async throws {
        let timing = RuntimeOpenManualTiming()
        let notifications = NotificationCenter()
        let app = try await makeApp(
            directTransport: nil,
            runtimeOpenTiming: timing,
            runtimeOpenNotificationCenter: notifications
        )
        let runtime = try await select("instance-1", in: app)
        let attemptID = try XCTUnwrap(app.runtimeOpenAttempt?.id)

        timing.advance(by: .seconds(5))
        notifications.post(
            name: UIApplication.didEnterBackgroundNotification,
            object: nil
        )
        timing.advance(by: .seconds(120))
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertEqual(app.runtimeOpenAttempt?.id, attemptID)
        XCTAssertNil(app.runtimeOpenNotice)

        notifications.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertEqual(app.runtimeOpenAttempt?.id, attemptID)
        // Five seconds of foreground time so far.
        XCTAssertFalse(try XCTUnwrap(app.runtimeOpenAttempt).isTakingLong)

        timing.advance(by: .seconds(3))
        XCTAssertTrue(try XCTUnwrap(app.runtimeOpenAttempt).isTakingLong)
        timing.advance(by: .seconds(51))
        XCTAssertEqual(app.route, .browser(runtime))
        timing.advance(by: .seconds(1))
        XCTAssertTrue(isOnInstancePicker(app))
        XCTAssertEqual(app.lastRuntimeOpenTermination?.reason, .timedOut)
    }

    /// After a timeout the page's current block is the one way to open the
    /// instance again. The native notice only explains and points to it,
    /// with no reopen of its own, and sits in the lower half, clear of the
    /// page's title, account button and current block.
    @MainActor
    func testRuntimeOpenTimeoutNoticeOffersNoSecondReopenAndLeavesThePageTopUncovered()
        async throws
    {
        let timing = RuntimeOpenManualTiming()
        let app = try await makeApp(
            directTransport: nil,
            runtimeOpenTiming: timing,
            runtimeOpenNotificationCenter: NotificationCenter()
        )
        let runtime = try await select("instance-1", in: app)
        timing.advance(by: .seconds(60))
        XCTAssertTrue(isOnInstancePicker(app))
        XCTAssertNotNil(app.runtimeOpenNotice)
        XCTAssertTrue(app.mobileSessionCoordinator.isSelectedRuntime(runtime))

        // SwiftUI builds its native accessibility tree only while
        // accessibility automation is on; turn it on for this test alone.
        let restoreAutomation = try enableAccessibilityAutomation()
        defer { restoreAutomation() }
        let scene = try XCTUnwrap(
            UIApplication.shared.connectedScenes
                .compactMap { $0 as? UIWindowScene }
                .first
        )
        let window = UIWindow(windowScene: scene)
        window.frame = scene.screen.bounds
        window.windowLevel = .normal + 1
        window.rootViewController = UIHostingController(
            rootView: RootView().environmentObject(app)
        )
        window.makeKeyAndVisible()
        defer {
            window.isHidden = true
            window.rootViewController = nil
        }

        let title = String(
            format: NSLocalizedString(
                "runtime.open.timeout.title.named",
                comment: ""
            ),
            "One"
        )
        let reopenHint = String(
            format: NSLocalizedString(
                "runtime.open.timeout.reopen_hint.named",
                comment: ""
            ),
            "One"
        )
        let retry = NSLocalizedString("browser.error.retry", comment: "")
        let dismiss = NSLocalizedString(
            "runtime.open.timeout.dismiss",
            comment: ""
        )
        var elements: [NativeAccessibilityElement] = []
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(15))
        while clock.now < deadline {
            window.layoutIfNeeded()
            elements = nativeAccessibilityElements(in: window)
            if elements.contains(where: { $0.label == title }),
               elements.contains(where: { $0.label == dismiss })
            {
                break
            }
            try await Task.sleep(for: .milliseconds(100))
        }
        let labels = elements.map(\.label)
        let notice = try XCTUnwrap(
            elements.first { $0.label == title },
            "The timeout notice is not on screen: \(labels)"
        )
        XCTAssertTrue(labels.contains(dismiss), "\(labels)")
        // It says where the one reopen is.
        XCTAssertTrue(labels.contains(reopenHint), "\(labels)")
        XCTAssertFalse(labels.contains(retry), "\(labels)")
        XCTAssertGreaterThan(notice.frame.minY, window.bounds.midY)
    }

    @MainActor
    func testRuntimeOpenNavigationStillSettlesDuringOrdinaryAccountWork() async throws {
        let hold = PushHold(path: "/api/push/hold-runtime-open")
        let timing = RuntimeOpenManualTiming()
        let app = try await makeApp(
            directTransport: nil,
            pushHolds: [hold],
            runtimeOpenTiming: timing
        )
        let runtime = try await select("instance-1", in: app)
        let attemptID = try XCTUnwrap(app.runtimeOpenAttempt?.id)
        let management = try await holdPushManagement(hold, in: app)
        XCTAssertFalse(app.mobileSessionCoordinator.isCurrentRuntime(runtime))
        XCTAssertTrue(app.mobileSessionCoordinator.isSelectedRuntime(runtime))

        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .finished, destination: .authentication),
            attemptID: attemptID,
            runtime: runtime
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .awaitingUser)
        XCTAssertNil(app.runtimeOpenAttempt?.deadline)

        hold.release.signal()
        try await management.value
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .started, destination: .instance),
            attemptID: attemptID,
            runtime: runtime
        )
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .finished, destination: .instance),
            attemptID: attemptID,
            runtime: runtime
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .documentLoaded)
    }

    @MainActor
    func testRuntimeOpenRetryDuringOrdinaryAccountWorkCreatesAndRegistersANewAttempt()
        async throws
    {
        let hold = PushHold(path: "/api/push/hold-runtime-retry")
        let timing = RuntimeOpenManualTiming()
        let transport = StandInDirectTransport()
        let app = try await makeApp(
            directTransport: transport,
            pushHolds: [hold],
            runtimeOpenTiming: timing
        )
        let runtime = try await select("instance-1", in: app)
        let firstID = try XCTUnwrap(app.runtimeOpenAttempt?.id)
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .failed, destination: .instance),
            attemptID: firstID,
            runtime: runtime
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .failed)

        let management = try await holdPushManagement(hold, in: app)
        XCTAssertFalse(app.mobileSessionCoordinator.isCurrentRuntime(runtime))
        XCTAssertTrue(app.mobileSessionCoordinator.isSelectedRuntime(runtime))

        XCTAssertTrue(app.retryRuntimeOpen(attemptID: firstID))
        let retry = try XCTUnwrap(app.runtimeOpenAttempt)
        XCTAssertGreaterThan(retry.id, firstID)
        XCTAssertEqual(retry.runtime, runtime)
        let reopened = try await waitForBrowser("instance-1", in: app)
        XCTAssertEqual(reopened, runtime)

        let controller = try makeRuntimeController(app, runtime)
        let deselected = transport.deselectCount
        app.registerRuntimeController(
            controller,
            for: runtime,
            attemptID: retry.id
        )
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertEqual(app.runtimeOpenAttempt?.id, retry.id)
        XCTAssertEqual(transport.deselectCount, deselected)

        hold.release.signal()
        try await management.value
        XCTAssertTrue(app.mobileSessionCoordinator.isCurrentRuntime(runtime))
        withExtendedLifetime(controller) {}
    }

    @MainActor
    func testRuntimeOpenFailureStaysTerminalUntilExplicitRetry() async throws {
        let timing = RuntimeOpenManualTiming()
        let app = try await makeApp(
            directTransport: nil,
            runtimeOpenTiming: timing
        )
        let runtime = try await select("instance-1", in: app)
        let attemptID = try XCTUnwrap(app.runtimeOpenAttempt?.id)

        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .failed, destination: .instance),
            attemptID: attemptID,
            runtime: runtime
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .failed)
        XCTAssertNil(app.runtimeOpenAttempt?.deadline)

        for stage in [
            RuntimeNavigationStage.started,
            .committed,
            .finished,
        ] {
            app.handleRuntimeNavigation(
                RuntimeNavigationEvent(stage: stage, destination: .instance),
                attemptID: attemptID,
                runtime: runtime
            )
        }
        timing.advance(by: .seconds(120))
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .failed)
        XCTAssertNil(app.runtimeOpenAttempt?.deadline)
    }

    @MainActor
    func testRuntimeOpenFailureAfterLegacyDocumentKeepsNativeRecovery() async throws {
        let app = try await makeApp(directTransport: nil)
        let runtime = try await select("instance-1", in: app)
        let attemptID = try XCTUnwrap(app.runtimeOpenAttempt?.id)

        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .finished, destination: .instance),
            attemptID: attemptID,
            runtime: runtime
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .documentLoaded)

        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .failed, destination: .instance),
            attemptID: attemptID,
            runtime: runtime
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .failed)
        XCTAssertTrue(app.retryRuntimeOpen(attemptID: attemptID))
        XCTAssertGreaterThan(
            try XCTUnwrap(app.runtimeOpenAttempt?.id),
            attemptID
        )
    }

    @MainActor
    func testContinuousRuntimeOpenTimerUsesTheScheduleCallAsItsDeadlineOrigin() async throws {
        let timing = ContinuousRuntimeOpenTiming()
        var fired = false
        let timer = timing.schedule(after: .milliseconds(80)) {
            fired = true
        }
        let holdUntil = ContinuousClock.now.advanced(by: .milliseconds(120))
        while ContinuousClock.now < holdUntil {
            _ = 1 + 1
        }
        try await Task.sleep(for: .milliseconds(20))
        XCTAssertTrue(fired)
        withExtendedLifetime(timer) {}
    }

    @MainActor
    func testRuntimeOpenCancelAndNewSelectionFenceEveryLateCallback() async throws {
        let timing = RuntimeOpenManualTiming()
        let app = try await makeApp(
            directTransport: nil,
            runtimeOpenTiming: timing
        )
        let first = try await select("instance-1", in: app)
        let firstID = try XCTUnwrap(app.runtimeOpenAttempt?.id)
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .committed, destination: .instance),
            attemptID: firstID,
            runtime: first
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .loadingPage)

        app.cancelRuntimeOpen(attemptID: firstID)
        XCTAssertTrue(isOnInstancePicker(app))
        XCTAssertNil(app.runtimeOpenAttempt)
        XCTAssertNil(app.runtimeOpenNotice)
        XCTAssertEqual(app.lastRuntimeOpenTermination?.reason, .cancelled)

        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .finished, destination: .instance),
            attemptID: firstID,
            runtime: first
        )
        XCTAssertTrue(isOnInstancePicker(app))

        let second = try await select("instance-2", in: app)
        let secondID = try XCTUnwrap(app.runtimeOpenAttempt?.id)
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .failed, destination: .instance),
            attemptID: firstID,
            runtime: first
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.id, secondID)
        XCTAssertEqual(app.runtimeOpenAttempt?.runtime, second)
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .connecting)
    }

    @MainActor
    func testRuntimeOpenAuthDocumentPausesAndResumesOnlyTheRemainingBudget() async throws {
        let timing = RuntimeOpenManualTiming()
        let notifications = NotificationCenter()
        let app = try await makeApp(
            directTransport: nil,
            runtimeOpenTiming: timing,
            runtimeOpenNotificationCenter: notifications
        )
        let runtime = try await select("instance-1", in: app)
        let attemptID = try XCTUnwrap(app.runtimeOpenAttempt?.id)

        timing.advance(by: .seconds(5))
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .finished, destination: .authentication),
            attemptID: attemptID,
            runtime: runtime
        )
        let awaiting = try XCTUnwrap(app.runtimeOpenAttempt)
        XCTAssertEqual(awaiting.phase, .awaitingUser)
        XCTAssertEqual(awaiting.remainingAutomaticBudget, .seconds(55))
        XCTAssertNil(awaiting.deadline)

        timing.elapseWithoutFiring(by: .seconds(60))
        notifications.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .awaitingUser)
        XCTAssertEqual(app.route, .browser(runtime))

        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .started, destination: .instance),
            attemptID: attemptID,
            runtime: runtime
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .connecting)
        XCTAssertEqual(
            app.runtimeOpenAttempt?.deadline,
            timing.now + .seconds(55)
        )

        timing.advance(by: .seconds(54))
        XCTAssertEqual(app.route, .browser(runtime))
        timing.elapseWithoutFiring(by: .seconds(1))
        notifications.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        XCTAssertTrue(isOnInstancePicker(app))
        XCTAssertEqual(app.lastRuntimeOpenTermination?.reason, .timedOut)
    }

    @MainActor
    func testLegacyDocumentFinishEndsTheAutomaticBudgetWithoutClaimingReady() async throws {
        let timing = RuntimeOpenManualTiming()
        let app = try await makeApp(
            directTransport: nil,
            runtimeOpenTiming: timing
        )
        let runtime = try await select("instance-1", in: app)
        let attemptID = try XCTUnwrap(app.runtimeOpenAttempt?.id)

        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .finished, destination: .instance),
            attemptID: attemptID,
            runtime: runtime
        )

        let loaded = try XCTUnwrap(app.runtimeOpenAttempt)
        XCTAssertEqual(loaded.phase, .documentLoaded)
        XCTAssertNil(loaded.deadline)
        XCTAssertFalse(loaded.isVerifiedReady)
        timing.advance(by: .seconds(120))
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertNil(app.runtimeOpenNotice)
    }

    @MainActor
    func testRuntimeOpenFailureRetryCreatesANewAttemptAndBudget() async throws {
        let timing = RuntimeOpenManualTiming()
        let app = try await makeApp(
            directTransport: nil,
            runtimeOpenTiming: timing
        )
        let runtime = try await select("instance-1", in: app)
        let firstID = try XCTUnwrap(app.runtimeOpenAttempt?.id)
        timing.advance(by: .seconds(12))
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .failed, destination: .instance),
            attemptID: firstID,
            runtime: runtime
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .failed)
        XCTAssertNil(app.runtimeOpenAttempt?.deadline)

        XCTAssertTrue(app.retryRuntimeOpen(attemptID: firstID))
        let retry = try XCTUnwrap(app.runtimeOpenAttempt)
        XCTAssertGreaterThan(retry.id, firstID)
        XCTAssertEqual(retry.remainingAutomaticBudget, .seconds(60))
        XCTAssertEqual(retry.phase, .connecting)
    }

    @MainActor
    func testPolicyHandoffEndsAutomaticTimingWithoutHandingOffAnEmptyDocument() async throws {
        let timing = RuntimeOpenManualTiming()
        let app = try await makeApp(
            directTransport: nil,
            runtimeOpenTiming: timing
        )
        let runtime = try await select("instance-1", in: app)
        let attemptID = try XCTUnwrap(app.runtimeOpenAttempt?.id)

        timing.advance(by: .seconds(5))
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .handedOff, destination: .external),
            attemptID: attemptID,
            runtime: runtime
        )

        let handedOff = try XCTUnwrap(app.runtimeOpenAttempt)
        XCTAssertEqual(handedOff.phase, .connecting)
        XCTAssertEqual(handedOff.remainingAutomaticBudget, .seconds(55))
        XCTAssertNil(handedOff.deadline)
        XCTAssertFalse(handedOff.canResumeAutomaticBudget)
        XCTAssertEqual(
            BrowserOverlay(
                attemptPhase: handedOff.phase,
                directFault: false
            ),
            .loading
        )

        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .restarting, destination: .external),
            attemptID: attemptID,
            runtime: runtime
        )
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .finished, destination: .authentication),
            attemptID: attemptID,
            runtime: runtime
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .awaitingUser)
        XCTAssertFalse(app.runtimeOpenAttempt?.canResumeAutomaticBudget == true)
        XCTAssertNil(app.runtimeOpenAttempt?.deadline)

        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .started, destination: .instance),
            attemptID: attemptID,
            runtime: runtime
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .connecting)
        XCTAssertNil(app.runtimeOpenAttempt?.deadline)

        timing.advance(by: .seconds(120))
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertNil(app.runtimeOpenNotice)

        app.cancelRuntimeOpen(attemptID: attemptID)
        XCTAssertTrue(isOnInstancePicker(app))
    }

    @MainActor
    func testPolicyCancellationFailsOnlyAnUnfinishedDocument() async throws {
        let timing = RuntimeOpenManualTiming()
        let app = try await makeApp(
            directTransport: nil,
            runtimeOpenTiming: timing
        )
        let runtime = try await select("instance-1", in: app)
        let firstID = try XCTUnwrap(app.runtimeOpenAttempt?.id)

        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .cancelled, destination: .external),
            attemptID: firstID,
            runtime: runtime
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .failed)
        XCTAssertNil(app.runtimeOpenAttempt?.deadline)
        XCTAssertEqual(
            BrowserOverlay(
                attemptPhase: app.runtimeOpenAttempt?.phase,
                directFault: false
            ),
            .loadFailure
        )

        XCTAssertTrue(app.retryRuntimeOpen(attemptID: firstID))
        let retryID = try XCTUnwrap(app.runtimeOpenAttempt?.id)
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .finished, destination: .instance),
            attemptID: retryID,
            runtime: runtime
        )
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .cancelled, destination: .external),
            attemptID: retryID,
            runtime: runtime
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .documentLoaded)
        XCTAssertNil(app.runtimeOpenAttempt?.deadline)
    }

    @MainActor
    func testWebContentRestartResumesOnlyAStoppedAuthenticationRemainder() async throws {
        let timing = RuntimeOpenManualTiming()
        let app = try await makeApp(
            directTransport: nil,
            runtimeOpenTiming: timing
        )
        let runtime = try await select("instance-1", in: app)
        let attemptID = try XCTUnwrap(app.runtimeOpenAttempt?.id)

        timing.advance(by: .seconds(5))
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .finished, destination: .authentication),
            attemptID: attemptID,
            runtime: runtime
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .awaitingUser)
        XCTAssertEqual(
            app.runtimeOpenAttempt?.remainingAutomaticBudget,
            .seconds(55)
        )
        XCTAssertTrue(app.runtimeOpenAttempt?.canResumeAutomaticBudget == true)

        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .restarting, destination: .authentication),
            attemptID: attemptID,
            runtime: runtime
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .connecting)
        XCTAssertEqual(
            app.runtimeOpenAttempt?.deadline,
            timing.now + .seconds(55)
        )
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .started, destination: .instance),
            attemptID: attemptID,
            runtime: runtime
        )
        XCTAssertEqual(
            app.runtimeOpenAttempt?.deadline,
            timing.now + .seconds(55)
        )

        timing.advance(by: .seconds(55))
        XCTAssertTrue(isOnInstancePicker(app))
        XCTAssertEqual(app.lastRuntimeOpenTermination?.reason, .timedOut)
    }

    /// The real WKNavigationDelegate seam reports a primary slow document as
    /// started/committed and does not invent a finish while its body is held.
    /// Releasing the body produces the one legacy-document finish AppModel
    /// consumes. This is the isolated fixture counterpart to the manual-clock
    /// budget tests above.
    @MainActor
    func testRuntimeOpenNavigationEventsFollowASlowPrimaryDocument() async throws {
        let headersSent = FixtureEvent(description: "slow document headers sent")
        let releaseBody = DispatchSemaphore(value: 0)
        let html = "<html><body>" + String(repeating: "x", count: 131_072)
            + "</body></html>"
        let server = try LocalHTTPServer { _ in
            LocalHTTPResponse(
                status: 200,
                headers: [("Content-Type", "text/html; charset=utf-8")],
                body: Data(html.utf8),
                headersSentEvent: headersSent,
                bodyRelease: releaseBody
            )
        }
        let url = try await server.start()
        defer {
            releaseBody.signal()
            server.stop()
        }
        let origin = try XCTUnwrap(
            WebOrigin(url: url, requiresHTTPS: false)
        )
        var events: [RuntimeNavigationEvent] = []
        let controller = WebViewController(
            instanceOrigin: origin,
            websiteDataStore: .nonPersistent(),
            runtimeNavigationChanged: { events.append($0) },
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        controller.loadViewIfNeeded()

        // The WebView's first load waits for its web content process.
        await fulfillment(
            of: [headersSent.expectation],
            timeout: WebKitReadiness.webContentLaunchSeconds
        )
        try await eventually {
            events.contains {
                $0.stage == .committed && $0.destination == .instance
            }
        }
        XCTAssertTrue(events.contains {
            $0.stage == .started && $0.destination == .instance
        })
        XCTAssertFalse(events.contains { $0.stage == .finished })
        XCTAssertFalse(events.contains { $0.stage == .failed })

        releaseBody.signal()
        try await eventually {
            events.contains {
                $0.stage == .finished && $0.destination == .instance
            }
        }
        XCTAssertFalse(events.contains { $0.stage == .failed })
        _ = controller.invalidateForAccountTransition()
    }

    @MainActor
    func testGoogleRedirectPausesTheRealAttemptPastItsOriginalDeadline() async throws {
        let server = try LocalHTTPServer { _ in
            LocalHTTPResponse(
                status: 302,
                headers: [
                    ("Location", "https://accounts.google.com/o/oauth2/v2/auth")
                ],
                body: Data()
            )
        }
        let url = try await server.start()
        defer { server.stop() }
        let origin = try XCTUnwrap(WebOrigin(url: url, requiresHTTPS: false))
        let timing = RuntimeOpenManualTiming()
        let app = try await makeApp(
            directTransport: nil,
            runtimeOpenTiming: timing
        )
        let runtime = try await select("instance-1", in: app)
        let attemptID = try XCTUnwrap(app.runtimeOpenAttempt?.id)
        var events: [RuntimeNavigationEvent] = []
        let controller = WebViewController(
            instanceOrigin: origin,
            websiteDataStore: .nonPersistent(),
            navigationPolicy: NavigationPolicy(instanceOrigins: [origin]),
            runtimeNavigationChanged: {
                events.append($0)
                app.handleRuntimeNavigation(
                    $0,
                    attemptID: attemptID,
                    runtime: runtime
                )
            },
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 700))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        defer {
            controller.presentedViewController?.dismiss(animated: false)
            _ = controller.invalidateForAccountTransition()
            window.rootViewController = nil
            window.isHidden = true
        }

        try await eventually {
            app.runtimeOpenAttempt?.phase == .awaitingUser
        }
        XCTAssertNil(app.runtimeOpenAttempt?.deadline)
        XCTAssertEqual(
            events.filter {
                [.finished, .handedOff, .cancelled, .failed].contains($0.stage)
            },
            [
                RuntimeNavigationEvent(
                    stage: .finished,
                    destination: .thirdPartySignIn(.google)
                )
            ]
        )

        timing.advance(by: .seconds(120))
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .awaitingUser)
    }

    @MainActor
    func testExternalRedirectEndsTimingButKeepsCancelOverTheEmptyDocument() async throws {
        let server = try LocalHTTPServer { _ in
            LocalHTTPResponse(
                status: 302,
                headers: [("Location", "https://example.com/docs")],
                body: Data()
            )
        }
        let url = try await server.start()
        defer { server.stop() }
        let origin = try XCTUnwrap(WebOrigin(url: url, requiresHTTPS: false))
        let timing = RuntimeOpenManualTiming()
        let app = try await makeApp(
            directTransport: nil,
            runtimeOpenTiming: timing
        )
        let runtime = try await select("instance-1", in: app)
        let attemptID = try XCTUnwrap(app.runtimeOpenAttempt?.id)
        var events: [RuntimeNavigationEvent] = []
        let controller = WebViewController(
            instanceOrigin: origin,
            websiteDataStore: .nonPersistent(),
            navigationPolicy: NavigationPolicy(instanceOrigins: [origin]),
            runtimeNavigationChanged: {
                events.append($0)
                app.handleRuntimeNavigation(
                    $0,
                    attemptID: attemptID,
                    runtime: runtime
                )
            },
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 700))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        defer {
            controller.presentedViewController?.dismiss(animated: false)
            _ = controller.invalidateForAccountTransition()
            window.rootViewController = nil
            window.isHidden = true
        }

        try await eventually {
            events.contains {
                $0.stage == .handedOff && $0.destination == .external
            }
        }
        let handedOff = try XCTUnwrap(app.runtimeOpenAttempt)
        XCTAssertNil(handedOff.deadline)
        XCTAssertFalse(handedOff.canResumeAutomaticBudget)
        XCTAssertEqual(
            BrowserOverlay(attemptPhase: handedOff.phase, directFault: false),
            .loading
        )
        XCTAssertEqual(events.filter { $0.stage == .handedOff }.count, 1)

        timing.advance(by: .seconds(120))
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertNil(app.runtimeOpenNotice)
    }

    @MainActor
    func testPrimaryDownloadEndsTimingWithoutASecondCancellationSettlement() async throws {
        let server = try LocalHTTPServer { _ in
            LocalHTTPResponse(
                status: 200,
                headers: [
                    ("Content-Type", "application/octet-stream"),
                    ("Content-Disposition", "attachment; filename=fixture.bin"),
                ],
                body: Data("fixture".utf8)
            )
        }
        let url = try await server.start()
        defer { server.stop() }
        let origin = try XCTUnwrap(WebOrigin(url: url, requiresHTTPS: false))
        let timing = RuntimeOpenManualTiming()
        let app = try await makeApp(
            directTransport: nil,
            runtimeOpenTiming: timing
        )
        let runtime = try await select("instance-1", in: app)
        let attemptID = try XCTUnwrap(app.runtimeOpenAttempt?.id)
        var events: [RuntimeNavigationEvent] = []
        let controller = WebViewController(
            instanceOrigin: origin,
            websiteDataStore: .nonPersistent(),
            navigationPolicy: NavigationPolicy(instanceOrigins: [origin]),
            runtimeNavigationChanged: {
                events.append($0)
                app.handleRuntimeNavigation(
                    $0,
                    attemptID: attemptID,
                    runtime: runtime
                )
            },
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 700))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        defer {
            controller.presentedViewController?.dismiss(animated: false)
            _ = controller.invalidateForAccountTransition()
            window.rootViewController = nil
            window.isHidden = true
        }

        try await eventually { events.contains { $0.stage == .handedOff } }
        XCTAssertEqual(events.filter { $0.stage == .handedOff }.count, 1)
        XCTAssertFalse(events.contains { $0.stage == .failed })
        XCTAssertNil(app.runtimeOpenAttempt?.deadline)
        XCTAssertFalse(app.runtimeOpenAttempt?.canResumeAutomaticBudget == true)
        XCTAssertEqual(
            BrowserOverlay(
                attemptPhase: app.runtimeOpenAttempt?.phase,
                directFault: false
            ),
            .loading
        )

        timing.advance(by: .seconds(120))
        XCTAssertEqual(app.route, .browser(runtime))
    }

    @MainActor
    func testBlockedPrimaryBackNavigationSettlesOnceWithoutCoveringALoadedPage() async throws {
        let server = try LocalHTTPServer { _ in
            .html(RuntimePage.listeningDocument(title: "Instance"))
        }
        let authenticationServer = try LocalHTTPServer { _ in
            .html(RuntimePage.listeningDocument(title: "Authentication"))
        }
        let url = try await server.start()
        let authenticationURL = try await authenticationServer.start()
        defer {
            server.stop()
            authenticationServer.stop()
        }
        let origin = try XCTUnwrap(WebOrigin(url: url, requiresHTTPS: false))
        let authenticationOrigin = try XCTUnwrap(
            WebOrigin(url: authenticationURL, requiresHTTPS: false)
        )
        let app = try await makeApp(directTransport: nil)
        let runtime = try await select("instance-1", in: app)
        let attemptID = try XCTUnwrap(app.runtimeOpenAttempt?.id)
        var events: [RuntimeNavigationEvent] = []
        let controller = WebViewController(
            instanceOrigin: origin,
            websiteDataStore: .nonPersistent(),
            navigationPolicy: NavigationPolicy(
                instanceOrigins: [origin],
                authenticationOrigins: [authenticationOrigin]
            ),
            runtimeNavigationChanged: {
                events.append($0)
                app.handleRuntimeNavigation(
                    $0,
                    attemptID: attemptID,
                    runtime: runtime
                )
            },
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 700))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        defer {
            _ = controller.invalidateForAccountTransition()
            window.rootViewController = nil
            window.isHidden = true
        }
        let primary = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )

        try await eventually {
            app.runtimeOpenAttempt?.phase == .documentLoaded
        }
        _ = primary.load(URLRequest(url: authenticationURL))
        try await eventually {
            (try? await primary.evaluateJavaScript("document.title") as? String)
                == "Authentication"
        }
        _ = primary.load(URLRequest(url: url))
        try await eventually {
            (try? await primary.evaluateJavaScript("document.title") as? String)
                == "Instance" && primary.canGoBack
        }
        let cancellationsBefore = events.filter { $0.stage == .cancelled }.count
        _ = primary.goBack()
        try await eventually {
            events.filter { $0.stage == .cancelled }.count
                == cancellationsBefore + 1
        }

        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .documentLoaded)
        XCTAssertNil(app.runtimeOpenAttempt?.deadline)
        XCTAssertEqual(
            events.filter { $0.stage == .cancelled }.count,
            cancellationsBefore + 1
        )
    }

    /// The WebView's own automatic loads (its first load, and the reload
    /// after its web content process ended) pass the same gate where they
    /// are made. Held: nothing is loaded, and one pending load is kept
    /// however often it is asked for; it is made once when the gate opens.
    /// The fault, or the controller's invalidation, drops it.
    @MainActor
    func testAnAutomaticLoadThatMeetsAHeldGateIsMadeOnceWhenTheGateOpens() async throws {
        let server = try LocalHTTPServer { _ in
            .html(RuntimePage.listeningDocument(title: "Instance"))
        }
        let url = try await server.start()
        defer { server.stop() }
        let origin = try XCTUnwrap(WebOrigin(url: url, requiresHTTPS: false))
        var gate = DirectLoadGate.held
        var starts = 0
        var finishes = 0
        var events: [RuntimeNavigationEvent] = []
        let controller = WebViewController(
            instanceOrigin: origin,
            websiteDataStore: .nonPersistent(),
            navigationPolicy: NavigationPolicy(
                instanceOrigins: [origin],
                authenticationOrigins: []
            ),
            directLoadGate: { gate },
            runtimeNavigationChanged: { events.append($0) },
            didStartLoading: { starts += 1 },
            didFinishLoading: { finishes += 1 },
            didFailLoading: {}
        )
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 700))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        defer {
            window.rootViewController = nil
            window.isHidden = true
        }
        let primary = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )

        // The first load met a held gate: nothing was loaded, and a change
        // that leaves the gate held loads nothing either.
        controller.directAvailabilityDidChange()
        try await settle()
        XCTAssertEqual(starts, 0)
        XCTAssertNil(primary.url)

        // The gate opens: the one load, once.
        gate = .open
        controller.directAvailabilityDidChange()
        try await eventually { finishes == 1 }
        controller.directAvailabilityDidChange()
        try await settle()
        XCTAssertEqual(starts, 1)

        // The web content process ends while held, reported more than once:
        // nothing is reloaded until the gate opens, and then once.
        gate = .held
        controller.webViewWebContentProcessDidTerminate(primary)
        controller.webViewWebContentProcessDidTerminate(primary)
        try await settle()
        XCTAssertEqual(
            events.filter { $0.stage == .restarting }.count,
            1
        )
        XCTAssertEqual(starts, 1)
        gate = .open
        controller.directAvailabilityDidChange()
        try await eventually { starts == 2 }
        controller.directAvailabilityDidChange()
        try await settle()
        XCTAssertEqual(starts, 2)

        // With an open gate the reload is made at once, as before.
        controller.webViewWebContentProcessDidTerminate(primary)
        try await eventually { starts == 3 }

        // The fault drops a held load for good.
        gate = .held
        controller.webViewWebContentProcessDidTerminate(primary)
        gate = .fault
        controller.directAvailabilityDidChange()
        gate = .open
        controller.directAvailabilityDidChange()
        try await settle()
        XCTAssertEqual(starts, 3)

        // So does the controller's invalidation.
        gate = .held
        controller.webViewWebContentProcessDidTerminate(primary)
        _ = controller.invalidateForAccountTransition()
        gate = .open
        controller.directAvailabilityDidChange()
        try await settle()
        XCTAssertEqual(starts, 3)
    }

    /// The App allowed the open, and the App went to the background before
    /// the WebView's first load: that load is held where it is made, the
    /// return makes it once, and a later round trip makes no other. A
    /// reload held when the runtime is replaced is never made.
    @MainActor
    func testAHeldFirstLoadIsMadeOnceByTheReturnAndNeverForAReplacedRuntime() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let runtime = try await select("instance-1", in: app)
        let origin = try XCTUnwrap(WebOrigin(url: runtime.instance.url))
        var starts = 0
        let controller = WebViewController(
            instanceOrigin: origin,
            websiteDataStore: app.runtimeWebsiteDataStore,
            directStatus: app.directStatusProvider,
            directLoadGate: app.directLoadGateProvider(for: runtime),
            didStartLoading: { starts += 1 },
            didFinishLoading: {},
            didFailLoading: {}
        )
        app.registerRuntimeController(controller, for: runtime)

        enterBackground()
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 700))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        defer {
            window.rootViewController = nil
            window.isHidden = true
        }
        try await settle()
        XCTAssertEqual(starts, 0)

        enterForeground()
        try await eventually { app.directLoadGate(for: runtime) != .held }
        guard !app.directListenerFault else {
            // The fault dropped the held load.
            try await settle()
            XCTAssertEqual(starts, 0)
            return
        }
        try await eventually { starts == 1 }

        // Consumed: another background and return loads nothing more.
        enterBackground()
        enterForeground()
        try await eventually { app.directLoadGate(for: runtime) != .held }
        try await settle()
        guard !app.directListenerFault else {
            return
        }
        XCTAssertEqual(starts, 1)

        // A replacement selection ends the held runtime before the return.
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-2")
        enterForeground()
        try await eventually { app.directLoadGate(for: runtime) != .held }
        try await settle()
        XCTAssertEqual(starts, 1)
    }

    @MainActor
    func testFailureInstancePickerCoversTheSelectedRuntimeWithoutStoppingIt()
        async throws
    {
        let app = try await makeApp(directTransport: nil)
        let runtime = try await select("instance-1", in: app)
        let identity = app.runtimeIdentity

        // The native failure action uses the same sheet as the avatar and
        // leaves the selected route available for Close/current-instance.
        app.showInstancePicker()
        XCTAssertEqual(app.route, .accountOverBrowser(runtime))
        XCTAssertEqual(app.runtimeIdentity, identity)
        XCTAssertTrue(app.mobileSessionCoordinator.isCurrentRuntime(runtime))
        XCTAssertTrue(app.returnToRuntime())
        XCTAssertEqual(app.route, .browser(runtime))
    }

    /// The named entry and avatar share one presentation owner: repeated
    /// requests cannot change its mode, reload the runtime or mutate selection.
    @MainActor
    func testNamedInstanceEntryKeepsOneSheetAndReadsTheCurrentCatalogName() async throws {
        for initialMode in [MobileOnboardingStartScreen.sheet, .sheetInstances, .sheetAccount] {
            let session = SessionAnswerOverride()
            let app = try await makeApp(directTransport: nil, sessionResponse: { session.response })
            XCTAssertNil(app.selectedInstanceName())
            let runtime = try await select("instance-1", in: app)
            let identity = app.runtimeIdentity
            XCTAssertEqual(app.selectedInstanceName(), "One")

            let presentations: [(MobileOnboardingStartScreen, () -> Void)] = [
                (.sheet, app.showAccount), (.sheetInstances, app.showInstances),
                (.sheetAccount, app.showAccountRoot),
            ]
            let initial = try XCTUnwrap(presentations.first { $0.0 == initialMode })
            initial.1()
            for (_, present) in presentations { present() }
            XCTAssertEqual(app.accountSheetStartScreen, initialMode)
            XCTAssertEqual(app.route, .accountOverBrowser(runtime))
            XCTAssertEqual(app.runtimeIdentity, identity)
            XCTAssertTrue(app.mobileSessionCoordinator.isCurrentRuntime(runtime))

            session.replace(with: .json(
                """
                {"account":{"id":"account-1","email":"max@example.com"},"instances":[
                  {"id":"instance-1","name":"Renamed One","host":"instance-one.example",
                   "url":"https://instance-one.example","relationship":"owner","available":true},
                  {"id":"instance-2","name":"Two","host":"instance-two.example",
                   "url":"https://instance-two.example","relationship":"owner","available":true}
                ]}
                """
            ))
            _ = try await app.mobileSessionCoordinator.refresh()
            XCTAssertEqual(app.selectedInstanceName(), "Renamed One")
            XCTAssertEqual(runtime.instance.name, "One")
            XCTAssertEqual(app.runtimeIdentity, identity)
            XCTAssertTrue(app.returnToRuntime())
            XCTAssertEqual(app.route, .browser(runtime))
            XCTAssertFalse(app.returnToRuntime())

            // After dismissal, every entry can choose its own mode; all
            // other valid entry requests stay quiet while it is presented.
            for (mode, present) in presentations {
                present()
                for (_, duplicate) in presentations { duplicate() }
                XCTAssertEqual(app.accountSheetStartScreen, mode)
                XCTAssertEqual(app.route, .accountOverBrowser(runtime))
                XCTAssertEqual(app.runtimeIdentity, identity)
                XCTAssertTrue(app.returnToRuntime())
                XCTAssertEqual(app.route, .browser(runtime))
            }
        }
    }

    /// the App is in the background with a route, open again once the
    /// listener serves.
    @MainActor
    func testTheRuntimeWebViewIsGivenTheAppsLoadGate() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let runtime = try await select("instance-1", in: app)
        let provider = try XCTUnwrap(app.directLoadGateProvider(for: runtime))
        XCTAssertEqual(provider(), .open)
        enterBackground()
        XCTAssertEqual(provider(), .held)
        enterForeground()
        XCTAssertEqual(provider(), .held)
        try await eventually { provider() != .held }
        XCTAssertEqual(provider(), app.directListenerFault ? .fault : .open)
    }

    /// A child window is presented above the view the reopen message
    /// covers; the fault takes it away.
    @MainActor
    func testTheListenerFaultTakesAwayAPresentedChildWindow() async throws {
        let server = try LocalHTTPServer { request in
            switch request.path {
            case "/child":
                return .html(RuntimePage.listeningDocument(title: "Child"))
            default:
                return .html(
                    RuntimePage.listeningDocument(
                        title: "Instance",
                        body: """
                            <button id="open-child" onclick="window.open('/child', '_blank')">Open</button>
                            """
                    )
                )
            }
        }
        let url = try await server.start()
        defer { server.stop() }
        let origin = try XCTUnwrap(WebOrigin(url: url, requiresHTTPS: false))
        let controller = WebViewController(
            instanceOrigin: origin,
            websiteDataStore: .nonPersistent(),
            navigationPolicy: NavigationPolicy(
                instanceOrigins: [origin],
                authenticationOrigins: []
            ),
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 700))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        controller.view.frame = window.bounds
        controller.view.layoutIfNeeded()
        defer {
            controller.presentedViewController?.dismiss(animated: false)
            window.rootViewController = nil
            window.isHidden = true
        }
        let primary = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        try await eventually {
            (try? await primary.evaluateJavaScript("document.title") as? String) == "Instance"
        }
        _ = try await primary.evaluateJavaScript(
            "document.querySelector('#open-child').click(); true"
        )
        try await eventually { controller.presentedViewController != nil }

        controller.discardPopupsForListenerFault()

        try await eventually { controller.presentedViewController == nil }
    }

    func testTheListenerFaultCoversTheRuntimeWithoutRetryOrSpinner() {
        XCTAssertEqual(
            BrowserOverlay(isLoading: true, failed: true, directFault: true),
            .directFault
        )
        XCTAssertEqual(
            BrowserOverlay(isLoading: true, failed: false, directFault: true),
            .directFault
        )
        XCTAssertEqual(
            BrowserOverlay(isLoading: false, failed: true, directFault: false),
            .loadFailure
        )
        XCTAssertEqual(
            BrowserOverlay(isLoading: true, failed: false, directFault: false),
            .loading
        )
        XCTAssertEqual(
            BrowserOverlay(
                attemptPhase: .awaitingUser,
                directFault: false
            ),
            .awaitingUser
        )
        XCTAssertEqual(
            BrowserOverlay(isLoading: false, failed: false, directFault: false),
            .none
        )
    }

    func testTheReopenMessageIsTheApprovedCopyInBothLocales() throws {
        func localized(_ language: String) throws -> String {
            let path = try XCTUnwrap(Bundle.main.path(forResource: language, ofType: "lproj"))
            let bundle = try XCTUnwrap(Bundle(path: path))
            return bundle.localizedString(
                forKey: "browser.direct_fault.title",
                value: "missing",
                table: nil
            )
        }

        XCTAssertEqual(try localized("en"), "Connection lost — reopen the app")
        XCTAssertEqual(try localized("zh-Hans"), "连接中断，请重新打开 App")
    }

    func testRuntimeOpeningCopyIsApprovedInBothLocales() throws {
        func localized(_ key: String, language: String) throws -> String {
            let path = try XCTUnwrap(Bundle.main.path(forResource: language, ofType: "lproj"))
            let bundle = try XCTUnwrap(Bundle(path: path))
            return bundle.localizedString(
                forKey: key,
                value: "missing",
                table: nil
            )
        }

        let expected = [
            (
                key: "runtime.open.phase.preparing_route.named",
                en: "Preparing to connect to %@…",
                zhHans: "正在准备连接 %@…"
            ),
            (
                key: "runtime.open.stage.preparing_route",
                en: "preparing to connect",
                zhHans: "准备连接"
            ),
            (
                key: "runtime.open.stage.authenticating",
                en: "verifying sign-in",
                zhHans: "验证登录"
            ),
            (
                key: "runtime.open.awaiting_user.body",
                en: "Finish signing in on this page.",
                zhHans: "请在此页面完成登录。"
            ),
            (
                key: "runtime.open.timeout.last_stage",
                en: "Stopped while %@. Your account and data are unaffected.",
                zhHans: "停在“%@”这一步。账号和数据不受影响。"
            ),
        ]

        for copy in expected {
            XCTAssertEqual(try localized(copy.key, language: "en"), copy.en)
            XCTAssertEqual(
                try localized(copy.key, language: "zh-Hans"),
                copy.zhHans
            )
        }
    }

    // MARK: An open across the push update it starts

    // Opening a runtime resumes push registration; on a device with
    // notifications on, that sends a provider update through the account
    // coordinator while the open is still settling and while the root view
    // makes its WebView. The request keeps the selection, so the open
    // stands: the browser shows the runtime, and its WebView is kept.

    @MainActor
    func testATappedInstanceOpensAcrossThePushUpdateItStartsWithATransport() async throws {
        try await assertTheOpenStandsAcrossAPushRequest(
            directTransport: StandInDirectTransport(),
            restoring: false
        )
    }

    @MainActor
    func testATappedInstanceOpensAcrossThePushUpdateItStartsWithoutATransport() async throws {
        try await assertTheOpenStandsAcrossAPushRequest(
            directTransport: nil,
            restoring: false
        )
    }

    @MainActor
    func testARestoredInstanceOpensAcrossThePushUpdateItStartsWithATransport() async throws {
        try await assertTheOpenStandsAcrossAPushRequest(
            directTransport: StandInDirectTransport(),
            restoring: true
        )
    }

    @MainActor
    func testARestoredInstanceOpensAcrossThePushUpdateItStartsWithoutATransport() async throws {
        try await assertTheOpenStandsAcrossAPushRequest(
            directTransport: nil,
            restoring: true
        )
    }

    // What does end an open while its route is prepared still does: the
    // App returns to the instance list and never shows the runtime.

    @MainActor
    func testASignOutDuringTheOpenReturnsToTheList() async throws {
        let app = try await makeApp(directTransport: StandInDirectTransport())
        let atOpen = AtTheOpen(app) { app in
            Task { @MainActor in
                _ = try await app.mobileSessionCoordinator.logout()
            }
        }
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await XCTUnwrap(atOpen.started).value
        trackStore(of: app)
        try await eventually { app.route != .loading }

        XCTAssertEqual(app.route, .onboarding(.instances))
        XCTAssertEqual(atOpen.routes, [.loading, .onboarding(.instances)])
    }

    @MainActor
    func testANewSignInDuringTheOpenReturnsToTheList() async throws {
        let app = try await makeApp(directTransport: StandInDirectTransport())
        // Starting another sign-in advances the account generation at once.
        let atOpen = AtTheOpen(app) { app in
            Task { @MainActor in
                // The fixture has no code endpoint; the request fails
                // after the generation has moved.
                _ = try await app.mobileSessionCoordinator.requestOTP(
                    email: "other@example.com"
                )
            }
        }
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        _ = try await XCTUnwrap(atOpen.started).result
        trackStore(of: app)
        try await eventually { app.route != .loading }

        XCTAssertEqual(app.route, .onboarding(.instances))
        XCTAssertEqual(atOpen.routes, [.loading, .onboarding(.instances)])
    }

    @MainActor
    func testAChangedStoredSelectionDuringTheOpenReturnsToTheList() async throws {
        let app = try await makeApp(directTransport: StandInDirectTransport())
        let defaults = try XCTUnwrap(appDefaults)
        let atOpen = AtTheOpen(app) { _ in
            defaults.removeObject(forKey: Self.selectionKey)
            return nil
        }
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await eventually { app.route != .loading }

        XCTAssertEqual(app.route, .onboarding(.instances))
        XCTAssertEqual(atOpen.routes, [.loading, .onboarding(.instances)])
    }

    /// A refresh keeps the runtime until its answer names another account;
    /// then the account transition ends the runtime, whether the open has
    /// settled or not, and releases its WebView.
    @MainActor
    func testAnAccountChangeDuringTheOpenReturnsToTheList() async throws {
        let session = SessionAnswerOverride()
        let transport = StandInDirectTransport()
        let app = try await makeApp(
            directTransport: transport,
            sessionResponse: { session.response }
        )
        let fetched = FixtureEvent(description: "refresh fetch held")
        let releaseFetch = DispatchSemaphore(value: 0)
        defer { releaseFetch.signal() }
        session.hold(
            .json(
                """
                {"account":{"id":"account-2","email":"other@example.com"},"instances":[
                  {"id":"instance-1","name":"One","host":"instance-one.example",
                   "url":"https://instance-one.example","relationship":"owner","available":true}
                ]}
                """
            ),
            started: fetched,
            release: releaseFetch
        )
        let atOpen = AtTheOpen(app) { app in
            Task { @MainActor in
                _ = try await app.mobileSessionCoordinator.refresh()
            }
        }
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        await fulfillment(of: [fetched.expectation], timeout: 30)
        try await eventually { app.route != .loading }
        var page: RuntimePage?
        var controller: WebViewController?
        if case .browser(let runtime) = app.route {
            let made = try makeRuntimeController(app, runtime)
            app.registerRuntimeController(made, for: runtime)
            page = try RuntimePage(controller: made)
            XCTAssertNotNil(made.livePrimaryWebView)
            controller = made
        }
        defer { page?.close() }

        releaseFetch.signal()
        try await XCTUnwrap(atOpen.started).value
        trackStore(of: app)
        try await eventually { app.route == .onboarding(.instances) }
        XCTAssertNil(app.mobileSessionCoordinator.currentAccount)
        if let controller {
            XCTAssertNil(controller.livePrimaryWebView)
        }
        XCTAssertEqual(atOpen.routes.last, .onboarding(.instances))
    }

    /// A notification tap from the background: the routed instance opens
    /// while its listener is closed, so the open is held, and the push
    /// update the open started is still in flight when the return to the
    /// foreground releases it. The request keeps the selection, so the
    /// held open settles at its browser.
    @MainActor
    func testAHeldOpenSettlesAcrossThePushUpdateOnTheReturnToTheForeground() async throws {
        let transport = StandInDirectTransport()
        let hold = PushHold(path: "/api/push/apns/installations/hold/provider")
        defer { hold.release.signal() }
        let app = try await makeApp(directTransport: transport, pushHolds: [hold])
        // Both hosts are routed; instance-2 is shown.
        _ = try await select("instance-1", in: app)
        _ = try await select("instance-2", in: app)

        enterBackground()
        let atOpen = AtTheOpen(app) { app in
            Task { @MainActor in
                _ = try await app.mobileSessionCoordinator.pushManagementRequest(
                    PushManagementRequest(
                        method: "POST",
                        path: hold.path,
                        headers: [:],
                        body: nil
                    )
                )
            }
        }
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        await fulfillment(of: [hold.inFlight.expectation], timeout: 60)
        try await settle()
        // Held: the listener is closed for the background.
        XCTAssertEqual(app.route, .loading)

        enterForeground()
        try await eventually { app.route != .loading }
        guard case .browser(let runtime) = app.route else {
            return XCTFail("The held open ended at \(app.route) while a push request ran.")
        }
        XCTAssertEqual(runtime.instance.id, "instance-1")
        XCTAssertEqual(atOpen.routes, [.loading, .browser(runtime)])
        // Still in flight when it settled.
        XCTAssertFalse(app.mobileSessionCoordinator.isCurrentRuntime(runtime))
        if !app.directListenerFault {
            XCTAssertEqual(transport.selections.last?.runtime, runtime)
        }

        hold.release.signal()
        try await XCTUnwrap(atOpen.started).value
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertTrue(app.mobileSessionCoordinator.isCurrentRuntime(runtime))
    }

    /// The same held open, released while an exclusive operation that does
    /// not keep the selection runs: it returns to the list.
    @MainActor
    func testAHeldOpenReleasedDuringANonKeepingOperationReturnsToTheList() async throws {
        let transport = StandInDirectTransport()
        let hold = PushHold(path: "/api/two-factor/verify")
        defer { hold.release.signal() }
        let app = try await makeApp(directTransport: transport, pushHolds: [hold])
        _ = try await select("instance-1", in: app)
        _ = try await select("instance-2", in: app)

        enterBackground()
        let atOpen = AtTheOpen(app) { _ in nil }
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await settle()
        XCTAssertEqual(app.route, .loading)

        app.mobileSessionCoordinator.activateAccountSurface()
        let operation = Task { @MainActor in
            _ = try await app.mobileSessionCoordinator.verifySecondFactor(
                method: .totp,
                code: "123456"
            )
        }
        await fulfillment(of: [hold.inFlight.expectation], timeout: 60)

        enterForeground()
        try await eventually { app.route != .loading }
        XCTAssertEqual(app.route, .onboarding(.instances))
        XCTAssertEqual(atOpen.routes, [.loading, .onboarding(.instances)])

        hold.release.signal()
        _ = await operation.result
        app.mobileSessionCoordinator.deactivateAccountSurface()
        XCTAssertEqual(app.route, .onboarding(.instances))
    }

    /// Opens instance-1 by a tap on the list, or by the restore of a launch
    /// whose stored selection names it, and starts a push-management
    /// request at the open's first route, as the App's own resume of push
    /// registration does. The request is held in flight until the end.
    @MainActor
    private func assertTheOpenStandsAcrossAPushRequest(
        directTransport: StandInDirectTransport?,
        restoring: Bool,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async throws {
        let hold = PushHold(path: "/api/push/apns/installations/hold/provider")
        // A second signal is harmless; a failure must not leave it held.
        defer { hold.release.signal() }
        let startRequest: @MainActor (AppModel) -> Task<Void, Error>? = { app in
            Task { @MainActor in
                _ = try await app.mobileSessionCoordinator.pushManagementRequest(
                    PushManagementRequest(
                        method: "POST",
                        path: hold.path,
                        headers: [:],
                        body: nil
                    )
                )
            }
        }
        var atOpen: AtTheOpen?
        let app = try await makeApp(
            directTransport: directTransport,
            pushHolds: [hold],
            launchesWithInstanceOneSelected: restoring,
            whenTheAppExists: { app in
                if restoring {
                    atOpen = AtTheOpen(app, start: startRequest)
                }
            }
        )
        if !restoring {
            atOpen = AtTheOpen(app, start: startRequest)
            _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        }
        let open = try XCTUnwrap(atOpen, file: file, line: line)
        if restoring {
            try await waitForTheBootstrap(of: app)
        }
        // The request launches its own account transport.
        await fulfillment(of: [hold.inFlight.expectation], timeout: 60)
        try await eventually { app.route != .loading }

        guard case .browser(let runtime) = app.route else {
            hold.release.signal()
            _ = await open.started?.result
            return XCTFail(
                "The open ended at \(app.route) while a push request ran.",
                file: file,
                line: line
            )
        }
        XCTAssertEqual(runtime.instance.id, "instance-1", file: file, line: line)
        // Still in flight: the coordinator keeps the runtime but is not idle.
        XCTAssertFalse(
            app.mobileSessionCoordinator.isCurrentRuntime(runtime),
            file: file,
            line: line
        )
        XCTAssertTrue(
            app.mobileSessionCoordinator.keepsSelectedRuntime(runtime),
            file: file,
            line: line
        )

        // The root view's WebView arrives while the request still runs.
        let controller = try makeRuntimeController(app, runtime)
        app.registerRuntimeController(controller, for: runtime)
        let page = try RuntimePage(controller: controller)
        defer { page.close() }
        XCTAssertEqual(app.route, .browser(runtime), file: file, line: line)
        XCTAssertNotNil(controller.livePrimaryWebView, file: file, line: line)

        hold.release.signal()
        try await XCTUnwrap(open.started, file: file, line: line).value
        XCTAssertEqual(app.route, .browser(runtime), file: file, line: line)
        XCTAssertTrue(
            app.mobileSessionCoordinator.isCurrentRuntime(runtime),
            file: file,
            line: line
        )
        XCTAssertNotNil(controller.livePrimaryWebView, file: file, line: line)
        if let directTransport {
            XCTAssertEqual(open.routes, [.loading, .browser(runtime)], file: file, line: line)
            let target = try XCTUnwrap(directTransport.selections.last, file: file, line: line)
            XCTAssertEqual(target.runtime, runtime, file: file, line: line)
            // The registered WebView is the runtime's, once the account is idle.
            XCTAssertTrue(
                app.directRendezvousWebView(for: DirectIrohSelection(target))
                    === controller.livePrimaryWebView,
                file: file,
                line: line
            )
        } else {
            XCTAssertEqual(open.routes, [.browser(runtime)], file: file, line: line)
        }
    }


    // MARK: With a transport, with notifications on

    /// The fault exists only with a transport; the launch after it has none.
    @MainActor
    func testListenerFaultFallsBackToPublicPathOnNextLaunchWithNotificationsOn() async throws {
        try await requireAHostWithoutNotificationPermission()
        try assertSocketScanSeesListeners()
        let notifications = try NotificationsOn()
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(
            directTransport: StandInDirectTransport(),
            notifications: notifications
        ) {
            listening = try listeningSockets()
        }
        let defaults = try XCTUnwrap(appDefaults)
        let first = try await openWithNotificationsOn(
            "instance-1",
            in: app,
            notifications: notifications
        ) {
            _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        }
        defer { first.page.close() }
        XCTAssertFalse(app.directFaultCovers(first.runtime))

        // The App's port is taken while it is in the background: the return
        // cannot bind it again, which is the listener fault.
        let socket = try XCTUnwrap(try newListeners(since: listening).first)
        let port = try XCTUnwrap(NWEndpoint.Port(rawValue: socket.port))
        enterBackground()
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: socket.port,
            reuseAddress: true
        )
        defer { squatter.close() }
        enterForeground()
        try await eventually { app.directListenerFault }
        // The reopen message covers the runtime, and the next launch is marked.
        XCTAssertTrue(app.directFaultCovers(first.runtime))
        XCTAssertNotNil(defaults.object(forKey: UserDefaultsDirectFaultMarker.key))

        // Opened again under the fault, with its provider update in flight:
        // the reopen message alone.
        let before = notifications.providerPatches.arrivals
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
            notifications.providerPatches.arrivals > before
        }
        try await eventually { app.route != .loading }
        guard case .browser(let reopened) = app.route else {
            notifications.providerPatches.releaseAll()
            return XCTFail("The reopen under the fault ended at \(app.route).")
        }
        XCTAssertTrue(app.presentsDirectFaultOnly(reopened))
        try await eventually {
            notifications.providerPatches.releaseAll()
            return app.mobileSessionCoordinator.isCurrentRuntime(reopened)
        }
        XCTAssertEqual(app.route, .browser(reopened))
        XCTAssertTrue(app.presentsDirectFaultOnly(reopened))

        // The next launch, with the same preferences and the same device:
        // no transport, so the restored instance loads on the public path.
        let nextTransport = StandInDirectTransport()
        let arrivals = notifications.providerPatches.arrivals
        let next = try await makeApp(
            directTransport: nextTransport,
            reusingDefaults: defaults,
            notifications: notifications
        )
        XCTAssertNil(next.directStatusProvider)
        XCTAssertNil(defaults.object(forKey: UserDefaultsDirectFaultMarker.key))
        let restored = try await openWithNotificationsOn(
            "instance-1",
            in: next,
            notifications: notifications,
            afterArrivals: arrivals
        ) {}
        defer { restored.page.close() }
        XCTAssertFalse(next.directListenerFault)
        XCTAssertEqual(next.directLoadGate(for: restored.runtime), .open)
        XCTAssertFalse(next.presentsDirectFaultOnly(restored.runtime))
        XCTAssertEqual(nextTransport.selections.count, 0)
    }


    /// Runs `open` and checks that its provider update reaches the control
    /// plane and stays in flight while the open of `instanceID` settles at
    /// `.browser` and the root view's WebView registers and is kept; then
    /// releases it and checks again once the account coordinator is idle.
    @MainActor
    private func openWithNotificationsOn(
        _ instanceID: String,
        in app: AppModel,
        notifications: NotificationsOn,
        afterArrivals arrivals: Int? = nil,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ open: () throws -> Void
    ) async throws -> (runtime: MobileRuntimeContext, page: RuntimePage) {
        let before = arrivals ?? notifications.providerPatches.arrivals
        try open()
        // A launch reads the session first (a restore opens only then).
        try await waitForTheBootstrap(of: app)
        try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
            notifications.providerPatches.arrivals > before
        }
        try await eventually { app.route != .loading }
        guard
            case .browser(let runtime) = app.route,
            runtime.instance.id == instanceID
        else {
            notifications.providerPatches.releaseAll()
            XCTFail(
                "The open of \(instanceID) ended at \(app.route) while its provider update was in flight.",
                file: file,
                line: line
            )
            throw DirectTestTimeout()
        }
        XCTAssertFalse(
            app.mobileSessionCoordinator.isCurrentRuntime(runtime),
            "the provider update is in flight",
            file: file,
            line: line
        )
        let page: RuntimePage
        let controller: WebViewController
        do {
            controller = try makeRuntimeController(app, runtime)
            app.registerRuntimeController(controller, for: runtime)
            page = try RuntimePage(controller: controller)
        } catch {
            notifications.providerPatches.releaseAll()
            throw error
        }
        XCTAssertEqual(app.route, .browser(runtime), file: file, line: line)
        XCTAssertNotNil(controller.livePrimaryWebView, file: file, line: line)

        // Released, including any follow-up update the coordinator sends.
        try await eventually {
            notifications.providerPatches.releaseAll()
            return app.mobileSessionCoordinator.isCurrentRuntime(runtime)
        }
        XCTAssertEqual(app.route, .browser(runtime), file: file, line: line)
        XCTAssertNotNil(controller.livePrimaryWebView, file: file, line: line)
        return (runtime, page)
    }


    // MARK: Review captures (draft only, not for merge)

    /// Frames for Product: the status pill, the skeleton, and cached-then-
    /// live (mocked: PR 3 makes the cached page real). The pill, skeleton
    /// and RootView are the App's own; frame 3's page is a mock document
    /// and its timeout banner a copy proposal. Fixture data only.
    @MainActor
    func testCaptureRuntimeOpenScreensForReview() async throws {
        let studioSession = """
            {"account":{"id":"account-1","email":"alex@example.invalid"},"instances":[
              {"id":"instance-1","name":"Studio","host":"studio.example.invalid",
               "url":"https://10.255.255.1","relationship":"owner","available":true},
              {"id":"instance-2","name":"Lab","host":"lab.example.invalid",
               "url":"https://instance-two.example","relationship":"owner","available":true}
            ]}
            """
        let timing = RuntimeOpenManualTiming()
        let app = try await makeApp(
            directTransport: nil,
            sessionResponse: { .json(studioSession) },
            runtimeOpenTiming: timing,
            runtimeOpenNotificationCenter: NotificationCenter()
        )
        let scene = try XCTUnwrap(
            UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first
        )
        let window = UIWindow(windowScene: scene)
        window.frame = scene.screen.bounds
        window.windowLevel = .normal + 1
        window.overrideUserInterfaceStyle = .light
        window.rootViewController = UIHostingController(
            rootView: RootView().environmentObject(app)
        )
        window.makeKeyAndVisible()

        // 2a: a cold start with no cached page: the skeleton, and the pill
        // below its top bar (the real RootView, connecting to an instance
        // that does not answer).
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await Task.sleep(for: .milliseconds(1500))
        capture(window, app, "2a-skeleton-connecting-light")
        window.overrideUserInterfaceStyle = .dark
        try await Task.sleep(for: .milliseconds(1200))
        capture(window, app, "2a-skeleton-connecting-dark")
        window.overrideUserInterfaceStyle = .light
        let attempt = try XCTUnwrap(app.runtimeOpenAttempt)

        // 1a and 3b (mock page with the Workbench's top bar and tab bar).
        let page = MockCachedPage()
        let mock = UIWindow(windowScene: scene)
        mock.frame = scene.screen.bounds
        mock.windowLevel = .normal + 2
        mock.overrideUserInterfaceStyle = .light
        let host = UIHostingController(rootView: AnyView(EmptyView()))
        mock.rootViewController = host
        mock.makeKeyAndVisible()
        func show(_ view: some View) async throws {
            host.rootView = AnyView(view)
            try await Task.sleep(for: .milliseconds(900))
        }
        try await page.load()
        // The first WebKit paint of the mock page takes a while on CI.
        try await Task.sleep(for: .seconds(3))
        var connecting = attempt
        connecting.phase = .connecting
        connecting.isTakingLong = false
        try await show(ZStack(alignment: .top) {
            page.view
            RuntimeOpenStatusPill(attempt: connecting, cancel: {})
        })
        try await Task.sleep(for: .seconds(2))
        captureWindow(mock, "1a-pill-connecting-over-cached-page-light")
        try await page.loadChat()
        try await Task.sleep(for: .seconds(2))
        try await show(ZStack(alignment: .top) {
            page.view
            RuntimeOpenStatusPill(attempt: connecting, cancel: {})
        })
        try await Task.sleep(for: .seconds(1))
        captureWindow(mock, "1a-pill-connecting-over-cached-chat-light")
        print("[top bar overlap] chat: \(await page.topBarOverlaps(windowWidth: Double(mock.bounds.width)))")
        // A regular session instead: TitleField's rename button, a long title.
        _ = try? await page.webView.evaluateJavaScript(
            "document.querySelector('.bot').outerHTML = '<button class=\"bot\" data-tap=\"标题（点按改名）\" style=\"flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis\"><b>冷启动方案评审与三张帧图的修改意见</b></button>'"
        )
        try await Task.sleep(for: .milliseconds(500))
        captureWindow(mock, "1a-pill-connecting-over-cached-session-light")
        print("[top bar overlap] session: \(await page.topBarOverlaps(windowWidth: Double(mock.bounds.width)))")
        try await page.load()
        try await Task.sleep(for: .seconds(2))
        try await show(ZStack(alignment: .bottom) {
            page.view
            CachedPageTimeoutBannerProposal(instanceName: "Studio")
        })
        captureWindow(mock, "3b-timeout-banner-over-cached-page-light")
        mock.overrideUserInterfaceStyle = .dark
        try await page.load(dark: true)
        try await show(ZStack(alignment: .top) {
            page.view
            RuntimeOpenStatusPill(attempt: connecting, cancel: {})
        })
        captureWindow(mock, "1a-pill-connecting-over-cached-page-dark")
        try await show(ZStack(alignment: .bottom) {
            page.view
            CachedPageTimeoutBannerProposal(instanceName: "Studio")
        })
        captureWindow(mock, "3b-timeout-banner-over-cached-page-dark")
        // Leave the windows alive past the test's own teardown (the earlier
        // run crashed releasing them mid-teardown).
        Self.retainedWindows += [mock, window]
    }

    private static var retainedWindows: [UIWindow] = []

    @MainActor
    private func captureWindow(_ window: UIWindow, _ name: String) {
        let language = Bundle.main.preferredLocalizations.first ?? "unknown"
        let image = UIGraphicsImageRenderer(bounds: window.bounds).image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        let shot = XCTAttachment(image: image)
        shot.name = "coldstart-\(language)-\(name)"
        shot.lifetime = .keepAlways
        add(shot)
        print("[review capture] \(name) language=\(language)")
    }

    @MainActor
    private func capture(_ window: UIWindow, _ app: AppModel, _ name: String) {
        let language = Bundle.main.preferredLocalizations.first ?? "unknown"
        let renderer = UIGraphicsImageRenderer(bounds: window.bounds)
        let image = renderer.image { _ in
            window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
        }
        let shot = XCTAttachment(image: image)
        shot.name = "runtime-open-\(language)-\(name)"
        shot.lifetime = .keepAlways
        add(shot)
        let state = XCTAttachment(string: """
            \(name): language=\(language) route=\(app.route)
            attempt=\(String(describing: app.runtimeOpenAttempt))
            notice=\(String(describing: app.runtimeOpenNotice))
            """)
        state.name = "runtime-open-\(language)-\(name)-state"
        state.lifetime = .keepAlways
        add(state)
        print("[review capture] \(name) language=\(language) route=\(app.route) phase=\(String(describing: app.runtimeOpenAttempt?.phase)) long=\(String(describing: app.runtimeOpenAttempt?.isTakingLong)) notice=\(String(describing: app.runtimeOpenNotice))")
    }

    // MARK: The App as shipped, with notifications on

    // Release checks for the App as it ships (no direct transport). Each
    // open resumes push registration; the device token is delivered to the
    // App's real `didRegister`, and the App's push coordinator sends the
    // provider update through the account coordinator, held in flight by
    // the control plane until the open's WebView has registered.
    //
    // `RootViewStandIn` makes and registers the runtime's WebView the way
    // the root view does. With `.nextUpdate` it does so on the main actor
    // right after the route changes, as SwiftUI's next update does, which
    // is before the provider update starts. With `.afterTheProviderUpdate`
    // it waits until that update is in flight: the race where the update
    // starts first (from recovery, or a late view update). The update keeps
    // the selection, so the registration is accepted either way.

    @MainActor
    func testColdStartRestoresLastInstanceWithNotificationsOn() async throws {
        try await assertColdStartRestoresInstanceOne(registration: .nextUpdate)
    }

    @MainActor
    func testTenConsecutiveSwitchesOpenWithNotificationsOn() async throws {
        try await assertTenSwitchesOpen(registration: .nextUpdate)
    }

    @MainActor
    func testColdStartRestoresLastInstanceWithNotificationsOnWithATransport() async throws {
        try await assertColdStartRestoresInstanceOne(
            registration: .nextUpdate,
            directTransport: StandInDirectTransport()
        )
    }

    @MainActor
    func testTenConsecutiveSwitchesOpenWithNotificationsOnWithATransport() async throws {
        try await assertTenSwitchesOpen(
            registration: .nextUpdate,
            directTransport: StandInDirectTransport()
        )
    }

    @MainActor
    func testColdStartRestoresLastInstanceWhenItsProviderUpdateIsInFlightAtRegistration() async throws {
        try await assertColdStartRestoresInstanceOne(registration: .afterTheProviderUpdate)
    }

    @MainActor
    func testTenConsecutiveSwitchesOpenWhenEachProviderUpdateIsInFlightAtRegistration() async throws {
        try await assertTenSwitchesOpen(registration: .afterTheProviderUpdate)
    }

    @MainActor
    private func assertColdStartRestoresInstanceOne(
        registration: RootViewStandIn.Registration,
        directTransport: StandInDirectTransport? = nil
    ) async throws {
        try await requireAHostWithoutNotificationPermission()
        let notifications = try NotificationsOn()
        var root: RootViewStandIn?
        let app = try await makeApp(
            directTransport: directTransport,
            launchesWithInstanceOneSelected: true,
            notifications: notifications,
            whenTheAppExists: { app in
                root = RootViewStandIn(
                    app: app,
                    notifications: notifications,
                    registration: registration
                )
            }
        )
        let view = try XCTUnwrap(root)
        try await waitForTheBootstrap(of: app)
        try await assertOpens(
            "instance-1",
            in: app,
            root: view,
            notifications: notifications,
            afterArrivals: 0,
            // The restore may have registered while the bootstrap was awaited.
            afterRegistrations: 0,
            registration: registration
        ) {}
    }

    @MainActor
    private func assertTenSwitchesOpen(
        registration: RootViewStandIn.Registration,
        directTransport: StandInDirectTransport? = nil
    ) async throws {
        try await requireAHostWithoutNotificationPermission()
        let notifications = try NotificationsOn()
        let app = try await makeApp(
            directTransport: directTransport,
            notifications: notifications
        )
        let root = RootViewStandIn(
            app: app,
            notifications: notifications,
            registration: registration
        )
        for index in 0..<10 {
            let instanceID = index.isMultiple(of: 2) ? "instance-1" : "instance-2"
            try await assertOpens(
                instanceID,
                in: app,
                root: root,
                notifications: notifications,
                afterArrivals: notifications.providerPatches.arrivals,
                registration: registration,
                step: "switch \(index + 1)"
            ) {
                _ = try app.mobileSessionCoordinator.selectInstance(id: instanceID)
            }
        }
        XCTAssertGreaterThanOrEqual(notifications.providerPatches.arrivals, 10)
    }

    // What the registration must still refuse while a provider update is in
    // flight: the WebView is invalidated, and an abandoned browser returns
    // to the instance list.

    @MainActor
    func testASignOutStillRefusesTheRegistrationWhileAProviderUpdateIsInFlight() async throws {
        try await requireAHostWithoutNotificationPermission()
        let notifications = try NotificationsOn()
        let app = try await makeApp(directTransport: nil, notifications: notifications)
        defer { notifications.providerPatches.releaseAll() }
        let runtime = try await openHoldingItsProviderUpdate("instance-1", in: app, notifications: notifications)

        // Sign-out revokes the browser synchronously, before push cleanup
        // finishes behind the held provider update. A late WebView must still
        // be refused while that sign-out is in flight.
        final class Finished {
            var value = false
        }
        let signedOut = Finished()
        let signOut = Task { @MainActor in
            defer { signedOut.value = true }
            _ = try await app.mobileSessionCoordinator.logout()
        }
        try await eventually { !app.mobileSessionCoordinator.isSelectedRuntime(runtime) }
        XCTAssertFalse(signedOut.value, "registration must be tested before the held sign-out finishes")
        XCTAssertEqual(app.route, .onboarding(.instances))
        let controller = registerTheRootViewsController(for: runtime, in: app)
        XCTAssertNil(controller.livePrimaryWebView)
        XCTAssertEqual(app.route, .onboarding(.instances))

        // Released until the sign-out ends: its cleanup may send one more
        // provider update, which the gate would otherwise hold.
        // Sign-out logs out through an account transport it may launch.
        try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
            notifications.providerPatches.releaseAll()
            return signedOut.value
        }
        _ = await signOut.result
        trackStore(of: app)
        XCTAssertEqual(app.route, .onboarding(.instances))
    }

    /// Another sign-in begins (the account and its generation change), and
    /// then a provider update runs when the old runtime's WebView arrives.
    @MainActor
    func testAnAccountSwitchStillRefusesTheRegistrationWhileAProviderUpdateIsInFlight() async throws {
        try await requireAHostWithoutNotificationPermission()
        let notifications = try NotificationsOn()
        let hold = PushHold(path: "/api/push/apns/installations/hold/provider")
        defer { hold.release.signal() }
        let app = try await makeApp(
            directTransport: nil,
            pushHolds: [hold],
            notifications: notifications
        )
        let runtime = try await openHoldingItsProviderUpdate("instance-1", in: app, notifications: notifications)
        try await eventually {
            notifications.providerPatches.releaseAll()
            return app.mobileSessionCoordinator.isCurrentRuntime(runtime)
        }

        // The fixture has no code endpoint: the sign-in fails after the
        // account transition has begun.
        _ = try? await app.mobileSessionCoordinator.requestOTP(email: "other@example.com")
        trackStore(of: app)
        XCTAssertFalse(app.mobileSessionCoordinator.isSelectedRuntime(runtime))
        // Every account transition moves the route synchronously, so the
        // route check is what refuses here; the coordinator would too.
        XCTAssertEqual(app.route, .onboarding(.instances))
        let management = try await holdPushManagement(hold, in: app)
        let controller = registerTheRootViewsController(for: runtime, in: app)
        XCTAssertNil(controller.livePrimaryWebView)
        XCTAssertEqual(app.route, .onboarding(.instances))

        hold.release.signal()
        _ = await management.result
        XCTAssertEqual(app.route, .onboarding(.instances))
    }

    /// The stored selection no longer names the runtime when its WebView
    /// arrives, while its provider update is in flight.
    @MainActor
    func testAChangedStoredSelectionStillRefusesTheRegistrationWhileAProviderUpdateIsInFlight() async throws {
        try await requireAHostWithoutNotificationPermission()
        let notifications = try NotificationsOn()
        let app = try await makeApp(directTransport: nil, notifications: notifications)
        defer { notifications.providerPatches.releaseAll() }
        let runtime = try await openHoldingItsProviderUpdate("instance-1", in: app, notifications: notifications)

        let defaults = try XCTUnwrap(appDefaults)
        defaults.set(
            Data(
                #"{"accountID":"account-1","instanceID":"instance-2","url":"https://instance-two.example"}"#.utf8
            ),
            forKey: Self.selectionKey
        )
        let controller = registerTheRootViewsController(for: runtime, in: app)
        XCTAssertNil(controller.livePrimaryWebView)
        XCTAssertEqual(app.route, .onboarding(.instances))
    }

    /// A different instance was opened meanwhile, and its own provider
    /// update is in flight when the first runtime's WebView arrives late:
    /// that WebView is refused and the newer browser stays.
    @MainActor
    func testADifferentInstanceOpenedMeanwhileRefusesTheLateRegistrationWhileAProviderUpdateIsInFlight() async throws {
        try await requireAHostWithoutNotificationPermission()
        let notifications = try NotificationsOn()
        let app = try await makeApp(directTransport: nil, notifications: notifications)
        defer { notifications.providerPatches.releaseAll() }
        let first = try await openHoldingItsProviderUpdate("instance-1", in: app, notifications: notifications)
        try await eventually {
            notifications.providerPatches.releaseAll()
            return app.mobileSessionCoordinator.isCurrentRuntime(first)
        }
        let second = try await openHoldingItsProviderUpdate("instance-2", in: app, notifications: notifications)

        // The newer open moved the route, so the route check refuses the
        // late WebView. The newer runtime's own WebView, registering while
        // its update is in flight, is the part the fix keeps.
        let late = registerTheRootViewsController(for: first, in: app)
        XCTAssertNil(late.livePrimaryWebView)
        XCTAssertEqual(app.route, .browser(second))
        let current = registerTheRootViewsController(for: second, in: app)
        XCTAssertNotNil(current.livePrimaryWebView)
        XCTAssertEqual(app.route, .browser(second))
    }

    /// Opens `instanceID` by a tap and returns once its provider update is
    /// in flight, before any WebView has registered.
    @MainActor
    private func openHoldingItsProviderUpdate(
        _ instanceID: String,
        in app: AppModel,
        notifications: NotificationsOn
    ) async throws -> MobileRuntimeContext {
        let before = notifications.providerPatches.arrivals
        _ = try app.mobileSessionCoordinator.selectInstance(id: instanceID)
        guard case .browser(let runtime) = app.route, runtime.instance.id == instanceID else {
            XCTFail("The open of \(instanceID) ended at \(app.route).")
            throw DirectTestTimeout()
        }
        try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
            notifications.providerPatches.arrivals > before
        }
        XCTAssertFalse(app.mobileSessionCoordinator.isCurrentRuntime(runtime))
        return runtime
    }

    /// The controller the root view makes for `runtime`, registered, with
    /// its view loaded (an invalidated one has no live WebView).
    @MainActor
    private func registerTheRootViewsController(
        for runtime: MobileRuntimeContext,
        in app: AppModel
    ) -> WebViewController {
        let controller = WebViewController(
            instanceOrigin: WebOrigin(url: runtime.instance.url)!,
            websiteDataStore: app.runtimeWebsiteDataStore,
            directStatus: app.directStatusProvider,
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        app.registerRuntimeController(controller, for: runtime)
        controller.loadViewIfNeeded()
        return controller
    }

    /// Runs `open`, then checks the open of `instanceID` end to end: its
    /// provider update reaches the control plane, the browser shows the
    /// runtime, the root view's WebView registers and is kept, and after
    /// the update is released and the account coordinator is idle, the
    /// browser and the WebView are still there.
    @MainActor
    private func assertOpens(
        _ instanceID: String,
        in app: AppModel,
        root: RootViewStandIn,
        notifications: NotificationsOn,
        afterArrivals arrivals: Int,
        afterRegistrations registrationsBefore: Int? = nil,
        registration: RootViewStandIn.Registration = .nextUpdate,
        step: String = "",
        file: StaticString = #filePath,
        line: UInt = #line,
        _ open: () throws -> Void
    ) async throws {
        defer { notifications.providerPatches.releaseAll() }
        let registered = registrationsBefore ?? root.registrations.count
        try open()
        try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
            notifications.providerPatches.arrivals > arrivals
                && root.registrations.count > registered
        }
        let made = try XCTUnwrap(root.registrations.last, step, file: file, line: line)
        XCTAssertEqual(made.runtime.instance.id, instanceID, step, file: file, line: line)
        XCTAssertEqual(
            app.route,
            .browser(made.runtime),
            "\(step): the WebView registered while the provider update was \(made.providerUpdateInFlight ? "in flight" : "not yet sent")",
            file: file,
            line: line
        )
        XCTAssertNotNil(made.controller.livePrimaryWebView, step, file: file, line: line)
        if registration == .afterTheProviderUpdate {
            // The race really happened: the update was in flight.
            XCTAssertTrue(made.providerUpdateInFlight, step, file: file, line: line)
        }

        // Released, including any follow-up update the coordinator sends.
        try await eventually {
            notifications.providerPatches.releaseAll()
            return app.mobileSessionCoordinator.isCurrentRuntime(made.runtime)
                || app.route != .browser(made.runtime)
        }
        XCTAssertEqual(app.route, .browser(made.runtime), step, file: file, line: line)
        XCTAssertNotNil(made.controller.livePrimaryWebView, step, file: file, line: line)
    }

    // MARK: The App with a transport

    /// The registered App's positive control, with a stand-in transport so
    /// this test exercises the App/seam composition without native iroh:
    /// the route is written before the first load, the capability reaches the
    /// page, and direct path reports change its status.
    @MainActor
    func testWithATransportTheRouteIsWrittenBeforeTheFirstLoadAndTheStatusReachesThePage() async throws {
        let transport = StandInDirectTransport()
        let routeWrites = try ProxyConfigurationWrites.count()
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(directTransport: transport) {
            listening = try listeningSockets()
        }
        let firstStore = app.runtimeWebsiteDataStore
        // What the store's route is each time a browser route is set.
        var routesAtOpen: [[String]] = []
        let opened = app.$route.sink { route in
            if case .browser = route {
                routesAtOpen.append(
                    firstStore.proxyConfigurations.flatMap(\.matchDomains)
                )
            }
        }
        defer { opened.cancel() }

        // The runtime waits for its route.
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        XCTAssertEqual(app.route, .loading)
        let runtime = try await waitForBrowser("instance-1", in: app)
        XCTAssertEqual(routesAtOpen, [["instance-one.example"]])
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites + 1)
        XCTAssertEqual(try newListeners(since: listening).map(\.address), ["127.0.0.1"])

        let page = try await showRuntimePage(app, runtime)
        let surface = try await page.directSurface()
        XCTAssertEqual(
            surface,
            #"{"capability":1,"status":{"closure":0,"preferred":"tunnel","sequence":0}}"#
        )
        let target = try XCTUnwrap(transport.selections.last)
        XCTAssertEqual(target.runtime, runtime)
        XCTAssertEqual(target.storeIdentifier, app.websiteDataStore.identifier)

        // The transport finds a path: the page is told, and asks.
        transport.report(.usable(generation: "g1"), for: target)
        try await eventually {
            try await page.changes()
                == #"[{"closure":0,"preferred":"direct","sequence":1}]"#
        }
        let after = try await page.directStatus()
        XCTAssertEqual(after, #"{"closure":0,"preferred":"direct","sequence":1}"#)
        page.close()

        // Another instance joins the same route; nothing is removed.
        _ = try await select("instance-2", in: app)
        XCTAssertEqual(
            firstStore.proxyConfigurations.first?.matchDomains,
            ["instance-one.example", "instance-two.example"]
        )
        XCTAssertEqual(try ProxyConfigurationWrites.count(), routeWrites + 2)
        // A late report for the first instance changes nothing.
        transport.report(.lost, for: target)
        XCTAssertEqual(directStatus(app, instance)?.sequence, 2)

        // Signing out rotates the store; the new one starts with no route
        // and the old one keeps its own until it is discarded.
        let writes = try ProxyConfigurationWrites.count()
        _ = try await app.mobileSessionCoordinator.logout()
        trackStore(of: app)
        XCTAssertFalse(app.runtimeWebsiteDataStore === firstStore)
        XCTAssertEqual(app.runtimeWebsiteDataStore.proxyConfigurations.count, 0)
        XCTAssertEqual(try ProxyConfigurationWrites.count(), writes)
    }

    @MainActor
    func testAnOpenReplacedWhileItsRouteWasPreparedNeverShowsItsBrowser() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        var browsers: [String] = []
        let opened = app.$route.sink { route in
            if case .browser(let runtime) = route {
                browsers.append(runtime.instance.id)
            }
        }
        defer { opened.cancel() }

        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-2")
        _ = try await waitForBrowser("instance-2", in: app)
        try await Task.sleep(for: .milliseconds(300))

        XCTAssertEqual(browsers, ["instance-2"])
        XCTAssertEqual(
            transport.selections.map(\.runtime.instance.id),
            ["instance-2"]
        )
    }

    // MARK: Account over the runtime while a push request runs

    /// The avatar while a push request the App made on its own is running:
    /// Account covers the runtime (it used to close it and show the plain
    /// account page), and Back returns to it.
    @MainActor
    func testAccountCoversTheRuntimeAndReturnsToItWhileAPushRequestRuns() async throws {
        let hold = PushHold(path: "/api/push/hold-account")
        let app = try await makeApp(directTransport: nil, pushHolds: [hold])
        let runtime = try await select("instance-1", in: app)
        // The App holds its runtime's controller weakly; the view would.
        let controller = try makeRuntimeController(app, runtime)
        app.registerRuntimeController(controller, for: runtime)
        XCTAssertEqual(app.route, .browser(runtime))

        let management = try await holdPushManagement(hold, in: app)
        app.showAccount()
        XCTAssertEqual(app.route, .accountOverBrowser(runtime))
        XCTAssertTrue(app.returnToRuntime())
        XCTAssertEqual(app.route, .browser(runtime))

        hold.release.signal()
        try await management.value
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertFalse(app.mobileSessionCoordinator.isAccountSurfaceActive)
        withExtendedLifetime(controller) {}
    }

    /// Exercise the real AppModel -> NativePushCoordinator cleanup callback,
    /// not just the coordinator hook: opening Account in this early window
    /// must bootstrap signed out rather than leave the new page on bootError.
    @MainActor
    func testAccountReopenedDuringRefreshPushCleanupBootstrapsSignedOut() async throws {
        let session = SessionAnswerOverride()
        let keychain = InMemoryPushKeychainStore()
        let marker = InMemoryPushInstallationMarkerStore()
        let center = HeldCleanupNotificationCenter()
        let app = try await makeApp(
            directTransport: nil,
            sessionResponse: { session.response },
            pushKeychain: keychain,
            pushInstallationMarker: marker,
            pushNotificationCenter: center
        )
        let runtime = try await select("instance-1", in: app)
        let installationID = UUID()
        marker.installationID = installationID
        keychain.installation = PushInstallationState(
            installationID: installationID,
            installationSecret: Data(repeating: 12, count: 32),
            accountID: "account-1", bindGeneration: 1,
            providerGeneration: 0, provider: nil,
            pendingBulkRevokeThroughGeneration: nil
        )
        // A retired key is enough for notification cleanup, without enabling
        // APNs registration, prompting permissions, or recovery networking.
        try keychain.saveRecord(PushKeyRecord(
            kid: "fixture-retired-key", instanceOrigin: "https://instance-one.example",
            accountID: "account-1", accountGeneration: 1, bindGeneration: 1,
            privateKey: Data(), authSecret: Data(), vapidKeyHash: "fixture",
            state: .retiring, pendingRevoke: false, replacementRegistrationIDs: [],
            retirementDeadline: .distantFuture
        ))
        let fetched = FixtureEvent(description: "refresh fetch held before 401")
        let releaseFetch = DispatchSemaphore(value: 0)
        defer { releaseFetch.signal() }
        session.holdSignedOut(started: fetched, release: releaseFetch)
        app.showAccount()
        app.mobileSessionCoordinator.activateAccountSurface()
        let refresh = Task { try await app.mobileSessionCoordinator.refresh() }
        // The refresh launches its own account transport first.
        await fulfillment(
            of: [fetched.expectation],
            timeout: WebKitReadiness.standardTransportOperationSeconds
        )
        XCTAssertTrue(app.returnToRuntime())
        XCTAssertEqual(app.route, .browser(runtime))
        releaseFetch.signal()
        await fulfillment(of: [center.cleanupStarted], timeout: 30)

        app.showAccount()
        XCTAssertEqual(app.route, .onboarding(.account))
        app.mobileSessionCoordinator.activateAccountSurface()
        let replacement: Result<MobileSessionState, Error>
        do {
            replacement = .success(try await app.mobileSessionCoordinator.bootstrap())
        } catch {
            replacement = .failure(error)
        }
        center.releaseCleanup()
        let final = try await refresh.value
        trackStore(of: app)
        XCTAssertEqual(try replacement.get(), .signedOut)
        XCTAssertEqual(final, .signedOut)
        XCTAssertNil(app.mobileSessionCoordinator.currentAccount)
        XCTAssertEqual(app.route, .onboarding(.account))
    }

    // MARK: A runtime abandoned when its WebView registers

    @MainActor
    func testARuntimeAbandonedAtRegistrationEndsItsDirectSelection() async throws {
        let transport = StandInDirectTransport()
        // An exclusive operation that does not keep the selection and leaves
        // the account as it is: a second-factor answer the server holds.
        let hold = PushHold(path: "/api/two-factor/verify")
        let app = try await makeApp(
            directTransport: transport,
            pushHolds: [hold]
        )

        let runtime = try await select("instance-1", in: app)
        let target = try XCTUnwrap(transport.selections.last)
        XCTAssertEqual(target.runtime, runtime)

        // That operation is in flight when the root view's WebView arrives:
        // the App refuses it and returns to the instance picker, without the
        // usual stop of a runtime. (A push-management request keeps the
        // selection and does not; see the next check.)
        app.mobileSessionCoordinator.activateAccountSurface()
        let operation = Task { @MainActor in
            _ = try await app.mobileSessionCoordinator.verifySecondFactor(
                method: .totp,
                code: "123456"
            )
        }
        // The operation launches its own account transport first.
        await fulfillment(
            of: [hold.inFlight.expectation],
            timeout: WebKitReadiness.standardTransportOperationSeconds
        )
        XCTAssertFalse(app.mobileSessionCoordinator.isCurrentRuntime(runtime))
        XCTAssertFalse(app.mobileSessionCoordinator.keepsSelectedRuntime(runtime))
        XCTAssertTrue(app.mobileSessionCoordinator.isSelectedRuntime(runtime))
        let deselected = transport.deselectCount
        app.registerRuntimeController(
            try makeRuntimeController(app, runtime),
            for: runtime
        )
        XCTAssertTrue(isOnInstancePicker(app))
        XCTAssertEqual(transport.deselectCount, deselected + 1)

        // The operation ends without changing the account. It still has its
        // instance, so only the ended selection stands between a report and
        // the direct path.
        hold.release.signal()
        _ = await operation.result
        app.mobileSessionCoordinator.deactivateAccountSurface()
        try await reportUsableWhileTheAccountIsIdle(
            transport,
            for: target,
            generation: "g1",
            runtime: runtime,
            in: app
        )
        XCTAssertEqual(directStatus(app, instance)?.preferred, .tunnel)

        // A return from the background does not select it again.
        let selections = transport.selections.count
        NotificationCenter.default.post(
            name: UIApplication.didEnterBackgroundNotification,
            object: nil
        )
        NotificationCenter.default.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertEqual(transport.selections.count, selections)
    }

    @MainActor
    func testRuntimeControllerRegistrationSurvivesOrdinaryAccountWork() async throws {
        let transport = StandInDirectTransport()
        let hold = PushHold(path: "/api/push/hold-1")
        let app = try await makeApp(
            directTransport: transport,
            pushHolds: [hold]
        )

        let runtime = try await select("instance-1", in: app)
        let target = try XCTUnwrap(transport.selections.last)
        XCTAssertEqual(target.runtime, runtime)

        // A push-management request preserves the account, store, and
        // selected runtime. A controller that arrives during it therefore
        // still belongs to the current attempt and must not close that
        // browser or its direct selection.
        let management = try await holdPushManagement(hold, in: app)
        XCTAssertFalse(app.mobileSessionCoordinator.isCurrentRuntime(runtime))
        XCTAssertTrue(app.mobileSessionCoordinator.keepsSelectedRuntime(runtime))
        XCTAssertTrue(app.mobileSessionCoordinator.isSelectedRuntime(runtime))
        let deselected = transport.deselectCount
        let controller = try makeRuntimeController(app, runtime)
        app.registerRuntimeController(controller, for: runtime)
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertEqual(transport.deselectCount, deselected)

        hold.release.signal()
        try await management.value
        try await reportUsableWhileTheAccountIsIdle(
            transport,
            for: target,
            generation: "g1",
            runtime: runtime,
            in: app
        )
        XCTAssertEqual(directStatus(app, instance)?.preferred, .direct)

        // A background return re-establishes transport for the same browser;
        // it does not send the user to the picker.
        NotificationCenter.default.post(
            name: UIApplication.didEnterBackgroundNotification,
            object: nil
        )
        NotificationCenter.default.post(
            name: UIApplication.willEnterForegroundNotification,
            object: nil
        )
        try await eventually { app.directLoadGate(for: runtime) != .held }
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertTrue(app.mobileSessionCoordinator.isSelectedRuntime(runtime))
        withExtendedLifetime(controller) {}
    }

    @MainActor
    func testALateControllerForAnOlderRuntimeLeavesTheNewerSelectionAlone() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let second = try XCTUnwrap(WebOrigin.parseConfigured("https://instance-two.example"))

        let older = try await select("instance-1", in: app)
        let newer = try await select("instance-2", in: app)
        let target = try XCTUnwrap(transport.selections.last)
        XCTAssertEqual(target.runtime, newer)

        // The WebView made for the first runtime arrives after the second
        // opened. It is refused; the browser and its selection stay.
        let deselected = transport.deselectCount
        app.registerRuntimeController(
            try makeRuntimeController(app, older),
            for: older
        )
        guard case .browser(let shown) = app.route else {
            return XCTFail("The newer runtime's browser was closed.")
        }
        XCTAssertEqual(shown, newer)
        XCTAssertEqual(transport.deselectCount, deselected)
        try await eventually {
            if app.mobileSessionCoordinator.isCurrentRuntime(newer) {
                transport.report(.usable(generation: "g1"), for: target)
            }
            return self.directStatus(app, second)?.preferred == .direct
        }
    }

    @MainActor
    func testALateControllerForAnOlderAttemptOfTheSameRuntimeLeavesTheRetryAlone()
        async throws
    {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let runtime = try await select("instance-1", in: app)
        let olderAttemptID = try XCTUnwrap(app.runtimeOpenAttempt?.id)
        let olderController = try makeRuntimeController(app, runtime)

        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .failed, destination: .instance),
            attemptID: olderAttemptID,
            runtime: runtime
        )
        XCTAssertTrue(app.retryRuntimeOpen(attemptID: olderAttemptID))
        _ = try await waitForBrowser("instance-1", in: app)
        let retryID = try XCTUnwrap(app.runtimeOpenAttempt?.id)
        XCTAssertGreaterThan(retryID, olderAttemptID)

        // The old BrowserScreen captured the old attempt id. Its controller
        // arriving now is rejected and released, but it cannot retire the
        // newer attempt merely because both attempts name the same runtime.
        let deselected = transport.deselectCount
        app.registerRuntimeController(
            olderController,
            for: runtime,
            attemptID: olderAttemptID
        )
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertEqual(app.runtimeOpenAttempt?.id, retryID)
        XCTAssertEqual(app.runtimeOpenAttempt?.runtime, runtime)
        XCTAssertEqual(transport.deselectCount, deselected)
        withExtendedLifetime(olderController) {}
    }

    // MARK: The transport's rendezvous WebView

    /// The rendezvous request runs in the primary WebView of the runtime
    /// the controller selected, and only for the controller's current
    /// target.
    @MainActor
    func testTheRendezvousWebViewIsTheSelectedRuntimesPrimaryAndNothingElse() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let runtime = try await select("instance-1", in: app)
        let target = try XCTUnwrap(transport.selections.last)
        let selection = DirectIrohSelection(target)
        XCTAssertEqual(selection.number, target.selection)
        XCTAssertEqual(selection.instanceID, "instance-1")
        XCTAssertEqual(selection.host, "instance-one.example")
        XCTAssertEqual(selection.storeIdentifier, app.websiteDataStore.identifier)

        // Selected, but the runtime's WebView is not made yet.
        XCTAssertNil(app.directRendezvousWebView(for: selection))

        let controller = try makeRuntimeController(app, runtime)
        app.registerRuntimeController(controller, for: runtime)
        let page = try RuntimePage(controller: controller)
        let primary = try XCTUnwrap(controller.livePrimaryWebView)
        XCTAssertTrue(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first === primary
        )
        XCTAssertTrue(app.directRendezvousWebView(for: selection) === primary)

        // A selection that differs from the target in any field is refused.
        let others = [
            DirectIrohSelection(
                number: selection.number + 1,
                instanceID: selection.instanceID,
                host: selection.host,
                storeIdentifier: selection.storeIdentifier
            ),
            DirectIrohSelection(
                number: selection.number,
                instanceID: "instance-2",
                host: selection.host,
                storeIdentifier: selection.storeIdentifier
            ),
            DirectIrohSelection(
                number: selection.number,
                instanceID: selection.instanceID,
                host: "instance-two.example",
                storeIdentifier: selection.storeIdentifier
            ),
            DirectIrohSelection(
                number: selection.number,
                instanceID: selection.instanceID,
                host: selection.host,
                storeIdentifier: UUID()
            ),
        ]
        for other in others {
            XCTAssertNil(app.directRendezvousWebView(for: other), "\(other)")
        }

        // Covered by Account, the runtime keeps its document and its WebView.
        app.showAccount()
        XCTAssertEqual(app.route, .accountOverBrowser(runtime))
        XCTAssertTrue(app.directRendezvousWebView(for: selection) === primary)
        XCTAssertTrue(app.returnToRuntime())
        XCTAssertTrue(app.directRendezvousWebView(for: selection) === primary)

        // Another instance: the first target is over, its WebView is
        // released, and the second runtime has no WebView yet.
        page.close()
        _ = try await select("instance-2", in: app)
        let second = DirectIrohSelection(try XCTUnwrap(transport.selections.last))
        XCTAssertNotEqual(second, selection)
        XCTAssertNil(controller.livePrimaryWebView)
        XCTAssertNil(app.directRendezvousWebView(for: selection))
        XCTAssertNil(app.directRendezvousWebView(for: second))

        // The instance picker now opens the instances sheet over the
        // selected runtime; it does not end the selection by itself.
        app.showInstancePicker()
        guard case .accountOverBrowser(let covered) = app.route else {
            return XCTFail("The instance picker did not open the sheet")
        }
        XCTAssertEqual(covered.instance.id, "instance-2")
    }

    /// Without a transport there is no target, so no WebView is handed
    /// out, and a source bound to the App asks nothing.
    @MainActor
    func testWithoutATransportNoRendezvousWebViewIsHandedOut() async throws {
        let app = try await makeApp(directTransport: nil)
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        guard case .browser(let runtime) = app.route else {
            return XCTFail("The runtime did not open at once.")
        }
        let controller = try makeRuntimeController(app, runtime)
        app.registerRuntimeController(controller, for: runtime)
        let page = try RuntimePage(controller: controller)
        defer { page.close() }
        XCTAssertNotNil(controller.livePrimaryWebView)

        let unbound = DirectRendezvousWebViewSource()
        let bound = DirectRendezvousWebViewSource()
        bound.appModel = app
        let client = try XCTUnwrap(DirectEndpointID(bytes: Data(repeating: 0xAB, count: 32)))
        for number in 0..<4 {
            let selection = DirectIrohSelection(
                number: number,
                instanceID: "instance-1",
                host: "instance-one.example",
                storeIdentifier: app.websiteDataStore.identifier
            )
            XCTAssertNil(app.directRendezvousWebView(for: selection))
            for source in [unbound, bound] {
                do {
                    _ = try await source.fetch(selection, client)
                    XCTFail("A rendezvous request was made.")
                } catch {
                    XCTAssertEqual(error as? DirectRendezvousFetchError, .pageNotReady)
                }
            }
        }
    }

    /// The iroh transport, over a stand-in for the binding, as the App's
    /// transport: the controller's target reaches it as its selection, its
    /// rendezvous request gets the runtime's primary WebView, its report
    /// reaches the page, a stream and a failed candidate pass through the
    /// seam, and signing out closes its connection.
    @MainActor
    func testTheIrohTransportBehindTheSeamWorksForTheControllersTarget() async throws {
        let log = DirectIrohEventLog()
        let engine = DirectIrohFakeEngine(log: log)
        let scheduler = DirectIrohManualScheduler()
        let source = DirectRendezvousWebViewSource()
        let asked = RendezvousRequests()
        let transport = DirectIrohTransport(
            engine: engine,
            fetchRendezvous: { selection, clientEndpointID in
                let webView = source.webView(for: selection)
                asked.record(selection, webView: webView)
                guard webView != nil else {
                    throw DirectRendezvousFetchError.pageNotReady
                }
                return DirectIrohFixtures.ready(
                    generation: "g1",
                    client: clientEndpointID,
                    instanceID: selection.instanceID,
                    hostname: selection.host
                )
            },
            network: DirectIrohFakeNetwork(),
            scheduler: scheduler,
            timing: Self.patientIrohTiming,
            isInBackground: false
        )
        let app = try await makeApp(directTransport: transport)
        source.appModel = app
        let connection = DirectIrohFakeConnection(
            log: log,
            remote: DirectIrohFixtures.instanceEndpoint,
            answers: [
                DirectIrohFakeConnection.established,
                Data("hello".utf8),
                DirectIrohFakeConnection.established,
            ]
        )
        engine.prepare().connection.resolve(connection)

        // The runtime's WebView is not made yet: the request is refused
        // before anything is bound, and asked again soon.
        let runtime = try await select("instance-1", in: app)
        try await eventually { asked.requests.count == 1 }
        let selection = try XCTUnwrap(asked.requests.first?.selection)
        XCTAssertEqual(selection.instanceID, "instance-1")
        XCTAssertEqual(selection.host, "instance-one.example")
        XCTAssertEqual(selection.storeIdentifier, app.websiteDataStore.identifier)
        XCTAssertNil(asked.requests.first?.webView)
        XCTAssertEqual(log.all, ["key"])
        XCTAssertEqual(scheduler.pending, [Self.patientIrohTiming.pageRetrySeconds])

        let controller = try makeRuntimeController(app, runtime)
        app.registerRuntimeController(controller, for: runtime)
        let page = try RuntimePage(controller: controller)
        try await page.load(RuntimePage.recordingDocument, baseURL: runtime.instance.url)
        let primary = try XCTUnwrap(controller.livePrimaryWebView)

        // Asked again, the request gets the primary WebView; the path comes
        // up and the page is told.
        XCTAssertTrue(scheduler.fire(Self.patientIrohTiming.pageRetrySeconds))
        try await eventually {
            try await page.changes()
                == #"[{"closure":0,"preferred":"direct","sequence":1}]"#
        }
        XCTAssertEqual(asked.requests.count, 2)
        XCTAssertEqual(asked.requests.last?.selection, selection)
        XCTAssertEqual(asked.requests.last?.webView, ObjectIdentifier(primary))
        XCTAssertEqual(connection.streams.count, 1)

        // A stream through the seam, for the controller's target.
        let target = DirectTarget(
            selection: selection.number,
            runtime: runtime,
            storeIdentifier: app.websiteDataStore.identifier,
            authority: try XCTUnwrap(DirectAuthority(instanceURL: runtime.instance.url))
        )
        XCTAssertEqual(DirectIrohSelection(target), selection)
        let tag = DirectStreamTag(target: target, rendezvousGeneration: "g1", pathGeneration: 1)
        let seam: any DirectTransporting = transport
        let stream = try await seam.openStream(for: tag)
        XCTAssertTrue(stream is DirectIrohByteStream)
        try await stream.write(Data("ping".utf8))
        let read = try await stream.read(maxLength: 1_024)
        XCTAssertEqual(read, Data("hello".utf8))
        XCTAssertEqual(connection.streams.count, 2)
        XCTAssertEqual(connection.streams.last?.send.written, Data("ping".utf8))
        stream.cancel()

        // A failed candidate is probed; the path answers and stays.
        seam.pathDidFail(for: tag)
        try await eventually { connection.streams.count == 3 }
        try await Task.sleep(for: .milliseconds(200))
        let changes = try await page.changes()
        XCTAssertEqual(changes, #"[{"closure":0,"preferred":"direct","sequence":1}]"#)
        page.close()

        // Signing out ends the selection: the connection closes, and no
        // WebView is handed out for it.
        _ = try await app.mobileSessionCoordinator.logout()
        trackStore(of: app)
        try await eventually { log.count(of: "connection.close") == 1 }
        XCTAssertNil(source.webView(for: selection))
    }

    // MARK: A loss of the iroh transport's path

    /// The App goes to the background with a direct relay committed on the
    /// iroh transport's path. The transport and the controller both hear
    /// it, in either order. The relay is counted once, and the first status
    /// the return settles already includes that closure.
    @MainActor
    func testTheBackgroundCountsTheDirectRelayOnTheIrohTransportsPathOnceInEitherOrder() async throws {
        enum Order: CaseIterable {
            case transportFirst
            case controllerFirst
        }
        for order in Order.allCases {
            // The transport hears the App's lifecycle on its own center, so
            // that the test decides which of the two hears it first.
            let transportNotifications = NotificationCenter()
            let fixture = try await makeAppWithACommittedDirectRelay(
                transportNotifications: transportNotifications
            )
            let centers = order == .transportFirst
                ? [transportNotifications, NotificationCenter.default]
                : [NotificationCenter.default, transportNotifications]

            for center in centers {
                center.post(name: UIApplication.didEnterBackgroundNotification, object: nil)
            }
            // Closed by the transport in its own turn, after the claim.
            XCTAssertEqual(fixture.log.count(of: "connection.close"), 1, "\(order)")
            // Counted, and not published while the App is away: no document
            // is told to reconnect while the listener cannot be reached.
            XCTAssertEqual(directStatus(fixture.app, instance)?.closure, 0, "\(order)")
            try await Task.sleep(for: .milliseconds(300))
            XCTAssertEqual(directStatus(fixture.app, instance)?.closure, 0, "\(order)")
            for center in centers {
                center.post(name: UIApplication.willEnterForegroundNotification, object: nil)
            }
            // The first status the return settles.
            let first = Recorder<DirectPathStatus?>()
            fixture.app.directStatusProvider?(instance) { first.record($0) }
            try await eventually { first.values.count == 1 }
            if let status = first.values.first ?? nil {
                XCTAssertEqual(status.closure, 1, "\(order)")
                try await Task.sleep(for: .milliseconds(300))
                XCTAssertEqual(directStatus(fixture.app, instance)?.closure, 1, "\(order)")
            } else {
                // The simulator refused the re-bind: the held answer is the
                // bridge's `failed`, and the App shows the reopen message.
                // This run covers no successful return.
                XCTAssertTrue(fixture.app.directListenerFault, "\(order)")
            }
            fixture.close()
        }
    }

    /// The instance answers that it offers no direct path while a relay is
    /// committed on it: the relay is counted as closed by that answer,
    /// once.
    @MainActor
    func testAnUnavailableAnswerCountsTheDirectRelayOnTheIrohTransportsPathOnce() async throws {
        let fixture = try await makeAppWithACommittedDirectRelay()
        defer { fixture.close() }

        fixture.rendezvous.next.append(
            DirectIrohFixtures.unavailable(
                retryAfterSeconds: 60,
                instanceID: "instance-1",
                hostname: "instance-one.example"
            )
        )
        try fixture.fireRefresh()
        try await eventually { self.directStatus(fixture.app, self.instance)?.closure == 1 }
        XCTAssertEqual(fixture.log.count(of: "connection.close"), 1)
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(directStatus(fixture.app, instance)?.closure, 1)
        XCTAssertEqual(directStatus(fixture.app, instance)?.preferred, .tunnel)
    }

    /// The instance answers with a new endpoint while a relay is committed
    /// on the old one: the relay is counted as closed by the replacement,
    /// once, and the new path comes up.
    @MainActor
    func testAReplacementAnswerCountsTheDirectRelayOnTheIrohTransportsPathOnce() async throws {
        let fixture = try await makeAppWithACommittedDirectRelay()
        defer { fixture.close() }

        fixture.engine.prepare().connection.resolve(
            DirectIrohFakeConnection(
                log: fixture.log,
                remote: DirectIrohFixtures.instanceEndpoint,
                answers: [DirectIrohFakeConnection.established]
            )
        )
        fixture.rendezvous.generation = "g2"
        try fixture.fireRefresh()
        try await eventually {
            let status = self.directStatus(fixture.app, self.instance)
            return status?.closure == 1 && status?.preferred == .direct
        }
        XCTAssertEqual(fixture.log.count(of: "connection.close"), 1)
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(directStatus(fixture.app, instance)?.closure, 1)
    }

    // MARK: The bridge

    @MainActor
    func testDirectStatusIsAdvertisedReadOnlyAndTakesNoParameters() async throws {
        var askedFor: WebOrigin?
        let webView = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: instance,
                setOuterScrollLocked: { _, _ in false },
                directStatus: { origin, reply in
                    askedFor = origin
                    reply(DirectPathStatus(preferred: .direct, sequence: 7, closure: 2))
                }
            )
        )
        let loader = PageLoader()
        webView.navigationDelegate = loader
        try await loader.loadHTML(
            "<!doctype html><title>instance</title>",
            baseURL: instance.url,
            in: webView
        )

        let result = try await webView.callAsyncJavaScript(
            """
            const info = await window.avibeNative.call("app.info");
            const status = await window.avibeNative.call("direct.status");
            const refused = [];
            for (const params of [{address: "192.168.1.20:443"}, {probe: true}, {answer: {}}]) {
              try {
                await window.avibeNative.call("direct.status", params);
                refused.push("accepted");
              } catch (error) {
                refused.push(error.code);
              }
            }
            return JSON.stringify({
              capability: info.capabilities.direct,
              advertised: window.avibeNative.capabilities.direct,
              status: [status.preferred, status.sequence, status.closure, Object.keys(status).length],
              refused
            });
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )

        XCTAssertEqual(
            result as? String,
            #"{"capability":1,"advertised":1,"status":["direct",7,2,3],"refused":["invalid_argument","invalid_argument","invalid_argument"]}"#
        )
        XCTAssertEqual(askedFor, instance)
        // The whole surface: one call, and nothing that takes an address,
        // starts a probe, or accepts a rendezvous answer.
        XCTAssertEqual(
            NativeBridgeMethod.allCases
                .map(\.rawValue)
                .filter { $0.hasPrefix("direct.") },
            ["direct.status"]
        )
    }

    @MainActor
    func testAStatusWithNothingCurrentToAnswerForIsAnErrorNotATunnel() async throws {
        let webView = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: instance,
                setOuterScrollLocked: { _, _ in false },
                directStatus: { _, reply in
                    reply(nil)
                }
            )
        )
        let loader = PageLoader()
        webView.navigationDelegate = loader
        try await loader.loadHTML(
            "<!doctype html><title>instance</title>",
            baseURL: instance.url,
            in: webView
        )

        let result = try await webView.callAsyncJavaScript(
            """
            try {
              await window.avibeNative.call("direct.status");
              return "answered";
            } catch (error) {
              return error.code;
            }
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )

        XCTAssertEqual(result as? String, "failed")
    }

    @MainActor
    func testATransportChangeIsDeliveredOnlyToInstanceDocumentsOfALiveRuntime() async throws {
        let controller = WebViewController(
            instanceOrigin: instance,
            websiteDataStore: .nonPersistent(),
            directStatus: { _, reply in
                reply(.initial)
            },
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        let page = try RuntimePage(controller: controller)
        defer { page.close() }

        try await page.load(
            RuntimePage.recordingDocument,
            baseURL: instance.url
        )
        var asked: [WebOrigin] = []
        controller.deliverDirectTransportChanged { origin in
            asked.append(origin)
            return DirectPathStatus(preferred: .direct, sequence: 3, closure: 1)
        }
        XCTAssertEqual(asked, [instance])
        try await eventually {
            try await page.changes()
                == #"[{"closure":1,"preferred":"direct","sequence":3}]"#
        }

        // A document on another origin has no bridge and is told nothing.
        try await page.load(
            RuntimePage.listeningDocument(title: "Elsewhere"),
            baseURL: URL(string: "https://example.com/")!
        )
        controller.deliverDirectTransportChanged { _ in
            DirectPathStatus(preferred: .tunnel, sequence: 4, closure: 1)
        }
        try await Task.sleep(for: .milliseconds(300))
        let elsewhere = try await page.changes()
        XCTAssertEqual(elsewhere, "[]")

        // A released runtime is not even asked.
        _ = controller.invalidateForAccountTransition()
        asked.removeAll()
        controller.deliverDirectTransportChanged { origin in
            asked.append(origin)
            return DirectPathStatus(preferred: .direct, sequence: 5, closure: 1)
        }
        XCTAssertEqual(asked, [])
    }

    /// The event is sent after a check of the WebView's URL, and runs later,
    /// in whatever document the WebView shows by then. The script checks the
    /// origin of the document it runs in.
    @MainActor
    func testTheEventScriptDispatchesOnlyInADocumentOfItsOrigin() async throws {
        let elsewhere = try XCTUnwrap(
            WebOrigin.parseConfigured("https://example.com")
        )
        let status = DirectPathStatus(preferred: .direct, sequence: 3, closure: 0)
        let forInstance = try XCTUnwrap(status.eventScript(for: instance))
        let forElsewhere = try XCTUnwrap(status.eventScript(for: elsewhere))
        let recorded = #"[{"closure":0,"preferred":"direct","sequence":3}]"#
        let webView = WKWebView(frame: .zero)
        let loader = PageLoader()
        webView.navigationDelegate = loader
        func changes() async throws -> String? {
            try await webView.evaluateJavaScript(
                "JSON.stringify(window.changes)"
            ) as? String
        }

        try await loader.loadHTML(
            RuntimePage.listeningDocument(title: "Instance"),
            baseURL: instance.url,
            in: webView
        )
        webView.evaluateJavaScript(forInstance, completionHandler: nil)
        try await eventually { try await changes() == recorded }

        // The document a navigation put in its place, on another origin,
        // gets the script that was meant for the instance.
        try await loader.loadHTML(
            RuntimePage.listeningDocument(title: "Elsewhere"),
            baseURL: elsewhere.url,
            in: webView
        )
        webView.evaluateJavaScript(forInstance, completionHandler: nil)
        try await Task.sleep(for: .milliseconds(300))
        let afterForeignScript = try await changes()
        XCTAssertEqual(afterForeignScript, "[]")
        // Its listener works: only the origin check stopped the event.
        webView.evaluateJavaScript(forElsewhere, completionHandler: nil)
        try await eventually { try await changes() == recorded }
    }

    @MainActor
    func testSameOriginPopupCannotUsePrimaryRuntimeScheduleAuthority() async throws {
        let server = try LocalHTTPServer { request in
            if request.path == "/child" {
                return .html("<!doctype html><title>Child</title>")
            }
            return .html(
                """
                <!doctype html><title>Primary</title>
                <button id="open-child" onclick="window.open('/child', '_blank')">Open</button>
                """
            )
        }
        let baseURL = try await server.start()
        defer { server.stop() }
        let origin = try XCTUnwrap(
            WebOrigin(url: baseURL, requiresHTTPS: false)
        )
        var capabilityCalls = 0
        var admissionCalls = 0
        var requestCalls = 0
        let controller = WebViewController(
            instanceOrigin: origin,
            websiteDataStore: .nonPersistent(),
            navigationPolicy: NavigationPolicy(
                instanceOrigins: [origin],
                authenticationOrigins: []
            ),
            systemScheduleAdmission: { origin in
                admissionCalls += 1
                return .authorized(SystemScheduleContext(
                    owner: .init(accountID: "account-1", ownerEpoch: "popup-test"),
                    instanceID: "instance-1", origin: origin.serialized,
                    runtimeIdentity: 0, navigationEpoch: 0))
            },
            systemScheduleCapabilities: { _ in
                capabilityCalls += 1
                return ["contract": 1]
            },
            systemScheduleRequest: { request, _ in
                requestCalls += 1
                return SystemScheduleReceipt.terminal(
                    request: request,
                    state: .scheduled,
                    authorization: .authorized,
                    systemIdentifier: "must-not-be-created"
                ).jsonObject
            },
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 700))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        controller.view.frame = window.bounds
        controller.view.layoutIfNeeded()
        defer {
            controller.presentedViewController?.dismiss(animated: false)
            window.rootViewController = nil
            window.isHidden = true
        }
        let primary = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        func title(of webView: WKWebView) async -> String? {
            try? await webView.evaluateJavaScript("document.title") as? String
        }
        try await eventually { await title(of: primary) == "Primary" }
        _ = try await primary.evaluateJavaScript(
            "document.querySelector('#open-child').click(); true"
        )
        var child: WKWebView?
        try await eventually {
            let popup = controller.presentedViewController
                as? UINavigationController
            child = popup?.topViewController?.viewIfLoaded?
                .subviews.compactMap { $0 as? WKWebView }.first
            return child != nil
        }
        let childWebView = try XCTUnwrap(child)
        try await eventually { await title(of: childWebView) == "Child" }

        let result = try await childWebView.callAsyncJavaScript(
            """
            const codes = [];
            try {
              await window.avibeNative.call("systemSchedule.capabilities");
              codes.push("accepted");
            } catch (error) {
              codes.push(error.code);
            }
            try {
              await window.avibeNative.call("systemSchedule.request", params);
              codes.push("accepted");
            } catch (error) {
              codes.push(error.code);
            }
            return JSON.stringify(codes);
            """,
            arguments: [
                "params": scheduleParams(
                    requestID: "popup-request",
                    operationID: "popup-operation",
                    now: Date()
                )
            ],
            in: nil,
            contentWorld: .page
        )
        XCTAssertEqual(result as? String, #"["forbidden","forbidden"]"#)
        XCTAssertEqual(capabilityCalls, 0)
        XCTAssertEqual(admissionCalls, 0)
        XCTAssertEqual(requestCalls, 0)
    }

    @MainActor
    func testATransportChangeReachesAnInstanceChildWindowUntilItLeavesTheInstance() async throws {
        let instanceServer = try LocalHTTPServer { request in
            switch request.path {
            case "/child":
                return .html(RuntimePage.listeningDocument(title: "Child"))
            default:
                return .html(
                    RuntimePage.listeningDocument(
                        title: "Instance",
                        body: """
                            <button id="open-child" onclick="window.open('/child', '_blank')">Open</button>
                            """
                    )
                )
            }
        }
        let elsewhereServer = try LocalHTTPServer { _ in
            .html(RuntimePage.listeningDocument(title: "Elsewhere"))
        }
        let instanceURL = try await instanceServer.start()
        let elsewhereURL = try await elsewhereServer.start()
        defer {
            instanceServer.stop()
            elsewhereServer.stop()
        }
        let origin = try XCTUnwrap(
            WebOrigin(url: instanceURL, requiresHTTPS: false)
        )
        let elsewhere = try XCTUnwrap(
            WebOrigin(url: elsewhereURL, requiresHTTPS: false)
        )
        XCTAssertNotEqual(origin, elsewhere)
        let controller = WebViewController(
            instanceOrigin: origin,
            websiteDataStore: .nonPersistent(),
            navigationPolicy: NavigationPolicy(
                instanceOrigins: [origin],
                authenticationOrigins: []
            ),
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 700))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        controller.view.frame = window.bounds
        controller.view.layoutIfNeeded()
        defer {
            controller.presentedViewController?.dismiss(animated: false)
            window.rootViewController = nil
            window.isHidden = true
        }
        let primary = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        func title(of webView: WKWebView) async -> String? {
            try? await webView.evaluateJavaScript("document.title") as? String
        }
        func changes(in webView: WKWebView) async throws -> String? {
            try await webView.evaluateJavaScript(
                "JSON.stringify(window.changes ?? null)"
            ) as? String
        }

        // The instance document opens a child window on the instance.
        try await eventually { await title(of: primary) == "Instance" }
        _ = try await primary.evaluateJavaScript(
            "document.querySelector('#open-child').click(); true"
        )
        var child: WKWebView?
        try await eventually {
            let popup = controller.presentedViewController
                as? UINavigationController
            child = popup?.topViewController?.viewIfLoaded?
                .subviews.compactMap { $0 as? WKWebView }.first
            return child != nil
        }
        let childWebView = try XCTUnwrap(child)
        try await eventually { await title(of: childWebView) == "Child" }

        // Both are told.
        controller.deliverDirectTransportChanged { _ in
            DirectPathStatus(preferred: .direct, sequence: 3, closure: 0)
        }
        let first = #"[{"closure":0,"preferred":"direct","sequence":3}]"#
        try await eventually { try await changes(in: primary) == first }
        try await eventually { try await changes(in: childWebView) == first }

        // The child leaves the instance. The primary is still told; the
        // child is not.
        childWebView.load(URLRequest(url: elsewhereURL))
        try await eventually { await title(of: childWebView) == "Elsewhere" }
        controller.deliverDirectTransportChanged { _ in
            DirectPathStatus(preferred: .tunnel, sequence: 4, closure: 1)
        }
        try await eventually {
            try await changes(in: primary)
                == #"[{"closure":0,"preferred":"direct","sequence":3},{"closure":1,"preferred":"tunnel","sequence":4}]"#
        }
        try await Task.sleep(for: .milliseconds(300))
        let untold = try await changes(in: childWebView)
        XCTAssertEqual(untold, "[]")

        // An event that was already on its way when the child left runs in
        // the new document, and dispatches nothing there.
        let late = try XCTUnwrap(
            DirectPathStatus(preferred: .direct, sequence: 5, closure: 1)
                .eventScript(for: origin)
        )
        childWebView.evaluateJavaScript(late, completionHandler: nil)
        try await Task.sleep(for: .milliseconds(300))
        let afterLateEvent = try await changes(in: childWebView)
        XCTAssertEqual(afterLateEvent, "[]")
        // The document does listen: an event for its own origin arrives.
        let own = try XCTUnwrap(
            DirectPathStatus(preferred: .direct, sequence: 5, closure: 1)
                .eventScript(for: elsewhere)
        )
        childWebView.evaluateJavaScript(own, completionHandler: nil)
        try await eventually {
            try await changes(in: childWebView)
                == #"[{"closure":1,"preferred":"direct","sequence":5}]"#
        }
    }

    // MARK: Fixture

    private var storeIdentifiers: Set<UUID> = []
    private var releaseFixtureApps: [@MainActor () -> Void] = []
    private func scheduleParams(
        requestID: String,
        operationID: String,
        now: Date
    ) -> [String: Any] {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return [
            "requestId": requestID,
            "operationId": operationID,
            "kind": "alarm",
            "title": "Take medicine",
            "fireAt": formatter.string(from: now.addingTimeInterval(3_600)),
            "timeZone": "UTC",
            "recurrence": NSNull(),
            "instanceId": "instance-1",
            "sessionId": "session-1",
            "expiresAt": formatter.string(from: now.addingTimeInterval(300)),
        ]
    }

    private func jsonObject(_ json: String?) throws -> [String: Any] {
        let data = try XCTUnwrap(json?.data(using: .utf8))
        return try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any]
        )
    }

    /// What `direct.status()` answers for `origin`, through the provider
    /// the App gives its WebViews. `nil` when it answers later or not at all.
    @MainActor
    private func directStatus(_ app: AppModel, _ origin: WebOrigin) -> DirectPathStatus? {
        var answer: DirectPathStatus?
        app.directStatusProvider?(origin) { answer = $0 }
        return answer
    }

    /// Bounds no test of the iroh transport should reach, so that a slow
    /// simulator does not turn a stand-in that answers at once into a
    /// timeout.
    private static let patientIrohTiming: DirectIrohTiming = {
        var timing = DirectIrohTiming()
        timing.fetchSeconds = 120
        timing.bindSeconds = 120
        timing.dialSeconds = 120
        timing.probeSeconds = 120
        return timing
    }()

    /// An App whose transport is the iroh transport over a stand-in for the
    /// binding, with its path to instance-1 up through the runtime's
    /// primary WebView, and one direct relay committed through the App's
    /// own listener on that path.
    @MainActor
    private func makeAppWithACommittedDirectRelay(
        transportNotifications: NotificationCenter = .default
    ) async throws -> CommittedDirectRelay {
        let log = DirectIrohEventLog()
        let engine = DirectIrohFakeEngine(log: log)
        let scheduler = DirectIrohManualScheduler()
        let source = DirectRendezvousWebViewSource()
        let rendezvous = ScriptedRendezvous()
        let transport = DirectIrohTransport(
            engine: engine,
            fetchRendezvous: { selection, clientEndpointID in
                guard source.webView(for: selection) != nil else {
                    throw DirectRendezvousFetchError.pageNotReady
                }
                return rendezvous.answer(for: selection, client: clientEndpointID)
            },
            network: DirectIrohFakeNetwork(),
            scheduler: scheduler,
            timing: Self.patientIrohTiming,
            notificationCenter: transportNotifications,
            isInBackground: false
        )
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(directTransport: transport) {
            listening = try listeningSockets()
        }
        source.appModel = app
        // The probe's stream, then the relay's.
        let connection = DirectIrohFakeConnection(
            log: log,
            remote: DirectIrohFixtures.instanceEndpoint,
            answers: [DirectIrohFakeConnection.established, DirectIrohFakeConnection.established]
        )
        engine.prepare().connection.resolve(connection)

        let runtime = try await select("instance-1", in: app)
        let controller = try makeRuntimeController(app, runtime)
        app.registerRuntimeController(controller, for: runtime)
        let page = try RuntimePage(controller: controller)
        // A document of the instance's origin, loaded here before the path
        // comes up: the runtime's own load of the instance would otherwise
        // send WebKit's own CONNECT through the route, and could take the
        // relay's stream below.
        try await page.load(RuntimePage.recordingDocument, baseURL: runtime.instance.url)
        try await eventually {
            // A request made before the WebView existed is asked again.
            scheduler.fire(Self.patientIrohTiming.pageRetrySeconds)
            return self.directStatus(app, self.instance)?.preferred == .direct
        }

        let socket = try XCTUnwrap(try newListeners(since: listening).first)
        let port = try XCTUnwrap(NWEndpoint.Port(rawValue: socket.port))
        let client = LoopbackClient(port: port)
        try await client.send(
            "CONNECT instance-one.example:443 HTTP/1.1\r\nHost: instance-one.example:443\r\n\r\n"
        )
        let established = "HTTP/1.1 200 Connection Established\r\n\r\n"
        let answer = try await client.receive(count: established.utf8.count)
        XCTAssertEqual(String(decoding: answer, as: UTF8.self), established)
        XCTAssertEqual(connection.streams.count, 2)
        XCTAssertEqual(log.count(of: "connection.close"), 0)
        XCTAssertEqual(directStatus(app, instance)?.closure, 0)
        return CommittedDirectRelay(
            app: app,
            engine: engine,
            scheduler: scheduler,
            rendezvous: rendezvous,
            log: log,
            page: page,
            client: client
        )
    }

    /// The isolated preferences of the last App `makeApp` built.
    private var appDefaults: UserDefaults?
    private var appScheduleStore: FileSystemSystemScheduleStore?
    private var appScheduleLedgerURL: URL?

    /// Where the account coordinator keeps the selected instance.
    private static let selectionKey = "avibe.mobile.selection"

    /// An `AppModel` signed in through a local control plane, past its
    /// bootstrap. `nil` builds it exactly as the App does, with no
    /// transport argument at all. `beforeTheAppExists` runs once the control
    /// plane listens and before the `AppModel` is made, which is where a
    /// test takes the baseline of what the App itself does.
    /// `launchesWithInstanceOneSelected` launches with instance-1 as the
    /// stored selection, so the bootstrap restores it; `makeApp` then
    /// returns at once and the test waits for the open itself.
    /// `notifications` is the device's push state. `whenTheAppExists` runs
    /// right after the `AppModel` is made, before its bootstrap runs.
    @MainActor
    private func makeApp(
        directTransport: DirectTransporting?,
        directFaultMarker: DirectFaultMarkerStoring? = nil,
        pushHolds: [PushHold] = [],
        sessionResponse: (@Sendable () -> LocalHTTPResponse?)? = nil,
        pushKeychain: InMemoryPushKeychainStore? = nil,
        pushInstallationMarker: InMemoryPushInstallationMarkerStore? = nil,
        pushNotificationCenter: PushNotificationCentering? = nil,
        runtimeOpenTiming: RuntimeOpenTiming? = nil,
        runtimeOpenNotificationCenter: NotificationCenter = .default,
        systemSchedulePlatform: SystemSchedulePlatforming? = nil,
        systemScheduleNow: @escaping @MainActor () -> Date = Date.init,
        systemScheduleCheckpoint: ((FaultingSystemScheduleFileIO.Checkpoint) throws -> Void)? = nil,
        systemScheduleFileURL: URL? = nil,
        launchesWithInstanceOneSelected: Bool = false,
        reusingDefaults: UserDefaults? = nil,
        notifications: NotificationsOn? = nil,
        // Before `beforeTheAppExists`: an unlabeled trailing closure at a
        // call site keeps meaning that last parameter.
        whenTheAppExists: (AppModel) -> Void = { _ in },
        beforeTheAppExists: () throws -> Void = {}
    ) async throws -> AppModel {
        let providerPatches = notifications?.providerPatches
        let server = try LocalHTTPServer { request in
            if let providerPatches,
               request.method == "PATCH",
               request.path == providerPatches.path
            {
                return providerPatches.response()
            }
            if request.method == "POST",
               let hold = pushHolds.first(where: { $0.path == request.path })
            {
                return hold.response
            }
            switch (request.method, request.path) {
            case ("GET", "/api/config/auth"):
                return .json("{}")
            case ("GET", "/api/mobile/session"):
                if let response = sessionResponse?() {
                    return response
                }
                return .json(
                    """
                    {
                      "account": {"id": "account-1", "email": "max@example.com"},
                      "instances": [
                        {
                          "id": "instance-1",
                          "name": "One",
                          "host": "instance-one.example",
                          "url": "https://instance-one.example",
                          "relationship": "owner",
                          "owner_email": "max@example.com",
                          "available": true
                        },
                        {
                          "id": "instance-2",
                          "name": "Two",
                          "host": "instance-two.example",
                          "url": "https://instance-two.example",
                          "relationship": "owner",
                          "owner_email": "max@example.com",
                          "available": true
                        }
                      ]
                    }
                    """
                )
            default:
                return .status(404)
            }
        }
        let baseURL = try await server.start()
        addTeardownBlock {
            server.stop()
        }
        let suiteName = "DirectPathAppTests.\(UUID().uuidString)"
        // `reusingDefaults`: the next launch of the same App, with its
        // preferences (the previous launch's teardown removes them).
        let defaults = try reusingDefaults ?? XCTUnwrap(UserDefaults(suiteName: suiteName))
        let scheduleDirectory = systemScheduleFileURL?.deletingLastPathComponent()
            ?? FileManager.default.temporaryDirectory
            .appendingPathComponent(
                "DirectPathScheduleTests.\(UUID().uuidString)",
                isDirectory: true
            )
        try FileManager.default.createDirectory(
            at: scheduleDirectory,
            withIntermediateDirectories: true
        )
        let scheduleLedgerURL = systemScheduleFileURL ?? scheduleDirectory.appendingPathComponent("ledger.json")
        let scheduleStore = FileSystemSystemScheduleStore(fileURL: scheduleLedgerURL,
                                                         fileIO: FaultingSystemScheduleFileIO(checkpoint: systemScheduleCheckpoint))
        appScheduleStore = scheduleStore
        appScheduleLedgerURL = scheduleLedgerURL
        addTeardownBlock {
            try? FileManager.default.removeItem(at: scheduleDirectory)
        }
        appDefaults = defaults
        if reusingDefaults == nil {
            addTeardownBlock {
                defaults.removePersistentDomain(forName: suiteName)
            }
        }
        addTeardownBlock { @MainActor [self] in
            await self.removeTrackedStores()
        }

        if let providerPatches {
            addTeardownBlock {
                providerPatches.releaseAll()
            }
        }
        if launchesWithInstanceOneSelected {
            defaults.set(
                Data(
                    #"{"accountID":"account-1","instanceID":"instance-1","url":"https://instance-one.example"}"#.utf8
                ),
                forKey: Self.selectionKey
            )
        }

        try beforeTheAppExists()

        let controlPlane = MobileControlPlaneConfiguration(testBaseURL: baseURL)
        let app: AppModel
        if let directFaultMarker {
            app = AppModel(
                defaults: defaults,
                controlPlane: controlPlane,
                directTransport: directTransport,
                directFaultMarker: directFaultMarker,
                pushKeychain: notifications?.keychain
                    ?? pushKeychain ?? InMemoryPushKeychainStore(),
                pushGenerationStore: notifications?.generationStore
                    ?? InMemoryPushGenerationStore(),
                pushInstallationMarker: notifications?.marker
                    ?? pushInstallationMarker ?? InMemoryPushInstallationMarkerStore(),
                pushNotificationCenter: pushNotificationCenter ?? SilentPushNotificationCenter(),
                systemSchedulePlatform: systemSchedulePlatform,
                systemScheduleStore: scheduleStore,
                systemScheduleNow: systemScheduleNow,
                runtimeOpenTiming: runtimeOpenTiming,
                runtimeOpenNotificationCenter: runtimeOpenNotificationCenter,
                badgeCenter: SilentBadgeCenter()
            )
        } else if let directTransport {
            app = AppModel(
                defaults: defaults,
                controlPlane: controlPlane,
                directTransport: directTransport,
                pushKeychain: notifications?.keychain
                    ?? pushKeychain ?? InMemoryPushKeychainStore(),
                pushGenerationStore: notifications?.generationStore
                    ?? InMemoryPushGenerationStore(),
                pushInstallationMarker: notifications?.marker
                    ?? pushInstallationMarker ?? InMemoryPushInstallationMarkerStore(),
                pushNotificationCenter: pushNotificationCenter ?? SilentPushNotificationCenter(),
                systemSchedulePlatform: systemSchedulePlatform,
                systemScheduleStore: scheduleStore,
                systemScheduleNow: systemScheduleNow,
                runtimeOpenTiming: runtimeOpenTiming,
                runtimeOpenNotificationCenter: runtimeOpenNotificationCenter,
                badgeCenter: SilentBadgeCenter()
            )
        } else {
            app = AppModel(
                defaults: defaults,
                controlPlane: controlPlane,
                pushKeychain: notifications?.keychain
                    ?? pushKeychain ?? InMemoryPushKeychainStore(),
                pushGenerationStore: notifications?.generationStore
                    ?? InMemoryPushGenerationStore(),
                pushInstallationMarker: notifications?.marker
                    ?? pushInstallationMarker ?? InMemoryPushInstallationMarkerStore(),
                pushNotificationCenter: pushNotificationCenter ?? SilentPushNotificationCenter(),
                systemSchedulePlatform: systemSchedulePlatform,
                systemScheduleStore: scheduleStore,
                systemScheduleNow: systemScheduleNow,
                runtimeOpenTiming: runtimeOpenTiming,
                runtimeOpenNotificationCenter: runtimeOpenNotificationCenter,
                badgeCenter: SilentBadgeCenter()
            )
        }
        notifications?.app = app
        whenTheAppExists(app)
        trackStore(of: app)
        releaseFixtureApps.append { [weak app] in
            app?.showInstancePicker()
            app?.mobileSessionCoordinator.deactivateAccountSurface()
        }
        addTeardownBlock { @MainActor [self] in
            server.requests.attach(to: self, name: "direct App bootstrap requests")
        }
        if launchesWithInstanceOneSelected || reusingDefaults != nil {
            return app
        }
        try await WebKitReadiness.waitForSignedInFixture(
            app,
            accountID: "account-1",
            instanceIDs: ["instance-1", "instance-2"],
            requests: server.requests
        )
        return app
    }

    @MainActor
    private func cleanupPendingRecord(
        request: SystemScheduleRequest,
        owner: SystemScheduleManagementOwner,
        systemIdentifier: String,
        updatedAt: Date
    ) -> StoredSystemSchedule {
        StoredSystemSchedule(
            operationID: request.operationID,
            owner: owner,
            intent: StoredSystemScheduleIntent(request: request),
            systemIdentifier: systemIdentifier,
            authorization: .authorized,
            lifecycle: .managedCleanupPending,
            effectMayExist: true,
            generation: 1,
            createState: .cancelled,
            lastError: nil,
            updatedAt: updatedAt
        )
    }

    /// Starts a push-management request and returns once the control plane
    /// is answering it. Until `hold.release` is signaled the request stays
    /// in flight, and the account coordinator is busy with it.
    @MainActor
    private func holdPushManagement(
        _ hold: PushHold,
        in app: AppModel
    ) async throws -> Task<Void, Error> {
        let request = Task { @MainActor in
            _ = try await app.mobileSessionCoordinator.pushManagementRequest(
                PushManagementRequest(
                    method: "POST",
                    path: hold.path,
                    headers: [:],
                    body: nil
                )
            )
        }
        // The request launches its own account transport first: the App
        // itself allows that the readiness and request deadlines together.
        await fulfillment(
            of: [hold.inFlight.expectation],
            timeout: WebKitReadiness.standardTransportOperationSeconds
        )
        return request
    }

    /// Reports a usable path ten times, each at a moment the account
    /// coordinator is idle and still has `runtime` as its instance: the
    /// moments at which the App's lease check would admit direct, so that
    /// only an ended selection can be what refuses these.
    @MainActor
    private func reportUsableWhileTheAccountIsIdle(
        _ transport: StandInDirectTransport,
        for target: DirectTarget,
        generation: String,
        runtime: MobileRuntimeContext,
        in app: AppModel
    ) async throws {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: .seconds(30))
        var reported = 0
        while reported < 10 {
            guard clock.now < deadline else {
                XCTFail("The account coordinator never became idle.")
                throw DirectTestTimeout()
            }
            // Checked and reported in one turn of the main actor.
            if app.mobileSessionCoordinator.isCurrentRuntime(runtime) {
                transport.report(.usable(generation: generation), for: target)
                reported += 1
            }
            try await Task.sleep(for: .milliseconds(50))
        }
    }

    @MainActor
    private func isOnInstancePicker(_ app: AppModel) -> Bool {
        if case .onboarding(.instances) = app.route {
            return true
        }
        return false
    }

    /// Selects an instance and waits for its browser. With a transport the
    /// runtime opens once its route is prepared; without one, at once.
    @MainActor
    private func select(
        _ instanceID: String,
        in app: AppModel
    ) async throws -> MobileRuntimeContext {
        _ = try app.mobileSessionCoordinator.selectInstance(id: instanceID)
        return try await waitForBrowser(instanceID, in: app)
    }

    @MainActor
    private func waitForBrowser(
        _ instanceID: String,
        in app: AppModel
    ) async throws -> MobileRuntimeContext {
        var opened: MobileRuntimeContext?
        try await eventually {
            if case .browser(let runtime) = app.route,
               runtime.instance.id == instanceID
            {
                opened = runtime
                return true
            }
            return false
        }
        return try XCTUnwrap(opened)
    }

    /// The runtime controller the root view would create for `runtime`,
    /// registered with the App, showing an instance document that records
    /// the transport changes it is told about.
    @MainActor
    private func showRuntimePage(
        _ app: AppModel,
        _ runtime: MobileRuntimeContext
    ) async throws -> RuntimePage {
        let controller = try makeRuntimeController(app, runtime)
        app.registerRuntimeController(controller, for: runtime)
        let page = try RuntimePage(controller: controller)
        try await page.load(
            RuntimePage.recordingDocument,
            baseURL: runtime.instance.url
        )
        return page
    }

    /// The checks with notifications on deliver each open's device token
    /// themselves. On a test host where the App may show alerts, the App's
    /// real `refreshAPNsRegistration` would also register with APNs, whose
    /// token reaches every push coordinator at an arbitrary time. Fail
    /// rather than skip, so the checks cannot go quiet.
    @MainActor
    private func requireAHostWithoutNotificationPermission() async throws {
        let status = await UNUserNotificationCenter.current()
            .notificationSettings().authorizationStatus
        switch status {
        case .notDetermined, .denied:
            return
        default:
            XCTFail(
                "The notifications-on checks need a test host without notification permission; erase the simulator."
            )
            throw DirectTestTimeout()
        }
    }

    /// A launch made with `launchesWithInstanceOneSelected` returns before
    /// its bootstrap: wait for its session read with the budget the fixture
    /// gives one WebKit transport launch.
    @MainActor
    private func waitForTheBootstrap(of app: AppModel) async throws {
        try await eventually(timeout: WebKitReadiness.standardTransportOperation) {
            app.mobileSessionCoordinator.currentAccount?.id == "account-1"
        }
    }

    /// The runtime controller the root view would create for `runtime`.
    @MainActor
    private func makeRuntimeController(
        _ app: AppModel,
        _ runtime: MobileRuntimeContext,
        afterScheduleAdmission: ((SystemScheduleAdmission?) -> Void)? = nil,
        onScheduleDelivery: ((SystemScheduleAdmission) -> Void)? = nil
    ) throws -> WebViewController {
        let origin = try XCTUnwrap(WebOrigin(url: runtime.instance.url))
        return WebViewController(
            instanceOrigin: origin,
            websiteDataStore: app.runtimeWebsiteDataStore,
            deviceID: { [weak app] in app?.systemScheduleDeviceID },
            directStatus: app.directStatusProvider,
            systemScheduleAdmission: { [weak app] origin in
                let admission = app?.captureSystemScheduleAdmission(origin: origin)
                afterScheduleAdmission?(admission)
                return admission
            },
            systemScheduleCapabilities: { [weak app] origin in
                app?.systemScheduleCapabilities(origin: origin)
            },
            systemScheduleRequest: { [weak app] request, admission in
                onScheduleDelivery?(admission)
                return await app?.requestSystemSchedule(request, admission: admission)
            },
            systemScheduleCancel: { [weak app] request, admission in
                onScheduleDelivery?(admission)
                return await app?.cancelSystemSchedule(request, admission: admission)
            },
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
    }

    /// The App made real named stores; remove exactly those.
    @MainActor
    private func trackStore(of app: AppModel) {
        storeIdentifiers.insert(app.websiteDataStore.identifier)
    }

    @MainActor
    private func removeTrackedStores() async {
        // Release every App's runtime/account views before removing any
        // store (some tests create more than one App). RuntimePage owners
        // close their windows before this cleanup.
        for release in releaseFixtureApps { release() }
        releaseFixtureApps.removeAll()
        print("[iOS fixture] stage=App views invalidated before store removal")
        for identifier in storeIdentifiers {
            var removed = false
            var lastError: Error?
            for _ in 0..<50 {
                lastError = await AccountWebsiteDataStoreOperations.live
                    .removeIdentifier(identifier)
                let registered = await withCheckedContinuation { continuation in
                    WKWebsiteDataStore.fetchAllDataStoreIdentifiers {
                        continuation.resume(returning: Set($0))
                    }
                }
                if !registered.contains(identifier) {
                    removed = true
                    break
                }
                try? await Task.sleep(for: .milliseconds(100))
            }
            XCTAssertTrue(removed, "Fixture store still registered: \(identifier); error=\(String(describing: lastError))")
            print("[iOS fixture] stage=store removal, identifier=\(identifier), removed=\(removed)")
        }
        storeIdentifiers.removeAll()
    }
}

/// Turns on the simulator's accessibility automation, as XCUITest does, and
/// returns what restores its previous value.
private func enableAccessibilityAutomation() throws -> () -> Void {
    let root = ProcessInfo.processInfo.environment["IPHONE_SIMULATOR_ROOT"] ?? ""
    guard
        let handle = dlopen(root + "/usr/lib/libAccessibility.dylib", RTLD_NOW)
            ?? dlopen("/usr/lib/libAccessibility.dylib", RTLD_NOW),
        let isEnabledSymbol = dlsym(handle, "_AXSAutomationEnabled"),
        let setEnabledSymbol = dlsym(handle, "_AXSSetAutomationEnabled")
    else {
        throw NSError(
            domain: "AccessibilityAutomation",
            code: 1,
            userInfo: [NSLocalizedDescriptionKey: "libAccessibility is not available."]
        )
    }
    typealias IsEnabled = @convention(c) () -> Int32
    typealias SetEnabled = @convention(c) (Int32) -> Void
    let isEnabled = unsafeBitCast(isEnabledSymbol, to: IsEnabled.self)
    let setEnabled = unsafeBitCast(setEnabledSymbol, to: SetEnabled.self)
    let previous = isEnabled()
    setEnabled(1)
    return { setEnabled(previous) }
}

/// One element of the native accessibility tree.
private struct NativeAccessibilityElement {
    let label: String
    let frame: CGRect
}

/// The native accessibility elements under `root`, as assistive technology
/// reads them. A WebView's own page elements are left out.
@MainActor
private func nativeAccessibilityElements(
    in root: NSObject
) -> [NativeAccessibilityElement] {
    var found: [NativeAccessibilityElement] = []
    var visited = Set<ObjectIdentifier>()
    func visit(_ object: NSObject, depth: Int) {
        guard
            depth < 64,
            !(object is WKWebView),
            visited.insert(ObjectIdentifier(object)).inserted
        else {
            return
        }
        if object.isAccessibilityElement {
            found.append(
                NativeAccessibilityElement(
                    label: object.accessibilityLabel ?? "",
                    frame: object.accessibilityFrame
                )
            )
        }
        if let children = object.accessibilityElements as? [NSObject],
           !children.isEmpty
        {
            for child in children {
                visit(child, depth: depth + 1)
            }
        } else {
            let count = object.accessibilityElementCount()
            if count != NSNotFound, count > 0 {
                for index in 0..<count {
                    if let child = object.accessibilityElement(at: index)
                        as? NSObject
                    {
                        visit(child, depth: depth + 1)
                    }
                }
            }
        }
        if let view = object as? UIView {
            for subview in view.subviews {
                visit(subview, depth: depth + 1)
            }
        }
    }
    visit(root, depth: 0)
    return found
}

private final class RuntimeOpenManualTiming: RuntimeOpenTiming {
    private final class Timer: RuntimeOpenTimer {
        let id: UUID
        weak var owner: RuntimeOpenManualTiming?

        init(id: UUID, owner: RuntimeOpenManualTiming) {
            self.id = id
            self.owner = owner
        }

        @MainActor
        func cancel() {
            owner?.entries.removeValue(forKey: id)
        }
    }

    private struct Entry {
        let deadline: Duration
        let action: @MainActor () -> Void
    }

    private(set) var now: Duration = .zero
    private var entries: [UUID: Entry] = [:]

    @MainActor
    func schedule(
        after delay: Duration,
        _ action: @escaping @MainActor () -> Void
    ) -> any RuntimeOpenTimer {
        let id = UUID()
        entries[id] = Entry(deadline: now + delay, action: action)
        return Timer(id: id, owner: self)
    }

    @MainActor
    func advance(by duration: Duration) {
        let target = now + duration
        while let next = entries.min(by: {
            if $0.value.deadline == $1.value.deadline {
                return $0.key.uuidString < $1.key.uuidString
            }
            return $0.value.deadline < $1.value.deadline
        }), next.value.deadline <= target {
            entries.removeValue(forKey: next.key)
            now = next.value.deadline
            next.value.action()
        }
        now = target
    }

    @MainActor
    func elapseWithoutFiring(by duration: Duration) {
        now += duration
    }
}

/// A push-management answer the control plane starts and does not finish
/// until `release` is signaled, so the request stays in flight until then.
private struct PushHold: Sendable {
    let path: String
    let inFlight: FixtureEvent
    let release = DispatchSemaphore(value: 0)

    init(path: String) {
        self.path = path
        inFlight = FixtureEvent(description: "\(path) in flight")
    }

    /// Larger than what the server sends before `release`.
    var response: LocalHTTPResponse {
        LocalHTTPResponse(
            status: 200,
            headers: [("Content-Type", "application/json")],
            body: Data(
                ("{\"pad\":\"" + String(repeating: "x", count: 100_000) + "\"}").utf8
            ),
            headersSentEvent: inFlight,
            bodyRelease: release
        )
    }
}

/// What the instance answers the iroh transport's rendezvous requests: a
/// ready answer of `generation` for the selection, unless a scripted
/// answer is next.
@MainActor
private final class ScriptedRendezvous {
    var generation = "g1"
    var next: [Data] = []

    func answer(for selection: DirectIrohSelection, client: DirectEndpointID) -> Data {
        guard next.isEmpty else {
            return next.removeFirst()
        }
        return DirectIrohFixtures.ready(
            generation: generation,
            client: client,
            instanceID: selection.instanceID,
            hostname: selection.host
        )
    }
}

/// An App with one direct relay committed on the iroh transport's path.
@MainActor
private struct CommittedDirectRelay {
    let app: AppModel
    let engine: DirectIrohFakeEngine
    let scheduler: DirectIrohManualScheduler
    let rendezvous: ScriptedRendezvous
    let log: DirectIrohEventLog
    let page: RuntimePage
    let client: LoopbackClient

    /// Fires the transport's refresh of its rendezvous answer: the one
    /// timer that is neither the stability timer nor the answer's deadline.
    func fireRefresh() throws {
        let stable = DirectIrohTiming.standard.stableSeconds
        let pending = scheduler.pending.filter { $0 != stable }.sorted()
        XCTAssertEqual(pending.count, 2, "\(scheduler.pending)")
        let refresh = try XCTUnwrap(pending.first)
        XCTAssertTrue(scheduler.fire(refresh))
    }

    func close() {
        client.close()
        page.close()
    }
}

/// The rendezvous requests a stand-in fetch was asked for, with the WebView
/// it was given for each.
@MainActor
private final class RendezvousRequests {
    struct Request {
        let selection: DirectIrohSelection
        let webView: ObjectIdentifier?
    }

    private(set) var requests: [Request] = []

    func record(_ selection: DirectIrohSelection, webView: WKWebView?) {
        requests.append(Request(selection: selection, webView: webView.map(ObjectIdentifier.init)))
    }
}

/// A controllable system-schedule platform for the App/bridge composition.
/// Its optional hold models the system permission sheet returning after the
/// Runtime that requested it has already been replaced.
@MainActor
private final class RuntimeSystemSchedulePlatform: SystemSchedulePlatforming {
    private var permissionContinuation: CheckedContinuation<Void, Never>?
    private var creationResultContinuation: CheckedContinuation<Void, Never>?
    private var cancelContinuation: CheckedContinuation<Void, Never>?
    var holdCancelIdentifier: String?
    var onCancelSuspended: (() -> Void)?
    private(set) var cancelIdentifiers: [String] = []
    private(set) var scheduleCalls = 0
    private(set) var systemCreationCalls = 0
    private(set) var cancelCalls = 0
    var holdBeforeSystemCreation = false
    var holdAfterSystemCreation = false
    var onCreationSuspended: (() -> Void)?
    var existenceResult: SystemScheduleExistence = .absentConclusive(.authorized)
    var cancelResult: Result<
        SystemScheduleAuthorization,
        SystemSchedulePlatformFailure
    > = .success(.authorized)

    var isWaitingForPermission: Bool {
        permissionContinuation != nil
    }

    var isWaitingAfterSystemCreation: Bool {
        creationResultContinuation != nil
    }

    func capabilities() -> SystemScheduleCapabilities {
        SystemScheduleCapabilities(
            iosVersion: "26.6.2",
            alarmKitAvailable: true,
            alarmAuthorization: .authorized,
            remindersAvailable: false,
            remindersAuthorization: .unsupported,
            supportedKinds: [.alarm]
        )
    }

    func systemIdentifier(
        for request: SystemScheduleRequest,
        owner: SystemScheduleManagementOwner
    ) -> String {
        "system-1"
    }

    func existence(
        kind: SystemScheduleKind,
        systemIdentifier: String
    ) async -> SystemScheduleExistence {
        existenceResult
    }

    func schedule(
        _ request: SystemScheduleRequest,
        systemIdentifier: String,
        stillCurrent: @escaping @MainActor () -> Bool
    ) async -> Result<SystemScheduleSubmission, SystemSchedulePlatformFailure> {
        scheduleCalls += 1
        if holdBeforeSystemCreation {
            await withCheckedContinuation { continuation in
                permissionContinuation = continuation
            }
        }
        guard stillCurrent() else {
            return .failure(.staleContext)
        }
        systemCreationCalls += 1
        if holdAfterSystemCreation {
            await withCheckedContinuation { continuation in
                creationResultContinuation = continuation
                onCreationSuspended?()
            }
        }
        return .success(
            SystemScheduleSubmission(
                systemIdentifier: systemIdentifier,
                authorization: .authorized
            )
        )
    }

    func cancel(
        kind: SystemScheduleKind,
        systemIdentifier: String,
        stillAuthorized: @escaping @MainActor () -> Bool
    ) async -> Result<SystemScheduleAuthorization, SystemSchedulePlatformFailure> {
        if holdCancelIdentifier == systemIdentifier {
            await withCheckedContinuation { continuation in
                cancelContinuation = continuation
                onCancelSuspended?()
            }
        }
        guard stillAuthorized() else { return .failure(.staleContext) }
        cancelCalls += 1
        cancelIdentifiers.append(systemIdentifier)
        return cancelResult
    }

    func releaseCancel() {
        let continuation = cancelContinuation
        cancelContinuation = nil
        holdCancelIdentifier = nil
        continuation?.resume()
    }

    func releasePermission() {
        let continuation = permissionContinuation
        permissionContinuation = nil
        holdBeforeSystemCreation = false
        continuation?.resume()
    }

    func releaseCreationResult() {
        let continuation = creationResultContinuation
        creationResultContinuation = nil
        holdAfterSystemCreation = false
        continuation?.resume()
    }
}

/// One runtime controller in a window, and its primary WebView.
@MainActor
private final class RuntimePage {
    /// Records each transport change dispatched in it, with no use of the
    /// bridge, so it works in any document.
    nonisolated static func listeningDocument(
        title: String,
        body: String = ""
    ) -> String {
        """
        <!doctype html>
        <title>\(title)</title>
        <script>
          window.changes = [];
          window.addEventListener(
            "avibe:native:direct.transportChanged",
            (event) => window.changes.push(event.detail)
          );
        </script>
        \(body)
        """
    }

    /// Records each transport change the bridge delivers.
    static let recordingDocument = """
        <!doctype html>
        <title>instance</title>
        <script>
          window.changes = [];
          window.avibeNative.on("direct.transportChanged", (status) => {
            window.changes.push(status);
          });
        </script>
        """

    private var window: UIWindow?
    private var webView: WKWebView?
    private let controller: WebViewController
    private let loader = PageLoader()

    init(controller: WebViewController) throws {
        self.controller = controller
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 700))
        window.rootViewController = controller
        window.makeKeyAndVisible()
        controller.loadViewIfNeeded()
        let webView = try XCTUnwrap(
            controller.view.subviews.compactMap { $0 as? WKWebView }.first
        )
        webView.navigationDelegate = loader
        self.window = window
        self.webView = webView
    }

    func load(_ html: String, baseURL: URL, forwardingNativeNavigation: Bool = false) async throws {
        let webView = try XCTUnwrap(webView)
        loader.forwardingDelegate = forwardingNativeNavigation ? controller : nil
        defer { loader.forwardingDelegate = nil }
        try await loader.loadHTML(html, baseURL: baseURL, in: webView)
    }

    /// The changes the document has recorded, as JSON.
    func changes() async throws -> String? {
        try await webView?.evaluateJavaScript(
            "JSON.stringify(window.changes ?? null)"
        ) as? String
    }

    /// The `direct` capability as the page sees it, and what
    /// `direct.status()` answers: the status, or the error's code.
    func directSurface() async throws -> String? {
        try await webView?.callAsyncJavaScript(
            """
            let status;
            try {
              const answer = await window.avibeNative.call("direct.status");
              status = Object.fromEntries(Object.keys(answer).sort().map((key) => [key, answer[key]]));
            } catch (error) {
              status = error.code;
            }
            return JSON.stringify({
              capability: window.avibeNative.capabilities.direct ?? null,
              status
            });
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        ) as? String
    }

    /// `direct.status()` as the page gets it, as JSON with sorted keys.
    func directStatus() async throws -> String? {
        try await webView?.callAsyncJavaScript(
            """
            const status = await window.avibeNative.call("direct.status");
            return JSON.stringify(status, Object.keys(status).sort());
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        ) as? String
    }

    /// Requests a system schedule through the page bridge and returns the
    /// terminal receipt exactly as JSON. The native confirmation may keep
    /// this call suspended until the test confirms or invalidates it.
    func systemScheduleRequest(_ params: [String: Any]) async throws -> String? {
        try await webView?.callAsyncJavaScript(
            """
            const receipt = await window.avibeNative.call(
              "systemSchedule.request",
              params
            );
            return JSON.stringify(receipt);
            """,
            arguments: ["params": params],
            in: nil,
            contentWorld: .page
        ) as? String
    }

    func systemScheduleOutcome(method: String, params: [String: Any]) async throws -> String? {
        try await webView?.callAsyncJavaScript(
            """
            try { return JSON.stringify({ receipt: await window.avibeNative.call(method, params) }); }
            catch (error) { return JSON.stringify({ errorCode: error.code }); }
            """,
            arguments: ["method": method, "params": params], in: nil, contentWorld: .page
        ) as? String
    }

    func systemScheduleCapabilities() async throws -> String? {
        try await webView?.callAsyncJavaScript(
            """
            const capabilities = await window.avibeNative.call(
              "systemSchedule.capabilities"
            );
            return JSON.stringify(capabilities);
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        ) as? String
    }

    func systemScheduleCancel(
        requestID: String,
        operationID: String,
        kind: String,
        purpose: String = "user"
    ) async throws -> String? {
        try await webView?.callAsyncJavaScript(
            """
            const receipt = await window.avibeNative.call(
              "systemSchedule.cancel",
              { requestId, operationId, kind, purpose }
            );
            return JSON.stringify(receipt);
            """,
            arguments: [
                "requestId": requestID,
                "operationId": operationID,
                "kind": kind,
                "purpose": purpose,
            ],
            in: nil,
            contentWorld: .page
        ) as? String
    }

    func hasTrustedScheduleSource(origin: WebOrigin) -> Bool {
        controller.hasTrustedPrimaryScheduleSource(origin: origin)
    }

    func beginPrimaryProvisionalNavigation() throws {
        controller.webView(
            try XCTUnwrap(webView),
            didStartProvisionalNavigation: nil
        )
    }

    /// Lets go of the WebView and its window, as the App does when the
    /// runtime is released.
    func close() {
        webView?.navigationDelegate = nil
        webView = nil
        window?.rootViewController = nil
        window?.isHidden = true
        window = nil
    }
}

/// Script terminal callbacks independently of WebContent timing. Navigation
/// tokens still come from WebKit: bare WKNavigation() instances lack the native
/// storage their deallocator requires. Token creation uses only local HTML.
final class PageLoaderTests: XCTestCase {
    @MainActor
    func testUnrelatedCallbacksCannotSettleTheRequestedDocument() async throws {
        enum Callback: CaseIterable {
            case staleFinish, staleFailure, staleProvisionalFailure
            case nilFinish, nilFailure, nilProvisionalFailure
            case wrongViewFinish, wrongViewFailure, wrongViewProvisionalFailure
            case wrongViewTermination
        }
        let view = makeWebView()
        let otherView = makeWebView()
        let loader = PageLoader()
        let staleNavigation = try view.makeNavigation()
        let expected = NSError(domain: "PageLoaderTests", code: 123)
        let unrelated = NSError(domain: "PageLoaderTests", code: 456)

        for callback in Callback.allCases {
            let navigation = try view.makeNavigation()
            view.nextNavigation = navigation
            view.onLoad = {
                switch callback {
                case .staleFinish:
                    loader.webView(view, didFinish: staleNavigation)
                case .staleFailure:
                    loader.webView(view, didFail: staleNavigation, withError: unrelated)
                case .staleProvisionalFailure:
                    loader.webView(view, didFailProvisionalNavigation: staleNavigation, withError: unrelated)
                case .nilFinish:
                    loader.webView(view, didFinish: nil)
                case .nilFailure:
                    loader.webView(view, didFail: nil, withError: unrelated)
                case .nilProvisionalFailure:
                    loader.webView(view, didFailProvisionalNavigation: nil, withError: unrelated)
                case .wrongViewFinish:
                    loader.webView(otherView, didFinish: navigation)
                case .wrongViewFailure:
                    loader.webView(otherView, didFail: navigation, withError: unrelated)
                case .wrongViewProvisionalFailure:
                    loader.webView(otherView, didFailProvisionalNavigation: navigation, withError: unrelated)
                case .wrongViewTermination:
                    loader.webViewWebContentProcessDidTerminate(otherView)
                }
                // A terminal fallback makes an incorrectly accepted callback
                // fail an assertion rather than leave this test hanging.
                loader.webView(view, didFail: navigation, withError: expected)
            }
            do {
                try await loader.loadHTML("fixture", baseURL: baseURL, in: view)
                XCTFail("Unrelated callback completed the load: \(callback)")
            } catch {
                XCTAssertEqual(error as NSError, expected, "\(callback)")
            }
        }
    }

    @MainActor
    func testMatchingFailuresIncludingCancellationAreNotIgnored() async throws {
        let view = makeWebView()
        let loader = PageLoader()
        for provisional in [false, true] {
            for code in [NSURLErrorCancelled, NSURLErrorCannotFindHost] {
                let navigation = try view.makeNavigation()
                let expected = NSError(domain: NSURLErrorDomain, code: code)
                view.nextNavigation = navigation
                view.onLoad = {
                    if provisional {
                        loader.webView(view, didFailProvisionalNavigation: navigation, withError: expected)
                    } else {
                        loader.webView(view, didFail: navigation, withError: expected)
                    }
                    loader.webView(view, didFinish: navigation)
                }
                do {
                    try await loader.loadHTML("fixture", baseURL: baseURL, in: view)
                    XCTFail("Matching failure was ignored: \(code), provisional=\(provisional)")
                } catch {
                    XCTAssertEqual(error as NSError, expected)
                }
            }
        }
    }

    @MainActor
    func testProcessTerminationFailsOnceAndLoaderCanBeReused() async throws {
        let view = makeWebView()
        let loader = PageLoader()
        let terminatedNavigation = try view.makeNavigation()
        view.nextNavigation = terminatedNavigation
        view.onLoad = {
            loader.webViewWebContentProcessDidTerminate(view)
            loader.webViewWebContentProcessDidTerminate(view)
            loader.webView(view, didFinish: terminatedNavigation)
        }
        do {
            try await loader.loadHTML("fixture", baseURL: baseURL, in: view)
            XCTFail("Process termination must fail, not accept a fallback document")
        } catch {
            XCTAssertEqual(error as? PageLoader.LoadError, .webContentProcessTerminated)
        }

        let nextNavigation = try view.makeNavigation()
        view.nextNavigation = nextNavigation
        view.onLoad = {
            loader.webView(
                view, didFail: terminatedNavigation,
                withError: NSError(domain: NSURLErrorDomain, code: NSURLErrorCancelled)
            )
            loader.webView(view, didFinish: nextNavigation)
            // Success must also clear the owner before a later termination.
            loader.webViewWebContentProcessDidTerminate(view)
        }
        try await loader.loadHTML("next fixture", baseURL: baseURL, in: view)
    }

    @MainActor
    func testMissingNavigationFailsAndClearsThePendingLoad() async throws {
        let view = makeWebView()
        let loader = PageLoader()
        view.nextNavigation = nil
        // A broken loader must fail an assertion, not suspend this test forever.
        view.onLoad = { loader.webView(view, didFinish: nil) }
        do {
            try await loader.loadHTML("fixture", baseURL: baseURL, in: view)
            XCTFail("A load without a navigation token cannot be observed")
        } catch {
            XCTAssertEqual(error as? PageLoader.LoadError, .navigationNotCreated)
        }
        let navigation = try view.makeNavigation()
        view.nextNavigation = navigation
        view.onLoad = { loader.webView(view, didFinish: navigation) }
        try await loader.loadHTML("next fixture", baseURL: baseURL, in: view)
    }

    private var baseURL: URL { URL(string: "https://fixture.example")! }

    @MainActor
    private func makeWebView() -> ScriptedPageLoaderWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        return ScriptedPageLoaderWebView(frame: .zero, configuration: configuration)
    }
}

@MainActor
private final class ScriptedPageLoaderWebView: WKWebView {
    var nextNavigation: WKNavigation?
    var onLoad: (() -> Void)?

    func makeNavigation() throws -> WKNavigation {
        // Bypass the scripted override to obtain a fully initialized token.
        // No navigationDelegate is installed, so WebKit cannot deliver these
        // real load callbacks to PageLoader; only onLoad does that.
        let navigation = try XCTUnwrap(super.loadHTMLString("<!doctype html>", baseURL: nil))
        stopLoading()
        return navigation
    }

    override func loadHTMLString(_ string: String, baseURL: URL?) -> WKNavigation? {
        let callback = onLoad
        onLoad = nil
        // Match WebKit's asynchronous delegate delivery: the loader must have
        // received the navigation token before these callbacks run.
        Task { @MainActor in callback?() }
        return nextNavigation
    }
}

/// Observes only the synthetic document requested by this loader. A runtime
/// controller can already have a URL navigation in flight when this delegate
/// is installed; its callbacks must not complete the synthetic document wait.
@MainActor
private final class PageLoader: NSObject, WKNavigationDelegate {
    enum LoadError: Error, Equatable, LocalizedError {
        case alreadyLoading
        case navigationNotCreated
        case webContentProcessTerminated

        var errorDescription: String? {
            switch self {
            case .alreadyLoading:
                return "PageLoader already has a pending fixture document load"
            case .navigationNotCreated:
                return "PageLoader could not create a navigation for the fixture document"
            case .webContentProcessTerminated:
                return "PageLoader WebContent process terminated before the fixture document finished loading"
            }
        }
    }

    private var continuation: CheckedContinuation<Void, Error>?
    private var navigation: WKNavigation?
    private weak var loadingWebView: WKWebView?
    weak var forwardingDelegate: WKNavigationDelegate?

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation?) {
        forwardingDelegate?.webView?(webView, didStartProvisionalNavigation: navigation)
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation?) {
        forwardingDelegate?.webView?(webView, didCommit: navigation)
    }

    func loadHTML(
        _ html: String,
        baseURL: URL,
        in webView: WKWebView
    ) async throws {
        guard continuation == nil else {
            throw LoadError.alreadyLoading
        }
        try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            loadingWebView = webView
            navigation = webView.loadHTMLString(html, baseURL: baseURL)
            if navigation == nil {
                finish(with: .failure(LoadError.navigationNotCreated))
            }
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation?) {
        forwardingDelegate?.webView?(webView, didFinish: navigation)
        finish(in: webView, navigation: navigation, with: .success(()))
    }

    func webView(
        _ webView: WKWebView,
        didFail navigation: WKNavigation?,
        withError error: Error
    ) {
        finish(in: webView, navigation: navigation, with: .failure(error))
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation?,
        withError error: Error
    ) {
        finish(in: webView, navigation: navigation, with: .failure(error))
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        guard webView === loadingWebView else { return }
        // The HTML document is gone. Do not let WebKit's fallback URL reload
        // masquerade as the requested fixture or wait for its DNS failure.
        finish(with: .failure(LoadError.webContentProcessTerminated))
    }

    private func finish(
        in webView: WKWebView,
        navigation: WKNavigation?,
        with result: Result<Void, Error>
    ) {
        guard webView === loadingWebView,
              let navigation, navigation === self.navigation
        else { return }
        // Cancellation of this navigation is a real failure. Only callbacks
        // for a different navigation are ignored, regardless of error code.
        finish(with: result)
    }

    private func finish(with result: Result<Void, Error>) {
        let pending = continuation
        continuation = nil
        navigation = nil
        loadingWebView = nil
        pending?.resume(with: result)
    }
}

/// Only the loopback server reads this from outside the main actor.
private final class SessionAnswerOverride: @unchecked Sendable {
    private let lock = NSLock()
    private var storedResponse: LocalHTTPResponse?
    private var heldFetch: (FixtureEvent, DispatchSemaphore)?
    var response: LocalHTTPResponse? {
        lock.lock()
        let response = storedResponse
        let held = heldFetch
        heldFetch = nil
        lock.unlock()
        // A 401 is consumed at headers, so hold BEFORE responding instead of
        // holding its body (which would not actually suspend fetchSession).
        if let (started, release) = held {
            started.signal()
            release.wait()
        }
        return response
    }

    func replace(with response: LocalHTTPResponse) {
        lock.lock()
        defer { lock.unlock() }
        storedResponse = response
    }

    func holdSignedOut(started: FixtureEvent, release: DispatchSemaphore) {
        hold(.status(401), started: started, release: release)
    }

    /// The next session read signals `started` and answers `response` once
    /// `release` is signaled; later reads answer `response` at once.
    func hold(
        _ response: LocalHTTPResponse,
        started: FixtureEvent,
        release: DispatchSemaphore
    ) {
        lock.lock()
        defer { lock.unlock() }
        storedResponse = response
        heldFetch = (started, release)
    }
}

/// Runs `start` at the first route the App sets after this is made, in the
/// same main-actor turn: for an open, that is where `openRuntime` has just
/// resumed push registration, before it starts preparing the route. Records
/// every route set from then on.
@MainActor
private final class AtTheOpen {
    private(set) var routes: [AppRoute] = []
    private(set) var started: Task<Void, Error>?
    private var didStart = false
    private var subscription: AnyCancellable?

    init(
        _ app: AppModel,
        start: @escaping @MainActor (AppModel) -> Task<Void, Error>?
    ) {
        subscription = app.$route.dropFirst().sink { [weak self, weak app] route in
            guard let self, let app else {
                return
            }
            self.routes.append(route)
            guard !self.didStart else {
                return
            }
            self.didStart = true
            self.started = start(app)
        }
    }
}

@MainActor
private final class HeldCleanupNotificationCenter: PushNotificationCentering {
    let cleanupStarted = XCTestExpectation(description: "native push notification cleanup suspended")
    private var continuation: CheckedContinuation<Void, Never>?

    func deliveredNotifications() async -> [PushNotificationCenterEntry] {
        await withCheckedContinuation { continuation in
            self.continuation = continuation
            cleanupStarted.fulfill()
        }
        return []
    }

    func releaseCleanup() {
        continuation?.resume()
        continuation = nil
    }

    func pendingNotificationRequests() async -> [PushNotificationCenterEntry] { [] }
    func removeDeliveredNotifications(withIdentifiers identifiers: [String]) {}
    func removePendingNotificationRequests(withIdentifiers identifiers: [String]) {}
}

@MainActor
private final class SilentPushNotificationCenter: PushNotificationCentering {
    func deliveredNotifications() async -> [PushNotificationCenterEntry] {
        []
    }

    func pendingNotificationRequests() async -> [PushNotificationCenterEntry] {
        []
    }

    func removeDeliveredNotifications(withIdentifiers identifiers: [String]) {}

    func removePendingNotificationRequests(withIdentifiers identifiers: [String]) {}
}

/// The App icon badge is not this suite's subject, and a test sets none.
private final class SilentBadgeCenter: AppBadgeCentering {
    func badgeAllowed() async -> Bool {
        false
    }

    func setBadgeCount(_ count: Int) async throws {}
}

/// The fault marker as a test sees it, in memory.
private final class FaultMarkerSpy: DirectFaultMarkerStoring {
    private(set) var isMarked: Bool
    private(set) var marks = 0
    private(set) var takes = 0
    /// Called inside `mark()`, before it returns.
    var whileMarking: () -> Void = {}

    init(marked: Bool) {
        isMarked = marked
    }

    func mark() {
        marks += 1
        whileMarking()
        isMarked = true
    }

    func take() -> Bool {
        takes += 1
        defer { isMarked = false }
        return isMarked
    }
}

/// A device with notifications on for account-1: the push keychain holds an
/// active registration for the account, and each open's push-registration
/// resume is answered with a device token, as APNs answers
/// `registerForRemoteNotifications` (the App delegate hands it to the push
/// coordinator's `didRegister`). The App's own push coordinator then sends
/// the provider update, which `providerPatches` holds in flight. The App's
/// real authorization read still runs; the tests require a host without
/// notification permission, so it never registers with APNs itself.
@MainActor
private final class NotificationsOn {
    let keychain = InMemoryPushKeychainStore()
    let generationStore = InMemoryPushGenerationStore()
    let marker = InMemoryPushInstallationMarkerStore()
    let providerPatches: ProviderPatchGate
    private(set) var tokensDelivered = 0
    private var opens: AnyCancellable?

    /// The App whose opens are answered: each open (a new runtime identity,
    /// set right after `openRuntime` resumes push registration) gets a token.
    weak var app: AppModel? {
        didSet {
            opens = app?.$runtimeIdentity.dropFirst().sink { [weak self] _ in
                self?.deliverToken()
            }
        }
    }

    init() throws {
        let installationID = UUID()
        marker.installationID = installationID
        keychain.installation = PushInstallationState(
            installationID: installationID,
            installationSecret: Data(repeating: 12, count: 32),
            accountID: "account-1",
            bindGeneration: 1,
            providerGeneration: 0,
            provider: nil,
            pendingBulkRevokeThroughGeneration: nil
        )
        try keychain.saveRecord(PushKeyRecord(
            kid: "fixture-active-key",
            instanceOrigin: "https://instance-one.example",
            accountID: "account-1",
            accountGeneration: generationStore.generation,
            bindGeneration: 1,
            privateKey: Data(),
            authSecret: Data(repeating: 3, count: 16),
            vapidKeyHash: "fixture",
            state: .active,
            pendingRevoke: false,
            replacementRegistrationIDs: []
        ))
        providerPatches = ProviderPatchGate(
            path: "/api/push/apns/installations/\(installationID.uuidString)/provider"
        )
    }

    /// As APNs does, after the registration call has returned.
    private func deliverToken() {
        tokensDelivered += 1
        Task { @MainActor [weak self] in
            self?.app?.pushCoordinator.didRegister(
                deviceToken: Data(repeating: 9, count: 32),
                environment: "sandbox"
            )
        }
    }
}

/// Answers each provider update with its headers at once and its body only
/// when released, so the request stays in flight until then.
private final class ProviderPatchGate: @unchecked Sendable {
    let path: String
    private let lock = NSLock()
    private var arrived = 0
    private var held: [DispatchSemaphore] = []

    init(path: String) {
        self.path = path
    }

    /// How many provider updates have reached the control plane.
    var arrivals: Int {
        lock.withLock { arrived }
    }

    func response() -> LocalHTTPResponse {
        let release = DispatchSemaphore(value: 0)
        lock.withLock {
            arrived += 1
            held.append(release)
        }
        return LocalHTTPResponse(
            status: 200,
            headers: [("Content-Type", "application/json")],
            body: Data(
                ("{\"pad\":\"" + String(repeating: "x", count: 100_000) + "\"}").utf8
            ),
            bodyRelease: release
        )
    }

    func releaseAll() {
        let released: [DispatchSemaphore] = lock.withLock {
            defer { held.removeAll() }
            return held
        }
        for release in released {
            release.signal()
        }
    }
}

/// Makes and registers each shown runtime's WebView controller, as the
/// root view does, and records what it made.
@MainActor
private final class RootViewStandIn {
    enum Registration {
        /// On the main actor right after the route changes: SwiftUI's next
        /// update.
        case nextUpdate
        /// Only once the open's provider update is in flight.
        case afterTheProviderUpdate
    }

    struct Made {
        let runtime: MobileRuntimeContext
        let controller: WebViewController
        /// Whether a provider update was in flight when it registered.
        let providerUpdateInFlight: Bool
    }

    private(set) var registrations: [Made] = []
    private var subscription: AnyCancellable?

    init(
        app: AppModel,
        notifications: NotificationsOn,
        registration: Registration
    ) {
        subscription = app.$route.dropFirst().sink { [weak self, weak app] route in
            guard let self, let app, case .browser(let runtime) = route else {
                return
            }
            let arrivals = notifications.providerPatches.arrivals
            switch registration {
            case .nextUpdate:
                // Queued behind the jobs the open has queued so far; the
                // provider update needs two more hops after those.
                Task { @MainActor in
                    self.register(runtime, in: app)
                }
            case .afterTheProviderUpdate:
                Task { @MainActor [weak self, weak app] in
                    let deadline = ContinuousClock.now.advanced(
                        by: WebKitReadiness.standardTransportOperation
                    )
                    while notifications.providerPatches.arrivals <= arrivals {
                        guard ContinuousClock.now < deadline else {
                            return
                        }
                        try? await Task.sleep(for: .milliseconds(10))
                    }
                    guard let self, let app else {
                        return
                    }
                    self.register(runtime, in: app)
                }
            }
        }
    }

    private func register(_ runtime: MobileRuntimeContext, in app: AppModel) {
        guard let origin = WebOrigin(url: runtime.instance.url) else {
            return
        }
        let controller = WebViewController(
            instanceOrigin: origin,
            websiteDataStore: app.runtimeWebsiteDataStore,
            directStatus: app.directStatusProvider,
            didStartLoading: {},
            didFinishLoading: {},
            didFailLoading: {}
        )
        let inFlight = !app.mobileSessionCoordinator.isCurrentRuntime(runtime)
            && app.mobileSessionCoordinator.isSelectedRuntime(runtime)
        app.registerRuntimeController(controller, for: runtime)
        controller.loadViewIfNeeded()
        registrations.append(
            Made(runtime: runtime, controller: controller, providerUpdateInFlight: inFlight)
        )
    }
}

/// Draft-only: a mock Workbench page standing in for the cached page PR 3
/// will restore.
@MainActor
private final class MockCachedPage {
    let webView = WKWebView(frame: .zero)
    var view: some View { MockWebViewHost(webView: webView).ignoresSafeArea() }

    func load(dark: Bool = false) async throws {
        let background = dark ? "#111" : "#fff"
        let text = dark ? "#eee" : "#111"
        let muted = dark ? "#999" : "#666"
        let rows = [
            ("奶酪包", "今天的日程我整理好了，下午三点有个评审。", "10:42"),
            ("产品伙伴", "冷启动方案的三张帧图已经发你了。", "10:30"),
            ("vibey", "PR #3920 的 CI 全部通过。", "昨天"),
            ("Studio 周报", "本周完成 12 项，延期 1 项。", "昨天"),
            ("账单提醒", "下月续费将在 3 天后扣款。", "周一"),
            ("旅行计划", "已为你比较了三家酒店的价格。", "周日"),
        ].map { name, line, time in
            """
            <div class="row"><div class="avatar">\(name.prefix(1))</div>
            <div class="body"><div class="top"><b>\(name)</b><span>\(time)</span></div>
            <div class="line">\(line)</div></div></div>
            """
        }.joined()
        let html = """
            <!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
            <style>
            body{margin:0;font-family:-apple-system,"PingFang SC";background:\(background);color:\(text)}
            .topbar{position:fixed;top:0;left:0;right:0;padding:env(safe-area-inset-top) 16px 0;height:44px;display:flex;align-items:center;gap:10px;background:\(background);border-bottom:1px solid rgba(127,127,127,.15)}
            .topbar .logo{width:28px;height:28px;border-radius:8px;background:#7c6cf2}
            .topbar b{flex:1;font-size:17px}.topbar .me{width:30px;height:30px;border-radius:15px;background:#10b981;color:#fff;display:flex;align-items:center;justify-content:center;font-size:13px}
            main{padding-top:calc(env(safe-area-inset-top) + 52px);padding-bottom:calc(env(safe-area-inset-bottom) + 64px)}
            h1{margin:8px 20px 2px;font-size:26px}
            .status{margin:0 20px 8px;font-size:12px;color:\(muted)}
            .row{display:flex;gap:12px;padding:12px 20px}
            .avatar{width:40px;height:40px;border-radius:20px;background:#7c6cf2;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:600}
            .body{flex:1;min-width:0}.top{display:flex;justify-content:space-between}
            .top span,.line{color:\(muted);font-size:13px}.line{margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
            .tabs{position:fixed;left:0;right:0;bottom:0;padding:6px 0 calc(env(safe-area-inset-bottom) + 4px);display:flex;justify-content:space-around;background:\(background);border-top:1px solid rgba(127,127,127,.15);font-size:11px;color:\(muted)}
            .tabs div{display:flex;flex-direction:column;align-items:center;gap:3px}.tabs i{width:24px;height:24px;border-radius:6px;background:rgba(127,127,127,.25)}
            .tabs .on{color:#10b981}.tabs .on i{background:#10b981}
            </style></head><body>
            <div class="topbar"><div class="logo"></div><b>Studio</b><div class="me">A</div></div>
            <main><h1>收件箱</h1><div class="status" id="status">上次更新 10:42</div>\(rows)</main>
            <div class="tabs"><div class="on"><i></i>收件箱</div><div><i></i>会话</div><div><i></i>应用</div><div><i></i>我</div></div>
            </body></html>
            """
        webView.isOpaque = false
        webView.loadHTMLString(html, baseURL: nil)
        try await Task.sleep(for: .milliseconds(800))
    }

    /// A Bot conversation, with the phone chat header's geometry from
    /// ChatHeaderBar and BotConversationTitle: a 44 pt bar with a 28 pt back
    /// button, the Bot switch (26 pt face, name, chevron), and on the right
    /// the Show Page control and the session menu.
    func loadChat() async throws {
        let html = """
            <!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
            <style>
            body{margin:0;font-family:-apple-system,"PingFang SC";background:#fff;color:#111}
            .hdr{position:fixed;top:0;left:0;right:0;height:44px;padding:env(safe-area-inset-top) 16px 0;display:flex;align-items:center;gap:12px;background:rgba(255,255,255,.92);border-bottom:1px solid #e5e5ea}
            .back{width:28px;height:28px;border:1px solid #d9d9e0;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0}
            .bot{margin-left:-6px;display:flex;align-items:center;gap:8px;padding:2px 8px 2px 6px;border-radius:999px;min-width:0}
            .face{width:26px;height:26px;border-radius:13px;background:#f5b041;flex-shrink:0}
            .bot b{font-size:16px;white-space:nowrap}.chev{color:#888;font-size:12px}
            .right{margin-left:auto;display:flex;gap:6px}.icon{width:28px;height:28px;border-radius:8px;border:1px solid #d9d9e0}
            main{padding:calc(env(safe-area-inset-top) + 56px) 16px 120px}
            .msg{max-width:78%;padding:10px 12px;border-radius:16px;margin:8px 0;font-size:15px;line-height:1.45}
            .me{margin-left:auto;background:#10b981;color:#fff}.it{background:#f2f2f7}
            .composer{position:fixed;left:12px;right:12px;bottom:calc(env(safe-area-inset-bottom) + 8px);height:44px;border-radius:22px;border:1px solid #d9d9e0;background:#fff;color:#999;display:flex;align-items:center;padding:0 16px;font-size:15px}
            </style></head><body>
            <div class="hdr"><button class="back" data-tap="返回">←</button>
            <button class="bot" data-tap="切换伙伴"><span class="face"></span><b>奶酪包</b><span class="chev">⌄</span></button>
            <div class="right"><button class="icon" data-tap="Show Page"></button><button class="icon" data-tap="会话菜单"></button></div></div>
            <main>
            <div class="msg me">帮我看看下午的日程</div>
            <div class="msg it">下午三点有一个评审，四点半和设计组同步。要我把评审材料先整理出来吗？</div>
            <div class="msg me">好，顺便提醒我带上周报</div>
            <div class="msg it">好的，三点前十分钟提醒你，周报我已经放进评审材料里了。</div>
            </main>
            <div class="composer">给奶酪包发消息</div>
            </body></html>
            """
        webView.loadHTMLString(html, baseURL: nil)
        try await Task.sleep(for: .milliseconds(800))
    }

    /// Each tappable element in the top bar, and whether it intersects the
    /// band the status pill can occupy: horizontally centred, at most
    /// `pillMaxWidth` wide, inside the top-bar row.
    func topBarOverlaps(windowWidth: Double) async -> String {
        let band = (windowWidth - Double(AvibeStyle.pillMaxWidth)) / 2
        let script = """
            JSON.stringify([...document.querySelectorAll('[data-tap]')].map(e => {
              const r = e.getBoundingClientRect();
              return {name: e.dataset.tap, left: r.left, right: r.right};
            }))
            """
        let json = (try? await webView.evaluateJavaScript(script) as? String) ?? "[]"
        let items = (try? JSONSerialization.jsonObject(with: Data(json.utf8)) as? [[String: Any]]) ?? []
        return items.map { item in
            let name = item["name"] as? String ?? "?"
            let left = item["left"] as? Double ?? 0
            let right = item["right"] as? Double ?? 0
            let overlap = max(0, min(right, band + Double(AvibeStyle.pillMaxWidth)) - max(left, band))
            return String(format: "%@ [%.0f–%.0f] overlap %.0f pt", name, left, right, overlap)
        }.joined(separator: "; ") + String(format: " (pill band %.0f–%.0f)", band, band + Double(AvibeStyle.pillMaxWidth))
    }

    func markLive() async throws {
        _ = try? await webView.evaluateJavaScript(
            "document.getElementById('status').textContent='刚刚更新';"
            + "document.querySelector('.row .line').textContent='新消息：下午三点的评审改到四点。';"
        )
    }

    func markCached() async throws {
        _ = try? await webView.evaluateJavaScript(
            "document.getElementById('status').textContent='上次更新 10:42';"
        )
    }
}

private struct MockWebViewHost: UIViewRepresentable {
    let webView: WKWebView
    func makeUIView(context: Context) -> WKWebView { webView }
    func updateUIView(_ uiView: WKWebView, context: Context) {}
}

/// Draft-only copy proposal for a timeout while a cached page shows: a
/// banner, the content kept (Product, 2026-11-18).
private struct CachedPageTimeoutBannerProposal: View {
    let instanceName: String

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: "wifi.exclamationmark")
                .font(.title3)
                .foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 3) {
                Text("暂时连不上 \(instanceName)")
                    .font(.subheadline.weight(.semibold))
                Text("显示的是上次的内容")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Button("重试", action: {})
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .tint(AvibeStyle.accent)
                .foregroundStyle(AvibeStyle.onAccent)
            Button(action: {}) {
                Image(systemName: "xmark")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
        }
        .padding(14)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .shadow(color: .black.opacity(0.12), radius: 14, y: 6)
        .padding(.horizontal, 16)
        // Above the page's tab bar, never over it.
        .padding(.bottom, AvibeStyle.tabBarHeight + 8)
    }
}
