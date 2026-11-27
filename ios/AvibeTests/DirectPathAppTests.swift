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
        // Nothing of the listener's recovery exists either.
        XCTAssertFalse(app.listenerRecoveryIsOverdue)
        XCTAssertTrue(app.directListenerFailures.isEmpty)
    }

    // MARK: The listener's recovery

    /// A launch with a transport removes what an earlier version stored to
    /// run the launch after a listener fault without a listener, and still
    /// uses the transport. Without a transport nothing of it is touched.
    @MainActor
    func testAnEarlierVersionsFaultNoteIsRemovedUnreadAndOnlyWithATransport() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport) {
            try XCTUnwrap(self.appDefaults).set(1, forKey: RetiredListenerFaultNote.key)
        }
        XCTAssertNil(try XCTUnwrap(appDefaults).object(forKey: RetiredListenerFaultNote.key))
        // The launch uses its listener all the same.
        XCTAssertNotNil(app.directStatusProvider)
        _ = try await select("instance-1", in: app)
        XCTAssertEqual(transport.selections.count, 1)

        let plain = try await makeApp(directTransport: nil) {
            try XCTUnwrap(self.appDefaults).set(1, forKey: RetiredListenerFaultNote.key)
        }
        XCTAssertNil(plain.directStatusProvider)
        XCTAssertEqual(
            try XCTUnwrap(appDefaults).object(forKey: RetiredListenerFaultNote.key) as? Int,
            1
        )
    }

    /// The owner's case (2026-11-24): the App comes back and its listener
    /// cannot be bound. Here something else holds the port, the one case
    /// the App cannot leave by itself. The page stays where it is, with the
    /// line; after the wait the message with Retry; Retry tries in place;
    /// and once the port is free the App serves again with nothing pressed,
    /// on the same page, with what was typed in it.
    @MainActor
    func testAReturnThatCannotBindItsPortKeepsThePageSaysSoAfterTheWaitAndRecoversByItself() async throws {
        let transport = StandInDirectTransport()
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(
            directTransport: transport,
            listenerRecoveryPatience: .milliseconds(600)
        ) {
            listening = try listeningSockets()
        }
        let first = try await select("instance-1", in: app)
        let port = try directListenerPort(of: app, since: listening)
        let page = try await showRuntimePage(app, first)
        defer { page.close() }
        // What the user typed and has not sent.
        _ = try await page.text("""
            document.body.insertAdjacentHTML("beforeend", "<textarea id='draft'></textarea>");
            document.getElementById("draft").value = "not sent yet";
            "typed"
            """)
        let identity = app.runtimeIdentity
        XCTAssertEqual(app.listenerRecoveryPresentation(for: first), .none)

        enterBackground()
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: port.rawValue,
            reuseAddress: true
        )
        var squatterIsOpen = true
        defer {
            if squatterIsOpen {
                squatter.close()
            }
        }
        // Nothing is shown, or bound, in the background.
        XCTAssertEqual(app.listenerRecoveryPresentation(for: first), .none)

        // The return, in its own turn: the page's loads wait for the first
        // attempt, as on every return, with nothing shown yet.
        enterForeground()
        XCTAssertEqual(app.directLoadGate(for: first), .held)
        XCTAssertEqual(app.listenerRecoveryPresentation(for: first), .none)
        XCTAssertFalse(app.listenerRecoveryIsOverdue)

        // Past the wait, the message with Retry. Something else may hold
        // the port, so nothing is loaded against it; the runtime, its page
        // and its open are as they were.
        try await eventually { app.listenerRecoveryPresentation(for: first) == .unreachable }
        XCTAssertEqual(app.directLoadGate(for: first), .fault)
        XCTAssertFalse(app.mayReload(first))
        XCTAssertFalse(app.authorizeReload(of: first))
        // Retry is offered all the same: its tap binds the port again.
        XCTAssertTrue(app.offersRetry(for: first))
        XCTAssertEqual(app.route, .browser(first))
        XCTAssertEqual(app.runtimeIdentity, identity)
        XCTAssertNil(app.runtimeOpenNotice)
        // The failure is on record, for the next rendezvous request.
        XCTAssertFalse(app.directListenerFailures.isEmpty)

        // Retry, with the port still taken: an attempt now, the line while
        // it runs, and the message again as soon as it has failed too.
        app.retryListenerRecovery()
        XCTAssertEqual(app.listenerRecoveryPresentation(for: first), .connecting)
        try await eventually { app.listenerRecoveryPresentation(for: first) == .unreachable }

        // The other holder lets go: the next attempt binds, by itself.
        squatter.close()
        squatterIsOpen = false
        try await eventually(timeout: .seconds(20)) {
            app.listenerRecoveryPresentation(for: first) == .none
                && app.directLoadGate(for: first) == .open
        }
        XCTAssertFalse(app.listenerRecoveryIsOverdue)
        XCTAssertTrue(app.mayReload(first))
        // The same page, never rebuilt or reloaded.
        XCTAssertEqual(app.route, .browser(first))
        XCTAssertEqual(app.runtimeIdentity, identity)
        XCTAssertFalse(page.webContentProcessTerminated)
        let draft = try await page.text("document.getElementById('draft').value")
        XCTAssertEqual(draft, "not sent yet")

        // Another stay in the foreground starts with nothing to say.
        enterBackground()
        enterForeground()
        try await eventually { app.directLoadGate(for: first) == .open }
        XCTAssertEqual(app.listenerRecoveryPresentation(for: first), .none)
        XCTAssertFalse(app.listenerRecoveryIsOverdue)
    }

    /// Retry binds the port at once: it does not wait for the recovery's
    /// own next attempt, which by then is seconds away.
    @MainActor
    func testRetryBindsTheListenersPortAtOnceWhenItIsFreeAgain() async throws {
        let transport = StandInDirectTransport()
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(
            directTransport: transport,
            listenerRecoveryPatience: .milliseconds(300)
        ) {
            listening = try listeningSockets()
        }
        let runtime = try await select("instance-1", in: app)
        let port = try directListenerPort(of: app, since: listening)

        enterBackground()
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: port.rawValue,
            reuseAddress: true
        )
        enterForeground()
        try await eventually { app.listenerRecoveryPresentation(for: runtime) == .unreachable }
        // The recovery's own attempts come 0.25, 0.5, 1, 2 and then 5 s
        // apart (`DirectPathTiming.standard`): past the fourth, the next is
        // about 5 s away.
        try await Task.sleep(for: .milliseconds(4_200))
        XCTAssertEqual(app.listenerRecoveryPresentation(for: runtime), .unreachable)
        squatter.close()

        app.retryListenerRecovery()
        try await eventually(timeout: .seconds(2)) {
            app.listenerRecoveryPresentation(for: runtime) == .none
                && app.directLoadGate(for: runtime) == .open
        }
    }

    /// The failure screen's Retry, pressed while something else holds the
    /// listener's port: nothing may be reloaded against that port, so the
    /// request is kept for that open, and made by itself as soon as the
    /// port is the App's again. The user taps once.
    @MainActor
    func testARetryPressedWhileThePortIsSomeoneElsesIsMadeByItselfWhenThePortIsFree() async throws {
        let transport = StandInDirectTransport()
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(directTransport: transport) {
            listening = try listeningSockets()
        }
        let runtime = try await select("instance-1", in: app)
        let port = try directListenerPort(of: app, since: listening)
        let failedID = try XCTUnwrap(app.runtimeOpenAttempt?.id)
        // The open ended on its failure screen.
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .failed, destination: .instance),
            attemptID: failedID,
            runtime: runtime
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .failed)

        enterBackground()
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: port.rawValue,
            reuseAddress: true
        )
        var squatterIsOpen = true
        defer {
            if squatterIsOpen {
                squatter.close()
            }
        }
        enterForeground()
        try await eventually { app.directLoadGate(for: runtime) == .fault }
        XCTAssertTrue(app.offersRetry(for: runtime))
        XCTAssertFalse(app.holdsRetry(forAttempt: failedID))

        // The one tap: nothing is reloaded against the port, and the
        // request is kept for this open.
        XCTAssertFalse(app.retryRuntimeOpen(attemptID: failedID))
        XCTAssertTrue(app.holdsRetry(forAttempt: failedID))
        XCTAssertEqual(app.runtimeOpenAttempt?.id, failedID)
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .failed)
        XCTAssertEqual(app.route, .browser(runtime))
        // Still kept while the port is someone else's, attempt after attempt.
        try await Task.sleep(for: .milliseconds(900))
        XCTAssertEqual(app.directLoadGate(for: runtime), .fault)
        XCTAssertTrue(app.holdsRetry(forAttempt: failedID))
        XCTAssertEqual(app.runtimeOpenAttempt?.id, failedID)

        // The port is free: the runtime opens again, with no second tap.
        squatter.close()
        squatterIsOpen = false
        try await eventually(timeout: .seconds(20)) {
            guard let attempt = app.runtimeOpenAttempt else {
                return false
            }
            return attempt.id != failedID && attempt.phase != .failed
        }
        XCTAssertFalse(app.holdsRetry(forAttempt: failedID))
        XCTAssertNil(app.heldRuntimeRetry)
        let reopened = try await waitForBrowser("instance-1", in: app)
        XCTAssertEqual(app.directLoadGate(for: reopened), .open)
        XCTAssertGreaterThan(app.runtimeIdentity, failedID)
    }

    /// A kept Retry belongs to the open it was pressed for: another
    /// instance opened meanwhile drops it, and nothing reopens the first
    /// when the port is free.
    @MainActor
    func testAKeptRetryIsDroppedWhenAnotherInstanceIsOpened() async throws {
        let transport = StandInDirectTransport()
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(directTransport: transport) {
            listening = try listeningSockets()
        }
        let runtime = try await select("instance-1", in: app)
        let port = try directListenerPort(of: app, since: listening)
        let failedID = try XCTUnwrap(app.runtimeOpenAttempt?.id)
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .failed, destination: .instance),
            attemptID: failedID,
            runtime: runtime
        )
        enterBackground()
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: port.rawValue,
            reuseAddress: true
        )
        var squatterIsOpen = true
        defer {
            if squatterIsOpen {
                squatter.close()
            }
        }
        enterForeground()
        try await eventually { app.directLoadGate(for: runtime) == .fault }
        XCTAssertFalse(app.retryRuntimeOpen(attemptID: failedID))
        XCTAssertTrue(app.holdsRetry(forAttempt: failedID))

        // The user goes to another instance instead.
        let other = try await select("instance-2", in: app)
        XCTAssertFalse(app.holdsRetry(forAttempt: failedID))

        squatter.close()
        squatterIsOpen = false
        try await eventually(timeout: .seconds(20)) { app.directLoadGate(for: runtime) == .open }
        try await settle()
        XCTAssertNil(app.heldRuntimeRetry)
        XCTAssertEqual(app.route, .browser(other))
    }

    /// The recovery covers only a runtime whose host is routed to the
    /// listener. Another instance, opened while the listener could not
    /// serve, is on WebKit's own path: not covered, its open not failed.
    /// And the routed instance, opened again meanwhile, waits and then
    /// opens by itself.
    @MainActor
    func testTheListenersRecoveryCoversOnlyARoutedRuntimeAndFailsNoOtherOpen() async throws {
        let transport = StandInDirectTransport()
        var listening: Set<ListeningSocket> = []
        let app = try await makeApp(
            directTransport: transport,
            listenerRecoveryPatience: .milliseconds(300)
        ) {
            listening = try listeningSockets()
        }
        let routed = try await select("instance-1", in: app)
        let port = try directListenerPort(of: app, since: listening)

        enterBackground()
        try await eventually { await !LoopbackClient.canConnect(to: port) }
        let squatter = try ControlListener(
            family: AF_INET,
            address: "127.0.0.1",
            port: port.rawValue,
            reuseAddress: true
        )
        var squatterIsOpen = true
        defer {
            if squatterIsOpen {
                squatter.close()
            }
        }
        let ordinary = try await select("instance-2", in: app)
        let attemptID = try XCTUnwrap(app.runtimeOpenAttempt?.id)

        enterForeground()
        try await eventually { app.listenerRecoveryPresentation(for: routed) == .unreachable }
        XCTAssertEqual(app.directLoadGate(for: routed), .fault)
        XCTAssertEqual(app.listenerRecoveryPresentation(for: ordinary), .none)
        XCTAssertEqual(app.directLoadGate(for: ordinary), .open)
        XCTAssertTrue(app.mayReload(ordinary))
        XCTAssertEqual(app.route, .browser(ordinary))
        XCTAssertEqual(app.runtimeOpenAttempt?.id, attemptID)
        XCTAssertNotEqual(app.runtimeOpenAttempt?.phase, .failed)

        // The routed instance is opened again while something else may
        // hold its port: no WebView is made against that port.
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        try await settle()
        XCTAssertEqual(app.route, .loading)

        // The port is free again: the open goes on by itself.
        squatter.close()
        squatterIsOpen = false
        let reopened = try await waitForBrowser("instance-1", in: app)
        XCTAssertEqual(app.directLoadGate(for: reopened), .open)
        XCTAssertEqual(app.listenerRecoveryPresentation(for: reopened), .none)
        XCTAssertTrue(app.mayReload(reopened))
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
        if !theSimulatorRefusedABind(of: app) {
            XCTAssertEqual(app.directLoadGate(for: reopened), .open)
            XCTAssertEqual(app.listenerRecoveryPresentation(for: reopened), .none)
            XCTAssertTrue(app.mayReload(reopened))
        }
    }

    /// The Action Button's hidden WebView asks the gate through the model
    /// (`VoiceCaptureLoadGate.provider`), by its take's own host. A take
    /// kept for an instance that is no longer the one shown still loads
    /// through the listener that host is routed to, so its gate follows
    /// that listener: held while the background has it closed, as the
    /// page's own gate for that instance is. A host that was never opened
    /// has no route, and a take on another store has none of ours: open.
    @MainActor
    func testTheVoiceGateSpeaksForATakesHostWhateverInstanceIsShown() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let first = try await select("instance-1", in: app)
        let routed = try XCTUnwrap(WebOrigin(url: first.instance.url))
        let neverOpened = try XCTUnwrap(
            WebOrigin(url: try XCTUnwrap(URL(string: "https://never-opened.example")))
        )
        let store = app.websiteDataStore.identifier
        let gate = try XCTUnwrap(VoiceCaptureLoadGate.provider)
        XCTAssertEqual(gate(routed, store), .open)

        // Another instance is the one shown now.
        let second = try await select("instance-2", in: app)
        let shown = try XCTUnwrap(WebOrigin(url: second.instance.url))
        XCTAssertEqual(app.route, .browser(second))
        XCTAssertEqual(gate(routed, store), .open, "its listener serves")

        enterBackground()
        XCTAssertEqual(
            gate(routed, store), .held,
            "not the instance shown, but still routed to the closed listener"
        )
        XCTAssertEqual(gate(routed, store), app.directLoadGate(for: first), "as the page's gate for it")
        XCTAssertEqual(gate(shown, store), app.directLoadGate(for: second))
        XCTAssertEqual(gate(neverOpened, store), .open, "no route: WebKit's normal path")
        XCTAssertEqual(gate(routed, UUID()), .open, "another store has no route of ours")

        // The return settles: the listener serves again, or failed.
        enterForeground()
        try await eventually { gate(routed, store) != .held }
        XCTAssertEqual(gate(routed, store), app.directLoadGate(for: first))
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
        guard !theSimulatorRefusedABind(of: app) else {
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
    func testBothScheduleKindsKeepTheirIntentThroughConfirmationReceiptAndCancellation() async throws {
        for kind in SystemScheduleKind.allCases {
            let now = Date(timeIntervalSince1970: 1_800_000_000)
            let platform = RuntimeSystemSchedulePlatform()
            platform.supportsReminders = true
            let app = try await makeApp(
                directTransport: nil,
                systemSchedulePlatform: platform,
                systemScheduleNow: { now }
            )
            let runtime = try await select("instance-1", in: app)
            let page = try await showRuntimePage(app, runtime)
            defer { page.close() }
            var params = scheduleParams(
                requestID: "create-\(kind.rawValue)",
                operationID: "operation-1",
                now: now
            )
            params["kind"] = kind.rawValue
            params["title"] = kind == .alarm ? "Wake up" : "取快递"
            let requestParams = params
            let creation = Task { @MainActor in
                try await page.systemScheduleRequest(requestParams)
            }
            try await eventually { app.systemScheduleConfirmation != nil }
            let confirmation = try XCTUnwrap(app.systemScheduleConfirmation)
            XCTAssertEqual(confirmation.request.kind, kind)
            XCTAssertEqual(confirmation.request.title, params["title"] as? String)
            XCTAssertEqual(platform.systemCreationCalls, 0)

            app.confirmSystemSchedule(id: confirmation.id)
            let receipt = try jsonObject(try await creation.value)
            XCTAssertEqual(receipt["kind"] as? String, kind.rawValue)
            XCTAssertEqual(receipt["state"] as? String, "scheduled")
            XCTAssertEqual(receipt["systemIdentifier"] as? String, "system-1")
            XCTAssertEqual(platform.systemCreationCalls, 1)
            XCTAssertNil(app.systemScheduleConfirmation)

            let cancellation = try jsonObject(try await page.systemScheduleCancel(
                requestID: "cancel-\(kind.rawValue)",
                operationID: "operation-1",
                kind: kind.rawValue
            ))
            XCTAssertEqual(cancellation["kind"] as? String, kind.rawValue)
            XCTAssertEqual(cancellation["state"] as? String, "cancelled")
            XCTAssertEqual(platform.cancelIdentifiers, ["system-1"])
            let repeated = try jsonObject(try await page.systemScheduleCancel(
                requestID: "repeat-cancel-\(kind.rawValue)",
                operationID: "operation-1",
                kind: kind.rawValue
            ))
            XCTAssertEqual(repeated["state"] as? String, "cancelled")
            XCTAssertEqual(platform.cancelCalls, 1)
        }
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
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
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
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
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
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
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
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
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
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
            failedWrites > 0 && controller.presentedViewController == nil
        }
        try await eventually {
            (try? await webView.evaluateJavaScript(
                """
                Array.from(document.querySelectorAll('button')).some(b =>
                  ['Delete account', 'Continue with email'].includes(b.textContent.trim()) && !b.disabled)
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
                try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
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
                try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
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
            try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
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
            try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
                for page in self.bundledWebViews(in: window) {
                    if (try? await page.evaluateJavaScript(
                        "document.body.textContent.includes('Continue with email')"
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
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
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
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) { await showsLocalError() }
        let retry = """
            Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === 'Retry').click()
            """
        _ = try await webView.evaluateJavaScript(retry)
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) { await showsLocalError() }
        XCTAssertNil(app.mobileSessionCoordinator.currentAccount)
        XCTAssertNil(app.captureSystemScheduleAdmission(origin: instance))
        failing = false
        _ = try await webView.evaluateJavaScript(retry)
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
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
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
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
            // The page's process has ended under this test on CI: see
            // `onceMoreIfItsPageProcessEnded`.
            try await onceMoreIfItsPageProcessEnded { shown in
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
                // Recorded before its first load, which is also the page's.
                shown.page = page
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
        try await onceMoreIfItsPageProcessEnded { record in
            try await self.sameOriginReturnScenario(recording: record)
        }
    }

    /// WebKit can end a page's WebContent process under a test (seen on CI
    /// at its network process's request, run 44658571690). The page's load
    /// or its bridge call in flight then fails, and the scenario cannot be
    /// finished without its page. So `scenario` is run once more, from the
    /// start, only when the fixture saw the page it recorded lose its
    /// process; any other failure, and a second ended process, fails the
    /// test. Only for a test with such a failure on record.
    @MainActor
    private func onceMoreIfItsPageProcessEnded(
        _ scenario: @MainActor (RuntimePageRecord) async throws -> Void
    ) async throws {
        let first = RuntimePageRecord()
        do {
            try await scenario(first)
            return
        } catch where first.page?.webContentProcessTerminated == true {
            print("[iOS fixture] stage=scenario run again, its page's process ended: \(error)")
        }
        // A page whose first load failed was never handed to the scenario,
        // which closes the ones it gets. Closing twice is harmless.
        first.page?.close()
        try await scenario(RuntimePageRecord())
    }

    @MainActor
    private func sameOriginReturnScenario(recording record: RuntimePageRecord) async throws {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        let platform = RuntimeSystemSchedulePlatform()
        let app = try await makeApp(
            directTransport: nil,
            systemSchedulePlatform: platform,
            systemScheduleNow: { now }
        )
        let runtime = try await select("instance-1", in: app)
        let page = try await showRuntimePage(app, runtime, recording: record)
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
        XCTAssertEqual(app.listenerRecoveryPresentation(for: runtime), .none)
        XCTAssertTrue(app.offersRetry(for: runtime))
        enterBackground()
        XCTAssertTrue(app.mayReload(runtime))
        enterForeground()
        XCTAssertTrue(app.mayReload(runtime))
    }

    /// An open whose page loaded from the cache stays covered (the loading
    /// layer) until the instance answers for this session, which ends the
    /// open. Without an answer the open ends as any open does: back to the
    /// list with the timeout notice. A stored page is used for at most a
    /// day.
    @MainActor
    func testACachedPageStaysCoveredUntilItsInstanceAnswers() async throws {
        let timing = RuntimeOpenManualTiming()
        let app = try await makeApp(directTransport: nil, runtimeOpenTiming: timing)
        defer { app.runtimePageCache.removeAll() }
        let runtime = try await select("instance-1", in: app)
        let attempt = try XCTUnwrap(app.runtimeOpenAttempt)

        // Covered from the cached document's start to its load, never the
        // uncovered `.loadingPage`.
        for stage in [RuntimeNavigationStage.started, .committed, .finished] {
            app.handleRuntimeNavigation(
                RuntimeNavigationEvent(stage: stage, destination: .instance, fromCache: true),
                attemptID: attempt.id,
                runtime: runtime
            )
            XCTAssertEqual(app.runtimeOpenAttempt?.phase, .showingCached, "\(stage)")
            XCTAssertEqual(
                BrowserOverlay(attemptPhase: try XCTUnwrap(app.runtimeOpenAttempt).phase),
                .loading,
                "\(stage)"
            )
        }
        app.runtimeCachedPageConnected(attemptID: attempt.id, runtime: runtime)
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .documentLoaded)
        XCTAssertEqual(app.route, .browser(runtime))

        // Opened again from the cache, and the instance never answers.
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-2")
        let other = try await waitForBrowser("instance-2", in: app)
        let second = try XCTUnwrap(app.runtimeOpenAttempt)
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .finished, destination: .instance, fromCache: true),
            attemptID: second.id,
            runtime: other
        )
        timing.advance(by: .seconds(60))
        XCTAssertTrue(isOnInstancePicker(app))
        XCTAssertEqual(app.runtimeOpenNotice?.lastPhase, .showingCached)

        // A day-old page is not used, and is removed.
        let access = try XCTUnwrap(app.runtimePageCacheAccess(for: runtime))
        let origin = try XCTUnwrap(WebOrigin(url: runtime.instance.url)).serialized
        app.runtimePageCache.store(
            RuntimeCachedPage(
                url: runtime.instance.url,
                headers: ["Content-Type": "text/html"],
                body: Data("<!doctype html>".utf8),
                capturedAt: Date().addingTimeInterval(-(RuntimePageCache.maximumAge + 60))
            ),
            accountID: "account-1",
            origin: origin
        )
        XCTAssertNil(access.load())
        XCTAssertNil(app.runtimePageCache.page(accountID: "account-1", origin: origin))
    }

    /// A cached page is covered from its start; once it says it keeps its
    /// shell while the instance cannot be reached it is uncovered: only the
    /// progress line, the long-wait row, and out of time the page stays with
    /// the "can't reach" banner and Retry (no return to the list). A late
    /// answer ends it. A live navigation from the page leaves it on screen
    /// until a document replaces it (a handoff, a cancel or a failure before
    /// that changes nothing); the replacing document opens covered, with a
    /// fresh, bounded budget. Retry opens it live, skipping the cache once.
    @MainActor
    func testAMarkedCachedPageShowsUncoveredAndKeepsItsContentOutOfTime() async throws {
        let timing = RuntimeOpenManualTiming()
        let app = try await makeApp(directTransport: nil, runtimeOpenTiming: timing)
        defer { app.runtimePageCache.removeAll() }
        let runtime = try await select("instance-1", in: app)
        let attempt = try XCTUnwrap(app.runtimeOpenAttempt)
        let cached = { (stage: RuntimeNavigationStage) in
            RuntimeNavigationEvent(stage: stage, destination: .instance, fromCache: true)
        }
        let live = { (stage: RuntimeNavigationStage) in
            RuntimeNavigationEvent(stage: stage, destination: .instance)
        }
        func show(_ attempt: RuntimeOpenAttempt, _ runtime: MobileRuntimeContext) {
            for stage in [RuntimeNavigationStage.started, .committed, .finished] {
                app.handleRuntimeNavigation(cached(stage), attemptID: attempt.id, runtime: runtime)
            }
            app.runtimeCachedPageKeepsShell(attemptID: attempt.id, runtime: runtime)
        }
        for stage in [RuntimeNavigationStage.started, .committed, .finished] {
            app.handleRuntimeNavigation(cached(stage), attemptID: attempt.id, runtime: runtime)
            // Covered until the page says it keeps its shell.
            XCTAssertEqual(app.runtimeOpenAttempt?.phase, .showingCached, "\(stage)")
        }
        app.runtimeCachedPageKeepsShell(attemptID: attempt.id, runtime: runtime)
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .showingCachedLive)
        XCTAssertEqual(
            BrowserOverlay(attemptPhase: .showingCachedLive),
            .showingCachedLive
        )
        // A live navigation from the page that never replaces it (a link
        // whose response is a download, then nothing; a blocked one; one that
        // fails): the page stays, the open keeps waiting.
        for end in [RuntimeNavigationStage.handedOff, .cancelled, .failed] {
            app.handleRuntimeNavigation(live(.started), attemptID: attempt.id, runtime: runtime)
            app.handleRuntimeNavigation(live(end), attemptID: attempt.id, runtime: runtime)
            XCTAssertEqual(app.runtimeOpenAttempt?.phase, .showingCachedLive, "\(end)")
        }

        timing.advance(by: .seconds(8))
        XCTAssertTrue(try XCTUnwrap(app.runtimeOpenAttempt).isTakingLong)
        timing.advance(by: .seconds(52))
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .cachedUnreachable)
        XCTAssertEqual(
            BrowserOverlay(attemptPhase: .cachedUnreachable),
            .cachedUnreachable
        )
        XCTAssertNil(app.runtimeOpenNotice)

        // A late answer from the instance ends it.
        app.runtimeCachedPageConnected(attemptID: attempt.id, runtime: runtime)
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .documentLoaded)

        // Out of time again; a live document replacing the page opens
        // covered, and is bounded again: out of time, the open ends.
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-2")
        let other = try await waitForBrowser("instance-2", in: app)
        let second = try XCTUnwrap(app.runtimeOpenAttempt)
        show(second, other)
        timing.advance(by: .seconds(60))
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .cachedUnreachable)
        app.handleRuntimeNavigation(live(.started), attemptID: second.id, runtime: other)
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .cachedUnreachable)
        app.handleRuntimeNavigation(live(.committed), attemptID: second.id, runtime: other)
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .loadingPage)
        XCTAssertEqual(
            BrowserOverlay(attemptPhase: .loadingPage),
            .loading
        )
        XCTAssertFalse(try XCTUnwrap(app.runtimeOpenAttempt).isTakingLong)
        timing.advance(by: .seconds(59))
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .loadingPage)
        timing.advance(by: .seconds(1))
        XCTAssertTrue(isOnInstancePicker(app))
        XCTAssertEqual(app.runtimeOpenNotice?.lastPhase, .loadingPage)

        // The page's process ending while out of time: the live reload is
        // bounded as well.
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        _ = try await waitForBrowser("instance-1", in: app)
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-2")
        let reopened = try await waitForBrowser("instance-2", in: app)
        let restarted = try XCTUnwrap(app.runtimeOpenAttempt)
        show(restarted, reopened)
        timing.advance(by: .seconds(60))
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .cachedUnreachable)
        app.handleRuntimeNavigation(live(.restarting), attemptID: restarted.id, runtime: reopened)
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .connecting)
        timing.advance(by: .seconds(60))
        XCTAssertTrue(isOnInstancePicker(app))

        // Out of time once more; Retry opens it live, skipping the cache once.
        let access = try XCTUnwrap(app.runtimePageCacheAccess(for: reopened))
        let page = RuntimeCachedPage(
            url: reopened.instance.url,
            headers: ["Content-Type": "text/html"],
            body: Data("<!doctype html>".utf8),
            capturedAt: Date(),
            keepsShellUnreachable: true
        )
        access.store(page)
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        let back = try await waitForBrowser("instance-1", in: app)
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-2")
        let again = try await waitForBrowser("instance-2", in: app)
        let third = try XCTUnwrap(app.runtimeOpenAttempt)
        _ = back
        show(third, again)
        timing.advance(by: .seconds(60))
        XCTAssertTrue(app.retryRuntimeCachedPage())
        XCTAssertGreaterThan(try XCTUnwrap(app.runtimeOpenAttempt).id, third.id)
        let retried = try XCTUnwrap(app.runtimePageCacheAccess(for: again))
        XCTAssertNil(retried.load())
        XCTAssertEqual(retried.load(), page)
    }

    /// avibe.bot listing the instance offline after an open-first decides
    /// the same way whenever its answer arrives. A cached page that keeps its
    /// shell offline (marked, and declaring a replayed shell) stays,
    /// uncovered, with the offline row, before or after it was uncovered;
    /// it connects by itself when the instance answers. With no such page
    /// (none stored, stored unmarked, or one that does not declare a
    /// replayed shell) the open ends at the instance list, as before.
    /// Retry opens it again and asks avibe.bot again; Switch instance shows
    /// the instances over the page.
    @MainActor
    func testAnInstanceListedOfflineKeepsOnlyACachedPageThatKeepsItsShell() async throws {
        let listing = { (available: Bool) in
            LocalHTTPResponse.json("""
                {"account":{"id":"account-1","email":"max@example.com"},"instances":[
                {"id":"instance-1","name":"One","host":"instance-one.example",
                 "url":"https://instance-one.example","relationship":"owner",
                 "owner_email":"max@example.com","available":\(available)}]}
                """)
        }
        enum Stored { case none, unmarked, marked }
        enum Shown { case live, covered, uncovered }
        enum Then { case declines, keepsShell, nothing }
        let rows: [(name: String, stored: Stored, shown: Shown?, then: Then, stays: Bool)] = [
            ("no cached page", .none, nil, .nothing, false),
            ("an unmarked cached page", .unmarked, .covered, .nothing, false),
            ("a live load started before the answer", .marked, .live, .nothing, false),
            ("a marked page that does not declare a replayed shell", .marked, .covered, .declines, false),
            ("answered before the uncover", .marked, .covered, .keepsShell, true),
            ("answered after the uncover", .marked, .uncovered, .nothing, true),
        ]
        for row in rows {
            // A manual clock: no time-out can end an open the rule failed to.
            let timing = RuntimeOpenManualTiming()
            let first = try await makeApp(directTransport: nil)
            let selected = try await select("instance-1", in: first)
            let defaults = try XCTUnwrap(appDefaults)
            if row.stored != .none {
                try XCTUnwrap(first.runtimePageCacheAccess(for: selected)).store(
                    RuntimeCachedPage(
                        url: selected.instance.url,
                        headers: ["Content-Type": "text/html"],
                        body: Data("<!doctype html>".utf8),
                        capturedAt: Date(),
                        keepsShellUnreachable: row.stored == .marked ? true : nil
                    )
                )
            }

            let session = SessionAnswerOverride()
            let started = FixtureEvent(description: "session read: \(row.name)")
            let release = DispatchSemaphore(value: 0)
            session.hold(listing(false), started: started, release: release)
            let next = try await makeApp(
                directTransport: nil,
                sessionResponse: { session.response },
                runtimeOpenTiming: timing,
                reusingDefaults: defaults
            )
            defer { next.runtimePageCache.removeAll() }
            await fulfillment(
                of: [started.expectation],
                timeout: WebKitReadiness.launchedTransportOperationSeconds
            )
            guard case .browser(let opened) = next.route, let attempt = next.runtimeOpenAttempt else {
                release.signal()
                return XCTFail("The instance did not open first: \(row.name)")
            }
            if row.shown == .live {
                next.handleRuntimeNavigation(
                    RuntimeNavigationEvent(stage: .started, destination: .instance),
                    attemptID: attempt.id,
                    runtime: opened
                )
            }
            let cached = { (stage: RuntimeNavigationStage) in
                next.handleRuntimeNavigation(
                    RuntimeNavigationEvent(stage: stage, destination: .instance, fromCache: true),
                    attemptID: attempt.id,
                    runtime: opened
                )
            }
            if row.shown == .covered || row.shown == .uncovered {
                for stage in [RuntimeNavigationStage.started, .committed, .finished] {
                    cached(stage)
                }
            }
            if row.shown == .uncovered {
                next.runtimeCachedPageKeepsShell(attemptID: attempt.id, runtime: opened)
                XCTAssertEqual(next.runtimeOpenAttempt?.phase, .showingCachedLive, row.name)
            }

            release.signal()
            try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
                next.mobileSessionCoordinator.currentInstances
                    .first { $0.id == "instance-1" }?.available == false
            }
            // What the page says once it has loaded (the probe).
            switch row.then {
            case .declines:
                // Covered, asked once without an answer: a live load.
                XCTAssertEqual(next.route, .browser(opened), row.name)
                next.handleRuntimeNavigation(
                    RuntimeNavigationEvent(stage: .started, destination: .instance),
                    attemptID: attempt.id,
                    runtime: opened
                )
            case .keepsShell:
                XCTAssertEqual(next.route, .browser(opened), row.name)
                XCTAssertEqual(next.runtimeOpenAttempt?.phase, .showingCached, row.name)
                next.runtimeCachedPageKeepsShell(attemptID: attempt.id, runtime: opened)
            case .nothing:
                break
            }
            guard row.stays else {
                // At once, and because the rule ended it (no time-out).
                XCTAssertEqual(next.route, .onboarding(.instances), row.name)
                XCTAssertEqual(next.lastRuntimeOpenTermination?.reason, .cancelled, row.name)
                continue
            }
            XCTAssertEqual(next.route, .browser(opened), row.name)
            XCTAssertEqual(next.runtimeOpenAttempt?.phase, .cachedOffline, row.name)
            // No time-out ends it.
            timing.advance(by: .seconds(120))
            XCTAssertEqual(next.route, .browser(opened), row.name)
            XCTAssertEqual(next.runtimeOpenAttempt?.phase, .cachedOffline, row.name)

            if row.shown == .uncovered {
                // Recovery: the instance answers the page; the open is done,
                // the row goes, and later navigations are ordinary.
                next.runtimeCachedPageConnected(attemptID: attempt.id, runtime: opened)
                XCTAssertEqual(next.runtimeOpenAttempt?.phase, .documentLoaded, row.name)
                XCTAssertEqual(
                    BrowserOverlay(attemptPhase: .documentLoaded),
                    .none
                )
                next.handleRuntimeNavigation(
                    RuntimeNavigationEvent(stage: .started, destination: .instance),
                    attemptID: attempt.id,
                    runtime: opened
                )
                XCTAssertEqual(next.route, .browser(opened), row.name)
                continue
            }

            // Switch instance: the instances over the page.
            next.showInstances()
            XCTAssertEqual(next.route, .accountOverBrowser(opened), row.name)
            XCTAssertTrue(next.returnToRuntime(), row.name)

            // Retry, still listed offline: a new open, waiting on its
            // cached page again.
            session.replace(with: listing(false))
            XCTAssertTrue(next.retryOfflineCachedPage(), row.name)
            let retried = try XCTUnwrap(next.runtimeOpenAttempt)
            XCTAssertGreaterThan(retried.id, attempt.id, row.name)
            try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
                next.runtimeOpenAttempt?.listedOffline == true
            }
            for stage in [RuntimeNavigationStage.started, .committed, .finished] {
                next.handleRuntimeNavigation(
                    RuntimeNavigationEvent(stage: stage, destination: .instance, fromCache: true),
                    attemptID: retried.id,
                    runtime: opened
                )
            }
            next.runtimeCachedPageKeepsShell(attemptID: retried.id, runtime: opened)
            XCTAssertEqual(next.runtimeOpenAttempt?.phase, .cachedOffline, row.name)

            // Retry, listed online again: the open goes on as any open of a
            // cached page, bounded.
            session.replace(with: listing(true))
            XCTAssertTrue(next.retryOfflineCachedPage(), row.name)
            let online = try XCTUnwrap(next.runtimeOpenAttempt)
            try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
                next.mobileSessionCoordinator.currentInstances
                    .first { $0.id == "instance-1" }?.available == true
            }
            XCTAssertEqual(next.runtimeOpenAttempt?.listedOffline, false, row.name)
            for stage in [RuntimeNavigationStage.started, .committed, .finished] {
                next.handleRuntimeNavigation(
                    RuntimeNavigationEvent(stage: stage, destination: .instance, fromCache: true),
                    attemptID: online.id,
                    runtime: opened
                )
            }
            next.runtimeCachedPageKeepsShell(attemptID: online.id, runtime: opened)
            XCTAssertEqual(next.runtimeOpenAttempt?.phase, .showingCachedLive, row.name)
            timing.advance(by: .seconds(60))
            XCTAssertEqual(next.runtimeOpenAttempt?.phase, .cachedUnreachable, row.name)
        }
    }

    /// A launch whose last session lists the selected instance offline opens
    /// it first only for a cached page that keeps its shell offline, and the
    /// open starts listed offline; without one it does not open first.
    @MainActor
    func testALaunchOpensAnInstanceListedOfflineOnlyForItsCachedPage() async throws {
        let offline = LocalHTTPResponse.json("""
            {"account":{"id":"account-1","email":"max@example.com"},"instances":[
            {"id":"instance-1","name":"One","host":"instance-one.example",
             "url":"https://instance-one.example","relationship":"owner",
             "owner_email":"max@example.com","available":false}]}
            """)
        for marked in [true, false] {
            let first = try await makeApp(directTransport: nil)
            let selected = try await select("instance-1", in: first)
            let defaults = try XCTUnwrap(appDefaults)
            defer { first.runtimePageCache.removeAll() }
            try XCTUnwrap(first.runtimePageCacheAccess(for: selected)).store(
                RuntimeCachedPage(
                    url: selected.instance.url,
                    headers: ["Content-Type": "text/html"],
                    body: Data("<!doctype html>".utf8),
                    capturedAt: Date(),
                    keepsShellUnreachable: marked ? true : nil
                )
            )
            // A launch whose session read lists it offline: the last good
            // session now says so.
            let second = try await makeApp(
                directTransport: nil,
                sessionResponse: { offline },
                reusingDefaults: defaults
            )
            try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
                second.mobileSessionCoordinator.currentInstances
                    .first { $0.id == "instance-1" }?.available == false
            }

            // The next launch, before avibe.bot answers.
            let session = SessionAnswerOverride()
            let started = FixtureEvent(description: "session read, marked: \(marked)")
            let release = DispatchSemaphore(value: 0)
            session.hold(offline, started: started, release: release)
            let third = try await makeApp(
                directTransport: nil,
                sessionResponse: { session.response },
                reusingDefaults: defaults
            )
            await fulfillment(
                of: [started.expectation],
                timeout: WebKitReadiness.launchedTransportOperationSeconds
            )
            if marked {
                guard case .browser(let opened) = third.route else {
                    release.signal()
                    return XCTFail("A launch did not open an offline instance with its cached page")
                }
                XCTAssertFalse(opened.instance.available)
                // avibe.bot confirms it offline: the open waits for its page.
                release.signal()
                try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
                    third.runtimeOpenAttempt?.listedOffline == true
                }
                XCTAssertEqual(third.route, .browser(opened))
            } else {
                XCTAssertFalse(third.route.runtime != nil, "opened without a cached page")
                release.signal()
                try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
                    third.route == .onboarding(.instances)
                }
            }

            // A launch with no last good session checks first: the confirmed
            // listing opens it only for its page, and the open waits for it.
            defaults.removeObject(forKey: "avibe.mobile.lastGoodSession")
            let checkFirst = try await makeApp(
                directTransport: nil,
                sessionResponse: { offline },
                reusingDefaults: defaults
            )
            if marked {
                try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
                    checkFirst.runtimeOpenAttempt?.listedOffline == true
                }
                guard case .browser(let opened) = checkFirst.route else {
                    return XCTFail("A checked launch did not open an offline instance with its cached page")
                }
                XCTAssertFalse(opened.instance.available)
            } else {
                try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
                    checkFirst.route == .onboarding(.instances)
                }
                XCTAssertNil(checkFirst.runtimeOpenAttempt)
            }
        }
    }

    /// An account's pages are removed when it signs out.
    @MainActor
    func testAnAccountsCachedPagesGoWithASignOut() async throws {
        let app = try await makeApp(directTransport: nil)
        let runtime = try await select("instance-1", in: app)
        let page = RuntimeCachedPage(
            url: runtime.instance.url,
            headers: ["Content-Type": "text/html"],
            body: Data("<!doctype html>".utf8),
            capturedAt: Date()
        )
        try XCTUnwrap(app.runtimePageCacheAccess(for: runtime)).store(page)
        let origin = try XCTUnwrap(WebOrigin(url: runtime.instance.url)).serialized
        // The pages of the store the account used, whatever store comes next.
        let accountsPages = RuntimePageCache(directory: RuntimePageCache.root
            .appendingPathComponent(runtime.websiteDataStoreID.uuidString, isDirectory: true))
        XCTAssertEqual(accountsPages.page(accountID: "account-1", origin: origin), page)

        _ = try await app.mobileSessionCoordinator.logout()

        XCTAssertNil(accountsPages.page(accountID: "account-1", origin: origin))
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
        var fired: [String] = []
        let held = timing.schedule(after: .milliseconds(80)) {
            fired.append("scheduled before the hold")
        }
        // The main actor is held past that timer's deadline.
        let holdUntil = ContinuousClock.now.advanced(by: .milliseconds(120))
        while ContinuousClock.now < holdUntil {
            _ = 1 + 1
        }
        // A second timer, scheduled only now and for less than the first
        // one's whole delay. Counted from its schedule call, the first
        // timer's deadline is already past, so it fires first. Counted from
        // when its task first ran, which is after the hold, it would be due
        // 80 ms from here and fire after this one. The order holds however
        // long a loaded simulator takes over either: no margin is waited.
        let reference = timing.schedule(after: .milliseconds(60)) {
            fired.append("scheduled after the hold")
        }
        try await eventually { fired.count == 2 }
        XCTAssertEqual(fired, ["scheduled before the hold", "scheduled after the hold"])
        withExtendedLifetime(held) {}
        withExtendedLifetime(reference) {}
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
        // Before the long-wait mark an ordinary open offers no Cancel yet.
        XCTAssertFalse(try XCTUnwrap(app.runtimeOpenAttempt).offersCancel)
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
            BrowserOverlay(attemptPhase: handedOff.phase),
            .loading
        )
        // No timeout will end this loading layer: it offers Cancel at once,
        // before the long-wait mark.
        XCTAssertFalse(handedOff.isTakingLong)
        XCTAssertTrue(handedOff.offersCancel)

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
        XCTAssertTrue(try XCTUnwrap(app.runtimeOpenAttempt).offersCancel)

        timing.advance(by: .seconds(120))
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertNil(app.runtimeOpenNotice)
        XCTAssertTrue(try XCTUnwrap(app.runtimeOpenAttempt).offersCancel)

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
            BrowserOverlay(attemptPhase: app.runtimeOpenAttempt?.phase),
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
            BrowserOverlay(attemptPhase: handedOff.phase),
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
            BrowserOverlay(attemptPhase: app.runtimeOpenAttempt?.phase),
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
    /// The fault (something else may hold the listener's port) keeps it the
    /// same way, for as long as that lasts: the fault ends by itself, and
    /// the page must load then. The controller's invalidation drops it.
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

        // The fault keeps a held load, and loads nothing while it lasts,
        // however often it is asked. It is made once when the gate opens.
        gate = .held
        controller.webViewWebContentProcessDidTerminate(primary)
        XCTAssertTrue(controller.isHoldingAutomaticLoad)
        gate = .fault
        controller.directAvailabilityDidChange()
        controller.directAvailabilityDidChange()
        try await settle()
        XCTAssertEqual(starts, 3)
        XCTAssertTrue(controller.isHoldingAutomaticLoad)
        gate = .open
        controller.directAvailabilityDidChange()
        try await eventually { starts == 4 }
        XCTAssertFalse(controller.isHoldingAutomaticLoad)
        controller.directAvailabilityDidChange()
        try await settle()
        XCTAssertEqual(starts, 4)

        // A process that ends under the fault itself is held as well.
        gate = .fault
        controller.webViewWebContentProcessDidTerminate(primary)
        try await settle()
        XCTAssertEqual(starts, 4)
        XCTAssertTrue(controller.isHoldingAutomaticLoad)
        gate = .open
        controller.directAvailabilityDidChange()
        try await eventually { starts == 5 }

        // The controller's invalidation drops a held load for good.
        gate = .held
        controller.webViewWebContentProcessDidTerminate(primary)
        _ = controller.invalidateForAccountTransition()
        gate = .open
        controller.directAvailabilityDidChange()
        try await settle()
        XCTAssertEqual(starts, 5)
        XCTAssertFalse(controller.isHoldingAutomaticLoad)
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
        trackOwner(controller)
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
        guard !theSimulatorRefusedABind(of: app) else {
            return
        }
        try await eventually { starts == 1 }

        // Consumed: another background and return loads nothing more.
        enterBackground()
        enterForeground()
        try await eventually { app.directLoadGate(for: runtime) != .held }
        try await settle()
        guard !theSimulatorRefusedABind(of: app) else {
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
        if !theSimulatorRefusedABind(of: app) {
            XCTAssertEqual(provider(), .open)
        }
    }

    func testTheOverlayFollowsTheOpenAlone() {
        XCTAssertEqual(BrowserOverlay(isLoading: true, failed: true), .loadFailure)
        XCTAssertEqual(BrowserOverlay(isLoading: true, failed: false), .loading)
        XCTAssertEqual(BrowserOverlay(attemptPhase: .awaitingUser), .awaitingUser)
        XCTAssertEqual(BrowserOverlay(attemptPhase: .documentLoaded), .none)
        XCTAssertEqual(BrowserOverlay(isLoading: false, failed: false), .none)
    }

    /// The recovery's message is the approved "can't reach" copy with
    /// Retry, over a page or alone, and the "reopen the app" text is gone
    /// from both locales.
    func testTheRecoveryMessageIsTheApprovedCopyAndNothingAsksToReopenTheApp() throws {
        func localized(_ key: String, _ language: String) throws -> String {
            let path = try XCTUnwrap(Bundle.main.path(forResource: language, ofType: "lproj"))
            let bundle = try XCTUnwrap(Bundle(path: path))
            return bundle.localizedString(forKey: key, value: "missing", table: nil)
        }
        for language in ["en", "zh-Hans"] {
            XCTAssertEqual(try localized("browser.direct_fault.title", language), "missing", language)
        }
        // Over the page that stays.
        XCTAssertEqual(try localized("runtime.open.cached_timeout.title.named", "en"), "Can't reach %@ right now")
        XCTAssertEqual(try localized("runtime.open.cached_timeout.title.named", "zh-Hans"), "暂时连不上 %@")
        XCTAssertEqual(try localized("runtime.open.cached_timeout.body", "en"), "Showing what was here last")
        XCTAssertEqual(try localized("runtime.open.cached_timeout.body", "zh-Hans"), "显示的是上次的内容")
        XCTAssertEqual(try localized("runtime.open.cached_timeout.retry", "en"), "Retry")
        XCTAssertEqual(try localized("runtime.open.cached_timeout.retry", "zh-Hans"), "重试")
        // With no page left to keep.
        XCTAssertEqual(try localized("browser.error.title.named", "en"), "Can't reach %@")
        XCTAssertEqual(try localized("browser.error.title.named", "zh-Hans"), "暂时连不上 %@")
        XCTAssertEqual(try localized("browser.error.retry", "en"), "Try Again")
        XCTAssertEqual(try localized("browser.error.retry", "zh-Hans"), "重试")
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
        // Selected with the open, unless the return's first bind failed:
        // the selection then waits for the attempt that serves.
        if !theSimulatorRefusedABind(of: app) {
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
            // What follows needs the host routed at its first load. A
            // listener fault leaves it on the public path with no selection
            // at all, and a first bind slower than the App's listener wait
            // routes it only afterwards; say which rather than fail below
            // on what never happened.
            XCTAssertTrue(
                hostWasRoutedAtItsFirstLoad(by: app),
                whyNoHostIsRouted(by: app),
                file: file,
                line: line
            )
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
        // A launch checks the session first and restores only then, unless
        // it opens first with the last confirmed session (a relaunch), in
        // which case this returns at once and the check runs behind the open.
        try await waitForTheBootstrap(of: app)
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
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

    /// A cold start with the last good account and its selected instance on
    /// this device opens that instance at once, while avibe.bot's answer is
    /// still on its way: the account check runs behind the open. Its answer
    /// then decides as a refresh does. The same account keeps the instance
    /// open, and so does a failed check (offline opens what it can). A signed-out
    /// session or another account closes it and leaves the account. An owed
    /// second factor closes it but keeps the selection for after the factor.
    @MainActor
    func testAColdStartOpensTheLastInstanceBeforeTheAccountIsChecked() async throws {
        let otherAccount = LocalHTTPResponse.json("""
            {"account":{"id":"account-2","email":"other@example.com"},"instances":[
            {"id":"instance-1","name":"One","host":"instance-one.example",
             "url":"https://instance-one.example","relationship":"owner",
             "owner_email":"other@example.com","available":true}]}
            """)
        let secondFactor = LocalHTTPResponse(
            status: 403,
            headers: [("Content-Type", "application/json")],
            body: Data("""
                {"error":"two_factor_required","email":"max@example.com",
                 "gate":"challenge","methods":["totp"]}
                """.utf8)
        )
        let rows: [(name: String, answer: LocalHTTPResponse?, staysOpen: Bool, account: String?, keepsSelection: Bool)] = [
            ("same account", nil, true, "account-1", true),
            ("network failure", .status(500), true, "account-1", true),
            ("signed out", .status(401), false, nil, false),
            ("other account", otherAccount, false, nil, false),
            ("second factor", secondFactor, false, nil, true),
        ]
        for row in rows {
            let first = try await makeApp(directTransport: nil)
            _ = try await select("instance-1", in: first)
            let defaults = try XCTUnwrap(appDefaults)

            let session = SessionAnswerOverride()
            let started = FixtureEvent(description: "session read: \(row.name)")
            let release = DispatchSemaphore(value: 0)
            session.hold(
                row.answer ?? Self.signedInSessionAnswer,
                started: started,
                release: release
            )
            let next = try await makeApp(
                directTransport: nil,
                sessionResponse: { session.response },
                reusingDefaults: defaults
            )
            await fulfillment(
                of: [started.expectation],
                timeout: WebKitReadiness.launchedTransportOperationSeconds
            )
            // avibe.bot has not answered, and the instance is already open.
            guard case .browser(let opened) = next.route else {
                release.signal()
                return XCTFail("Before the account check answered, the App showed \(next.route): \(row.name)")
            }
            XCTAssertEqual(opened.instance.id, "instance-1", row.name)
            XCTAssertNotNil(next.runtimeOpenAttempt, row.name)

            release.signal()
            if row.staysOpen {
                try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
                    next.mobileSessionCoordinator.isCurrentRuntime(opened)
                }
                XCTAssertEqual(next.route, .browser(opened), row.name)
            } else {
                try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
                    next.route == .onboarding(.instances)
                }
                XCTAssertFalse(next.mobileSessionCoordinator.isSelectedRuntime(opened), row.name)
            }
            XCTAssertEqual(next.mobileSessionCoordinator.currentAccount?.id, row.account, row.name)
            XCTAssertEqual(
                defaults.data(forKey: Self.selectionKey) != nil,
                row.keepsSelection,
                row.name
            )
        }
    }

    /// A session avibe.bot last confirmed more than its lifetime ago opens
    /// nothing on a cold start: the App asks avibe.bot first, with no runtime
    /// and no account restored meanwhile, and only then opens the instance
    /// if the account still lists it. With no answer, or the instance gone
    /// from the list, it stops at the instance list. (Within the lifetime
    /// the instance opens before the answer:
    /// `testAColdStartOpensTheLastInstanceBeforeTheAccountIsChecked`.)
    @MainActor
    func testAColdStartPastTheConfirmationsLifetimeAsksAvibeBotBeforeOpening() async throws {
        let withoutInstanceOne = LocalHTTPResponse.json("""
            {"account":{"id":"account-1","email":"max@example.com"},"instances":[
            {"id":"instance-2","name":"Two","host":"instance-two.example",
             "url":"https://instance-two.example","relationship":"owner",
             "owner_email":"max@example.com","available":true}]}
            """)
        let rows: [(name: String, answer: LocalHTTPResponse, opens: Bool)] = [
            ("still listed", Self.signedInSessionAnswer, true),
            ("no longer listed", withoutInstanceOne, false),
            ("no answer", .status(500), false),
        ]
        for row in rows {
            let first = try await makeApp(directTransport: nil)
            _ = try await select("instance-1", in: first)
            let defaults = try XCTUnwrap(appDefaults)
            confirmSession(in: defaults, daysAgo: 15)

            let session = SessionAnswerOverride()
            let started = FixtureEvent(description: "session read: \(row.name)")
            let release = DispatchSemaphore(value: 0)
            session.hold(row.answer, started: started, release: release)
            let next = try await makeApp(
                directTransport: nil,
                sessionResponse: { session.response },
                reusingDefaults: defaults
            )
            await fulfillment(
                of: [started.expectation],
                timeout: WebKitReadiness.launchedTransportOperationSeconds
            )
            // avibe.bot is being asked, and nothing of the instance exists.
            XCTAssertEqual(next.route, .loading, row.name)
            XCTAssertNil(next.runtimeOpenAttempt, row.name)
            XCTAssertNil(next.mobileSessionCoordinator.currentAccount, row.name)

            release.signal()
            if row.opens {
                _ = try await waitForBrowser("instance-1", in: next)
                XCTAssertTrue(next.mobileSessionCoordinator.sessionConfirmationIsCurrent, row.name)
            } else {
                try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
                    next.route == .onboarding(.instances)
                }
                XCTAssertNil(next.runtimeOpenAttempt, row.name)
            }
        }
    }

    /// An App that stays alive past the confirmation's lifetime: on its
    /// return to the foreground the page is closed and avibe.bot asked
    /// before the instance opens again; an open made past the lifetime
    /// (another instance chosen) waits for the answer the same way; with no
    /// answer the App stops at the instance list. Within the lifetime a
    /// return changes nothing.
    @MainActor
    func testAnAppAlivePastTheConfirmationsLifetimeClosesItsPageAndAsksFirst() async throws {
        let center = NotificationCenter()
        let session = SessionAnswerOverride()
        let app = try await makeApp(
            directTransport: nil,
            sessionResponse: { session.response },
            runtimeOpenNotificationCenter: center
        )
        let defaults = try XCTUnwrap(appDefaults)
        let returnToTheForeground = {
            center.post(name: UIApplication.willEnterForegroundNotification, object: nil)
        }
        let runtime = try await select("instance-1", in: app)
        let opened = try XCTUnwrap(app.runtimeOpenAttempt).id

        // Confirmed within a week: a return reads nothing.
        let confirmedAtKey = "avibe.mobile.lastGoodSessionConfirmedAt"
        let confirmedAtSignIn = defaults.double(forKey: confirmedAtKey)
        returnToTheForeground()
        try await Task.sleep(for: AppModel.sessionRenewalDelay + .milliseconds(1_500))
        XCTAssertEqual(defaults.double(forKey: confirmedAtKey), confirmedAtSignIn)
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertEqual(app.runtimeOpenAttempt?.id, opened)

        // More than a week old, still within the lifetime: the session is
        // read quietly behind the open page, and the record renewed.
        confirmSession(in: defaults, daysAgo: 8)
        let quiet = FixtureEvent(description: "the quiet session read")
        let releaseQuiet = DispatchSemaphore(value: 0)
        session.hold(Self.signedInSessionAnswer, started: quiet, release: releaseQuiet)
        returnToTheForeground()
        XCTAssertEqual(app.route, .browser(runtime))
        await fulfillment(
            of: [quiet.expectation],
            timeout: WebKitReadiness.launchedTransportOperationSeconds
        )
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertEqual(app.runtimeOpenAttempt?.id, opened)
        releaseQuiet.signal()
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
            Date().timeIntervalSince1970 - defaults.double(forKey: confirmedAtKey) < 60
        }
        XCTAssertEqual(app.route, .browser(runtime))
        XCTAssertEqual(app.runtimeOpenAttempt?.id, opened)

        // Past the lifetime: closed at the return, opened after the answer.
        confirmSession(in: defaults, daysAgo: 15)
        let started = FixtureEvent(description: "session read after the return")
        let release = DispatchSemaphore(value: 0)
        session.hold(Self.signedInSessionAnswer, started: started, release: release)
        returnToTheForeground()
        XCTAssertEqual(app.route, .loading)
        XCTAssertNil(app.runtimeOpenAttempt)
        await fulfillment(
            of: [started.expectation],
            timeout: WebKitReadiness.launchedTransportOperationSeconds
        )
        XCTAssertEqual(app.route, .loading)
        XCTAssertNil(app.runtimeOpenAttempt)
        release.signal()
        _ = try await waitForBrowser("instance-1", in: app)
        XCTAssertGreaterThan(try XCTUnwrap(app.runtimeOpenAttempt).id, opened)
        XCTAssertTrue(app.mobileSessionCoordinator.sessionConfirmationIsCurrent)

        // An open made past the lifetime waits for the answer too.
        confirmSession(in: defaults, daysAgo: 15)
        let startedAgain = FixtureEvent(description: "session read before another instance opens")
        let releaseAgain = DispatchSemaphore(value: 0)
        session.hold(Self.signedInSessionAnswer, started: startedAgain, release: releaseAgain)
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-2")
        XCTAssertEqual(app.route, .loading)
        XCTAssertNil(app.runtimeOpenAttempt)
        await fulfillment(
            of: [startedAgain.expectation],
            timeout: WebKitReadiness.launchedTransportOperationSeconds
        )
        XCTAssertEqual(app.route, .loading)
        releaseAgain.signal()
        _ = try await waitForBrowser("instance-2", in: app)

        // A shortcut (or a notification tap) that arrives while avibe.bot
        // is being asked waits for the answer, then opens what it names:
        // its page is told the path.
        confirmSession(in: defaults, daysAgo: 15)
        let startedForAPath = FixtureEvent(description: "session read with a shortcut waiting")
        let releaseForAPath = DispatchSemaphore(value: 0)
        session.hold(Self.signedInSessionAnswer, started: startedForAPath, release: releaseForAPath)
        returnToTheForeground()
        await fulfillment(
            of: [startedForAPath.expectation],
            timeout: WebKitReadiness.launchedTransportOperationSeconds
        )
        app.openSelectedInstance(path: "/chat/waited")
        XCTAssertEqual(app.route, .loading)
        XCTAssertNil(app.runtimeOpenAttempt)
        releaseForAPath.signal()
        let reopened = try await waitForBrowser("instance-2", in: app)
        let controller = try makeRuntimeController(app, reopened)
        app.registerRuntimeController(controller, for: reopened)
        let page = try RuntimePage(controller: controller)
        try await page.load(
            """
            <!doctype html><script>
            window.changes = [];
            window.addEventListener("avibe:native:push.opened", (event) => {
              if (!window.changes.includes(event.detail.url)) {
                window.changes.push(event.detail.url);
              }
            });
            </script>
            """,
            baseURL: reopened.instance.url
        )
        app.didFinishRuntimeLoading()
        try await eventually { try await page.changes() == #"["/chat/waited"]"# }
        // Closed before the App closes the page itself below: on iOS 27 a
        // page closed after its controller was invalidated keeps the store
        // in use past the teardown.
        page.close()
        withExtendedLifetime(controller) {}

        // No answer: the instance list, and what waited goes with it.
        confirmSession(in: defaults, daysAgo: 15)
        session.replace(with: .status(500))
        returnToTheForeground()
        XCTAssertEqual(app.route, .loading)
        app.openSelectedInstance(path: "/chat/dropped")
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
            app.route == .onboarding(.instances)
        }
        XCTAssertNil(app.runtimeOpenAttempt)
        XCTAssertFalse(app.mobileSessionCoordinator.sessionConfirmationIsCurrent)

        // An answer again ends the test settled: no session read is left
        // on its last transport when the teardown removes the store.
        session.replace(with: Self.signedInSessionAnswer)
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-2")
        _ = try await waitForBrowser("instance-2", in: app)
        XCTAssertTrue(app.mobileSessionCoordinator.sessionConfirmationIsCurrent)
    }

    /// With a direct transport an open waits for its route before its
    /// WebView exists. The confirmation's lifetime ending in that wait holds
    /// the open too: no browser before avibe.bot has answered.
    @MainActor
    func testAnOpenWhoseRouteWasPreparedAcrossTheLifetimesEndAsksFirst() async throws {
        let session = SessionAnswerOverride()
        let app = try await makeApp(
            directTransport: StandInDirectTransport(),
            sessionResponse: { session.response }
        )
        let defaults = try XCTUnwrap(appDefaults)
        var browsers: [String] = []
        let opened = app.$route.sink { route in
            if case .browser(let runtime) = route {
                browsers.append(runtime.instance.id)
            }
        }
        defer { opened.cancel() }
        let started = FixtureEvent(description: "session read before the prepared open")
        let release = DispatchSemaphore(value: 0)
        session.hold(Self.signedInSessionAnswer, started: started, release: release)

        // The open starts on a current confirmation and waits for its
        // route; the lifetime ends before that wait does.
        _ = try app.mobileSessionCoordinator.selectInstance(id: "instance-1")
        XCTAssertEqual(app.route, .loading)
        XCTAssertNotNil(app.runtimeOpenAttempt)
        confirmSession(in: defaults, daysAgo: 15)

        await fulfillment(
            of: [started.expectation],
            timeout: WebKitReadiness.launchedTransportOperationSeconds
        )
        XCTAssertEqual(browsers, [])
        XCTAssertEqual(app.route, .loading)
        release.signal()
        _ = try await waitForBrowser("instance-1", in: app)
        XCTAssertEqual(browsers, ["instance-1"])
    }

    /// Dates avibe.bot's last confirmation of the stored session.
    private func confirmSession(in defaults: UserDefaults, daysAgo days: Double) {
        defaults.set(
            Date().addingTimeInterval(-days * 24 * 60 * 60).timeIntervalSince1970,
            forKey: "avibe.mobile.lastGoodSessionConfirmedAt"
        )
    }

    /// Opening an instance records its address for the account. When
    /// avibe.bot's answers have not listed it for a day, what the App keeps
    /// for that address goes: its stored site data (the removal itself:
    /// `NavigationPolicyTests.testOnlyTheDepartedAddressesStorageIsRemoved`),
    /// its cached page, and this device's path choice. The other instance
    /// keeps its own.
    @MainActor
    func testAnAddressTheAccountNoLongerListsLosesWhatTheAppKeptForIt() async throws {
        let first = try await makeApp(directTransport: nil)
        let one = try await select("instance-1", in: first)
        let two = try await select("instance-2", in: first)
        let defaults = try XCTUnwrap(appDefaults)
        let ledgerKey = DepartedInstanceData.key
        func recorded() throws -> [String: DepartedInstanceData.Entry] {
            let data = try XCTUnwrap(defaults.data(forKey: ledgerKey))
            return try XCTUnwrap(
                JSONDecoder().decode([String: [String: DepartedInstanceData.Entry]].self, from: data)["account-1"]
            )
        }
        let originOne = try XCTUnwrap(WebOrigin(url: one.instance.url)).serialized
        let originTwo = try XCTUnwrap(WebOrigin(url: two.instance.url)).serialized
        XCTAssertEqual(Set(try recorded().keys), [originOne, originTwo])
        XCTAssertEqual(try recorded()[originOne]?.store, one.websiteDataStoreID)

        // What the App keeps per address.
        let page = RuntimeCachedPage(
            url: one.instance.url,
            headers: ["Content-Type": "text/html"],
            body: Data("<!doctype html>".utf8),
            capturedAt: Date()
        )
        for origin in [originOne, originTwo] {
            first.runtimePageCache.store(page, accountID: "account-1", origin: origin)
        }
        defer { first.runtimePageCache.removeAll() }
        let choices = DirectPathPreferenceStore(defaults: defaults)
        choices.set(DirectPathPreference(mode: .tunnel), accountID: "account-1", origin: originOne)
        choices.set(DirectPathPreference(mode: .tunnel), accountID: "account-1", origin: originTwo)

        // avibe.bot has not listed instance-1 for more than a day, and its
        // next answer still does not. (Past the confirmation's lifetime, so
        // the launch asks first and opens nothing of instance-1.)
        var ledger = try JSONDecoder().decode(
            [String: [String: DepartedInstanceData.Entry]].self,
            from: try XCTUnwrap(defaults.data(forKey: ledgerKey))
        )
        ledger["account-1"]?[originOne]?.missingSince = Date().addingTimeInterval(-25 * 60 * 60)
        defaults.set(try JSONEncoder().encode(ledger), forKey: ledgerKey)
        confirmSession(in: defaults, daysAgo: 15)
        let withoutInstanceOne = LocalHTTPResponse.json("""
            {"account":{"id":"account-1","email":"max@example.com"},"instances":[
            {"id":"instance-2","name":"Two","host":"instance-two.example",
             "url":"https://instance-two.example","relationship":"owner",
             "owner_email":"max@example.com","available":true}]}
            """)
        let next = try await makeApp(
            directTransport: nil,
            sessionResponse: { withoutInstanceOne },
            reusingDefaults: defaults
        )
        // The removal is one attempt for each signed-in answer. One that
        // cannot finish in its bounds (its page has 10 s to load and each of
        // its steps 8 s; a web content launch on the CI simulator can take
        // longer) leaves everything in place and is made again at the next
        // answer, by design. The launch gave one answer. So while the
        // address is still recorded the App is asked again, through its own
        // session check, as a later launch or refresh asks: one check at a
        // time, 10 s after the last one ended. A newer answer waits for a
        // running attempt, so this is a few attempts, not one per check.
        let checks = SessionChecks()
        let clock = ContinuousClock()
        let began = clock.now
        do {
            try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
                let signedIn = next.mobileSessionCoordinator.currentAccount != nil
                if signedIn, (try? recorded()[originOne]) == nil {
                    return true
                }
                if signedIn {
                    checks.askAgainIfDue(next, at: clock.now)
                }
                return false
            }
        } catch {
            print("[iOS fixture] stage=departed address still recorded, after=\(began.duration(to: clock.now)), answers=\(1 + checks.asked)")
            await checks.settle()
            throw error
        }
        // A check still in flight ends before the fixture removes its store.
        await checks.settle()
        // How long the removal took and how many answers it needed: one
        // answer and a few seconds when the first attempt succeeded.
        print("[iOS fixture] stage=departed address removed, after=\(began.duration(to: clock.now)), answers=\(1 + checks.asked)")
        XCTAssertNil(next.runtimePageCache.page(accountID: "account-1", origin: originOne))
        XCTAssertEqual(choices.preference(accountID: "account-1", origin: originOne), .automatic)
        // The listed instance keeps its own.
        XCTAssertNotNil(try recorded()[originTwo])
        XCTAssertNotNil(next.runtimePageCache.page(accountID: "account-1", origin: originTwo))
        XCTAssertEqual(choices.preference(accountID: "account-1", origin: originTwo).mode, .tunnel)
    }

    /// The control plane's ordinary signed-in answer for account-1.
    private static var signedInSessionAnswer: LocalHTTPResponse {
        .json("""
            {"account":{"id":"account-1","email":"max@example.com"},"instances":[
            {"id":"instance-1","name":"One","host":"instance-one.example",
             "url":"https://instance-one.example","relationship":"owner",
             "owner_email":"max@example.com","available":true},
            {"id":"instance-2","name":"Two","host":"instance-two.example",
             "url":"https://instance-two.example","relationship":"owner",
             "owner_email":"max@example.com","available":true}]}
            """)
    }

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
                    registration: registration,
                    didMake: { [weak self] in self?.trackOwner($0) }
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
            registration: registration,
            didMake: { [weak self] in self?.trackOwner($0) }
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
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
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
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
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
        trackOwner(controller)
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
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
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
        // What follows needs the host routed at its first load. A listener
        // fault leaves it on the public path with no selection at all, and a
        // first bind slower than the App's listener wait routes it only
        // afterwards; say which rather than time out below.
        XCTAssertTrue(
            hostWasRoutedAtItsFirstLoad(by: app),
            whyNoHostIsRouted(by: app)
        )
        // The transport's selection follows the browser asynchronously: wait
        // for it rather than for a fixed time a loaded simulator may exceed.
        do {
            try await eventually {
                transport.selections.map(\.runtime.instance.id) == ["instance-2"]
            }
        } catch {
            // Which way it went: none, or the replaced open's too.
            XCTFail("The transport was selected for \(transport.selections.map(\.runtime.instance.id)).")
            throw error
        }
        // The replaced open must never surface later either. A "never" cannot
        // be awaited, so a short window gives a late browser or selection for
        // instance-1 the chance to appear before the final check.
        try await Task.sleep(for: .milliseconds(300))

        XCTAssertEqual(browsers, ["instance-2"])
        XCTAssertEqual(
            transport.selections.map(\.runtime.instance.id),
            ["instance-2"]
        )
    }

    // MARK: This device's path choice

    /// "Cloudflare tunnel only" for an instance: its open prepares the route
    /// (so a later Automatic takes effect on this store) but selects
    /// nothing, so the transport never dials and nothing can hand over; its
    /// pages are told the tunnel. Switched to Automatic in the same process,
    /// it dials.
    @MainActor
    func testTunnelOnlyRoutesButNeverSelectsDialsOrHandsOver() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let defaults = try XCTUnwrap(appDefaults)
        DirectPathPreferenceStore(defaults: defaults).set(
            DirectPathPreference(mode: .tunnel),
            accountID: "account-1",
            origin: instance.serialized
        )

        let runtime = try await select("instance-1", in: app)
        XCTAssertEqual(app.directPathPreference(for: runtime).mode, .tunnel)
        XCTAssertFalse(app.runtimeWebsiteDataStore.proxyConfigurations.isEmpty)
        // The selection would have been made in the turn of the open.
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertEqual(transport.selections.count, 0)
        let status = await withCheckedContinuation { continuation in
            app.directStatusProvider?(instance) { continuation.resume(returning: $0) }
        }
        XCTAssertEqual(status?.preferred, .tunnel)
        let handover = await withCheckedContinuation { continuation in
            app.directHandoverProvider?(instance, true) { continuation.resume(returning: $0) }
        }
        XCTAssertEqual(handover, .skipped(.notUsable))

        let page = try XCTUnwrap(URL(string: "https://instance-one.example/"))
        XCTAssertEqual(try XCTUnwrap(app.directPreferenceProvider).write(instance, .automatic, page), .automatic)
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
            transport.selections.map(\.runtime.instance.id) == ["instance-1"]
        }
    }

    /// The page sets the choice for its own instance: the answer comes
    /// first, then the instance opens again at the page's URL, once, on the
    /// new path; setting the same mode again reopens nothing. Unknown modes
    /// are refused by the bridge (`testTheDirectPreferenceSurface...`).
    @MainActor
    func testSettingTheChoiceReopensTheInstanceWhereThePageWas() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let runtime = try await select("instance-1", in: app)
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
            transport.selections.count == 1
        }
        let provider = try XCTUnwrap(app.directPreferenceProvider)
        XCTAssertEqual(provider.read(instance), .automatic)
        // Another origin's page is not the open instance's.
        let other = try XCTUnwrap(WebOrigin.parseConfigured("https://instance-two.example"))
        XCTAssertNil(provider.read(other))

        let page = try XCTUnwrap(URL(string: "https://instance-one.example/settings/devices/connection"))
        let identity = try XCTUnwrap(app.runtimeOpenAttempt).id
        let answer = provider.write(instance, .tunnel, page)
        XCTAssertEqual(answer, DirectPathPreference(mode: .tunnel))
        // Answered before anything was reopened.
        XCTAssertEqual(app.runtimeOpenAttempt?.id, identity)
        try await eventually { (app.runtimeOpenAttempt?.id ?? 0) > identity }
        let reopenedRuntime = try await waitForBrowser("instance-1", in: app)
        XCTAssertEqual(reopenedRuntime, runtime)
        XCTAssertEqual(transport.deselectCount, 1)
        XCTAssertEqual(app.takeRuntimeStartURL(for: runtime), page)
        XCTAssertNil(app.takeRuntimeStartURL(for: runtime))
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertEqual(transport.selections.count, 1)

        let reopened = try XCTUnwrap(app.runtimeOpenAttempt).id
        XCTAssertEqual(provider.write(instance, .tunnel, page), DirectPathPreference(mode: .tunnel))
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(app.runtimeOpenAttempt?.id, reopened)

        // Back to automatic: it dials again.
        XCTAssertEqual(provider.write(instance, .automatic, page), .automatic)
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
            transport.selections.count == 2
        }
    }

    /// "Cloudflare tunnel only" and the instance cannot be reached on it (its
    /// open runs out of time, or its document fails to load) while the
    /// device has a network and avibe.bot lists it online: the choice goes
    /// back to automatic, says so, and the instance opens again, automatic.
    /// Once: an automatic open that then fails ends as any open. Without a
    /// network nothing is changed.
    @MainActor
    func testTunnelOnlyGoesBackToAutomaticWhenTheInstanceCannotBeReached() async throws {
        let network = StandInNetworkPath()
        let timing = RuntimeOpenManualTiming()
        let transport = StandInDirectTransport()
        let app = try await makeApp(
            directTransport: transport,
            runtimeOpenTiming: timing,
            networkPath: network
        )
        let store = DirectPathPreferenceStore(defaults: try XCTUnwrap(appDefaults))
        let tunnelOnly = {
            store.set(DirectPathPreference(mode: .tunnel), accountID: "account-1", origin: self.instance.serialized)
        }

        // Out of time, without a network: unchanged, and to the list.
        tunnelOnly()
        network.isSatisfied = false
        _ = try await select("instance-1", in: app)
        timing.advance(by: .seconds(60))
        XCTAssertEqual(app.lastRuntimeOpenTermination?.reason, .timedOut)
        XCTAssertEqual(app.route, .onboarding(.instances))
        XCTAssertEqual(store.preference(accountID: "account-1", origin: instance.serialized).mode, .tunnel)

        // Out of time, with a network: automatic, reverted, opened again.
        network.isSatisfied = true
        _ = try await select("instance-1", in: app)
        let timedOut = try XCTUnwrap(app.runtimeOpenAttempt).id
        timing.advance(by: .seconds(60))
        let reverted = store.preference(accountID: "account-1", origin: instance.serialized)
        XCTAssertEqual(reverted.mode, .automatic)
        XCTAssertEqual(reverted.reverted?.reason, .unreachable)
        XCTAssertEqual(app.lastRuntimeOpenTermination?.reason, .replaced)
        XCTAssertGreaterThan(try XCTUnwrap(app.runtimeOpenAttempt).id, timedOut)
        _ = try await waitForBrowser("instance-1", in: app)
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
            transport.selections.map(\.runtime.instance.id) == ["instance-1"]
        }
        XCTAssertEqual(try XCTUnwrap(app.directPreferenceProvider).read(instance), reverted)

        // Once: the automatic open running out of time ends as any open.
        timing.advance(by: .seconds(60))
        XCTAssertEqual(app.lastRuntimeOpenTermination?.reason, .timedOut)
        XCTAssertEqual(app.route, .onboarding(.instances))
        XCTAssertEqual(store.preference(accountID: "account-1", origin: instance.serialized), reverted)

        // The document failing to load counts the same.
        tunnelOnly()
        let again = try await select("instance-1", in: app)
        let failed = try XCTUnwrap(app.runtimeOpenAttempt)
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .failed, destination: .instance),
            attemptID: failed.id,
            runtime: again
        )
        XCTAssertEqual(store.preference(accountID: "account-1", origin: instance.serialized).mode, .automatic)
        try await eventually { (app.runtimeOpenAttempt?.id ?? 0) > failed.id }
        _ = try await waitForBrowser("instance-1", in: app)

        // Not the tunnel's doing: a sign-in page failing, and a cached page
        // shown uncovered running out of time. Unchanged.
        let two = try XCTUnwrap(WebOrigin.parseConfigured("https://instance-two.example"))
        store.set(DirectPathPreference(mode: .tunnel), accountID: "account-1", origin: two.serialized)
        let second = try await select("instance-2", in: app)
        let signIn = try XCTUnwrap(app.runtimeOpenAttempt)
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .started, destination: .authentication),
            attemptID: signIn.id,
            runtime: second
        )
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .failed, destination: .authentication),
            attemptID: signIn.id,
            runtime: second
        )
        XCTAssertEqual(app.runtimeOpenAttempt?.id, signIn.id)
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .failed)
        XCTAssertEqual(store.preference(accountID: "account-1", origin: two.serialized).mode, .tunnel)

        _ = try await select("instance-1", in: app)
        let cachedSecond = try await select("instance-2", in: app)
        let cached = try XCTUnwrap(app.runtimeOpenAttempt)
        for stage in [RuntimeNavigationStage.started, .committed, .finished] {
            app.handleRuntimeNavigation(
                RuntimeNavigationEvent(stage: stage, destination: .instance, fromCache: true),
                attemptID: cached.id,
                runtime: cachedSecond
            )
        }
        app.runtimeCachedPageKeepsShell(attemptID: cached.id, runtime: cachedSecond)
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .showingCachedLive)
        timing.advance(by: .seconds(60))
        XCTAssertEqual(app.runtimeOpenAttempt?.id, cached.id)
        XCTAssertEqual(app.runtimeOpenAttempt?.phase, .cachedUnreachable)
        XCTAssertEqual(store.preference(accountID: "account-1", origin: two.serialized).mode, .tunnel)
    }

    /// 「仅直连和中继」 with the path controller that enforces it: the page may
    /// choose it, the answer says it takes effect now (`pendingRelaunch`
    /// false), the choice is kept, and the instance opens again where the
    /// page was. Back to automatic, the same.
    @MainActor
    func testChoosingDirectOnlyKeepsItAndOpensTheInstanceAgain() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let runtime = try await select("instance-1", in: app)
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
            transport.selections.count == 1
        }
        let provider = try XCTUnwrap(app.directPreferenceProvider)
        XCTAssertTrue(provider.acceptsDirectOnly)
        let store = DirectPathPreferenceStore(defaults: try XCTUnwrap(appDefaults))

        let page = try XCTUnwrap(URL(string: "https://instance-one.example/settings/devices/connection"))
        let identity = try XCTUnwrap(app.runtimeOpenAttempt).id
        let answer = try XCTUnwrap(provider.write(instance, .direct, page))
        XCTAssertEqual(answer.mode, .direct)
        XCTAssertFalse(answer.pendingRelaunch)
        XCTAssertEqual(store.preference(accountID: "account-1", origin: instance.serialized).mode, .direct)
        // Answered before anything was reopened; then reopened where it was.
        XCTAssertEqual(app.runtimeOpenAttempt?.id, identity)
        try await eventually { (app.runtimeOpenAttempt?.id ?? 0) > identity }
        let reopened = try await waitForBrowser("instance-1", in: app)
        XCTAssertEqual(reopened, runtime)
        XCTAssertEqual(app.takeRuntimeStartURL(for: runtime), page)
        XCTAssertEqual(provider.read(instance)?.mode, .direct)

        let direct = try XCTUnwrap(app.runtimeOpenAttempt).id
        XCTAssertEqual(provider.write(instance, .automatic, page)?.mode, .automatic)
        try await eventually { (app.runtimeOpenAttempt?.id ?? 0) > direct }
        XCTAssertEqual(store.preference(accountID: "account-1", origin: instance.serialized), .automatic)
    }

    /// Rule a1 through the App: the instance answers two rendezvous in one
    /// bootstrap that end without a link. One is not enough; the second
    /// sets the choice back to automatic, from `direct`, tells the page, and
    /// opens the instance again.
    @MainActor
    func testDirectOnlyGoesBackToAutomaticAfterTwoUnlinkedAnswers() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let store = DirectPathPreferenceStore(defaults: try XCTUnwrap(appDefaults))
        store.set(DirectPathPreference(mode: .direct), accountID: "account-1", origin: instance.serialized)

        _ = try await select("instance-1", in: app)
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
            !transport.selections.isEmpty
        }
        let target = try XCTUnwrap(transport.selections.last)
        let opened = try XCTUnwrap(app.runtimeOpenAttempt).id

        transport.report(.answeredWithoutLink, for: target)
        XCTAssertEqual(store.preference(accountID: "account-1", origin: instance.serialized).mode, .direct)
        XCTAssertEqual(app.runtimeOpenAttempt?.id, opened)

        transport.report(.answeredWithoutLink, for: target)
        let reverted = store.preference(accountID: "account-1", origin: instance.serialized)
        XCTAssertEqual(reverted.mode, .automatic)
        XCTAssertEqual(reverted.reverted?.reason, .unreachable)
        XCTAssertEqual(reverted.reverted?.from, .direct)
        try await eventually { (app.runtimeOpenAttempt?.id ?? 0) > opened }
        _ = try await waitForBrowser("instance-1", in: app)
        XCTAssertEqual(try XCTUnwrap(app.directPreferenceProvider).read(instance)?.reverted?.from, .direct)
    }

    /// A drop of the link under 「仅直连和中继」 (the controller counts a
    /// strict failure once its link stays lost) is only news: the choice
    /// stays, and the App re-establishes the link by opening the instance
    /// again once, through the route (stop, prepare, open), which selects
    /// the transport again. A second look with nothing new does not reopen.
    @MainActor
    func testADropUnderDirectOnlyReopensTheInstanceOnceAndKeepsTheChoice() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let store = DirectPathPreferenceStore(defaults: try XCTUnwrap(appDefaults))
        store.set(DirectPathPreference(mode: .direct), accountID: "account-1", origin: instance.serialized)

        _ = try await select("instance-1", in: app)
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
            !transport.selections.isEmpty
        }
        let target = try XCTUnwrap(transport.selections.last)
        transport.report(.usable(generation: "one"), for: target)
        // The controller arms the link after draining the host's
        // connections (none here), in a task of its own.
        try await Task.sleep(for: .seconds(1))
        let opened = try XCTUnwrap(app.runtimeOpenAttempt).id
        let selections = transport.selections.count

        transport.report(.lost, for: target)
        // The controller's own wait for the link to come back, then the drop.
        try await eventually(timeout: .seconds(30)) { (app.runtimeOpenAttempt?.id ?? 0) > opened }
        _ = try await waitForBrowser("instance-1", in: app)
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
            transport.selections.count > selections
        }
        XCTAssertEqual(store.preference(accountID: "account-1", origin: instance.serialized).mode, .direct)
        let reopened = try XCTUnwrap(app.runtimeOpenAttempt).id
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertEqual(app.runtimeOpenAttempt?.id, reopened)
    }

    /// An Action Button take made before the instance was opened in this
    /// process goes out with no route: 「仅直连和中继」 then waits for the
    /// next launch. The stored choice is kept, never set back: the page
    /// reads it as chosen and pending, and the revert rules do not run.
    @MainActor
    func testAVoiceTakeBeforeTheOpenLeavesDirectOnlyPendingNeverReverted() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let store = DirectPathPreferenceStore(defaults: try XCTUnwrap(appDefaults))
        store.set(DirectPathPreference(mode: .direct), accountID: "account-1", origin: instance.serialized)

        // The hidden view's load, on the App's current store.
        let note = try XCTUnwrap(VoiceCaptureInstanceLoads.note)
        note(instance, app.websiteDataStore.identifier)

        _ = try await select("instance-1", in: app)
        let read = try XCTUnwrap(try XCTUnwrap(app.directPreferenceProvider).read(instance))
        XCTAssertEqual(read.mode, .direct)
        XCTAssertTrue(read.pendingRelaunch)
        XCTAssertNil(read.reverted)
        XCTAssertEqual(
            store.preference(accountID: "account-1", origin: instance.serialized),
            DirectPathPreference(mode: .direct)
        )

        // What would set an enforced choice back changes nothing here.
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
            !transport.selections.isEmpty
        }
        let target = try XCTUnwrap(transport.selections.last)
        transport.report(.answeredWithoutLink, for: target)
        transport.report(.answeredWithoutLink, for: target)
        XCTAssertEqual(
            store.preference(accountID: "account-1", origin: instance.serialized),
            DirectPathPreference(mode: .direct)
        )
    }

    /// A sign-in page in front is never replaced. A revert made while it
    /// shows sets the choice back and tells the controller, without opening
    /// the instance again.
    @MainActor
    func testARevertOverASignInPageLeavesThePage() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let store = DirectPathPreferenceStore(defaults: try XCTUnwrap(appDefaults))
        store.set(DirectPathPreference(mode: .direct), accountID: "account-1", origin: instance.serialized)

        let runtime = try await select("instance-1", in: app)
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
            !transport.selections.isEmpty
        }
        let target = try XCTUnwrap(transport.selections.last)
        let opened = try XCTUnwrap(app.runtimeOpenAttempt).id
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .committed, destination: .authentication),
            attemptID: opened,
            runtime: runtime
        )

        transport.report(.answeredWithoutLink, for: target)
        transport.report(.answeredWithoutLink, for: target)
        let reverted = store.preference(accountID: "account-1", origin: instance.serialized)
        XCTAssertEqual(reverted.mode, .automatic)
        XCTAssertEqual(reverted.reverted?.from, .direct)
        // A reopen would be made in a later turn: none is.
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertEqual(app.runtimeOpenAttempt?.id, opened)
        XCTAssertEqual(app.route, .browser(runtime))
    }

    /// A drop while a sign-in page is in front: the instance is opened
    /// again only when the view is back on the instance.
    @MainActor
    func testADropOverASignInPageReopensOnlyWhenTheInstanceIsBack() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let store = DirectPathPreferenceStore(defaults: try XCTUnwrap(appDefaults))
        store.set(DirectPathPreference(mode: .direct), accountID: "account-1", origin: instance.serialized)

        let runtime = try await select("instance-1", in: app)
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
            !transport.selections.isEmpty
        }
        let target = try XCTUnwrap(transport.selections.last)
        transport.report(.usable(generation: "one"), for: target)
        // The controller arms the link in a task of its own.
        try await Task.sleep(for: .seconds(1))
        let opened = try XCTUnwrap(app.runtimeOpenAttempt).id
        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .committed, destination: .authentication),
            attemptID: opened,
            runtime: runtime
        )

        transport.report(.lost, for: target)
        // Past the controller's own 10 s for the link to come back: the
        // drop is counted, and nothing is opened over the sign-in page.
        try await Task.sleep(for: .seconds(13))
        XCTAssertEqual(app.runtimeOpenAttempt?.id, opened)
        XCTAssertEqual(store.preference(accountID: "account-1", origin: instance.serialized).mode, .direct)

        app.handleRuntimeNavigation(
            RuntimeNavigationEvent(stage: .committed, destination: .instance),
            attemptID: opened,
            runtime: runtime
        )
        try await eventually(timeout: .seconds(30)) { (app.runtimeOpenAttempt?.id ?? 0) > opened }
        XCTAssertEqual(store.preference(accountID: "account-1", origin: instance.serialized).mode, .direct)
    }

    /// Without 「仅直连和中继」 the same answers change nothing: the rules run
    /// only for that choice.
    @MainActor
    func testUnlinkedAnswersChangeNothingForAutomatic() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let store = DirectPathPreferenceStore(defaults: try XCTUnwrap(appDefaults))
        _ = try await select("instance-1", in: app)
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
            !transport.selections.isEmpty
        }
        let target = try XCTUnwrap(transport.selections.last)
        let opened = try XCTUnwrap(app.runtimeOpenAttempt).id
        for _ in 0..<3 {
            transport.report(.answeredWithoutLink, for: target)
        }
        XCTAssertEqual(store.preference(accountID: "account-1", origin: instance.serialized), .automatic)
        XCTAssertEqual(app.runtimeOpenAttempt?.id, opened)
    }

    /// The bridge surface: `directPreference` 1 only with a provider; the
    /// reading takes no parameters; the setting takes exactly a known mode
    /// (a later mode is refused, not guessed); both answer the agreed shape;
    /// the event reaches only a document on the instance's origin.
    @MainActor
    func testTheDirectPreferenceSurfaceTakesOnlyKnownModes() async throws {
        var written: [(WebOrigin, DirectPathPreference.Mode, URL)] = []
        let reverted = DirectPathPreference(
            mode: .automatic,
            reverted: .init(at: Date(timeIntervalSince1970: 1_700_000_000), reason: .unreachable)
        )
        let webView = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: instance,
                setOuterScrollLocked: { _, _ in false },
                directStatus: { _, reply in
                    reply(DirectPathStatus(preferred: .tunnel, sequence: 1, closure: 0))
                },
                directHandover: { _, _, reply in reply(.skipped(.notUsable)) },
                directPreference: DirectPreferenceProvider(
                    read: { _ in reverted },
                    write: { origin, mode, url in
                        written.append((origin, mode, url))
                        return DirectPathPreference(mode: mode)
                    }
                )
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
            const canon = (value) => JSON.stringify(value, (key, item) =>
              item && typeof item === "object" && !Array.isArray(item)
                ? Object.fromEntries(Object.entries(item).sort()) : item);
            const refusals = [];
            for (const params of [{mode: "relayOff"}, {mode: "direct"}, {}, {mode: "tunnel", extra: 1}, {mode: 1}]) {
              try {
                await window.avibeNative.call("direct.setPreference", params);
                refusals.push("accepted");
              } catch (error) {
                refusals.push(error.code);
              }
            }
            try {
              await window.avibeNative.call("direct.preference", {mode: "tunnel"});
              refusals.push("accepted");
            } catch (error) {
              refusals.push(error.code);
            }
            const read = await window.avibeNative.call("direct.preference");
            const set = await window.avibeNative.call("direct.setPreference", {mode: "tunnel"});
            return canon({
              capability: window.avibeNative.capabilities.directPreference,
              read, set, refusals
            });
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        XCTAssertEqual(
            result as? String,
            #"{"capability":1,"read":{"mode":"automatic","pendingRelaunch":false,"reverted":{"at":1700000000000,"from":"tunnel","reason":"unreachable"}},"refusals":["invalid_argument","invalid_argument","invalid_argument","invalid_argument","invalid_argument","invalid_argument"],"set":{"mode":"tunnel","pendingRelaunch":false,"reverted":null}}"#
        )
        XCTAssertEqual(written.count, 1)
        XCTAssertEqual(written.first?.0, instance)
        XCTAssertEqual(written.first?.1, .tunnel)
        XCTAssertEqual(written.first?.2, webView.url)

        // The event, on this origin only.
        let script = try XCTUnwrap(reverted.eventScript(for: instance))
        let heard = try await webView.callAsyncJavaScript(
            """
            const heard = new Promise((resolve) => {
              window.addEventListener("avibe:native:direct.preferenceChanged",
                (event) => resolve(JSON.stringify(event.detail, (key, item) =>
                  item && typeof item === "object" && !Array.isArray(item)
                    ? Object.fromEntries(Object.entries(item).sort()) : item)), {once: true});
            });
            eval(script);
            return await heard;
            """,
            arguments: ["script": script],
            in: nil,
            contentWorld: .page
        )
        XCTAssertEqual(
            heard as? String,
            #"{"mode":"automatic","pendingRelaunch":false,"reverted":{"at":1700000000000,"from":"tunnel","reason":"unreachable"}}"#
        )
        let elsewhere = try XCTUnwrap(
            reverted.eventScript(for: try XCTUnwrap(WebOrigin.parseConfigured("https://instance-two.example")))
        )
        XCTAssertTrue(elsewhere.contains("window.location.origin !== event.origin"))

        // Without a provider there is no capability and no answer.
        let bare = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: instance,
                setOuterScrollLocked: { _, _ in false },
                directStatus: { _, reply in reply(nil) }
            )
        )
        let bareLoader = PageLoader()
        bare.navigationDelegate = bareLoader
        try await bareLoader.loadHTML("<!doctype html>", baseURL: instance.url, in: bare)
        let bareResult = try await bare.callAsyncJavaScript(
            """
            let code = "answered";
            try { await window.avibeNative.call("direct.preference"); } catch (error) { code = error.code; }
            return JSON.stringify({capability: window.avibeNative.capabilities.directPreference ?? null, code});
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        XCTAssertEqual(bareResult as? String, #"{"capability":null,"code":"unsupported"}"#)
    }

    /// With a controller that enforces 「仅直连和中继」 and measures:
    /// `directPreference` 2 and `directMeasure` 1; `direct` is accepted
    /// and the answer carries `pendingRelaunch`; `direct.measure` takes no
    /// parameters, answers the measurement, refuses a second call as busy
    /// and an unfinished one as failed. Without them neither capability and
    /// `direct.measure` is unsupported.
    @MainActor
    func testTheDirectOnlyAndMeasureSurface() async throws {
        var replies: [DirectMeasureReply] = [
            .measured(DirectPathSeamsTests.measurement), .busy, .timedOut,
        ]
        var measuredFor: [WebOrigin] = []
        let webView = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: instance,
                setOuterScrollLocked: { _, _ in false },
                directStatus: { _, reply in
                    reply(DirectPathStatus(preferred: .tunnel, sequence: 1, closure: 0))
                },
                directPreference: DirectPreferenceProvider(
                    read: { _ in DirectPathPreference(mode: .direct, pendingRelaunch: true) },
                    write: { _, mode, _ in DirectPathPreference(mode: mode, pendingRelaunch: mode == .direct) },
                    acceptsDirectOnly: true,
                    measure: { origin in
                        measuredFor.append(origin)
                        return replies.removeFirst()
                    }
                )
            )
        )
        let loader = PageLoader()
        webView.navigationDelegate = loader
        try await loader.loadHTML("<!doctype html>", baseURL: instance.url, in: webView)
        let result = try await webView.callAsyncJavaScript(
            """
            const canon = (value) => JSON.stringify(value, (key, item) =>
              item && typeof item === "object" && !Array.isArray(item)
                ? Object.fromEntries(Object.entries(item).sort()) : item);
            const outcome = async (method, params) => {
              try { return await window.avibeNative.call(method, params); }
              catch (error) { return error.code; }
            };
            return canon({
              preference: window.avibeNative.capabilities.directPreference,
              measure: window.avibeNative.capabilities.directMeasure,
              read: await outcome("direct.preference"),
              set: await outcome("direct.setPreference", {mode: "direct"}),
              withParams: await outcome("direct.measure", {leg: "tunnel"}),
              measured: await outcome("direct.measure"),
              busy: await outcome("direct.measure"),
              timedOut: await outcome("direct.measure")
            });
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        XCTAssertEqual(
            result as? String,
            #"{"busy":"busy","measure":1,"measured":{"basis":"warm-https-health","current":{"error":null,"kind":"relay","maxMs":61,"medianMs":48,"ok":5,"relayRegion":"tokyo","scope":null},"measuredAt":1700000000000,"samples":5,"tunnel":{"error":"timeout","kind":"tunnel","maxMs":null,"medianMs":null,"ok":0,"relayRegion":null,"scope":null}},"preference":2,"read":{"mode":"direct","pendingRelaunch":true,"reverted":null},"set":{"mode":"direct","pendingRelaunch":true,"reverted":null},"timedOut":"failed","withParams":"invalid_argument"}"#
        )
        XCTAssertEqual(measuredFor, [instance, instance, instance])

        // A provider without them: version 1, no measurement.
        let plain = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: instance,
                setOuterScrollLocked: { _, _ in false },
                directStatus: { _, reply in reply(nil) },
                directPreference: DirectPreferenceProvider(
                    read: { _ in .automatic },
                    write: { _, _, _ in nil }
                )
            )
        )
        let plainLoader = PageLoader()
        plain.navigationDelegate = plainLoader
        try await plainLoader.loadHTML("<!doctype html>", baseURL: instance.url, in: plain)
        let plainResult = try await plain.callAsyncJavaScript(
            """
            let code = "answered";
            try { await window.avibeNative.call("direct.measure"); } catch (error) { code = error.code; }
            return JSON.stringify({
              preference: window.avibeNative.capabilities.directPreference ?? null,
              measure: window.avibeNative.capabilities.directMeasure ?? null,
              code
            });
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        XCTAssertEqual(plainResult as? String, #"{"preference":1,"measure":null,"code":"unsupported"}"#)
    }

    /// The stored choices: per account and instance; a mode this build
    /// does not know reads as automatic; automatic with nothing to say is
    /// not kept.
    @MainActor
    func testTheStoredChoiceIsPerAccountAndInstanceAndAnUnknownModeReadsAutomatic() throws {
        let suite = "DirectPathPreference.\(UUID().uuidString)"
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = DirectPathPreferenceStore(defaults: defaults)
        store.set(DirectPathPreference(mode: .tunnel), accountID: "a", origin: "https://one.example")
        XCTAssertEqual(store.preference(accountID: "a", origin: "https://one.example").mode, .tunnel)
        XCTAssertEqual(store.preference(accountID: "a", origin: "https://two.example"), .automatic)
        XCTAssertEqual(store.preference(accountID: "b", origin: "https://one.example"), .automatic)
        store.set(.automatic, accountID: "a", origin: "https://one.example")
        XCTAssertNil(defaults.dictionary(forKey: DirectPathPreferenceStore.key)?["a https://one.example"])
        defaults.set(
            ["a https://one.example": Data(#"{"mode":"lanOnly"}"#.utf8)],
            forKey: DirectPathPreferenceStore.key
        )
        XCTAssertEqual(store.preference(accountID: "a", origin: "https://one.example"), .automatic)
        XCTAssertNil(DirectPathPreference.Mode(name: "lanOnly"))
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
            timeout: WebKitReadiness.launchedTransportOperationSeconds
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
            timeout: WebKitReadiness.launchedTransportOperationSeconds
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
    func testAHandOverActsOnlyWhileTheHostsPathIsUsableAndOnlyForTheSoleDocument() async throws {
        let transport = StandInDirectTransport()
        let app = try await makeApp(directTransport: transport)
        let runtime = try await select("instance-1", in: app)
        let target = try XCTUnwrap(transport.selections.last)

        // Before the path is usable nothing is closed.
        let early = await handover(app, instance, sole: true)
        XCTAssertEqual(early, .skipped(.notUsable))

        try await eventually {
            if app.mobileSessionCoordinator.isCurrentRuntime(runtime) {
                transport.report(.usable(generation: "g1"), for: target)
            }
            return self.directStatus(app, self.instance)?.preferred == .direct
        }
        // Another document of the store has the host loaded, or the App
        // cannot tell: skipped.
        let unknown = await handover(app, instance, sole: nil)
        XCTAssertEqual(unknown, .skipped(.otherDocuments))
        let shared = await handover(app, instance, sole: false)
        XCTAssertEqual(shared, .skipped(.otherDocuments))
        // The sole document: done, here with no public relay to close.
        let sole = await handover(app, instance, sole: true)
        XCTAssertEqual(sole, .done(closed: 0, kept: 0))
        // Another host is not the selected instance's.
        let elsewhere = try XCTUnwrap(WebOrigin.parseConfigured("https://instance-two.example"))
        let other = await handover(app, elsewhere, sole: true)
        XCTAssertEqual(other, .skipped(.notUsable))

        // Lost: not usable any more.
        transport.report(.lost, for: target)
        try await eventually { self.directStatus(app, self.instance)?.preferred == .tunnel }
        let lost = await handover(app, instance, sole: true)
        XCTAssertEqual(lost, .skipped(.notUsable))
    }

    @MainActor
    func testTheWebViewRegistryTellsTheSoleDocumentOfAHost() async throws {
        let registry = DirectWebViewRegistry()
        let store = WKWebsiteDataStore.nonPersistent()
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = store
        let page = WKWebView(frame: .zero, configuration: configuration)
        // Not registered: the App cannot tell.
        XCTAssertNil(registry.isSoleDocument(page, host: "instance-one.example"))
        registry.register(page)
        XCTAssertEqual(registry.isSoleDocument(page, host: "instance-one.example"), true)

        // Another WebView of the same store with no page yet: cannot tell.
        let second = WKWebViewConfiguration()
        second.websiteDataStore = store
        let other = WKWebView(frame: .zero, configuration: second)
        registry.register(other)
        XCTAssertNil(registry.isSoleDocument(page, host: "instance-one.example"))
        other.loadHTMLString("<p>x</p>", baseURL: URL(string: "https://instance-one.example/"))
        try await eventually { other.url != nil }
        XCTAssertEqual(registry.isSoleDocument(page, host: "instance-one.example"), false)
        XCTAssertEqual(registry.isSoleDocument(page, host: "instance-two.example"), true)

        // A WebView of another store does not count.
        let elsewhere = WKWebViewConfiguration()
        elsewhere.websiteDataStore = WKWebsiteDataStore.nonPersistent()
        let foreign = WKWebView(frame: .zero, configuration: elsewhere)
        registry.register(foreign)
        foreign.loadHTMLString("<p>y</p>", baseURL: URL(string: "https://instance-two.example/"))
        try await eventually { foreign.url != nil }
        XCTAssertEqual(registry.isSoleDocument(page, host: "instance-two.example"), true)
        withExtendedLifetime((page, other, foreign)) {}
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
            // (If the simulator refused the re-bind, the closure is
            // published by the attempt that serves or finds the port
            // closed: that run covers no return that served at once.)
            if !theSimulatorRefusedABind(of: fixture.app) {
                let status = try XCTUnwrap(first.values.first ?? nil, "\(order)")
                XCTAssertEqual(status.closure, 1, "\(order)")
                try await Task.sleep(for: .milliseconds(300))
                XCTAssertEqual(directStatus(fixture.app, instance)?.closure, 1, "\(order)")
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
        // The whole surface: a status, a hand-over that takes nothing, the
        // path choice, and the measurement the App's controller makes.
        // Nothing takes an address or accepts a rendezvous answer.
        XCTAssertEqual(
            NativeBridgeMethod.allCases
                .map(\.rawValue)
                .filter { $0.hasPrefix("direct.") },
            ["direct.status", "direct.handover", "direct.preference", "direct.setPreference", "direct.measure"]
        )
    }

    @MainActor
    func testDirectHandoverIsVersionTwoTakesNoParametersAndPassesTheSoleDocument() async throws {
        var asked: [(WebOrigin, Bool?)] = []
        let webView = WKWebView(
            frame: .zero,
            configuration: NativeBridgeConfiguration.make(
                instanceOrigin: instance,
                setOuterScrollLocked: { _, _ in false },
                directStatus: { _, reply in
                    reply(DirectPathStatus(preferred: .direct, sequence: 1, closure: 0))
                },
                directHandover: { origin, sole, reply in
                    asked.append((origin, sole))
                    reply(.done(closed: 2, kept: 1))
                }
            )
        )
        DirectWebViewRegistry.shared.register(webView)
        let loader = PageLoader()
        webView.navigationDelegate = loader
        try await loader.loadHTML(
            "<!doctype html><title>instance</title>",
            baseURL: instance.url,
            in: webView
        )
        let result = try await webView.callAsyncJavaScript(
            """
            const answer = await window.avibeNative.call("direct.handover");
            let refused = "accepted";
            try {
              await window.avibeNative.call("direct.handover", {host: "x"});
            } catch (error) {
              refused = error.code;
            }
            return JSON.stringify({
              capability: window.avibeNative.capabilities.direct,
              answer: [answer.closed, answer.kept],
              refused
            });
            """,
            arguments: [:],
            in: nil,
            contentWorld: .page
        )
        XCTAssertEqual(
            result as? String,
            #"{"capability":3,"answer":[2,1],"refused":"invalid_argument"}"#
        )
        XCTAssertEqual(asked.count, 1)
        XCTAssertEqual(asked.first?.0, instance)
        // Other WebViews of the shared default store may be alive in this
        // test process; whatever the registry says is passed on unchanged.
        XCTAssertEqual(
            asked.first?.1,
            DirectWebViewRegistry.shared.isSoleDocument(webView, host: instance.host)
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
    /// Whether an App or a runtime controller this test made is still
    /// alive: either keeps its store (a controller through its WebViews).
    private var fixtureOwnerIsAlive: [@MainActor () -> Bool] = []
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

    /// What `direct.handover()` answers for `origin`, through the provider
    /// the App gives its WebViews; nil without a direct transport.
    @MainActor
    private func handover(_ app: AppModel, _ origin: WebOrigin, sole: Bool?) async -> DirectHandoverOutcome? {
        guard let provider = app.directHandoverProvider else {
            return nil
        }
        return await withCheckedContinuation { continuation in
            provider(origin, sole) { continuation.resume(returning: $0) }
        }
    }

    /// What `direct.status()` answers for `origin`, through the provider
    /// the App gives its WebViews. `nil` when it answers later or not at all.
    @MainActor
    private func directStatus(_ app: AppModel, _ origin: WebOrigin) -> DirectPathStatus? {
        var answer: DirectPathStatus?
        app.directStatusProvider?(origin) { answer = $0 }
        return answer
    }

    /// The premise of a test that follows WebKit's own connections through
    /// the listener: the host had its route before its first load. Read
    /// through WebKit's own store and the App's own record.
    @MainActor
    private func hostWasRoutedAtItsFirstLoad(by app: AppModel) -> Bool {
        !app.runtimeWebsiteDataStore.proxyConfigurations.isEmpty
            && !app.directListenerFailures.contains { $0.step == .late }
    }

    /// Why the host was not routed at its first load, as far as the App
    /// itself says. Two things leave it on the public path then, both by
    /// design: the listener's first bind or its check failed (seen on CI
    /// after one of the simulator's system services was interrupted, run
    /// 44934936305), and nothing is selected and no listener serves; or
    /// they were slower than the App's listener wait (2 s), and the host
    /// gets its route once the bind is confirmed, which the App records as
    /// `late` with the bind's duration.
    @MainActor
    private func whyNoHostIsRouted(by app: AppModel) -> String {
        let record = app.directListenerFailures
        if let late = record.last(where: { $0.step == .late }) {
            return "The App routed the host only after its first load: the listener's first bind took \(late.code.map(String.init) ?? "?") ms, longer than the listener wait, so the first load went on the public path and nothing of it passed through the listener."
        }
        let failures = record.map { "\($0.step.rawValue) \($0.code.map(String.init) ?? "-")" }
        return failures.isEmpty
            ? "The App has routed no host and recorded no listener failure: its first bind is slower than the listener wait, so the host loaded on the public path and gets its route when the bind is confirmed."
            : "The App routed no host: its listener failed (\(failures.joined(separator: ", "))), so the host loads on the public path and nothing is selected."
    }

    /// Whether a bind of the listener's port, or its check, failed in this
    /// test: the simulator can refuse one (seen on CI, run 44934936305).
    /// The App then binds the port again by itself, so what a test says of
    /// a return that served at its first attempt does not apply to that
    /// run.
    @MainActor
    private func theSimulatorRefusedABind(of app: AppModel) -> Bool {
        // A first bind confirmed late is on the same record and is none.
        app.directListenerFailures.contains { $0.step != .late }
    }

    /// The App's direct listener, found from outside: the one new listening
    /// socket on 127.0.0.1 since `baseline`. First, through WebKit's own
    /// store, that the App routed its host at all: when it did not, there is
    /// no listener to find, and the failure says why (`whyNoHostIsRouted`)
    /// instead of reading as a scan that missed one.
    @MainActor
    private func directListenerPort(
        of app: AppModel,
        since baseline: Set<ListeningSocket>,
        file: StaticString = #filePath,
        line: UInt = #line
    ) throws -> NWEndpoint.Port {
        XCTAssertTrue(
            hostWasRoutedAtItsFirstLoad(by: app),
            whyNoHostIsRouted(by: app),
            file: file,
            line: line
        )
        let loopback = try newListeners(since: baseline).filter { $0.address == "127.0.0.1" }
        XCTAssertEqual(loopback.count, 1, "new loopback listeners: \(loopback)", file: file, line: line)
        let socket = try XCTUnwrap(loopback.first, file: file, line: line)
        return try XCTUnwrap(NWEndpoint.Port(rawValue: socket.port), file: file, line: line)
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
        // The probe's stream, then the relay's. The dial waits for it: see
        // below.
        let connection = DirectIrohFakeConnection(
            log: log,
            remote: DirectIrohFixtures.instanceEndpoint,
            answers: [DirectIrohFakeConnection.established, DirectIrohFakeConnection.established]
        )
        let endpoint = engine.prepare()

        let runtime = try await select("instance-1", in: app)
        let controller = try makeRuntimeController(app, runtime)
        app.registerRuntimeController(controller, for: runtime)
        let page = try RuntimePage(controller: controller)
        // A document of the instance's origin, loaded here before the path
        // comes up.
        try await page.load(RuntimePage.recordingDocument, baseURL: runtime.instance.url)
        // The runtime's own load of the instance sends WebKit's own CONNECT
        // through the route. With no path up it ends on the public leg. Once
        // the path is up it would be a direct relay and take the stream the
        // CONNECT below is to get (seen on CI, run 44981277303: the path came
        // up first, `504` here, and four streams). So the dial is held until
        // the App has recorded that CONNECT's end; WebKit sends it once its
        // processes are running.
        guard hostWasRoutedAtItsFirstLoad(by: app) else {
            // Nothing of the runtime's first load goes through the listener
            // then.
            XCTFail(whyNoHostIsRouted(by: app))
            throw DirectTestTimeout()
        }
        do {
            try await eventually(timeout: WebKitReadiness.webContentLaunch) {
                self.directStatus(app, self.instance)?.races.isEmpty == false
            }
        } catch {
            XCTFail("WebKit's own CONNECT for the runtime's load never reached the App's listener.")
            throw error
        }
        endpoint.connection.resolve(connection)
        try await eventually {
            // A request made before the WebView existed is asked again.
            scheduler.fire(Self.patientIrohTiming.pageRetrySeconds)
            return self.directStatus(app, self.instance)?.preferred == .direct
        }

        let port = try directListenerPort(of: app, since: listening)
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
    ///
    /// One bootstrap outcome builds the App a second time, once: the stall
    /// after a web content launch (`FixtureBootstrap.stalledAfterWebContentLaunch`).
    /// The returned App is then the second one, `whenTheAppExists` has run
    /// for both, and the log has one line beginning
    /// `[iOS fixture] stage=bootstrap stalled after its web content launch`.
    /// Only here: a stall in another suite, or a second one here, fails.
    @MainActor
    private func makeApp(
        directTransport: DirectTransporting?,
        listenerRecoveryPatience: Duration = .seconds(10),
        pushHolds: [PushHold] = [],
        sessionResponse: (@Sendable () -> LocalHTTPResponse?)? = nil,
        pushKeychain: InMemoryPushKeychainStore? = nil,
        pushInstallationMarker: InMemoryPushInstallationMarkerStore? = nil,
        pushNotificationCenter: PushNotificationCentering? = nil,
        runtimeOpenTiming: RuntimeOpenTiming? = nil,
        runtimeOpenNotificationCenter: NotificationCenter = .default,
        networkPath: NetworkPathReading? = nil,
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
        let timing = runtimeOpenTiming ?? LaunchTolerantRuntimeOpenTiming()
        // One App of this fixture. Made a second time only by the rebuild
        // below, on the same preferences, control plane and stores.
        func makeModel() -> AppModel {
            if let directTransport {
                return AppModel(
                    defaults: defaults,
                    controlPlane: controlPlane,
                    directTransport: directTransport,
                    listenerRecoveryPatience: listenerRecoveryPatience,
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
                    runtimeOpenTiming: timing,
                    runtimeOpenNotificationCenter: runtimeOpenNotificationCenter,
                    badgeCenter: SilentBadgeCenter(),
                    networkPath: networkPath
                )
            }
            return AppModel(
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
                runtimeOpenTiming: timing,
                runtimeOpenNotificationCenter: runtimeOpenNotificationCenter,
                badgeCenter: SilentBadgeCenter(),
                networkPath: networkPath
            )
        }
        // What every App of this fixture is given before it is waited for.
        func adopt(_ app: AppModel) {
            notifications?.app = app
            whenTheAppExists(app)
            trackStore(of: app)
            releaseFixtureApps.append { [weak app] in
                app?.showInstancePicker()
                app?.mobileSessionCoordinator.deactivateAccountSurface()
            }
        }
        let app = makeModel()
        adopt(app)
        addTeardownBlock { @MainActor [self] in
            server.requests.attach(to: self, name: "direct App bootstrap requests")
        }
        if launchesWithInstanceOneSelected || reusingDefaults != nil {
            return app
        }
        func signedIn(_ app: AppModel, since: Int = 0, recordsFailure: Bool) async throws {
            try await WebKitReadiness.waitForSignedInFixture(
                app,
                accountID: "account-1",
                instanceIDs: ["instance-1", "instance-2"],
                requests: server.requests,
                requestsSince: since,
                budget: WebKitReadiness.launchedTransportOperation,
                recordsFailure: recordsFailure
            )
        }
        let readiness = Duration.milliseconds(timing.accountTransportDeadlines.readinessMilliseconds)
        do {
            try await signedIn(app, recordsFailure: false)
            return app
        } catch let error as FixtureReadinessError
            where error.bootstrap?.stalledAfterWebContentLaunch(readiness: readiness) == true
        {
            // The one state this fixture builds its App again for, once
            // (`FixtureBootstrap.stalledAfterWebContentLaunch` has what it
            // is and where it was seen): its web content process came up
            // late and then never finished its first document. Counted in
            // the logs by this line.
            print(
                "[iOS fixture] stage=bootstrap stalled after its web content launch, App rebuilt once; "
                    + (error.bootstrap?.stallEvidence(readiness: readiness) ?? "")
            )
        } catch {
            // Every other outcome fails as it did, recorded here instead of
            // in the wait, which was asked not to record the one above.
            throw recordThrown("The signed-in fixture did not bootstrap", error)
        }
        // The stalled App keeps its account page until the new App has
        // settled, and lets go of it then: the page that never answered
        // stays where it is while the new App launches its own. (Whether a
        // page let go first would have been handed on was not measured.)
        defer {
            app.showInstancePicker()
            app.mobileSessionCoordinator.deactivateAccountSurface()
        }
        // The log already has the stalled App's requests: the new App is
        // judged, and its line written, by its own, those from here on.
        let before = server.requests.count
        let rebuilt = makeModel()
        adopt(rebuilt)
        do {
            try await signedIn(rebuilt, since: before, recordsFailure: true)
        } catch let error as FixtureReadinessError
            where error.bootstrap?.stalledAfterWebContentLaunch(readiness: readiness) == true
        {
            // A second stall is a finding of its own, not a reason to
            // build a third App.
            print(
                "[iOS fixture] stage=bootstrap stalled again in the rebuilt App; "
                    + (error.bootstrap?.stallEvidence(readiness: readiness) ?? "")
            )
            throw error
        }
        return rebuilt
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
    ///
    /// A request that never gets there ends the test here, with that as its
    /// one failure: the account transport's page did not load, which on the
    /// CI simulator is a WebKit process that stalled (run 45758417964). What
    /// a test checks after this wait is about a request in flight; checked
    /// without one it only adds failures that say nothing.
    @MainActor
    private func holdPushManagement(
        _ hold: PushHold,
        in app: AppModel,
        file: StaticString = #filePath,
        line: UInt = #line
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
        let budget = WebKitReadiness.launchedTransportOperationSeconds
        let reached = await XCTWaiter.fulfillment(
            of: [hold.inFlight.expectation],
            timeout: budget
        )
        guard reached == .completed else {
            // Nothing is left held or running behind the failure.
            hold.release.signal()
            request.cancel()
            throw recordThrown(
                "The push request to \(hold.path) did not reach the control plane within \(budget) s "
                    + "(the account transport's page did not load); nothing after this wait was checked",
                DirectTestTimeout(),
                file: file,
                line: line
            )
        }
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
        _ runtime: MobileRuntimeContext,
        recording record: RuntimePageRecord? = nil
    ) async throws -> RuntimePage {
        let controller = try makeRuntimeController(app, runtime)
        app.registerRuntimeController(controller, for: runtime)
        let page = try RuntimePage(controller: controller)
        // Recorded before its first load, which is also the page's.
        record?.page = page
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
        try await eventually(timeout: WebKitReadiness.launchedTransportOperation) {
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
        let controller = WebViewController(
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
        trackOwner(controller)
        return controller
    }

    /// A runtime controller this test made on a tracked store keeps that
    /// store until it is released (`removeTrackedStores`).
    @MainActor
    private func trackOwner(_ controller: WebViewController) {
        fixtureOwnerIsAlive.append { [weak controller] in controller != nil }
    }

    /// The App made real named stores; remove exactly those.
    @MainActor
    private func trackStore(of app: AppModel) {
        storeIdentifiers.insert(app.websiteDataStore.identifier)
        fixtureOwnerIsAlive.append { [weak app] in app != nil }
    }

    /// Removes the stores this test's Apps used (`FixtureStoreRemoval`):
    /// its Apps and runtime controllers are released and no page of theirs
    /// uses a store, or the test fails; a store WebKit alone still holds is
    /// left to later removals.
    @MainActor
    private func removeTrackedStores() async {
        // Release every App's runtime/account views before removing any
        // store (some tests create more than one App). RuntimePage owners
        // close their windows before this cleanup.
        for release in releaseFixtureApps { release() }
        releaseFixtureApps.removeAll()
        print("[iOS fixture] stage=App views invalidated before store removal")
        let owners = fixtureOwnerIsAlive
        for identifier in storeIdentifiers {
            let failure = await FixtureStoreRemoval.remove(identifier) {
                owners.contains { $0() }
            }
            XCTAssertNil(failure, String(describing: failure))
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

/// The App's own clock, with account transports whose control-plane page
/// may take a web content launch's time on the CI simulator
/// (`WebKitReadiness.afterWebContentLaunch`). This rides on the App's
/// existing timing injection rather than a test-only App parameter.
@MainActor
private final class LaunchTolerantRuntimeOpenTiming: RuntimeOpenTiming {
    private let clock = ContinuousRuntimeOpenTiming()

    var now: Duration { clock.now }

    var accountTransportDeadlines: MobileAuthTransportDeadlines {
        WebKitReadiness.afterWebContentLaunch
    }

    func schedule(
        after delay: Duration,
        _ action: @escaping @MainActor () -> Void
    ) -> any RuntimeOpenTimer {
        clock.schedule(after: delay, action)
    }
}

private final class RuntimeOpenManualTiming: RuntimeOpenTiming {
    var accountTransportDeadlines: MobileAuthTransportDeadlines {
        WebKitReadiness.afterWebContentLaunch
    }

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
    var supportsReminders = false
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
            remindersAvailable: supportsReminders,
            remindersAuthorization: supportsReminders ? .authorized : .unsupported,
            supportedKinds: supportsReminders ? [.alarm, .reminder] : [.alarm]
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

/// The App's own session checks, asked for by a test that waits for
/// something the App does at each signed-in answer: one at a time, and not
/// sooner than `spacing` after the last one ended.
@MainActor
private final class SessionChecks {
    private static let spacing: Duration = .seconds(10)
    private(set) var asked = 0
    private var running: Task<Void, Never>?
    private var lastEnded: ContinuousClock.Instant?

    func askAgainIfDue(_ app: AppModel, at now: ContinuousClock.Instant) {
        guard let since = lastEnded else {
            // The launch's own answer has just been given.
            lastEnded = now
            return
        }
        guard running == nil, since.duration(to: now) >= Self.spacing else {
            return
        }
        asked += 1
        running = Task { @MainActor in
            _ = try? await app.mobileSessionCoordinator.refresh()
            self.lastEnded = ContinuousClock.now
            self.running = nil
        }
    }

    /// Returns once no check is in flight.
    func settle() async {
        await running?.value
    }
}

/// The page a scenario showed, kept so its caller can ask afterwards
/// whether WebKit ended that page's process.
@MainActor
private final class RuntimePageRecord {
    var page: RuntimePage?
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

    /// WebKit ended this page's WebContent process at some point: what the
    /// page was doing then (a load, a bridge call in flight) is lost with it.
    var webContentProcessTerminated: Bool {
        loader.processTerminations > 0
    }

    func load(_ html: String, baseURL: URL, forwardingNativeNavigation: Bool = false) async throws {
        let webView = try XCTUnwrap(webView)
        loader.forwardingDelegate = forwardingNativeNavigation ? controller : nil
        defer { loader.forwardingDelegate = nil }
        try await loader.loadHTML(html, baseURL: baseURL, in: webView)
    }

    /// Runs `script` in the page; its result, when it is text.
    func text(_ script: String) async throws -> String? {
        try await webView?.evaluateJavaScript(script) as? String
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
              // The status itself. What the shell adds for diagnosis depends
              // on which connections the page happened to make by now.
              const diagnostics = ["attempts", "connections", "losses"];
              status = Object.fromEntries(
                Object.keys(answer).filter((key) => !diagnostics.includes(key)).sort()
                  .map((key) => [key, answer[key]])
              );
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

    /// `direct.status()` as the page gets it, as JSON with sorted keys,
    /// without what the shell adds for diagnosis (which depends on the
    /// connections the page happened to make by then).
    func directStatus() async throws -> String? {
        try await webView?.callAsyncJavaScript(
            """
            const status = await window.avibeNative.call("direct.status");
            const diagnostics = ["attempts", "connections", "losses"];
            return JSON.stringify(
              status,
              Object.keys(status).filter((key) => !diagnostics.includes(key)).sort()
            );
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
        // Each ended process is counted, whether or not a load was pending.
        XCTAssertEqual(loader.processTerminations, 2)

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
    /// How many times WebKit ended a WebContent process of this loader's
    /// WebViews, loading or not.
    private(set) var processTerminations = 0

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
        processTerminations += 1
        // Evidence for a page WebKit ends under a test (for example at its
        // network process's request): which page, and whether it was loading.
        print(
            "[iOS fixture] stage=runtime page process terminated, loading=\(webView === loadingWebView), "
                + "host=\(webView.url?.host ?? "none")"
        )
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

    /// Told of each controller made, for the test's store bookkeeping.
    private let didMake: @MainActor (WebViewController) -> Void

    init(
        app: AppModel,
        notifications: NotificationsOn,
        registration: Registration,
        didMake: @escaping @MainActor (WebViewController) -> Void
    ) {
        self.didMake = didMake
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
                        by: WebKitReadiness.launchedTransportOperation
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

    @MainActor
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
        didMake(controller)
        let inFlight = !app.mobileSessionCoordinator.isCurrentRuntime(runtime)
            && app.mobileSessionCoordinator.isSelectedRuntime(runtime)
        app.registerRuntimeController(controller, for: runtime)
        controller.loadViewIfNeeded()
        registrations.append(
            Made(runtime: runtime, controller: controller, providerUpdateInFlight: inFlight)
        )
    }
}

@MainActor
private final class StandInNetworkPath: NetworkPathReading {
    var isSatisfied = true
}
