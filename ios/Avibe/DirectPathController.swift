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

    /// Whether `add` would add `host` to `store`'s route: it is not excluded
    /// there, and the process-wide cap leaves room for it.
    func canAdd(_ host: String, store: UUID) -> Bool {
        guard !routes(host, store: store), !isExcluded(host, store: store) else {
            return false
        }
        return allowedHosts.contains(host) || allowedHosts.count < Self.maxHosts
    }

    /// Adds `host` to `store`'s route, written to `dataStore`, pointing at
    /// `port`. `false` when the host is excluded for this store or the
    /// process-wide cap is reached; the host then keeps WebKit's normal path.
    @discardableResult
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
    /// The foreground watchdog: how long, in one stay in the foreground,
    /// the listener may be neither serving nor failed (binding, checking,
    /// closing, closed and about to be bound again) before that is the
    /// listener fault. One fixed bound for the whole stay, whatever
    /// attempts happen inside it. Longer than a first bind or a re-bind
    /// with its check is allowed to take, so it only ends what those bounds
    /// did not. A conservative proposal for the internal trial.
    let foregroundSeconds: TimeInterval

    init(
        listenerWaitSeconds: TimeInterval,
        rebindSeconds: TimeInterval,
        foregroundSeconds: TimeInterval = 5
    ) {
        self.listenerWaitSeconds = listenerWaitSeconds
        self.rebindSeconds = rebindSeconds
        self.foregroundSeconds = foregroundSeconds
    }

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
    /// Terminal for this process: a bind, its ownership check, or the
    /// listening socket failed, before or after a route. Direct is never
    /// authorized again, no route is written, no listener is made again,
    /// and the next launch uses no listener at all. A host that has a route
    /// still points at a port the App may not hold, and nothing in the
    /// process can make WebKit leave it (docs/plans/remote-direct-ios.md
    /// section 9): its runtime asks the user to reopen the App. A host
    /// without a route keeps WebKit's normal path and is not interrupted.
    case faulted
}

/// Whether the shell may create or reload a runtime's WebView now. Derived,
/// each time it is asked, from the current store's route and the listener's
/// lifecycle; nothing keeps a copy.
enum DirectLoadGate: Equatable, Sendable {
    /// The host has no route on the current store (WebKit's normal path),
    /// or its route points at a listener that serves.
    case open
    /// The host has a route and the listener is closed for the background
    /// or being bound again: nothing is created or reloaded against that
    /// port until the return settles.
    case held
    /// The host has a route and the listener failed: only the reopen
    /// message is shown.
    case fault
}

/// Answers the load gate for one runtime, to that runtime's WebView
/// controller.
typealias DirectLoadGateProvider = @MainActor () -> DirectLoadGate

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
    /// Called once, when the listener fault is entered.
    private let onFault: () -> Void
    /// Called when a load gate may have changed without a fault: the
    /// listener was closed for the background, or a return was settled.
    private let onAvailabilityChanged: () -> Void
    private var lifecycleObservers: [NSObjectProtocol] = []

    private var listener: DirectConnectListener?
    private var listenerPort: UInt16?
    /// The bind of `listener` that serves: route publication names it.
    private var listenerGeneration: Int?
    private var listenerStart: Task<Bool, Never>?
    /// A return's re-bind task is running. Only this controller's own
    /// bookkeeping of that task; whether the listener admits or publishes
    /// is decided by its lifecycle alone.
    private var rebinds = DirectRebindAttempts()
    /// Where the delivery of the listener fault stands. The fault itself is
    /// the lifecycle's `failed` phase, which fences everything at once; this
    /// only makes its one delivery (the marker, the reopen message) happen
    /// once. It is `delivered` only after `onFault` has returned, so an
    /// observation of the failure before that still delivers.
    private enum FaultDelivery {
        case none
        case delivering
        case delivered
    }

    private var faultDelivery = FaultDelivery.none
    /// The one wake-up scheduled for the foreground watchdog, and the
    /// episode it is for.
    private var watchdog: (episode: UUID, wake: Task<Void, Never>)?

    /// The listener, as the controller sees it: derived from the listener's
    /// lifecycle, the one authority (`DirectListenerLifecycle`).
    var listenerState: DirectListenerState {
        // A failed listener is kept, so its phase says so for as long as the
        // process lives.
        guard let listener else {
            return .none
        }
        switch listener.lifecycle.phase {
        case .idle, .binding(_, false), .verifying(_, false):
            return .binding
        case .binding(_, true), .verifying(_, true):
            return .rebinding
        case .closing, .closed:
            return rebinds.inFlight ? .rebinding : .closedForBackground
        case .serving:
            return .ready
        case .failed:
            return .faulted
        case .stopped:
            return .none
        }
    }

    private var runtime: MobileRuntimeContext?
    private var target: DirectTarget?
    private var selection = 0
    private var acceptedGeneration: String?
    private var replacedGenerations: Set<String> = []
    /// The transport reported the accepted generation usable, and nothing
    /// has suspended that since.
    private var isUsable = false
    private var pathGeneration = 0
    private var accountEpoch = 0
    private var isInBackground: Bool

    private var preferred: DirectPathStatus.Preference = .tunnel
    private var sequence = 0
    private var closures: [String: Int] = [:]
    /// Hosts whose relays the background closed: published on the return,
    /// once the listener is ready again (or its recovery has failed).
    private var pendingClosureHosts: Set<String> = []
    /// Status answers held while the App returns from the background.
    private struct HeldReply {
        let origin: WebOrigin
        let store: UUID
        let runtime: MobileRuntimeContext?
        let reply: @MainActor (DirectPathStatus?) -> Void
    }

    private var heldReplies: [HeldReply] = []
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
        onStatusChanged: @escaping () -> Void,
        onFault: @escaping () -> Void = {},
        onAvailabilityChanged: @escaping () -> Void = {}
    ) {
        self.websiteDataStore = websiteDataStore
        self.transport = transport
        self.isCurrentRuntime = isCurrentRuntime
        self.publicBackend = publicBackend
        self.limits = limits
        self.timing = timing
        self.notificationCenter = notificationCenter
        // The App can be launched into the background.
        self.isInBackground = UIApplication.shared.applicationState == .background
        self.onStatusChanged = onStatusChanged
        self.onFault = onFault
        self.onAvailabilityChanged = onAvailabilityChanged

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
        settleRecordedFault()
        if isResuming {
            heldReplies.append(
                HeldReply(
                    origin: origin,
                    store: websiteDataStore.identifier,
                    runtime: runtime,
                    reply: reply
                )
            )
        } else {
            reply(answer(for: origin))
        }
    }

    /// The runtime or the account the held answers were asked under has
    /// ended: they are settled without a status.
    private func settleHeldReplies() {
        let held = heldReplies
        heldReplies.removeAll()
        for entry in held {
            entry.reply(nil)
        }
    }

    /// No status once the fault covers `origin`'s host: a page behind it is
    /// never told anything that reads as ready.
    private func answer(for origin: WebOrigin) -> DirectPathStatus? {
        if isFaulted, routeOwner.routes(origin.host, store: websiteDataStore.identifier) {
            return nil
        }
        return currentStatus(for: origin)
    }

    /// The listener fault is terminal for this process. Read from the
    /// lifecycle, so status answers and events are fenced from the moment
    /// the failure is recorded, before its delivery reaches this actor.
    var isFaulted: Bool {
        listenerState == .faulted
    }

    // MARK: Runtime and account

    /// Prepares the route for `runtime`'s host before its first load: binds
    /// the listener if this is the first route of the process, checks it,
    /// and adds the host to the current store's route. Waits at most the
    /// listener bound. If the listener is not ready by then, or the host
    /// cannot be added, the runtime loads on WebKit's normal path and the
    /// host is never added to this store later. `stillWanted` is asked after
    /// every wait: a superseded open adds nothing and, if no route was ever
    /// written, leaves no listener behind. A host that already has a route
    /// is left as it is, with no wait: whether its runtime may load now is
    /// `loadGate(for:)`, asked by the caller afterwards.
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
            listenerState != .faulted
        else {
            return
        }
        let store = websiteDataStore.identifier
        if routeOwner.routes(authority.host, store: store) {
            return
        }
        guard !routeOwner.isExcluded(authority.host, store: store) else {
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
        guard stillWanted(), websiteDataStore.identifier == store else {
            return
        }
        // The runtime loads now, with or without a route, so a host that
        // gets none here never gets one on this store.
        // The last checks and the publication, in one main-actor turn with
        // no suspension. The claim is ordered against the socket's failure
        // under the listener's own lock (`DirectListenerLifecycle`): a failure
        // recorded first stops the route here; a failure after the claim is
        // one after a route, whenever its callback arrives.
        guard
            listenerState == .ready,
            !isInBackground,
            self.listener === listener,
            self.listenerPort == listenerPort,
            routeOwner.canAdd(authority.host, store: store),
            let listenerGeneration,
            listener.lifecycle.publish(listenerGeneration, {
                routeOwner.add(
                    authority.host,
                    store: store,
                    dataStore: websiteDataStore.dataStore,
                    port: listenerPort
                )
            })
        else {
            routeOwner.exclude(authority.host, store: store)
            return
        }
    }

    /// Delivers the listener fault if the lifecycle has recorded a failure
    /// that has not been delivered yet (the watchdog's deadline passed, or
    /// a failure's own callback is still on its way). For callers that are
    /// about to act on the gate or the status outside a view update; the
    /// gate and the status themselves never wait for it.
    func settleRecordedFault() {
        if let listener {
            deliverFault(of: listener, "a recorded failure was observed")
        }
    }

    /// Whether the shell may create or reload `runtime`'s WebView now.
    /// Synchronous, and read from the route table and the lifecycle each
    /// time, with no side effect: a failure the lifecycle has recorded,
    /// including the watchdog's deadline passing, closes the gate before
    /// its delivery reaches this actor.
    func loadGate(for runtime: MobileRuntimeContext) -> DirectLoadGate {
        guard
            let authority = DirectAuthority(instanceURL: runtime.instance.url),
            routeOwner.routes(authority.host, store: websiteDataStore.identifier)
        else {
            return .open
        }
        switch listenerState {
        case .ready:
            return .open
        case .binding, .rebinding, .closedForBackground:
            return .held
        case .faulted:
            return .fault
        case .none:
            // Not reachable: a listener a route was published to is never
            // stopped or replaced. Shown as the fault rather than held, so
            // nothing could wait on it for ever.
            return .fault
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
        settleHeldReplies()
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
        settleHeldReplies()
        deselect()
        accountEpoch += 1
        scopeDidChange()
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
        scopeDidChange()
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

    private func deselect(stoppingTransport: Bool = true) {
        isUsable = false
        acceptedGeneration = nil
        replacedGenerations.removeAll()
        guard target != nil else {
            return
        }
        target = nil
        // Ending a selection is a new scope too.
        selection += 1
        scopeDidChange()
        if stoppingTransport {
            transport.deselect()
        }
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
            scope: scope,
            generation: pathGeneration,
            openDirect: openDirect,
            openPublic: {
                try await publicBackend.open(authority)
            },
            isCurrent: { [weak self] in
                self?.isCurrent(fence, lease: lease) ?? false
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

    /// The selection and account new plans are made under.
    private var scope: DirectPlanScope {
        DirectPlanScope(selection: selection, accountEpoch: accountEpoch)
    }

    /// A CONNECT planned under an older selection or account is not carried
    /// on: its uncommitted work ends, and it is never planned again.
    private func scopeDidChange() {
        retireObsoleteWork()
    }

    /// Retires uncommitted work made before the current path generation and
    /// scope (`DirectConnectListener.retireUncommitted`). Both are captured
    /// now; they only grow, so this retirement running late never touches
    /// work planned after it.
    private func retireObsoleteWork() {
        guard let listener else {
            return
        }
        let generation = pathGeneration
        let scope = scope
        Task {
            await listener.retireUncommitted(before: generation, outside: scope)
        }
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

    /// Every event of the path-generation table: plans made before it are
    /// fenced at their commit point, and their races are cancelled now.
    private func advanceGeneration() {
        pathGeneration += 1
        retireObsoleteWork()
    }

    /// Closes committed relays on `backend` (`nil`: both). When the event
    /// counts as a closure, each host that lost at least one committed relay
    /// gets one closure, however many relays it lost. In the background the
    /// closure waits for the return.
    /// `then` runs on the main actor once the close has run (at once when
    /// there is no listener).
    private func closeRelays(
        _ backend: DirectRelayBackend?,
        countsAsClosure: Bool,
        then: (@MainActor () -> Void)? = nil
    ) {
        guard let listener else {
            then?()
            return
        }
        // Only relays committed under a plan made before this event: one
        // committed after it, by the time this task runs, is kept.
        let generation = pathGeneration
        Task { [weak self] in
            let closed = await listener.closeRelays(backend: backend, before: generation)
            let hosts = closed.filter { $0.value > 0 }.map(\.key)
            await MainActor.run {
                if countsAsClosure {
                    self?.recordClosure(hosts: hosts)
                }
                then?()
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
        notify()
    }

    /// Sends the state to the runtime's documents; never once faulted, so a
    /// covered page is asked to start nothing.
    private func notify() {
        guard !isFaulted else {
            return
        }
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
        notify()
    }

    // MARK: The listener

    /// Waits, at most the listener bound, for the listener to be ready:
    /// starting it if none exists, or letting a bind or a re-bind under way
    /// finish.
    private func ensureListener() async -> Bool {
        switch listenerState {
        case .ready:
            return true
        case .faulted, .closedForBackground:
            return false
        case .none:
            // No bind in the background: a listening socket does not survive
            // suspension reliably.
            guard !isInBackground else {
                return false
            }
            if listener != nil {
                // Out of service and not yet retired.
                retireAndStop()
            }
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
        // The failure callback names the listener it came from, so one from
        // a listener that was replaced or stopped does nothing.
        let listenerBox = DirectListenerBox()
        let listener = DirectConnectListener(
            limits: limits,
            planner: { [weak self] authority in
                await self?.plan(for: authority)
            },
            onListenerFailure: { [weak self, listenerBox] in
                Task { @MainActor in
                    guard let failed = listenerBox.listener else {
                        return
                    }
                    self?.listenerDidFail(failed)
                }
            }
        )
        listenerBox.listener = listener
        self.listener = listener
        // The first bind starts in the foreground: its episode begins here.
        armWatchdog()
        let rebindSeconds = timing.rebindSeconds
        let task = Task { [weak self] () -> Bool in
            guard let binding = try? await listener.bind(port: nil) else {
                // The bind failed, and its lifecycle already says so: the
                // fault. (A bind that only ended because the listener was
                // stopped is no longer this controller's listener.)
                await MainActor.run {
                    guard let self, self.listener === listener else {
                        return
                    }
                    if !self.deliverFault(of: listener, "the first bind failed") {
                        self.retireAndStop()
                    }
                }
                return false
            }
            let owned = await DirectConnectListener.ownershipCheck(
                port: binding.port,
                nonce: listener.ownershipNonce,
                within: rebindSeconds
            )
            return await MainActor.run {
                guard let self, self.listener === listener else {
                    return false
                }
                // Confirmed only if this bind is still verifying: a failure
                // recorded first wins. Not owned, or failed meanwhile: the
                // fault.
                guard owned, listener.lifecycle.confirm(binding.generation) else {
                    listener.lifecycle.reject(binding.generation)
                    if !self.deliverFault(of: listener, "the first bind was not confirmed") {
                        self.retireAndStop()
                    }
                    return false
                }
                // Serving implies the foreground: the background retires a
                // first bind still under way before it can be confirmed.
                self.listenerPort = binding.port
                self.listenerGeneration = binding.generation
                self.disarmWatchdog()
                return true
            }
        }
        listenerStart = task
        Task { [weak self] in
            let ready = await task.value
            await MainActor.run {
                guard let self, self.listenerStart == task else {
                    return
                }
                self.listenerStart = nil
                // A first bind that did not end serving leaves no listener
                // that could be used: failed (the fault), or retired.
                if !ready, self.listener === listener, self.listenerState == .none {
                    self.retireAndStop()
                }
            }
        }
        return task
    }

    private func stopListener() async {
        await retireListener()?.stop()
    }

    private func retireAndStop() {
        let retired = retireListener()
        Task {
            await retired?.stop()
        }
    }

    /// Takes the listener out of service now, if it may be: no route
    /// preparation or plan sees it ready from here on. Returns it for its
    /// socket to be stopped. One decision under the listener's own lock
    /// (`retire`): a failure recorded first wins, and then the listener is
    /// kept and the fault delivered; a listener a route was ever published
    /// to is never stopped or replaced, because the route cannot be taken
    /// back and only that listener, on that port, can serve it.
    private func retireListener() -> DirectConnectListener? {
        guard let listener else {
            return nil
        }
        switch listener.lifecycle.retire() {
        case .retired:
            disarmWatchdog()
            self.listener = nil
            listenerPort = nil
            listenerGeneration = nil
            listenerStart = nil
            rebinds.abandon()
            return listener
        case .failed:
            enterFault("the listener failed before it could be retired")
            return nil
        case .published:
            return nil
        }
    }

    /// The listening socket of `failed` failed, as its lifecycle recorded
    /// where Network reported it.
    func listenerDidFail(_ failed: DirectConnectListener) {
        deliverFault(of: failed, "the listening socket failed")
    }

    /// Delivers the fault if `listener` is this controller's and its
    /// lifecycle has recorded a failure. The controller never decides a
    /// failure itself: a stale generation, a deliberate close, or a stop
    /// records none, and then this does nothing and returns `false`.
    @discardableResult
    private func deliverFault(of listener: DirectConnectListener, _ reason: String) -> Bool {
        guard listener === self.listener, case .failed = listener.lifecycle.phase else {
            return false
        }
        enterFault(reason)
        return true
    }

    /// The one exit for every listener failure, entered once, in the
    /// foreground or the background. The lifecycle's `failed` phase has
    /// already ended every admission, plan, grant, and publication, and
    /// revoked what was not granted; this delivers the rest. No listener is
    /// made again in this process, no new connection is authorized for
    /// direct, no route is written, held status answers are settled without
    /// a status for a routed host, and no event follows. Granted relays are
    /// left as the close-and-keep table has them: the fault closes none of
    /// them, and it is not a closure. The listener is kept, never stopped:
    /// it holds those relays.
    private func enterFault(_ reason: String) {
        guard faultDelivery == .none else {
            return
        }
        faultDelivery = .delivering
        Self.logger.error("Direct path: listener fault: \(reason, privacy: .public)")
        disarmWatchdog()
        // Whatever return was under way is settled by this.
        rebinds.abandon()
        if let listener {
            // The socket goes, and the revoked work with it.
            Task {
                await listener.closeListeningSocket()
            }
        }
        // The selection stays: deselecting would have the transport close
        // its streams, and the fault closes no relay.
        advanceGeneration()
        preferred = .tunnel
        // The marker and the reopen message. Delivered only once this has
        // returned.
        onFault()
        faultDelivery = .delivered
        if isResuming, !isInBackground {
            finishResume()
        }
    }

    // MARK: The foreground watchdog

    /// Begins the foreground episode for the listener, or keeps the one
    /// running, and makes sure exactly one wake-up is scheduled for its
    /// deadline. The lifecycle owns the episode and its deadline; an attempt
    /// that starts inside it does not move either.
    private func armWatchdog() {
        guard
            let listener,
            let episode = listener.lifecycle.beginEpisode(
                deadline: ContinuousClock.now.advanced(by: .seconds(timing.foregroundSeconds))
            )
        else {
            disarmWatchdog()
            return
        }
        scheduleWatchdogWake(for: episode, of: listener)
    }

    private func scheduleWatchdogWake(
        for episode: DirectListenerLifecycle.Episode,
        of listener: DirectConnectListener
    ) {
        guard watchdog?.episode != episode.id else {
            return
        }
        watchdog?.wake.cancel()
        let id = episode.id
        let deadline = episode.deadline
        // A task that sleeps until the deadline on the continuous clock,
        // which keeps counting while the device sleeps.
        let wake = Task { @MainActor [weak self, weak listener] in
            do {
                try await Task.sleep(until: deadline, clock: ContinuousClock())
            } catch {
                return
            }
            self?.watchdogDidWake(listener, episode: id)
        }
        watchdog = (id, wake)
    }

    private func disarmWatchdog() {
        watchdog?.wake.cancel()
        watchdog = nil
    }

    /// The watchdog's wake-up. It proves nothing by itself: a wake-up for
    /// another listener or an episode that is over does nothing, and for
    /// the current one the lifecycle is read again. Its deadline decided
    /// there, under its lock, whether the listener has failed; this only
    /// delivers that. Woken before the deadline, it is scheduled again.
    private func watchdogDidWake(_ woken: DirectConnectListener?, episode: UUID) {
        guard
            let listener,
            woken === listener,
            watchdog?.episode == episode
        else {
            return
        }
        watchdog = nil
        if deliverFault(of: listener, "the listener did not serve in the foreground in time") {
            return
        }
        if let running = listener.lifecycle.currentEpisode, running.id == episode {
            scheduleWatchdogWake(for: running, of: listener)
        }
    }

    // MARK: Background and foreground

    private func applicationDidEnterBackground() {
        guard !isInBackground else {
            return
        }
        isInBackground = true
        // Nothing is watched while the App is away. An episode already
        // overdue fails the listener here, before the close below; the
        // background itself is never a failure.
        listener?.lifecycle.endEpisode()
        disarmWatchdog()
        settleRecordedFault()
        // The relays are closed and counted before the transport is told to
        // stop, which closes its streams: a relay whose stream ended first
        // would no longer be there to count.
        deselect(stoppingTransport: false)
        advanceGeneration()
        // Out of service now, before any close runs, and on purpose: what a
        // bind under way reports after this is not a failure. A serving
        // listener is closed here. A return's re-bind still running is
        // closed by its own completion, which parks it or, if its bind had
        // already failed, delivers that. A first bind still under way has no
        // route pointing at it: its listener is retired, and a later open in
        // the foreground makes a new one.
        if let listener {
            if rebinds.inFlight {
                listener.lifecycle.beginClose()
            } else if listenerState == .ready {
                listener.lifecycle.beginClose()
                Task {
                    await listener.closeListeningSocket()
                }
            } else if listenerState == .binding {
                retireAndStop()
            }
        }
        closeRelays(.direct, countsAsClosure: true) { [weak self] in
            // Unless a new selection was made meanwhile: selecting already
            // stops the transport's work for the previous target.
            guard let self, self.target == nil else {
                return
            }
            self.transport.deselect()
        }
        updatePreference()
        onAvailabilityChanged()
    }

    private func applicationWillEnterForeground() {
        guard isInBackground else {
            return
        }
        isInBackground = false
        // A new stay in the foreground: a new episode, with its own
        // deadline, for a listener that does not serve yet.
        armWatchdog()
        if rebinds.inFlight {
            // A return before the previous return's re-bind finished: that
            // re-bind settles this resume too. Held answers stay held and
            // pending closures stay pending until the listener is ready.
            isResuming = true
            return
        }
        guard
            let listener,
            let listenerPort,
            listenerState == .closedForBackground
        else {
            finishResume()
            return
        }
        isResuming = true
        startRebind(listener, port: listenerPort)
    }

    /// Binds `port` again, checks it, and settles the return.
    private func startRebind(_ listener: DirectConnectListener, port listenerPort: UInt16) {
        // Each attempt has its own identity: only the current attempt's
        // completion may settle anything.
        let attempt = rebinds.begin()
        let rebindSeconds = timing.rebindSeconds
        let nonce = listener.ownershipNonce
        // The bind's generation is recorded as soon as the bind returns, so
        // the completion confirms or rejects exactly that bind, even when
        // the deadline ends the ownership check.
        let bound = DirectBindingBox()
        Task { [weak self] in
            let ready: Bool
            do {
                ready = try await directDeadline(rebindSeconds) {
                    // The background's close may not have run yet.
                    await listener.closeListeningSocket()
                    let binding = try await listener.bind(port: listenerPort)
                    bound.set(binding.generation)
                    return await DirectConnectListener.ownershipCheck(
                        port: binding.port,
                        nonce: nonce,
                        within: rebindSeconds
                    )
                }
            } catch {
                ready = false
            }
            let generation = bound.value
            await MainActor.run {
                self?.rebindDidComplete(
                    attempt,
                    listener,
                    port: listenerPort,
                    generation: generation,
                    ready: ready
                )
            }
        }
    }

    private func rebindDidComplete(
        _ attempt: Int,
        _ listener: DirectConnectListener,
        port listenerPort: UInt16,
        generation: Int?,
        ready: Bool
    ) {
        // Only the current attempt settles anything. A stale one (settled by
        // a failure, or replaced by a newer attempt) changes no state here;
        // the socket it bound, if any, is the listener's, which the newer
        // attempt closes before binding, and its generation can no longer
        // be confirmed or rejected.
        guard self.listener === listener, rebinds.end(attempt) else {
            return
        }
        // A bind that already failed (its lifecycle recorded it) is the
        // fault, in the foreground or the background: failure wins over
        // parking.
        if deliverFault(of: listener, "the port could not be re-bound and checked") {
            return
        }
        if isInBackground {
            // Back in the background meanwhile: parked, on purpose, whatever
            // the ownership check said; the next return binds again, and
            // the held answers wait for it.
            listener.lifecycle.beginClose()
            Task {
                await listener.closeListeningSocket()
            }
            return
        }
        // Serving only if this bind is still verifying.
        if ready, let generation, listener.lifecycle.confirm(generation) {
            listenerGeneration = generation
            disarmWatchdog()
            finishResume()
            return
        }
        switch listener.lifecycle.phase {
        case .closing, .closed:
            // Closed on purpose while this attempt ran (a background that
            // came and went): not a failure. This return binds again, so
            // the foreground is never left without a current attempt.
            startRebind(listener, port: listenerPort)
            return
        default:
            break
        }
        // Not owned, or out of time. This attempt was the only owner of the
        // current bind, whether or not that bind ever returned a generation
        // to it: whatever the bind had reached, it fails here, under the
        // lifecycle's lock. Nothing is left binding or verifying with no
        // attempt behind it.
        listener.lifecycle.failUnconfirmed()
        if deliverFault(of: listener, "the port could not be re-bound and checked") {
            return
        }
        switch listener.lifecycle.phase {
        case .closing, .closed:
            // A background closed it in between: the return binds again.
            startRebind(listener, port: listenerPort)
        default:
            // Serving (nothing to do but settle) or stopped (no listener).
            // Neither is held: the gate is open, or shows the fault.
            finishResume()
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
        for entry in held {
            // Answered only for the runtime and store it was asked under.
            guard
                entry.store == websiteDataStore.identifier,
                entry.runtime == runtime
            else {
                entry.reply(nil)
                continue
            }
            entry.reply(answer(for: entry.origin))
        }
        select()
        updatePreference()
        notify()
        onAvailabilityChanged()
    }
}

/// Lets a listener's failure callback, made before the listener exists,
/// name that listener.
private final class DirectListenerBox: @unchecked Sendable {
    weak var listener: DirectConnectListener?
}

/// Remembers, until the next launch, that the listener faulted
/// (docs/plans/remote-direct-ios.md section 9). Only touched when a direct
/// transport is registered.
protocol DirectFaultMarkerStoring: AnyObject {
    /// Records the fault.
    func mark()
    /// Whether a fault was recorded, clearing it. Any stored value counts,
    /// including one this version cannot read.
    func take() -> Bool
}

/// The marker as one key in the App's preferences. It holds a version
/// number and nothing else.
final class UserDefaultsDirectFaultMarker: DirectFaultMarkerStoring {
    static let key = "avibe.direct.listenerFault"
    static let version = 1

    private let defaults: UserDefaults

    init(defaults: UserDefaults) {
        self.defaults = defaults
    }

    func mark() {
        defaults.set(Self.version, forKey: Self.key)
    }

    func take() -> Bool {
        guard defaults.object(forKey: Self.key) != nil else {
            return false
        }
        defaults.removeObject(forKey: Self.key)
        return true
    }
}

/// The generation of a bind made inside a deadline, read after it.
private final class DirectBindingBox: @unchecked Sendable {
    private let lock = NSLock()
    private var generation: Int?

    func set(_ generation: Int) {
        lock.withLock { self.generation = generation }
    }

    var value: Int? {
        lock.withLock { generation }
    }
}

/// The controller's own bookkeeping of a return's re-bind attempts: which
/// one is current. Not an authority over the listener (that is its
/// lifecycle); only over which completion may settle the return.
struct DirectRebindAttempts: Equatable {
    private var issued = 0
    private(set) var current: Int?

    var inFlight: Bool {
        current != nil
    }

    /// Starts a new attempt, which replaces any earlier one.
    mutating func begin() -> Int {
        issued += 1
        current = issued
        return issued
    }

    /// Ends `attempt` if it is the current one. `false` for a stale one,
    /// which then changes nothing.
    mutating func end(_ attempt: Int) -> Bool {
        guard current == attempt else {
            return false
        }
        current = nil
        return true
    }

    /// The return was settled another way (a failure, a retirement).
    mutating func abandon() {
        current = nil
    }
}
