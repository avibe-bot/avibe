import Foundation
import Network
import os
import UIKit
import WebKit

/// What `direct.status()` answers and `direct.transportChanged` carries
/// (docs/plans/ios-app.md section 4).
struct DirectPathStatus: Equatable, Sendable {
    enum Preference: String, Equatable, Sendable {
        /// The instance's public origin (Cloudflare in Avibe's deployment).
        case tunnel
        /// The direct transport, through the App's local proxy.
        case direct
    }

    /// What the calling document's new connections try first. Never where
    /// an existing connection runs.
    let preferred: Preference
    /// Grows with every change of `preferred` or `closure` while the App
    /// process lives; orders `preferred`.
    let sequence: Int
    /// How many times the App closed connections for this host because
    /// their path was lost. Never decreases.
    let closure: Int

    static let initial = DirectPathStatus(preferred: .tunnel, sequence: 0, closure: 0)

    var jsonObject: [String: Any] {
        [
            "preferred": preferred.rawValue,
            "sequence": sequence,
            "closure": closure,
        ]
    }

    /// The script that delivers `direct.transportChanged` to a document.
    /// It dispatches only if the document it runs in is on `origin`. The
    /// shell checks the WebView's URL before it sends the script, but a
    /// navigation can commit before the script runs.
    func eventScript(for origin: WebOrigin) -> String? {
        guard
            let data = try? JSONSerialization.data(
                withJSONObject: [
                    "origin": origin.serialized,
                    "detail": jsonObject,
                ],
                options: [.sortedKeys]
            ),
            let payload = String(data: data, encoding: .utf8)
        else {
            return nil
        }
        return """
        (() => {
          const event = \(payload);
          if (window.location.origin !== event.origin) return;
          window.dispatchEvent(new CustomEvent("avibe:native:direct.transportChanged", {
            detail: event.detail
          }));
        })();
        """
    }
}

/// Answers `direct.status` for an instance origin, possibly later: the
/// answer is held while the App returns from the background. `nil` when
/// nothing current is left to answer for.
typealias DirectStatusProvider = @MainActor (
    WebOrigin,
    @escaping @MainActor (DirectPathStatus?) -> Void
) -> Void

/// The only writer of `proxyConfigurations`. A store's route lists exact
/// instance hosts and points at the App's one listener. Its host list only
/// grows while the store lives: no host is removed and no route is cleared,
/// because WebKit does not reliably apply a cleared or changed route
/// (docs/plans/remote-direct-ios.md sections 2 and 5).
@MainActor
final class DirectRouteOwner {
    /// The most hosts the process's routes may list together.
    static let maxHosts = 16

    private struct StoreHost: Hashable {
        let store: UUID
        let host: String
    }

    /// Every host written to any route in this process, in order.
    private(set) var allowedHosts: [String] = []
    private var hostsByStore: [UUID: [String]] = [:]
    /// Hosts a store loaded without a route. They are never added to that
    /// store later: WebKit would keep using its existing connections.
    private var excluded: Set<StoreHost> = []

    func routes(_ host: String, store: UUID) -> Bool {
        hostsByStore[store]?.contains(host) == true
    }

    func isExcluded(_ host: String, store: UUID) -> Bool {
        excluded.contains(StoreHost(store: store, host: host))
    }

    /// Records that `store` loaded `host` without a route.
    func exclude(_ host: String, store: UUID) {
        guard !routes(host, store: store) else {
            return
        }
        excluded.insert(StoreHost(store: store, host: host))
    }

    /// Adds `host` to `store`'s route, written to `dataStore`, pointing at
    /// `port`. `false` when the host is excluded for this store or the
    /// process-wide cap is reached; the host then keeps WebKit's normal path.
    func add(_ host: String, store: UUID, dataStore: WKWebsiteDataStore, port: UInt16) -> Bool {
        if routes(host, store: store) {
            return true
        }
        guard !isExcluded(host, store: store) else {
            return false
        }
        if !allowedHosts.contains(host) {
            guard allowedHosts.count < Self.maxHosts else {
                return false
            }
            allowedHosts.append(host)
        }
        let hosts = (hostsByStore[store] ?? []) + [host]
        hostsByStore[store] = hosts
        dataStore.proxyConfigurations = [Self.route(port: port, hosts: hosts)]
        return true
    }

    static func route(port: UInt16, hosts: [String]) -> ProxyConfiguration {
        var route = ProxyConfiguration(
            httpCONNECTProxy: .hostPort(
                host: "127.0.0.1",
                port: NWEndpoint.Port(rawValue: port) ?? .any
            )
        )
        // A suffix match: the listener refuses the subdomains it also gets.
        route.matchDomains = hosts
        // A candidate safety net, not a guarantee (section 8): WebKit may
        // take its normal path when the listener cannot be reached.
        route.allowFailover = true
        return route
    }
}

/// Proposed bounds for the controller (docs/plans/remote-direct-ios.md
/// section 9). Not measurements.
struct DirectPathTiming: Sendable {
    /// How long a runtime's first load waits for the listener and its route.
    let listenerWaitSeconds: TimeInterval
    /// How long the return to the foreground may take to re-bind and check
    /// the listener's port.
    let rebindSeconds: TimeInterval

    static let standard = DirectPathTiming(listenerWaitSeconds: 1, rebindSeconds: 2)
}

/// The listener's state, as the controller sees it.
enum DirectListenerState: Equatable, Sendable {
    /// Not created: no route has ever been prepared in this process.
    case none
    case binding
    case ready
    /// Closed on background (B2); the same port is re-bound on return.
    case closedForBackground
    case rebinding
    /// The port could not be re-bound exclusively, or the ownership check or
    /// the socket failed. Direct is never authorized in this state, and no
    /// recovery has been qualified (section 9). The routes still point at
    /// the old port. It is not a steady state the design accepts.
    case recoveryNotQualified
}

/// Decides, for each new CONNECT, whether direct may carry it, and keeps the
/// path generation, the preference, and the closure counters the bridge
/// reports. It exists only when a transport is registered.
@MainActor
final class DirectPathController {
    private struct Fence: Equatable, Sendable {
        let selection: Int
        let pathGeneration: Int
        let accountEpoch: Int
    }

    private static let logger = Logger(
        subsystem: Bundle.main.bundleIdentifier ?? "com.avibe",
        category: "DirectPath"
    )

    let routeOwner = DirectRouteOwner()
    private let websiteDataStore: AccountWebsiteDataStore
    private let transport: DirectTransporting
    private let isCurrentRuntime: (MobileRuntimeContext) -> Bool
    private let publicBackend: DirectPublicBackend
    private let limits: DirectConnectLimits
    private let timing: DirectPathTiming
    private let notificationCenter: NotificationCenter
    private let onStatusChanged: () -> Void
    private var lifecycleObservers: [NSObjectProtocol] = []

    private var listener: DirectConnectListener?
    private var listenerPort: UInt16?
    private(set) var listenerState: DirectListenerState = .none
    private var listenerStart: Task<Bool, Never>?

    private var runtime: MobileRuntimeContext?
    private var target: DirectTarget?
    private var selection = 0
    private var acceptedGeneration: String?
    private var replacedGenerations: Set<String> = []
    /// The transport reported the accepted generation usable, and nothing
    /// has suspended that since.
    private var isUsable = false
    private(set) var pathGeneration = 0
    private var accountEpoch = 0
    private var isInBackground: Bool

    private var preferred: DirectPathStatus.Preference = .tunnel
    private var sequence = 0
    private var closures: [String: Int] = [:]
    /// Hosts whose relays the background closed: published on the return,
    /// once the listener is ready again (or its recovery has failed).
    private var pendingClosureHosts: Set<String> = []
    /// Status answers held while the App returns from the background.
    private var heldReplies: [(origin: WebOrigin, reply: @MainActor (DirectPathStatus?) -> Void)] = []
    private var isResuming = false
    /// Route preparations under way; the listener is kept while any is.
    private var preparations = 0

    init(
        websiteDataStore: AccountWebsiteDataStore,
        transport: DirectTransporting,
        isCurrentRuntime: @escaping (MobileRuntimeContext) -> Bool,
        publicBackend: DirectPublicBackend = DirectPublicBackend(),
        limits: DirectConnectLimits = .standard,
        timing: DirectPathTiming = .standard,
        notificationCenter: NotificationCenter = .default,
        isInBackground: Bool? = nil,
        onStatusChanged: @escaping () -> Void
    ) {
        self.websiteDataStore = websiteDataStore
        self.transport = transport
        self.isCurrentRuntime = isCurrentRuntime
        self.publicBackend = publicBackend
        self.limits = limits
        self.timing = timing
        self.notificationCenter = notificationCenter
        // The App can be launched into the background.
        self.isInBackground = isInBackground
            ?? (UIApplication.shared.applicationState == .background)
        self.onStatusChanged = onStatusChanged

        websiteDataStore.onWillRotate = { [weak self] in
            self?.accountWillChange()
        }
        lifecycleObservers = [
            notificationCenter.addObserver(
                forName: UIApplication.didEnterBackgroundNotification,
                object: nil,
                queue: nil
            ) { [weak self] _ in
                MainActor.assumeIsolated {
                    self?.applicationDidEnterBackground()
                }
            },
            notificationCenter.addObserver(
                forName: UIApplication.willEnterForegroundNotification,
                object: nil,
                queue: nil
            ) { [weak self] _ in
                MainActor.assumeIsolated {
                    self?.applicationWillEnterForeground()
                }
            },
        ]
    }

    deinit {
        for observer in lifecycleObservers {
            notificationCenter.removeObserver(observer)
        }
        let listener = listener
        Task {
            await listener?.stop()
        }
    }

    // MARK: Status

    /// What `direct.status()` answers for `origin` now. `preferred` is
    /// `direct` only for the selected instance's host.
    func currentStatus(for origin: WebOrigin) -> DirectPathStatus {
        DirectPathStatus(
            preferred: preferred == .direct && origin.host == target?.authority.host
                ? .direct : .tunnel,
            sequence: sequence,
            closure: closures[origin.host] ?? 0
        )
    }

    /// Answers `direct.status()` for `origin`. While the App returns from the
    /// background the answer is held until the listener is ready again and
    /// the pending closure is published, or until a failed recovery is.
    func status(
        for origin: WebOrigin,
        reply: @escaping @MainActor (DirectPathStatus?) -> Void
    ) {
        if isResuming {
            heldReplies.append((origin, reply))
        } else {
            reply(currentStatus(for: origin))
        }
    }

    var isRecoveryNotQualified: Bool {
        listenerState == .recoveryNotQualified
    }

    // MARK: Runtime and account

    /// Prepares the route for `runtime`'s host before its first load: binds
    /// the listener if this is the first route of the process, checks it,
    /// and adds the host to the current store's route. Waits at most the
    /// listener bound. If the listener is not ready by then, or the host
    /// cannot be added, the runtime loads on WebKit's normal path and the
    /// host is never added to this store later. `stillWanted` is asked after
    /// every wait: a superseded open adds nothing and, if no route was ever
    /// written, leaves no listener behind.
    func prepareRoute(
        for runtime: MobileRuntimeContext,
        stillWanted: @escaping () -> Bool
    ) async {
        preparations += 1
        await addRoute(for: runtime, stillWanted: stillWanted)
        preparations -= 1
        // A listener no route points at, and no open is waiting for, has
        // nothing to do.
        if routeOwner.allowedHosts.isEmpty, preparations == 0, listener != nil {
            await stopListener()
        }
    }

    private func addRoute(
        for runtime: MobileRuntimeContext,
        stillWanted: () -> Bool
    ) async {
        guard
            let authority = DirectAuthority(instanceURL: runtime.instance.url),
            listenerState != .recoveryNotQualified
        else {
            return
        }
        let store = websiteDataStore.identifier
        guard
            !routeOwner.routes(authority.host, store: store),
            !routeOwner.isExcluded(authority.host, store: store)
        else {
            return
        }
        let ready = await ensureListener()
        guard stillWanted(), websiteDataStore.identifier == store else {
            return
        }
        guard
            ready,
            let listener,
            let listenerPort,
            listenerState == .ready
        else {
            routeOwner.exclude(authority.host, store: store)
            return
        }
        // The listener accepts the host before WebKit can be routed to it.
        // Its list only grows: `plan(for:)` refuses a host no route lists.
        await listener.allowHost(authority.host)
        guard
            stillWanted(),
            websiteDataStore.identifier == store,
            listenerState == .ready,
            self.listener === listener,
            self.listenerPort == listenerPort
        else {
            return
        }
        if !routeOwner.add(
            authority.host,
            store: store,
            dataStore: websiteDataStore.dataStore,
            port: listenerPort
        ) {
            routeOwner.exclude(authority.host, store: store)
        }
    }

    /// A runtime opened, after its route was prepared.
    func runtimeDidOpen(_ runtime: MobileRuntimeContext) {
        self.runtime = runtime
        advanceGeneration()
        select()
        updatePreference()
    }

    /// The runtime's browser went away: its selection ends. Direct relays of
    /// that selection close; public relays and closures are untouched (the
    /// documents that would act on a closure are gone).
    func runtimeDidStop() {
        let hadSelection = runtime != nil || target != nil
        runtime = nil
        deselect()
        guard hadSelection else {
            return
        }
        advanceGeneration()
        closeRelays(.direct, countsAsClosure: false)
        updatePreference()
    }

    /// Sign-in, sign-out, an account switch: the account store rotates.
    /// Every relay of both backends closes; nothing of the old account is
    /// kept. Not a closure: its documents are gone.
    func accountWillChange() {
        runtime = nil
        deselect()
        accountEpoch += 1
        advanceGeneration()
        closeRelays(nil, countsAsClosure: false)
        updatePreference()
    }

    // MARK: Transport

    private func select() {
        guard
            target == nil,
            let runtime,
            !isInBackground,
            listenerState == .ready,
            // Whether `runtime` is still the account's current one is
            // checked for each CONNECT (`directAuthorized`), not here.
            let authority = DirectAuthority(instanceURL: runtime.instance.url),
            routeOwner.routes(authority.host, store: websiteDataStore.identifier)
        else {
            return
        }
        selection += 1
        let target = DirectTarget(
            selection: selection,
            runtime: runtime,
            storeIdentifier: websiteDataStore.identifier,
            authority: authority
        )
        self.target = target
        transport.select(target) { [weak self] report in
            self?.receive(report, for: target)
        }
    }

    private func deselect() {
        isUsable = false
        acceptedGeneration = nil
        replacedGenerations.removeAll()
        guard target != nil else {
            return
        }
        target = nil
        transport.deselect()
    }

    private func receive(_ report: DirectPathReport, for target: DirectTarget) {
        // A report for a selection that has ended changes nothing.
        guard target == self.target else {
            return
        }
        switch report {
        case .usable(let generation):
            // Generations never repeat, so one that a newer answer replaced
            // can only be a late result.
            guard !replacedGenerations.contains(generation) else {
                return
            }
            if let accepted = acceptedGeneration, accepted != generation {
                // A new instance endpoint key or address set.
                replacedGenerations.insert(accepted)
                advanceGeneration()
                closeRelays(.direct, countsAsClosure: true)
            }
            acceptedGeneration = generation
            isUsable = true
        case .verifying:
            // A network callback is not proof of loss: only the preference
            // changes until the transport validates the path again.
            isUsable = false
            advanceGeneration()
        case .lost:
            isUsable = false
            advanceGeneration()
            closeRelays(.direct, countsAsClosure: true)
        }
        updatePreference()
    }

    /// A direct candidate failed before anything was forwarded. Only a
    /// failure under the current path generation reaches the transport.
    private func directCandidateFailed(_ tag: DirectStreamTag) {
        guard tag.target == target, tag.pathGeneration == pathGeneration else {
            return
        }
        transport.pathDidFail(for: tag)
    }

    // MARK: Plans

    /// Decides one accepted CONNECT (section 6). `nil` refuses it.
    func plan(for authority: DirectAuthority) -> DirectConnectPlan? {
        guard routeOwner.allowedHosts.contains(authority.host) else {
            return nil
        }
        let fence = Fence(
            selection: selection,
            pathGeneration: pathGeneration,
            accountEpoch: accountEpoch
        )
        let transport = transport
        let publicBackend = publicBackend
        let directTag = directStreamTag(for: authority)
        let openDirect: (@Sendable () async throws -> any DirectByteStream)?
        if let directTag {
            openDirect = {
                try await transport.openStream(for: directTag)
            }
        } else {
            openDirect = nil
        }
        // A plan with a direct candidate also needs the lease to be current
        // at its commit point; a public-only plan needs only the fence.
        let lease = directTag?.target.runtime
        return DirectConnectPlan(
            openDirect: openDirect,
            openPublic: {
                try await publicBackend.open(authority)
            },
            isCurrent: { [weak self] in
                await self?.isCurrent(fence, lease: lease) ?? false
            },
            directFailed: { [weak self] in
                guard let directTag else {
                    return
                }
                Task { @MainActor in
                    self?.directCandidateFailed(directTag)
                }
            }
        )
    }

    private func directStreamTag(for authority: DirectAuthority) -> DirectStreamTag? {
        guard
            directAuthorized(for: authority),
            let target,
            let acceptedGeneration
        else {
            return nil
        }
        return DirectStreamTag(
            target: target,
            rendezvousGeneration: acceptedGeneration,
            pathGeneration: pathGeneration
        )
    }

    private func isCurrent(_ fence: Fence, lease: MobileRuntimeContext?) -> Bool {
        guard
            fence == Fence(
                selection: selection,
                pathGeneration: pathGeneration,
                accountEpoch: accountEpoch
            )
        else {
            return false
        }
        if let lease {
            return isCurrentRuntime(lease) && listenerState == .ready && !isInBackground
        }
        return true
    }

    private func directAuthorized(for authority: DirectAuthority) -> Bool {
        guard
            let target,
            authority == target.authority,
            target.storeIdentifier == websiteDataStore.identifier,
            routeOwner.routes(authority.host, store: target.storeIdentifier),
            isUsable,
            acceptedGeneration != nil,
            listenerState == .ready,
            !isInBackground
        else {
            return false
        }
        return isCurrentRuntime(target.runtime)
    }

    // MARK: Generations, relays, the bridge

    private func advanceGeneration() {
        pathGeneration += 1
    }

    /// Closes committed relays on `backend` (`nil`: both). When the event
    /// counts as a closure, each host that lost at least one committed relay
    /// gets one closure, however many relays it lost. In the background the
    /// closure waits for the return.
    private func closeRelays(_ backend: DirectRelayBackend?, countsAsClosure: Bool) {
        guard let listener else {
            return
        }
        Task { [weak self] in
            let closed = await listener.closeRelays(backend: backend)
            guard countsAsClosure else {
                return
            }
            let hosts = closed.filter { $0.value > 0 }.map(\.key)
            await MainActor.run {
                self?.recordClosure(hosts: hosts)
            }
        }
    }

    /// One closure per host for one event. While the App is in the
    /// background or still returning, it waits to be published on return.
    private func recordClosure(hosts: [String]) {
        guard !hosts.isEmpty else {
            return
        }
        if isInBackground || isResuming {
            pendingClosureHosts.formUnion(hosts)
            return
        }
        for host in hosts {
            closures[host, default: 0] += 1
        }
        sequence += 1
        onStatusChanged()
    }

    private func updatePreference() {
        let next: DirectPathStatus.Preference
        if let target, directAuthorized(for: target.authority) {
            next = .direct
        } else {
            next = .tunnel
        }
        guard next != preferred else {
            return
        }
        preferred = next
        sequence += 1
        onStatusChanged()
    }

    // MARK: The listener

    /// Waits, at most the listener bound, for the listener to be ready:
    /// starting it if none exists, or letting a bind or a re-bind under way
    /// finish.
    private func ensureListener() async -> Bool {
        switch listenerState {
        case .ready:
            return true
        case .recoveryNotQualified, .closedForBackground:
            return false
        case .none:
            listenerStart = startListener()
        case .binding, .rebinding:
            break
        }
        // The bind runs on its own; this wait is bounded either way.
        let deadline = ContinuousClock.now.advanced(by: .seconds(timing.listenerWaitSeconds))
        while listenerState == .binding || listenerState == .rebinding,
              ContinuousClock.now < deadline
        {
            try? await Task.sleep(for: .milliseconds(25))
        }
        return listenerState == .ready
    }

    private func startListener() -> Task<Bool, Never> {
        let listener = DirectConnectListener(
            limits: limits,
            planner: { [weak self] authority in
                await self?.plan(for: authority)
            },
            onListenerFailure: { [weak self] in
                Task { @MainActor in
                    self?.listenerDidFail()
                }
            }
        )
        self.listener = listener
        listenerState = .binding
        let rebindSeconds = timing.rebindSeconds
        let task = Task { [weak self] () -> Bool in
            guard let port = try? await listener.bind(port: nil) else {
                return false
            }
            let owned = await DirectConnectListener.ownershipCheck(
                port: port,
                nonce: listener.ownershipNonce,
                within: rebindSeconds
            )
            return await MainActor.run {
                guard let self, self.listener === listener, self.listenerState == .binding else {
                    return false
                }
                guard owned else {
                    self.listenerState = .none
                    self.listener = nil
                    Task {
                        await listener.stop()
                    }
                    return false
                }
                self.listenerPort = port
                self.listenerState = .ready
                return true
            }
        }
        Task { [weak self] in
            let ready = await task.value
            await MainActor.run {
                guard let self, self.listenerStart == task else {
                    return
                }
                self.listenerStart = nil
                if !ready, self.listenerState == .binding {
                    self.listenerState = .none
                    self.listener = nil
                    Task {
                        await listener.stop()
                    }
                }
            }
        }
        return task
    }

    private func stopListener() async {
        let listener = listener
        self.listener = nil
        listenerPort = nil
        listenerStart = nil
        listenerState = .none
        await listener?.stop()
    }

    private func listenerDidFail() {
        guard listenerState == .ready else {
            return
        }
        enterRecoveryNotQualified("the listening socket failed")
    }

    /// No new connection is authorized for direct from here on. Committed
    /// relays are left as they are: the port loss closes none of them, and
    /// it is not a closure.
    private func enterRecoveryNotQualified(_ reason: String) {
        Self.logger.error("Direct path: recovery not qualified: \(reason, privacy: .public)")
        listenerState = .recoveryNotQualified
        advanceGeneration()
        updatePreference()
    }

    // MARK: Background and foreground

    private func applicationDidEnterBackground() {
        guard !isInBackground else {
            return
        }
        isInBackground = true
        deselect()
        advanceGeneration()
        if let listener, listenerState == .ready {
            listenerState = .closedForBackground
            Task {
                await listener.closeListeningSocket()
            }
        }
        closeRelays(.direct, countsAsClosure: true)
        updatePreference()
    }

    private func applicationWillEnterForeground() {
        guard isInBackground else {
            return
        }
        isInBackground = false
        guard
            let listener,
            let listenerPort,
            listenerState == .closedForBackground
        else {
            finishResume()
            return
        }
        isResuming = true
        listenerState = .rebinding
        let rebindSeconds = timing.rebindSeconds
        let nonce = listener.ownershipNonce
        Task { [weak self] in
            let ready: Bool
            do {
                ready = try await directDeadline(rebindSeconds) {
                    // The background's close may not have run yet.
                    await listener.closeListeningSocket()
                    let port = try await listener.bind(port: listenerPort)
                    return await DirectConnectListener.ownershipCheck(
                        port: port,
                        nonce: nonce,
                        within: rebindSeconds
                    )
                }
            } catch {
                ready = false
            }
            await MainActor.run {
                guard let self, self.listener === listener, self.listenerState == .rebinding else {
                    return
                }
                if self.isInBackground {
                    // Back in the background meanwhile: the next return
                    // re-binds, and the held answers wait for it.
                    Task {
                        await listener.closeListeningSocket()
                    }
                    self.listenerState = .closedForBackground
                    return
                }
                if ready {
                    self.listenerState = .ready
                    self.finishResume()
                } else {
                    Task {
                        await listener.closeListeningSocket()
                    }
                    self.enterRecoveryNotQualified("the port could not be re-bound and checked")
                    self.finishResume()
                }
            }
        }
    }

    /// Publishes the closures the background caused, answers the held
    /// status requests, sends the state once more, and selects again.
    private func finishResume() {
        isResuming = false
        if !pendingClosureHosts.isEmpty {
            for host in pendingClosureHosts {
                closures[host, default: 0] += 1
            }
            pendingClosureHosts.removeAll()
            sequence += 1
        }
        let held = heldReplies
        heldReplies.removeAll()
        for (origin, reply) in held {
            reply(currentStatus(for: origin))
        }
        select()
        updatePreference()
        onStatusChanged()
    }
}
