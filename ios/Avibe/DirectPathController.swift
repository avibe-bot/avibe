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
    /// For diagnosis only, and only for the selected instance's host: the
    /// path the App's own direct connection runs on, and its last attempts.
    /// Families and counts, never an address. Nil when there is nothing.
    var diagnostics: DirectPathDiagnostics? = nil
    /// The kind of path the App's own direct connection runs on while
    /// `preferred` is `direct` for this host: through the relay, or straight
    /// to an IPv4 or IPv6 address. Nil when not direct, or not known. A
    /// change of it advances `sequence`.
    var path: DirectPathKind? = nil
    /// For display, with `path`: whether the path's far end is on a local
    /// network, and its round-trip time. Read when the status is asked for;
    /// a change of it is not a change of the status.
    var selected: DirectSelectedPath? = nil
    /// For diagnosis: how often the transport checked its path again, and
    /// why, since the instance was selected. Counts only.
    var counts: DirectPathCounts = .zero
    /// For diagnosis: the last few CONNECTs' races, oldest first.
    var races: [DirectRaceRecord] = []
    /// For diagnosis: the last few times committed direct relays were closed
    /// because their path was lost, oldest first.
    var losses: [DirectLossRecord] = []
    /// For diagnosis, for a host that has a route: the last few times the
    /// App's own listener could not be bound or checked, oldest first.
    var listenerFaults: [DirectListenerFailure] = []

    static let initial = DirectPathStatus(preferred: .tunnel, sequence: 0, closure: 0)

    /// Diagnostics are not part of what a status says: two statuses with
    /// the same preference, path, sequence, and closure are the same.
    static func == (lhs: DirectPathStatus, rhs: DirectPathStatus) -> Bool {
        lhs.preferred == rhs.preferred && lhs.sequence == rhs.sequence
            && lhs.closure == rhs.closure && lhs.path == rhs.path
    }

    /// What `direct.transportChanged` carries: the status, with `path` when
    /// it is known, and no diagnostics.
    var eventObject: [String: Any] {
        var object: [String: Any] = [
            "preferred": preferred.rawValue,
            "sequence": sequence,
            "closure": closure,
        ]
        if let path {
            object["path"] = path.rawValue
        }
        return object
    }

    /// What `direct.status()` answers: the status, the selected path's class
    /// (`path_scope`, `local` or `global`) and round-trip time (`rtt_ms`)
    /// when known, and the diagnostics if any.
    var jsonObject: [String: Any] {
        var object = eventObject
        if let path, let selected, selected.kind == path {
            if let local = selected.local {
                object["path_scope"] = local ? "local" : "global"
            }
            if let rttMs = selected.rttMs {
                object["rtt_ms"] = rttMs
            }
        }
        if let diagnostics {
            if path == nil {
                object["path"] = NSNull()
            }
            object["attempts"] = diagnostics.attempts.map(\.jsonObject)
        }
        if counts != .zero {
            object["counts"] = counts.jsonObject
        }
        if !races.isEmpty {
            object["connections"] = races.map(\.jsonObject)
        }
        if !losses.isEmpty {
            object["losses"] = losses.map(\.jsonObject)
        }
        if !listenerFaults.isEmpty {
            object["listener_faults"] = listenerFaults.map(\.jsonObject)
        }
        return object
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
                    "detail": eventObject,
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

/// How a page's request to move to the direct path ended
/// (`direct.handover`).
enum DirectHandoverOutcome: Equatable, Sendable {
    /// The quiet public relays for the page's host were closed; busy ones
    /// were kept.
    case done(closed: Int, kept: Int)
    /// Nothing was closed.
    case skipped(DirectHandoverSkip)

    var jsonObject: [String: Any] {
        switch self {
        case .done(let closed, let kept):
            return ["closed": closed, "kept": kept]
        case .skipped(let reason):
            return ["closed": 0, "kept": 0, "skipped": reason.rawValue]
        }
    }
}

enum DirectHandoverSkip: String, Equatable, Sendable {
    /// The App's direct path is not usable for this host now.
    case notUsable = "not_usable"
    /// Another live document of the same store has this host loaded, or
    /// the App cannot tell.
    case otherDocuments = "other_documents"
}

/// What else of the calling WebView's data store has the instance loaded
/// when a page asks to hand over (`DirectWebViewRegistry`). Anything but
/// `sole` refuses the hand-over; which one it was goes into the hand-over's
/// record (`DirectPathUse.Handover`), in these words.
enum DirectOtherDocument: String, Equatable, Sendable {
    /// Nothing: the caller is the only live document of its store with the
    /// host loaded.
    case sole = "none"
    /// A voice take's hidden WebView.
    case voice
    /// A window a page of the runtime opened.
    case child
    /// A runtime's own page: an earlier runtime not freed yet, or the page
    /// that opened the calling window.
    case runtime
    /// A WebView of the store with no address yet, whatever it is: the App
    /// cannot tell what it will load.
    case unloaded
    /// The caller itself is not one of the App's listed WebViews.
    case unknown
}

/// Answers `direct.handover` for an instance origin. `other` is what else
/// of the calling WebView's data store has that host loaded.
typealias DirectHandoverProvider = @MainActor (
    WebOrigin,
    _ other: DirectOtherDocument,
    @escaping @MainActor (DirectHandoverOutcome) -> Void
) -> Void

/// The only writer of `proxyConfigurations`. A store's route lists exact
/// instance hosts and points at the App's one listener. Its host list only
/// grows while the store lives, because WebKit does not reliably apply a
/// cleared or changed route while a connection of the host lives
/// (docs/plans/remote-direct-ios.md sections 2 and 5). The one exception is
/// `remove`: a host the listener could not carry at all, which therefore has
/// no live connection through it, leaves the route for the rest of the
/// process (the next connection follows the removal: section 2,
/// `remove-close`).
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
    /// A host and the WebKit policy currently requested for it on this store.
    private struct RoutedHost {
        let name: String
        let allowsFailover: Bool
    }
    private var hostsByStore: [UUID: [RoutedHost]] = [:]
    /// The port each store's route was last written with.
    private var portByStore: [UUID: UInt16] = [:]
    /// Hosts a store loaded without a route and that get none there later:
    /// the listener was failed, closed or never started when they loaded.
    /// (A host that loaded while the first bind was still under way is not
    /// one of them: `DirectPathController.lateRoute`.)
    private var excluded: Set<StoreHost> = []

    /// The hosts `store`'s route lists now. Unlike `allowedHosts`, a host
    /// taken off the route (`remove`) is not among them, nor is a host only
    /// another store routes.
    func routedHosts(store: UUID) -> [String] {
        (hostsByStore[store] ?? []).map(\.name)
    }

    func routes(_ host: String, store: UUID) -> Bool {
        hostsByStore[store]?.contains { $0.name == host } == true
    }

    func allowsFailover(_ host: String, store: UUID) -> Bool {
        hostsByStore[store]?.first { $0.name == host }?.allowsFailover == true
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
    func add(
        _ host: String, store: UUID, dataStore: WKWebsiteDataStore, port: UInt16,
        allowsFailover: Bool = true
    ) -> Bool {
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
        let hosts = (hostsByStore[store] ?? []) + [RoutedHost(name: host, allowsFailover: allowsFailover)]
        write(hosts, store: store, dataStore: dataStore, port: port)
        return true
    }

    /// Changes only an already-routed host. The caller drains its connections
    /// before asking; reapplying a policy does not rewrite the store.
    @discardableResult
    func rewrite(_ host: String, store: UUID, dataStore: WKWebsiteDataStore, port: UInt16, allowsFailover: Bool) -> Bool {
        guard var hosts = hostsByStore[store], let index = hosts.firstIndex(where: { $0.name == host }) else {
            return false
        }
        if hosts[index].allowsFailover != allowsFailover {
            hosts[index] = RoutedHost(name: host, allowsFailover: allowsFailover)
            write(hosts, store: store, dataStore: dataStore, port: port)
        }
        return true
    }

    /// Takes `host` out of `store`'s route and excludes it there for the rest
    /// of the process: its loads go by WebKit's own path. Only for a host
    /// that has no live connection through the listener (it carried none).
    /// `false` when the host was not routed on that store.
    @discardableResult
    func remove(_ host: String, store: UUID, dataStore: WKWebsiteDataStore) -> Bool {
        guard let hosts = hostsByStore[store], hosts.contains(where: { $0.name == host }) else {
            return false
        }
        let remaining = hosts.filter { $0.name != host }
        if let port = portByStore[store] {
            write(remaining, store: store, dataStore: dataStore, port: port)
        } else {
            hostsByStore[store] = remaining
            dataStore.proxyConfigurations = []
        }
        excluded.insert(StoreHost(store: store, host: host))
        return true
    }

    private func write(_ hosts: [RoutedHost], store: UUID, dataStore: WKWebsiteDataStore, port: UInt16) {
        hostsByStore[store] = hosts
        portByStore[store] = port
        dataStore.proxyConfigurations = [false, true].compactMap { allowsFailover in
            let domains = hosts.filter { $0.allowsFailover == allowsFailover }.map(\.name)
            guard !domains.isEmpty else { return nil }
            return Self.route(port: port, hosts: domains, allowsFailover: allowsFailover)
        }
    }

    private static func route(port: UInt16, hosts: [String], allowsFailover: Bool) -> ProxyConfiguration {
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
        route.allowFailover = allowsFailover
        return route
    }
}

/// Proposed bounds for the controller (docs/plans/remote-direct-ios.md
/// section 9). Not measurements.
struct DirectPathTiming: Sendable {
    /// How long a runtime's first load waits for the listener and its route:
    /// a 「仅直连和中继」 host (`strictOpenSeconds`), and any other host
    /// (`listenerWaitSeconds`). An ordinary host whose wait runs out loads on
    /// WebKit's normal path and gets its route when the bind is confirmed
    /// (`lateRoute`). The ordinary wait is 2 s since 2026-11-27 (1 s before):
    /// a cold start's first bind can take longer than a second, and every
    /// load that goes first starts on the public path.
    let strictOpenSeconds: TimeInterval
    let listenerWaitSeconds: TimeInterval
    /// How long the return to the foreground may take to re-bind and check
    /// the listener's port.
    let rebindSeconds: TimeInterval
    /// The foreground watchdog: how long, in one stay in the foreground,
    /// the listener may be neither serving nor failed (binding, checking,
    /// closing, closed and about to be bound again) before that is a
    /// failure of the listener. One fixed bound for the whole stay, whatever
    /// attempts happen inside it. Longer than a first bind or a re-bind
    /// with its check is allowed to take, so it only ends what those bounds
    /// did not. A conservative proposal for the internal trial.
    let foregroundSeconds: TimeInterval
    /// How long after a failed attempt the listener's port is bound and
    /// checked again, attempt by attempt; the last one repeats for as long
    /// as the App stays in the foreground.
    let retrySeconds: [TimeInterval]
    /// In the background (`backgroundLoadCheck`): how long the listener's
    /// own socket may take to be closed after the App left the foreground
    /// (a close takes up to a second, and an attempt that was under way
    /// ends first), and how long the port may take to accept or refuse one
    /// connection attempt.
    let ownCloseSeconds: TimeInterval
    let portProbeSeconds: TimeInterval
    /// A change of a host's 「仅直连和中继」 policy closes its connections.
    /// How long that waits for the host's tunnel connections to carry
    /// nothing for a second, so that what the page is loading right then
    /// (it has just opened) is not cut; what is still busy after it is
    /// closed anyway.
    let cutoverQuietSeconds: TimeInterval

    init(
        listenerWaitSeconds: TimeInterval,
        rebindSeconds: TimeInterval,
        foregroundSeconds: TimeInterval = 5,
        strictOpenSeconds: TimeInterval = 10,
        retrySeconds: [TimeInterval] = [0.25, 0.5, 1, 2, 5],
        ownCloseSeconds: TimeInterval = 3,
        portProbeSeconds: TimeInterval = 0.3,
        cutoverQuietSeconds: TimeInterval = 5
    ) {
        self.cutoverQuietSeconds = cutoverQuietSeconds
        self.strictOpenSeconds = strictOpenSeconds
        self.listenerWaitSeconds = listenerWaitSeconds
        self.rebindSeconds = rebindSeconds
        self.foregroundSeconds = foregroundSeconds
        self.retrySeconds = retrySeconds
        self.ownCloseSeconds = ownCloseSeconds
        self.portProbeSeconds = portProbeSeconds
    }

    static let standard = DirectPathTiming(listenerWaitSeconds: 2, rebindSeconds: 2)
}

/// One failed attempt to bind the listener's port or to check that the
/// listener on it is this App's; or, as `late`, a first bind that was
/// confirmed after a host's first load. Numbers only: no wall time,
/// address, port or identifier.
struct DirectListenerFailure: Equatable, Sendable {
    enum Step: String, Sendable {
        /// The port could not be bound.
        case bind
        /// The bind was left waiting by the system.
        case waiting
        /// The port was bound, and this listener did not answer the
        /// ownership check on it in what was left of the attempt's bound.
        case ownership
        /// The attempt ran out of time before its check could begin.
        case deadline
        /// The listening socket failed while it was serving.
        case socket
        /// Not a failure: the first bind was confirmed only after a host's
        /// first load had gone ahead without a route. `code` is how long
        /// that bind took, in milliseconds.
        case late
    }

    let step: Step
    /// The POSIX or Network error number, when there is one. For `late`,
    /// how long the first bind took, in milliseconds.
    let code: Int32?
    /// How long the App had been away before the stay in the foreground
    /// this attempt belongs to; `nil` for a failure with no background
    /// before it.
    let backgroundSeconds: Int?
    let at: ContinuousClock.Instant

    /// Something else may hold the port (`DirectListenerFailureCause`).
    var portMayBeForeign: Bool {
        DirectListenerFailureCause(step, code: code).portMayBeForeign
    }

    /// The most seconds away that are reported; longer reads as this.
    static let maxBackgroundSeconds = Int(Int32.max)

    /// For `direct.status()` and the rendezvous request: the step and the
    /// numbers, nothing else.
    var jsonObject: [String: Any] {
        var object: [String: Any] = ["step": step.rawValue]
        if let code {
            object["code"] = Int(code)
        }
        if let backgroundSeconds {
            object["background_seconds"] = min(max(backgroundSeconds, 0), Self.maxBackgroundSeconds)
        }
        return object
    }
}

/// A listener that a route points at is not serving, in the foreground, and
/// its port is being bound again.
struct DirectListenerRecovery: Equatable, Sendable {
    /// When this recovery began: the return to the foreground, or the
    /// failure if the listener failed while the App was in front. It starts
    /// again at each return.
    let since: ContinuousClock.Instant
    /// The last attempt that failed in this recovery; `nil` while the first
    /// one is still running.
    let lastFailure: DirectListenerFailure?
    /// Something else may hold the port: loads of routed hosts are refused
    /// (`DirectLoadGate.fault`), because WebKit would send them to whatever
    /// answers there. Kept across a return to the foreground, until an
    /// attempt serves or finds the port merely closed.
    let portMayBeForeign: Bool
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
    /// A bind, its ownership check, or the listening socket failed, and a
    /// route points at the listener: direct is not authorized and no route
    /// is written until the same port is bound and checked again, which is
    /// tried for as long as the App is in the foreground
    /// (docs/plans/remote-direct-ios.md section 9). A host that has a route
    /// still points at that port, and nothing in the process can make
    /// WebKit leave it: while the port is closed WebKit fails over to its
    /// normal path; while something else may hold it, the host's loads are
    /// refused. A host without a route keeps WebKit's normal path and is not
    /// interrupted. (A listener no route points at is not kept when it
    /// fails: it is stopped, and the next open makes another.)
    case failed
}

/// What a load made while the App is in the background would meet
/// (`DirectPathController.backgroundLoadCheck`).
enum DirectBackgroundCheck: Equatable, Sendable {
    /// The load may go by this gate: the port refused, or the App is back
    /// in front. Never `.fault`: that is `foreign`.
    case gate(DirectLoadGate)
    /// Something else holds the port: it accepted, or the synchronous gate
    /// already says so.
    case foreign
    /// Nothing could be told.
    case undecided(Reason)

    enum Reason: String, Sendable {
        /// The port gave no answer within the bound (or the attempt failed
        /// some other way).
        case noAnswer = "no_answer"
        /// The App's own listening socket was not closed within its bound.
        case ownSocketOpen = "own_socket_open"
        /// The App came to the front and left again while the question was
        /// open.
        case stayChanged = "stay_changed"
    }

    /// The answer `loadGate` alone gives: `foreign` where it is `fault`.
    init(_ gate: DirectLoadGate) {
        self = gate == .fault ? .foreign : .gate(gate)
    }
}

/// Whether the shell may create or reload a runtime's WebView now. Derived,
/// each time it is asked, from the current store's route and the listener's
/// lifecycle; nothing keeps a copy.
enum DirectLoadGate: Equatable, Sendable {
    /// The host has no route on the current store (WebKit's normal path),
    /// or its route points at a listener that serves, or at one whose port
    /// is closed after a failed attempt: WebKit then fails over to its
    /// normal path by itself, and the port is bound again underneath.
    case open
    /// The host has a route, and the listener is closed for the background
    /// or being bound again on a return whose first attempt has not ended.
    /// Nothing is created or reloaded against that port until the return
    /// settles. In the foreground this state is bounded: the first attempt
    /// ends within its own bound or the watchdog's.
    case held
    /// The host has a route, and something else may hold the listener's
    /// port: a bind found it in use. Nothing may be loaded against that
    /// port by anyone,
    /// for as long as that lasts, in the foreground or the background:
    /// WebKit would send it to whatever holds the port. No wait on this
    /// value may end in a load. It is not terminal: the port is bound again
    /// attempt after attempt, and the gate leaves this value when an
    /// attempt serves or finds the port merely closed.
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
    /// The account of which leg carried the pages' connections, sent to the
    /// instance with the rendezvous request.
    private let pathUse: DirectPathUse
    private let onStatusChanged: () -> Void
    /// Called when a load gate or `listenerRecovery` may have changed: the
    /// listener was closed for the background, a return was settled, an
    /// attempt to bind the listener's port again failed, or one succeeded.
    private let onAvailabilityChanged: () -> Void
    /// Whether the shell is still loading this runtime's document (its
    /// navigation has started and not finished). Asked just before a
    /// cut-over closes the host's tunnel connections.
    private let isLoadingDocument: (MobileRuntimeContext) -> Bool
    /// A cut-over closed connections of this runtime's page while its
    /// document was still loading: the shell loads the page again.
    private let onLoadingPageCut: (MobileRuntimeContext) -> Void
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
    /// The bind generation of the last failure this controller has heard
    /// of. A failure is the lifecycle's `failed` phase, which fences
    /// everything at once, where it is decided; this makes what follows
    /// from it here (its record, the next attempt) happen once.
    private var heardFailure: Int?
    /// Whether the last failed attempt on a listener a route points at
    /// found that something else may hold its port. It outlives the stay:
    /// a return does not make the port ours, only an attempt that serves
    /// or finds it merely closed does.
    private var portLastKnownForeign = false
    /// This stay's recovery, while a listener a route points at does not
    /// serve in the foreground (`listenerRecovery`).
    private struct Recovery {
        let since: ContinuousClock.Instant
        var lastFailure: DirectListenerFailure?
    }

    private var recovery: Recovery?
    /// The last few failures of the listener in this process, oldest first,
    /// and a first bind confirmed late (`late`, which is no failure and
    /// starts no recovery). Numbers only; for `direct.status()` and the
    /// rendezvous request.
    private(set) var listenerFailures: [DirectListenerFailure] = []
    static let listenerFailureLimit = 5
    /// How many attempts this stay's recovery has scheduled, and the one
    /// wake-up for the next.
    private var retriesScheduled = 0
    private var retryWake: Task<Void, Never>?
    /// When the App last left the foreground, and how long it had been away
    /// when it came back to this stay.
    private var leftForegroundAt: ContinuousClock.Instant?
    private var awaySeconds: Int?
    /// The one wake-up scheduled for the foreground watchdog, and the
    /// episode it is for.
    private var watchdog: (episode: UUID, wake: Task<Void, Never>)?

    /// The listener, as the controller sees it: derived from the listener's
    /// lifecycle, the one authority (`DirectListenerLifecycle`).
    var listenerState: DirectListenerState {
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
            // Failed, and an attempt already on its way to bind again.
            return rebinds.inFlight ? .rebinding : .failed
        case .stopped:
            return .none
        }
    }

    /// The target the transport is working for, if any. Read only: the
    /// controller alone selects and deselects.
    var selectedTarget: DirectTarget? {
        target
    }

    private var runtime: MobileRuntimeContext?
    private var target: DirectTarget?
    private var selection = 0
    private var acceptedGeneration: String?
    private var replacedGenerations: Set<String> = []
    /// The transport reported the accepted generation usable, and nothing
    /// has suspended that since.
    private var requestedStrict: Set<String> = []
    // WebKit may own a connection outside the listener: an unmanaged load, or
    // a timed-out CONNECT still using a failover-enabled route during cutover.
    private var hostsNeedingColdStore: Set<String> = []
    private var armed: Set<String> = []
    /// The control plane's host (avibe.bot). The only host the listener
    /// carries without a route of its own (`carriesControlPlane`).
    private let controlPlaneHost: String?
    private var rewriting: Set<String> = []
    /// Hosts whose policy is changing: from the wait for their tunnel
    /// connections to go quiet until the drain and rewrite are done.
    private var cuttingOver: Set<String> = []
    private var policyGeneration: [String: UInt64] = [:]
    private var strictFailures: [String: UInt64] = [:]
    private var unlinkedAnswers: [String: UInt64] = [:]
    private var lossWake: Task<Void, Never>?
    /// The transport reports a usable path, and the probation rule has
    /// adopted its link: only then do plans, the preference and the status
    /// speak of a direct path (`transportUsable`, `probation`).
    private var isUsable = false
    /// The transport's own word: a path to the instance answers.
    private var transportUsable = false
    /// Probation before a direct path is adopted
    /// (docs/plans/remote-direct-probation.md).
    private var probation: DirectProbationGuard
    private let probationStore: DirectProbationStoring
    private let probationClock: DirectProbationClock
    /// The link the transport has up, as the rule numbers it, and whose it
    /// is. A link begins at a `usable` report while none is up, goes on
    /// through a `verifying`, and ends at a loss, the background, or the end
    /// of the selection.
    private var probationLink: (number: Int, key: String, host: String)?
    private var probationLinks = 0
    /// The kind of path that link was last seen on, for the report: at its
    /// loss the transport no longer names one.
    private var probationPath = DirectPathUse.Path.none
    /// What the store holds, so that it is written only when it changes.
    private var storedProbation: [String: DirectProbationRecord] = [:]
    /// The wait of a link that is not adopted yet, and the probe it is
    /// asked once a minute meanwhile.
    private var probationWake: Task<Void, Never>?
    private var probationProbe: Task<Void, Never>?
    /// How often a waiting link is asked whether it still answers.
    private static let probationProbeMs: Int64 = 60_000
    /// The kind of path the usable transport's connection runs on, as last
    /// reported; nil when none is usable or the transport cannot tell.
    private var currentPath: DirectPathKind?
    /// The path of the last plan that had a direct candidate, as it was
    /// named for `DirectPathUse` then: connections closed for that path
    /// are counted under the name they were planned on. Read when the close
    /// is decided, never after a wait.
    private var plannedPath: (kind: DirectPathKind, use: DirectPathUse.Path)?

    /// A plan's own facts for `DirectPathUse`, read when the plan is made.
    private struct PlanUse: Sendable {
        let scope: DirectPathUse.Scope
        let path: DirectPathUse.Path
        let strict: Bool
        let tunnelOnly: Bool
    }

    /// The record of a plan's last race, kept from the race's end until the
    /// listener says what became of the CONNECT.
    private final class PlanRace: @unchecked Sendable {
        private let lock = NSLock()
        private var record: DirectRaceRecord?

        var last: DirectRaceRecord? {
            get { lock.withLock { record } }
            set { lock.withLock { record = newValue } }
        }
    }

    /// Whose connections a count is of: this account's store and `host`.
    private func useScope(_ host: String) -> DirectPathUse.Scope {
        DirectPathUse.Scope(store: websiteDataStore.identifier, host: host)
    }

    /// The direct path new connections to `host` prefer now, in
    /// `DirectPathUse`'s words: `none` unless they prefer one.
    private func pathInUse(for host: String) -> DirectPathUse.Path {
        guard preferred == .direct, host == target?.authority.host, let currentPath else {
            return .none
        }
        let selected = transport.pathDiagnostics.selected
        return DirectPathUse.Path(kind: currentPath, local: selected?.kind == currentPath ? selected?.local : nil)
    }
    /// A sticky plan's direct candidate missed its bound: plans hedge again
    /// until a direct candidate wins, or the path changes.
    private var stickyDropped = false
    /// Counts the drops of stickiness. A direct win restores stickiness only
    /// if its plan was made after the last drop, so a result that arrives
    /// late cannot undo a newer one.
    private var stickyDropSerial = 0
    /// The last few CONNECTs' races, oldest first; for diagnosis.
    private var races: [DirectRaceRecord] = []
    static let raceLimit = 5
    /// The last few closes of committed direct relays for a lost path.
    private var losses: [DirectLossRecord] = []
    private var pathGeneration = 0
    private var accountEpoch = 0
    private var isInBackground: Bool

    private var preferred: DirectPathStatus.Preference = .tunnel
    private var sequence = 0
    private var closures: [String: Int] = [:]
    /// Hosts whose relays the background closed: published on the return,
    /// once the listener is ready again (or the return's first attempt has
    /// failed).
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
    /// Relay closes decided on the main actor that the listener has not
    /// issued yet: the relays not yet cancelled, their authority not yet
    /// ended. Bookkeeping only; the claims decided what each close counts.
    private var closesInFlight = 0
    /// The current re-bind attempt's successful completion, kept while a
    /// close is in flight (`rebindDidComplete`). The attempt stays current
    /// and its episode open meanwhile.
    private var completionAwaitingCloses: DirectRebindCompletion?
    /// Route preparations under way; the listener is kept while any is.
    private var preparations = 0
    // The runtime whose first strict route was not ready. Unlike an excluded
    // host it may prepare again: its gate has not admitted a WebKit load.
    private struct FirstRouteWait {
        let runtime: MobileRuntimeContext
        let store: UUID
        let since = ContinuousClock.now
        var lastFailure: DirectListenerFailure?
        var retries = 0
    }
    private var firstRouteWait: FirstRouteWait?
    private var firstRouteWake: Task<Void, Never>?
    private var firstRouteRetry: Task<Void, Never>?
    /// An ordinary host's runtime that loaded without a route because the
    /// listener's first bind was still under way when its wait ran out. The
    /// listener is kept, and the host gets its route when the bind is
    /// confirmed. WebKit uses a route added then from its next new
    /// connection to the host, not on the connection it already has
    /// (docs/plans/remote-direct-ios.md section 2).
    private struct LateRoute {
        let runtime: MobileRuntimeContext
        let store: UUID
        let host: String
    }
    private var lateRoute: LateRoute?
    private var lateRouteTask: Task<Void, Never>?
    /// When the first bind of the listener that is there now was started.
    private var firstBindStarted: ContinuousClock.Instant?
    /// Listeners started in this process: only the first one's bind is a
    /// cold start's (`ColdStartTimings`).
    private var listenersStarted = 0
    /// Hosts whose route was added after their first load, until one of
    /// their connections reaches the listener. Their documents are told
    /// `tunnel` meanwhile: nothing of theirs passes through the listener
    /// yet, so a hand-over could move nothing.
    private var awaitingFirstConnect: Set<String> = []
    /// What the listener did with each routed host's connections on each
    /// store in this process: how many it carried (on either leg) and how
    /// many it gave up. A host it gave up on and never carried may be taken
    /// off the route (`bypassListener`).
    private struct Carry {
        var carried = 0
        var failed = 0
    }
    private var carries: [String: Carry] = [:]

    private static func carryKey(_ host: String, _ store: UUID) -> String {
        store.uuidString + " " + host
    }

    private func noteCarry(host: String, store: UUID, carried: Bool) {
        let key = Self.carryKey(host, store)
        var carry = carries[key] ?? Carry()
        if carried { carry.carried += 1 } else { carry.failed += 1 }
        carries[key] = carry
    }

    /// The listener gave up every connection of `url`'s host on the current
    /// store, at least one, and carried none: neither leg could reach the
    /// instance from here (for example a VPN whose addresses the public leg
    /// refuses, or a network that drops its connections), while WebKit's own
    /// path may.
    func listenerCannotCarry(forInstanceURL url: URL) -> Bool {
        guard let host = DirectAuthority(instanceURL: url)?.host else { return false }
        let store = websiteDataStore.identifier
        guard routeOwner.routes(host, store: store), let carry = carries[Self.carryKey(host, store)] else {
            return false
        }
        return carry.failed > 0 && carry.carried == 0
    }

    /// Takes `url`'s host off the current store's route for the rest of the
    /// process, so its loads go by WebKit's own path, as Safari's do. Only
    /// for an automatic host (a 「仅直连和中继」 one must go back to
    /// automatic first) that the listener has carried nothing for: no
    /// connection of it lives through the listener, so the next one follows
    /// the removal. The next launch tries the listener again. `false` when
    /// nothing changed.
    ///
    /// `force` (the second 重试 on the failure screen): also when the
    /// listener carried some of the host's connections earlier. One still
    /// alive keeps its way through the listener; every new one goes by
    /// WebKit's own path.
    @discardableResult
    func bypassListener(forInstanceURL url: URL, force: Bool = false) -> Bool {
        guard let host = DirectAuthority(instanceURL: url)?.host, !needsListener(host) else { return false }
        let store = websiteDataStore.identifier
        if !force, let carry = carries[Self.carryKey(host, store)], carry.carried > 0 {
            return false
        }
        if lateRoute?.host == host, lateRoute?.store == store {
            lateRoute = nil
            lateRouteTask?.cancel()
            lateRouteTask = nil
        }
        guard routeOwner.remove(host, store: store, dataStore: websiteDataStore.dataStore) else {
            routeOwner.exclude(host, store: store)
            return false
        }
        armed.remove(host)
        awaitingFirstConnect.remove(host)
        Self.logger.notice("Direct path: a host the listener could not carry loads by WebKit's own path")
        sequence += 1
        notify()
        onAvailabilityChanged()
        return true
    }

    init(
        websiteDataStore: AccountWebsiteDataStore,
        transport: DirectTransporting,
        isCurrentRuntime: @escaping (MobileRuntimeContext) -> Bool,
        publicBackend: DirectPublicBackend = DirectPublicBackend(),
        limits: DirectConnectLimits = .standard,
        timing: DirectPathTiming = .standard,
        notificationCenter: NotificationCenter = .default,
        pathUse: DirectPathUse = .shared,
        probationStore: DirectProbationStoring,
        probationClock: DirectProbationClock = .system,
        controlPlaneHost: String? = MobileControlPlaneConfiguration().baseURL.host,
        onStatusChanged: @escaping () -> Void,
        onAvailabilityChanged: @escaping () -> Void = {},
        isLoadingDocument: @escaping (MobileRuntimeContext) -> Bool,
        onLoadingPageCut: @escaping (MobileRuntimeContext) -> Void
    ) {
        self.isLoadingDocument = isLoadingDocument
        self.onLoadingPageCut = onLoadingPageCut
        self.pathUse = pathUse
        self.probationStore = probationStore
        self.probationClock = probationClock
        self.probation = DirectProbationGuard(stored: probationStore.load())
        self.storedProbation = probation.stored
        self.controlPlaneHost = controlPlaneHost?.lowercased()
        self.websiteDataStore = websiteDataStore
        self.transport = transport
        self.isCurrentRuntime = isCurrentRuntime
        self.publicBackend = publicBackend
        self.limits = limits
        self.timing = timing
        self.notificationCenter = notificationCenter
        // The App can be launched into the background.
        self.isInBackground = UIApplication.shared.applicationState == .background
        if !isInBackground {
            probation.foreground()
        }
        self.onStatusChanged = onStatusChanged
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
        let selected = origin.host == target?.authority.host
        // A host whose route came after its first load: `direct` is said to
        // its documents only once one of its connections has reached the
        // listener. The diagnostics are shown meanwhile.
        let direct = preferred == .direct && selected && !awaitingFirstConnect.contains(origin.host)
        var diagnostics: DirectPathDiagnostics?
        var selectedPath: DirectSelectedPath?
        var counts = DirectPathCounts.zero
        if selected {
            let read = transport.pathDiagnostics
            selectedPath = read.selected
            counts = transport.pathCounts
            // The current path only while new connections prefer it.
            let shown = DirectPathDiagnostics(
                current: direct ? currentPath : nil,
                attempts: read.attempts
            )
            diagnostics = shown == .empty ? nil : shown
        }
        return DirectPathStatus(
            preferred: direct ? .direct : .tunnel,
            sequence: sequence,
            closure: closures[origin.host] ?? 0,
            diagnostics: diagnostics,
            path: direct ? currentPath : nil,
            selected: direct ? selectedPath : nil,
            counts: counts,
            races: selected ? races : [],
            losses: selected ? losses : [],
            // The listener is the App's one, not a host's: its failures are
            // told to any host that has a route to it.
            listenerFaults: routeOwner.routes(origin.host, store: websiteDataStore.identifier)
                ? listenerFailures
                : []
        )
    }

    /// Quiet time after which a public relay counts as idle for a hand-over.
    static let handoverQuietSeconds: TimeInterval = 1
    /// How long a hand-over waits for the host's public relays to go quiet.
    static let handoverWaitSeconds: TimeInterval = 3

    /// A page asks to move its new connections to the direct path
    /// (`direct.handover`): while the path is usable for `origin`'s host, the
    /// committed public relays for that host that carried nothing for
    /// `handoverQuietSeconds` (waiting up to `handoverWaitSeconds` for them to
    /// get there) are closed, so WebKit's next connection takes
    /// the route to the direct backend. Busy relays are kept; nothing is
    /// replayed. Only when the caller is the sole live document of the store
    /// with that host loaded (`other == .sole`): a relay of another
    /// document's connection is never closed for this one.
    ///
    /// Each ask is counted in its minute when it is made, and recorded once
    /// it is answered, with how it ended (`DirectPathUse.Handover`).
    func handover(
        for origin: WebOrigin,
        other: DirectOtherDocument,
        reply: @escaping @MainActor (DirectHandoverOutcome) -> Void
    ) {
        settleRecordedFault()
        let asked = ContinuousClock.now
        let scope = useScope(origin.host)
        let path = pathInUse(for: origin.host)
        let pathUse = pathUse
        // An ask is counted when it is made and recorded when it is
        // answered, with the path present when it was asked.
        let answer: @MainActor (DirectHandoverOutcome, DirectOtherDocument) -> Void = { outcome, other in
            pathUse.handoverAnswered(
                outcome, refusedBy: other, took: asked.duration(to: .now), scope: scope, path: path
            )
            reply(outcome)
        }
        guard
            !isResuming,
            preferred == .direct,
            let target,
            origin.host == target.authority.host,
            directAuthorized(for: target.authority),
            let listener
        else {
            pathUse.handover(refused: .notUsable, scope: scope, path: path)
            answer(.skipped(.notUsable), .sole)
            return
        }
        guard other == .sole else {
            pathUse.handover(refused: .otherDocuments, scope: scope, path: path)
            answer(.skipped(.otherDocuments), other)
            return
        }
        pathUse.handover(refused: nil, scope: scope, path: path)
        let host = origin.host
        Task {
            let result = await listener.closeQuietRelays(
                host: host,
                backend: .publicOrigin,
                quietSeconds: Self.handoverQuietSeconds,
                waitSeconds: Self.handoverWaitSeconds
            )
            Self.logger.info(
                "Direct hand-over: closed \(result.closed, privacy: .public), kept \(result.kept, privacy: .public) public relays"
            )
            await MainActor.run {
                answer(.done(closed: result.closed, kept: result.kept), .sole)
            }
        }
    }

    /// Answers `direct.status()` for `origin`. While the App returns from the
    /// background the answer is held until the listener is ready again and
    /// the pending closure is published, or until the return's first
    /// attempt has failed: from then on the answer is the public path, at
    /// once, while the port is bound again underneath.
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
            reply(currentStatus(for: origin))
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

    // MARK: Runtime and account

    /// Prepares the route for `runtime`'s host before its first load: binds
    /// the listener if this is the first route of the process, checks it,
    /// and adds the host to the current store's route. Waits at most the
    /// listener bound. If the listener is not ready by then, or the host
    /// cannot be added, the runtime loads on WebKit's normal path. A host
    /// whose wait ran out while the first bind was still under way gets its
    /// route when that bind is confirmed (`lateRoute`); any other is never
    /// added to this store later. `stillWanted` is asked after
    /// every wait: a superseded open adds nothing and, if no route was ever
    /// written, leaves no listener behind. A host that already has a route
    /// is left as it is, with no wait: whether its runtime may load now is
    /// `loadGate(for:)`, asked by the caller afterwards.
    func prepareRoute(
        for runtime: MobileRuntimeContext,
        stillWanted: @escaping () -> Bool
    ) async {
        await prepareRoute(for: runtime, late: false, stillWanted: stillWanted)
    }

    /// `late`: for a host whose first load has already gone ahead
    /// (`addLateRoute`). Only a route this call itself writes is a late one.
    private func prepareRoute(
        for runtime: MobileRuntimeContext,
        late: Bool,
        stillWanted: @escaping () -> Bool
    ) async {
        preparations += 1
        await addRoute(for: runtime, late: late, stillWanted: stillWanted)
        preparations -= 1
        // A listener no route points at, and no open is waiting for, has
        // nothing to do.
        if routeOwner.allowedHosts.isEmpty, preparations == 0, listener != nil, !firstRouteWanted, lateRoute == nil {
            await stopListener()
        }
        scheduleFirstRouteRetry()
    }

    private func addRoute(
        for runtime: MobileRuntimeContext,
        late: Bool,
        stillWanted: () -> Bool
    ) async {
        guard let authority = DirectAuthority(instanceURL: runtime.instance.url) else {
            return
        }
        let store = websiteDataStore.identifier
        if routeOwner.routes(authority.host, store: store) {
            return
        }
        guard !routeOwner.isExcluded(authority.host, store: store) else {
            return
        }
        if needsListener(authority.host), stillWanted(),
           (firstRouteWait?.runtime != runtime || firstRouteWait?.store != store) {
            cancelFirstRouteWait()
            firstRouteWait = FirstRouteWait(runtime: runtime, store: store)
        }
        let ready = await ensureListener(for: authority.host)
        guard stillWanted(), websiteDataStore.identifier == store else {
            return
        }
        guard
            ready,
            let listener,
            let listenerPort,
            listenerState == .ready
        else {
            if !needsListener(authority.host) {
                if listenerState == .binding, lateRouteTask == nil {
                    // The first bind is still under way. The runtime loads
                    // now, on WebKit's normal path, and the host gets its
                    // route when the bind is confirmed. One host at a time:
                    // an earlier one that was still waiting gets none.
                    if let earlier = lateRoute, earlier.host != authority.host || earlier.store != store {
                        routeOwner.exclude(earlier.host, store: earlier.store)
                    }
                    lateRoute = LateRoute(runtime: runtime, store: store, host: authority.host)
                } else {
                    routeOwner.exclude(authority.host, store: store)
                }
            }
            return
        }
        // The listener accepts the host before WebKit can be routed to it.
        // Its list only grows: `plan(for:)` refuses a host no route lists.
        await listener.allowHost(authority.host)
        // And the control plane's host, where WebKit will send it here too
        // because it is this host's parent domain (`carriesControlPlane`).
        if let controlPlaneHost, DirectAuthority.isParentDomain(controlPlaneHost, ofAny: [authority.host]) {
            await listener.allowHost(controlPlaneHost)
        }
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
                    port: listenerPort,
                    allowsFailover: !needsListener(authority.host)
                )
            })
        else {
            if !needsListener(authority.host) {
                routeOwner.exclude(authority.host, store: store)
            }
            return
        }
        if late {
            // Written after the host's first load, and marked in the same
            // turn as the write: no connection of the host can have reached
            // the listener through this route before the mark. A route an
            // ordinary open wrote first (the same host opened again while
            // the bind was under way) is not marked: that open's own load
            // goes through the listener.
            awaitingFirstConnect.insert(authority.host)
        }
        if firstRouteWait?.runtime == runtime, firstRouteRetry == nil { cancelFirstRouteWait() }
    }

    /// Takes in a failure the lifecycle has recorded and this controller
    /// has not heard of yet (the watchdog's deadline passed, or a failure's
    /// own callback is still on its way): its record, and the next attempt.
    /// For callers that are about to act on the gate or the status outside a
    /// view update; the gate and the status themselves never wait for it.
    func settleRecordedFault() {
        if let listener {
            hearFailure(of: listener, "a recorded failure was observed")
        }
    }

    /// Whether the shell may create or reload `runtime`'s WebView now.
    /// Synchronous, and read from the route table and the lifecycle each
    /// time, with no side effect: a failure the lifecycle has recorded,
    /// including the watchdog's deadline passing, is in the gate before
    /// this controller has heard of it.
    func loadGate(for runtime: MobileRuntimeContext) -> DirectLoadGate {
        loadGate(forInstanceURL: runtime.instance.url)
    }

    /// The same gate for a load to `url`'s host on the current store, for a
    /// caller that has no runtime: the gate depends only on whether that
    /// host has a route on this store, and on the listener. A host that was
    /// routed stays routed after its runtime closed, so its loads still go
    /// through the listener.
    func loadGate(forInstanceURL url: URL) -> DirectLoadGate {
        guard let authority = DirectAuthority(instanceURL: url) else { return .open }
        let host = authority.host
        if rewriting.contains(host) || cuttingOver.contains(host) { return .held }
        let strict = needsListener(host)
        guard routeOwner.routes(host, store: websiteDataStore.identifier), let listener else {
            return strict ? .held : .open
        }
        let gate = listenerGate(listener)
        if strict, gate == .open {
            guard case .serving = listener.lifecycle.phase else { return .held }
        }
        return gate
    }

    /// What a load made while the App is in the background (an Action
    /// Button voice take) would meet. In the foreground it is `loadGate`, at
    /// once. In the background `loadGate` cannot say what a load would
    /// meet: the listener is closed there on purpose, a load of a routed
    /// host goes to the listener's port all the same, and that port may
    /// have been taken by another process since the App left. So the port
    /// is asked, right before the load:
    ///
    /// - something accepts a connection there: `foreign`. It is not this
    ///   App's listener, and the load would go to it;
    /// - the connection is refused: `gate(loadGate)`. Nothing listens
    ///   there, and the load goes as it does today;
    /// - neither within `timing.portProbeSeconds`: `undecided(.noAnswer)`.
    ///
    /// "It is not this App's listener" holds only once the listener's own
    /// socket is closed, which only this controller and its listener know:
    /// right after the App leaves the foreground the close is still under
    /// way, and an attempt to bind that was running ends first. That is
    /// waited for, up to `timing.ownCloseSeconds`; a socket still not
    /// closed then is `undecided(.ownSocketOpen)`, since nothing could be
    /// told from an answer on the port. A return to the front and a new
    /// stay in the background while it was asked is `undecided(.stayChanged)`.
    /// Wherever `loadGate` itself is `fault`, the answer is `foreign`.
    ///
    /// Nothing is recorded or changed by asking, so an undecided answer may
    /// be asked again. What it cannot cover: the port can be taken between
    /// this answer and the load's own connection, a few milliseconds later;
    /// and it is asked for one load, so a page that is already loaded makes
    /// its later connections without it.
    func backgroundLoadCheck(forInstanceURL url: URL) async -> DirectBackgroundCheck {
        let gate = loadGate(forInstanceURL: url)
        guard
            isInBackground,
            gate != .fault,
            let stay = leftForegroundAt,
            let authority = DirectAuthority(instanceURL: url),
            routeOwner.routes(authority.host, store: websiteDataStore.identifier),
            let listener,
            let port = listenerPort
        else {
            return DirectBackgroundCheck(gate)
        }
        // The same stay in the background throughout: after a return the
        // listener binds this port again, and an answer on it is its own.
        let stayNow = { [weak self] () -> BackgroundStay in
            guard let self, self.isInBackground else {
                return .ended
            }
            return self.leftForegroundAt == stay && self.listener === listener ? .same : .another
        }
        let closeBy = ContinuousClock.now.advanced(by: .seconds(timing.ownCloseSeconds))
        while true {
            // An attempt still running binds the port, or has, and is
            // closed by its own completion.
            if !rebinds.inFlight, await listener.holdsNoListeningSocket(), !rebinds.inFlight {
                break
            }
            guard stayNow() == .same, ContinuousClock.now < closeBy, !Task.isCancelled else {
                return Self.backgroundCheck(nil, stay: stayNow(), otherwise: loadGate(forInstanceURL: url))
            }
            try? await Task.sleep(for: .milliseconds(10))
        }
        guard stayNow() == .same else {
            return Self.backgroundCheck(nil, stay: stayNow(), otherwise: loadGate(forInstanceURL: url))
        }
        let probe = await DirectConnectListener.probe(port: port, within: timing.portProbeSeconds)
        return Self.backgroundCheck(probe, stay: stayNow(), otherwise: loadGate(forInstanceURL: url))
    }

    /// Where the App is when an answer about the background is ready.
    enum BackgroundStay: Equatable {
        /// In the background, as when the question was asked.
        case same
        /// In the background again, after a return in between.
        case another
        /// In the foreground.
        case ended
    }

    /// What the port's answer makes of the gate. In the stay it was asked
    /// in: a refused connection leaves the gate as it is, an accepted one is
    /// `foreign`, and no answer is `undecided`. `probe` nil: the App's own
    /// socket was not closed in time, and the port was not asked. Back in
    /// the foreground the gate alone answers, as it does there. In another
    /// stay nothing was learnt about this one, and the port may be the
    /// listener's own again or someone else's.
    nonisolated static func backgroundCheck(
        _ probe: DirectPortProbe?,
        stay: BackgroundStay,
        otherwise gate: DirectLoadGate
    ) -> DirectBackgroundCheck {
        switch (stay, probe) {
        case (.ended, _), (.same, .refused):
            return DirectBackgroundCheck(gate)
        case (.another, _):
            return .undecided(.stayChanged)
        case (.same, .accepted):
            return .foreign
        case (.same, .undecided):
            return .undecided(.noAnswer)
        case (.same, nil):
            return .undecided(.ownSocketOpen)
        }
    }

    /// The gate of a host routed to `listener`, from its lifecycle and from
    /// what is known of the port. Read each time: a failure decides the
    /// gate as soon as the lifecycle records it, before this controller has
    /// heard of it.
    private func listenerGate(_ listener: DirectConnectListener) -> DirectLoadGate {
        switch listener.lifecycle.phase {
        case .serving, .stopped:
            return .open
        case .idle, .binding, .verifying, .closing, .closed, .failed:
            break
        }
        // One read of the failure, with the generation that names it.
        let failure = listener.lifecycle.failure
        let unheard = failure.map { heardFailure != $0.generation } ?? false
        // Whether something else may hold the port: what the newest
        // failure found, also across a return. A failure that only ran out
        // of time found nothing, and leaves what was known before it.
        var foreign = portLastKnownForeign
        if let failure, unheard, failure.cause.step != .deadline {
            foreign = failure.cause.portMayBeForeign
        }
        if foreign {
            return .fault
        }
        if failure != nil, unheard || !rebinds.inFlight {
            // Failed, and the port closed or refusing: WebKit fails over by
            // itself, between attempts and in the background alike.
            return .open
        }
        // An attempt is under way, or the listener is closed for the
        // background: held on a return whose first attempt has not ended,
        // open after a failed attempt in this stay.
        return recovery?.lastFailure == nil ? .held : .open
    }

    /// Records a real load decision, not a presentation read of the load gate.
    /// A load of a host whose route has no failover cannot leave that route
    /// while the listener does not serve (it fails), so it needs no relaunch:
    /// the Action Button's hidden view loads in the background, where the
    /// listener is closed.
    func noteInstanceLoad(forInstanceURL url: URL) {
        guard let host = DirectAuthority(instanceURL: url)?.host else { return }
        let store = websiteDataStore.identifier
        if routeOwner.routes(host, store: store) {
            if let listener, case .serving = listener.lifecycle.phase { return }
            if !routeOwner.allowsFailover(host, store: store) { return }
        }
        if hostsNeedingColdStore.insert(host).inserted, requestedStrict.contains(host) {
            onAvailabilityChanged()
        }
    }

    /// Hosts whose route was opened to failover for a voice take
    /// (`noteVoiceLoad`): it stays so for the rest of this process.
    private var failoverOpenedForTakes: Set<String> = []

    /// The Action Button's hidden view is starting a load of the instance.
    /// A voice message must arrive. Where the host's route has no failover
    /// and the listener does not serve (the background, where it is
    /// closed), the load would fail: the host is marked as taking
    /// 「仅直连和中继」 only at the next launch, and in the same turn its
    /// route is opened to failover, so nothing leaves before the mark. The
    /// route then stays open for this process; the next launch is strict
    /// again by the normal path. Any other load is the page's kind
    /// (`noteInstanceLoad`). Only that hidden view calls this: the page's
    /// view never loads while its gate is held.
    func noteVoiceLoad(forInstanceURL url: URL) {
        guard let host = DirectAuthority(instanceURL: url)?.host else { return }
        let store = websiteDataStore.identifier
        var serving = false
        if let listener, case .serving = listener.lifecycle.phase { serving = true }
        guard routeOwner.routes(host, store: store), !serving,
              !routeOwner.allowsFailover(host, store: store), let port = listenerPort else {
            noteInstanceLoad(forInstanceURL: url)
            return
        }
        hostsNeedingColdStore.insert(host)
        failoverOpenedForTakes.insert(host)
        armed.remove(host)
        policyGeneration[host, default: 0] &+= 1
        if target?.authority.host == host { lossWake?.cancel(); lossWake = nil }
        _ = routeOwner.rewrite(host, store: store, dataStore: websiteDataStore.dataStore,
                               port: port, allowsFailover: true)
        onAvailabilityChanged()
    }

    private func needsListener(_ host: String) -> Bool {
        requestedStrict.contains(host) && !hostsNeedingColdStore.contains(host)
    }

    func directOnlyNeedsRelaunch(forInstanceURL url: URL) -> Bool {
        guard let host = DirectAuthority(instanceURL: url)?.host else { return false }
        return requestedStrict.contains(host) && hostsNeedingColdStore.contains(host)
    }

    func unlinkedRendezvousCount(forInstanceURL url: URL) -> UInt64 {
        DirectAuthority(instanceURL: url).map { unlinkedAnswers[$0.host, default: 0] } ?? 0
    }

    func strictFailureCount(forInstanceURL url: URL) -> UInt64 {
        DirectAuthority(instanceURL: url).map { strictFailures[$0.host, default: 0] } ?? 0
    }

    func isDirectOnlyLinked(forInstanceURL url: URL) -> Bool {
        guard let authority = DirectAuthority(instanceURL: url) else { return false }
        return armed.contains(authority.host) && !rewriting.contains(authority.host) && directAuthorized(for: authority)
    }

    func setDirectOnly(_ enabled: Bool, forInstanceURL url: URL) {
        guard let authority = DirectAuthority(instanceURL: url) else { return }
        let host = authority.host
        // Whether the tunnel is ruled out in effect, before the change.
        let wasStrict = needsListener(host)
        if enabled {
            guard requestedStrict.insert(host).inserted else { return }
            choiceDidChangeOnLiveLink(host, wasStrict: wasStrict)
            policyGeneration[host, default: 0] &+= 1
            if needsListener(host), !directAuthorized(for: authority), let port = listenerPort {
                _ = routeOwner.rewrite(host, store: websiteDataStore.identifier,
                                       dataStore: websiteDataStore.dataStore, port: port, allowsFailover: false)
            }
            if !hostsNeedingColdStore.contains(host), directAuthorized(for: authority) { cutover(host) }
        } else {
            guard requestedStrict.remove(host) != nil else { return }
            choiceDidChangeOnLiveLink(host, wasStrict: wasStrict)
            if let wait = firstRouteWait, DirectAuthority(instanceURL: wait.runtime.instance.url)?.host == host {
                cancelFirstRouteWait()
            }
            policyGeneration[host, default: 0] &+= 1
            armed.remove(host)
            if target?.authority.host == host { lossWake?.cancel(); lossWake = nil }
            cutover(host)
        }
        onAvailabilityChanged()
    }

    /// One change of policy at a time per host. It first waits, for a
    /// bounded time, until the host's tunnel connections carry nothing: the
    /// page has just opened and may still be using them. Plans go on as
    /// they were meanwhile; a load the shell would start waits
    /// (`loadGate`). Then the drain: new plans wait; in-flight plans also
    /// fail their commit check. Sample the latest choice after the drain so
    /// a late completion cannot reverse a newer choice or touch another
    /// store.
    ///
    /// A page that has loaded is told of what was closed as of any other
    /// closure: it opens its stream again and catches up, and nothing is
    /// reloaded. A page whose document was still loading cannot make a cut
    /// script, stylesheet or first read again (a slow network: the wait ran
    /// out, or a request was waiting for its answer through the quiet
    /// second), so the shell loads it again, on the new policy. Whether it
    /// was loading is asked before the drain, never after: a cut load ends
    /// the navigation, and the page would look loaded.
    private func cutover(_ host: String) {
        let store = websiteDataStore.identifier
        guard routeOwner.routes(host, store: store), let listener, let port = listenerPort,
              cuttingOver.insert(host).inserted else { return }
        policyGeneration[host, default: 0] &+= 1
        let quietWait = timing.cutoverQuietSeconds
        Task { [weak self] in
            await listener.waitUntilQuiet(
                host: host, backend: .publicOrigin,
                quietSeconds: Self.handoverQuietSeconds, waitSeconds: quietWait
            )
            guard let self else { return }
            let loading = self.runtime.flatMap { runtime in
                DirectAuthority(instanceURL: runtime.instance.url)?.host == host
                    && self.isLoadingDocument(runtime) ? runtime : nil
            }
            self.rewriting.insert(host)
            let drained = await listener.drainHost(host)
            self.rewriting.remove(host)
            self.cuttingOver.remove(host)
            if self.websiteDataStore.identifier == store, self.listener === listener {
                let strict = self.requestedStrict.contains(host) && !self.hostsNeedingColdStore.contains(host)
                    && self.target?.authority.host == host && self.isUsable && !self.isInBackground
                    && self.listenerState == .ready
                // A route opened for a voice take is never closed again here.
                let allowsFailover = !self.requestedStrict.contains(host) || self.failoverOpenedForTakes.contains(host)
                if self.routeOwner.rewrite(host, store: store, dataStore: self.websiteDataStore.dataStore,
                                           port: port, allowsFailover: allowsFailover), strict {
                    self.armed.insert(host)
                } else { self.armed.remove(host) }
            }
            // After the policy is in force: the connections the page opens
            // in answer are planned under it.
            if drained > 0 {
                if let loading, self.runtime == loading {
                    // The gate is open again; the load goes by the policy
                    // now in force. No closure: that document is replaced.
                    self.onLoadingPageCut(loading)
                } else {
                    self.recordClosure(hosts: [host])
                }
            }
            self.onAvailabilityChanged()
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
        abandonLateRoute()
        cancelFirstRouteWait()
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
        // Nothing counted for this account is sent after it.
        pathUse.removeAll()
        abandonLateRoute()
        awaitingFirstConnect.removeAll()
        cancelFirstRouteWait()
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

    /// `keepingDiagnostics`: the background ends the selection too, but the
    /// same instance is selected again on the return, and what was recorded
    /// before is what a diagnosis after the return needs.
    private func deselect(stoppingTransport: Bool = true, keepingDiagnostics: Bool = false) {
        if let host = target?.authority.host { armed.remove(host) }
        lossWake?.cancel()
        lossWake = nil
        // The selection ends by a decision of this App's. The background
        // has ended its link already, as its own.
        endProbationLink(.choice)
        transportUsable = false
        isUsable = false
        currentPath = nil
        stickyDropped = false
        if !keepingDiagnostics {
            races.removeAll()
            losses.removeAll()
        }
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
        case .answeredWithoutLink:
            unlinkedAnswers[target.authority.host, default: 0] &+= 1
            onAvailabilityChanged()
            return
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
                closeRelays(
                    .direct, countsAsClosure: true,
                    loss: (.generationReplaced, currentPath, target.authority.host, .superseded)
                )
            }
            acceptedGeneration = generation
            transportUsable = true
            // A link that is not adopted yet carries nothing and is not
            // reported: the page goes on by the tunnel and asks nothing.
            guard linkIsAdopted(for: target) else {
                isUsable = false
                currentPath = nil
                updatePreference()
                return
            }
            adoptedPathIsUsable(for: target)
            return
        case .pathChanged(let kind):
            // The connection moved between the relay and a direct address.
            // Nothing is closed; the page is told, so it can move its stream
            // onto a direct path, and plans may stay on it (section 6).
            guard isUsable, kind != currentPath else {
                return
            }
            currentPath = kind
            stickyDropped = false
            if preferred == .direct {
                sequence += 1
                notify()
            }
            return
        case .verifying:
            // A network callback is not proof of loss: only the preference
            // changes until the transport validates the path again.
            isUsable = false
            transportUsable = false
            currentPath = nil
            advanceGeneration()
        case .lost(let cause):
            isUsable = false
            transportUsable = false
            endProbationLink(cause.linkEnd)
            let pathAtLoss = currentPath
            currentPath = nil
            advanceGeneration()
            // The preference is published before the closure, which is now
            // published in this turn: no status says `direct` for a path
            // that is gone.
            updatePreference()
            closeRelays(
                .direct, countsAsClosure: true,
                loss: (.lost, pathAtLoss, target.authority.host, DirectPathUse.RetiredRule(cause))
            )
        case .lostToBackground:
            // The transport heard the background first. It is this App's
            // background: entered here now, as the background observer
            // would, so the closure waits for the return. The observer, when
            // it comes, finds it entered.
            applicationDidEnterBackground()
            return
        }
        updatePreference()
        let host = target.authority.host
        if armed.contains(host), lossWake == nil {
            let seconds = timing.strictOpenSeconds
            lossWake = Task { [weak self] in
                do { try await Task.sleep(for: .seconds(seconds)) } catch { return }
                guard let self, self.target == target, !self.isUsable,
                      self.armed.contains(host) else { return }
                // Leave the completed wake in place: another loss report in this
                // outage is not a new episode. Only recovery, a deliberate new
                // selection, or disabling the choice can reset it. In particular,
                // an ordinary browser reconnect must not acquire a public opener.
                self.strictFailures[host, default: 0] &+= 1
                self.onAvailabilityChanged()
            }
            onAvailabilityChanged()
        }
    }

    /// A direct candidate failed before anything was forwarded. Only a
    /// failure under the current path generation reaches the transport.
    private func directCandidateFailed(_ tag: DirectStreamTag) {
        guard tag.target == target, tag.pathGeneration == pathGeneration else {
            return
        }
        // At once, not when the race it lost has finished: the next plans
        // hedge again while the transport probes the path.
        dropStickiness()
        transport.pathDidFail(for: tag)
    }

    // MARK: Probation

    /// A usable path of `target`: the link it belongs to is begun if none is
    /// up, and asked whether it is adopted. A link the rule makes wait gets
    /// its timer and its probe here.
    private func linkIsAdopted(for target: DirectTarget) -> Bool {
        let host = target.authority.host
        if probationLink == nil {
            probationLinks += 1
            let key = DirectProbationDefaultsStore.entry(accountID: target.runtime.account.id, host: host)
            let link = (number: probationLinks, key: key, host: host)
            probationLink = link
            probationPath = .none
            let waitMs = probation.linkUp(
                key, link: link.number, now: probationClock.now(), wall: probationClock.wall(),
                // With the tunnel ruled out by the user there is nothing
                // to wait on. Ruled out in effect: a choice that waits for
                // the next launch still has the tunnel.
                strict: needsListener(host)
            )
            if waitMs > 0 {
                noteProbation(held: true)
                waitOnProbation(link.number, milliseconds: waitMs)
            }
        }
        guard let link = probationLink else {
            return false
        }
        if !probation.isAdopted(link.key), !isInBackground,
           probation.adopt(link.key, link: link.number, now: probationClock.now()) {
            probationWake?.cancel()
            probationWake = nil
            probationProbe?.cancel()
            probationProbe = nil
            noteProbation(held: false)
        }
        storeProbation()
        return probation.isAdopted(link.key)
    }

    /// The wait of a link, and one probe a minute while it lasts: the
    /// instance closes a connection that has had no stream, and keep-alives
    /// do not show that a path carries anything.
    private func waitOnProbation(_ number: Int, milliseconds: Int64) {
        let clock = probationClock
        probationWake?.cancel()
        probationWake = Task { [weak self] in
            do { try await clock.sleep(milliseconds) } catch { return }
            self?.probationWaitDidEnd(number)
        }
        probationProbe?.cancel()
        probationProbe = Task { [weak self] in
            while true {
                do { try await clock.sleep(Self.probationProbeMs) } catch { return }
                guard let self, self.probationLink?.number == number else { return }
                if self.transportUsable, !self.isInBackground {
                    self.transport.probeWaitingPath()
                }
            }
        }
    }

    /// The wait is over. The link is adopted only if it is still the one,
    /// the transport still calls its path usable and the App is in the
    /// foreground; otherwise the next `usable` report asks again.
    private func probationWaitDidEnd(_ number: Int) {
        guard let target, probationLink?.number == number, transportUsable, !isInBackground,
              linkIsAdopted(for: target) else {
            return
        }
        adoptedPathIsUsable(for: target)
    }

    /// The link's path is usable and adopted: it is published.
    private func adoptedPathIsUsable(for target: DirectTarget) {
        let wasUsable = isUsable
        isUsable = true
        lossWake?.cancel()
        lossWake = nil
        let host = target.authority.host
        if requestedStrict.contains(host), !hostsNeedingColdStore.contains(host) {
            if !armed.contains(host) { cutover(host) }
            else if !wasUsable { onAvailabilityChanged() }
        }
        let previous = currentPath
        currentPath = transport.pathDiagnostics.current
        stickyDropped = false
        let wasDirect = preferred == .direct
        updatePreference()
        // The same preference on another kind of path is a change too.
        if wasDirect, preferred == .direct, previous != currentPath {
            sequence += 1
            notify()
        }
    }

    /// The link ended, and why. Only a loss can raise the level.
    private func endProbationLink(_ cause: DirectLinkEnd) {
        probationWake?.cancel()
        probationWake = nil
        probationProbe?.cancel()
        probationProbe = nil
        guard let link = probationLink else {
            return
        }
        probation.linkDown(
            link.key, link: link.number, cause: cause, now: probationClock.now(), wall: probationClock.wall()
        )
        noteProbation(held: false)
        probationLink = nil
        storeProbation()
    }

    /// The user ruled the tunnel out, or allowed it again, while a link is
    /// up: the link's turn ends as a choice and it begins another under the
    /// new choice, with a new number, so a timer of the old turn adopts
    /// nothing. A link that was waiting is used at once; one that was used
    /// at once waits the level's time from here.
    private func choiceDidChangeOnLiveLink(_ host: String, wasStrict: Bool) {
        guard let target, target.authority.host == host, probationLink != nil,
              needsListener(host) != wasStrict else {
            return
        }
        endProbationLink(.choice)
        guard transportUsable else {
            return
        }
        if linkIsAdopted(for: target) {
            adoptedPathIsUsable(for: target)
        } else {
            isUsable = false
            currentPath = nil
            updatePreference()
        }
    }

    /// The level, for the instance's log: only while it is above 0 or a
    /// link is being held (`DirectPathUse`, `path_guard`).
    private func noteProbation(held: Bool) {
        guard let link = probationLink else {
            return
        }
        let level = probation.levelSeconds(link.key, now: probationClock.now(), wall: probationClock.wall())
        guard level > 0 || held else {
            return
        }
        // The kind the transport names now, or the last it named for this
        // link: at a loss it names none.
        let diagnostics = transport.pathDiagnostics
        let seen = DirectPathUse.Path(kind: diagnostics.current, local: diagnostics.selected?.local)
        if seen != .none {
            probationPath = seen
        }
        pathUse.probation(
            scope: useScope(link.host),
            path: probationPath,
            levelSeconds: level,
            rose: probation.rose(link.key),
            held: held
        )
    }

    /// Kept across launches, when it changed.
    private func storeProbation() {
        if probation.stored != storedProbation {
            storedProbation = probation.stored
            probationStore.save(probation.stored)
        }
    }

    /// Whether a CONNECT for `host` is carried though no route lists it:
    /// only the control plane's own host, only while it is exactly the
    /// parent domain of a host the current account store's route lists now,
    /// and not once it is routed itself (its own policy then decides). That
    /// is read from the route as it stands, not from every host ever
    /// routed: when the last host under it leaves the route
    /// (`bypassListener`), or the account's store is replaced, WebKit no
    /// longer sends the parent here, and anything else on this device that
    /// asks the loopback port for it is refused again.
    private func carriesControlPlane(_ host: String) -> Bool {
        host == controlPlaneHost
            && !routeOwner.allowedHosts.contains(host)
            && DirectAuthority.isParentDomain(
                host, ofAny: routeOwner.routedHosts(store: websiteDataStore.identifier)
            )
    }

    // MARK: Plans

    /// Decides one accepted CONNECT (section 6). `nil` refuses it.
    func plan(for authority: DirectAuthority) -> DirectConnectPlan? {
        let host = authority.host
        guard routeOwner.allowedHosts.contains(host) else {
            // The control plane's own host, when it is the parent domain of
            // a routed host (avibe.bot, for an instance under it): WebKit
            // sends it here though no route lists it. It is not the
            // instance: it goes straight to that host through the public
            // opener and its address policy under every choice, never over
            // the direct path, and is in no count of the instance's
            // connections. Fenced as any public-only plan is, so nothing
            // planned before a change of selection or account is granted
            // after it; and no longer carried once the host is routed in
            // its own right or stops being a routed host's parent.
            guard carriesControlPlane(host) else { return nil }
            let fence = Fence(selection: selection, pathGeneration: pathGeneration, accountEpoch: accountEpoch)
            let publicBackend = publicBackend
            return DirectConnectPlan(
                scope: scope, generation: pathGeneration, openDirect: nil,
                openPublic: { try await publicBackend.open(authority) },
                isCurrent: { [weak self] in
                    guard let self else { return false }
                    return self.isCurrent(fence, lease: nil) && self.carriesControlPlane(host)
                },
                directFailed: {}
            )
        }
        if awaitingFirstConnect.remove(host) != nil {
            // WebKit brought a connection of this host to the listener: from
            // here on its documents hear what the transport prefers.
            sequence += 1
            notify()
        }
        if rewriting.contains(host) {
            // Not a refusal: WebKit might still have the old failover route. Neither
            // opener may run until the drain and rewrite have both finished.
            var hold = DirectConnectPlan(scope: scope, generation: pathGeneration, openDirect: nil,
                                         openPublic: nil, isCurrent: { false }, directFailed: {})
            hold.rewriteWaitSeconds = timing.strictOpenSeconds
            let store = websiteDataStore.identifier
            let policy = policyGeneration[host, default: 0]
            if requestedStrict.contains(host), routeOwner.allowsFailover(host, store: store) {
                hold.rewriteTimedOut = { [weak self] in
                    guard let self, self.websiteDataStore.identifier == store,
                          self.policyGeneration[host] == policy, self.requestedStrict.contains(host) else { return }
                    // This request was made on a failover-enabled route. A silent
                    // timeout can leave an origin socket beyond the listener's reach.
                    self.armed.remove(host)
                    if self.hostsNeedingColdStore.insert(host).inserted { self.onAvailabilityChanged() }
                }
            }
            return hold
        }
        let policy = policyGeneration[host, default: 0]
        let strictChoice = needsListener(host)
        let strict = strictChoice && armed.contains(host)
        let fence = Fence(
            selection: selection,
            pathGeneration: pathGeneration,
            accountEpoch: accountEpoch
        )
        let transport = transport
        let publicBackend = publicBackend
        let directTag = strictChoice && !strict ? nil : directStreamTag(for: authority)
        let openPublic: (@Sendable () async throws -> any DirectByteStream)?
        if strict { openPublic = nil } else { openPublic = { try await publicBackend.open(authority) } }
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
        // Sticky: the App's own path is a direct address, and no sticky
        // attempt has missed since. The direct candidate then runs alone for
        // its bound instead of racing the public origin from the hedge on.
        let sticky = directTag != nil
            && (currentPath == .ipv4 || currentPath == .ipv6)
            && !stickyDropped
        let generation = pathGeneration
        let dropSerial = stickyDropSerial
        // For the account of which leg carries the pages' connections
        // (`DirectPathUse`): the plan's own facts, read now, with the plan.
        let planPath = directTag == nil ? nil : currentPath
        let selectedPath = planPath == nil ? nil : transport.pathDiagnostics.selected
        let use = PlanUse(
            scope: useScope(host),
            path: DirectPathUse.Path(
                kind: planPath,
                local: selectedPath?.kind == planPath ? selectedPath?.local : nil
            ),
            strict: strict,
            // No direct session for this host at all: the choice of the
            // tunnel alone, no permission, or an instance that is not the
            // selected one. Otherwise there is one and it has no usable path.
            tunnelOnly: directTag == nil && target?.authority != authority
        )
        if let planPath {
            plannedPath = (planPath, use.path)
        }
        var plan = DirectConnectPlan(
            scope: scope,
            generation: pathGeneration,
            openDirect: openDirect,
            openPublic: openPublic,
            isCurrent: { [weak self] in
                guard let self, self.policyGeneration[host, default: 0] == policy,
                      !self.rewriting.contains(host), self.armed.contains(host) == strict else { return false }
                return self.isCurrent(fence, lease: lease)
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
        plan.strictWaitSeconds = strict ? timing.strictOpenSeconds : nil
        plan.sticky = !strict && sticky
        plan.path = directTag == nil ? nil : currentPath
        plan.stickyMissed = { [weak self] in
            Task { @MainActor in
                self?.stickyAttemptMissed(generation: generation, selection: fence.selection)
            }
        }
        let lastRace = PlanRace()
        plan.recordRace = { [weak self] record in
            lastRace.last = record
            Task { @MainActor in
                self?.raceDidFinish(
                    record,
                    generation: generation,
                    selection: fence.selection,
                    dropSerial: dropSerial
                )
            }
        }
        // Counted once for the CONNECT, when the listener has granted its
        // relay or given it up: a race that is fenced and planned again is
        // not a connection. Whichever instance is selected by then, the
        // connection was made, and it is counted for its own host.
        let carryStore = websiteDataStore.identifier
        plan.connectionSettled = { [weak self, pathUse] carried in
            let carriedAny = carried != nil
            Task { @MainActor in
                self?.noteCarry(host: host, store: carryStore, carried: carriedAny)
            }
            guard let record = lastRace.last else {
                return
            }
            pathUse.connection(
                DirectPathUseConnection(
                    record: record, carried: carried, strict: use.strict, tunnelOnly: use.tunnelOnly
                ),
                scope: use.scope,
                path: use.path
            )
        }
        return plan
    }

    /// For a measurement only (`DirectMeasure.swift`): the path usable for
    /// the instance at `url`, under the conditions a plan's direct candidate
    /// has, or nil. What it gives opens streams over that path and says
    /// whether everything it was made under still holds. It makes no plan
    /// and records nothing: no race, failure, stickiness, closure or count
    /// is touched by a measurement.
    ///
    /// Which path it is, and its labels, are the transport's own reading at
    /// this moment (`DirectMarkedPath`), not `currentPath`: the transport
    /// may have found a move this controller is told of a turn later, and
    /// the labels must be those of the path the mark is of. For the same
    /// reason the status's `sequence` is no part of the fence: it advances
    /// when that news arrives, and for closures and preferences, none of
    /// which is a change of the path.
    func measurementLink(forInstanceURL url: URL) -> DirectMeasureLink? {
        let transport = transport
        guard
            let authority = DirectAuthority(instanceURL: url),
            let tag = directStreamTag(for: authority),
            let marked = transport.markedPath
        else {
            return nil
        }
        let fence = Fence(
            selection: selection,
            pathGeneration: pathGeneration,
            accountEpoch: accountEpoch
        )
        let mark = marked.mark
        return DirectMeasureLink(
            authority: authority,
            kind: marked.path.kind,
            local: marked.path.local,
            relayRegion: marked.path.relayRegion,
            open: {
                try await transport.openStream(for: tag)
            },
            isCurrent: { [weak self] in
                guard let self else {
                    return false
                }
                return self.isCurrent(fence, lease: tag.target.runtime)
                    && transport.markedPath?.mark == mark
            }
        )
    }

    /// A sticky direct attempt did not answer within its bound. Plans hedge
    /// again, at once. The path is not reported as failed: no probe is
    /// started and nothing it carries is closed, so a request already
    /// running on it ends only when the transport itself reports the path
    /// lost (section 7).
    private func stickyAttemptMissed(generation: Int, selection raced: Int) {
        guard raced == selection, generation == pathGeneration else {
            return
        }
        dropStickiness()
    }

    private func dropStickiness() {
        stickyDropped = true
        stickyDropSerial += 1
    }

    /// One CONNECT's race ended. Kept for diagnosis; and a direct candidate
    /// that won restores stickiness, if its plan was made after the last
    /// drop. A race ends only when both candidates have settled, so its
    /// record can arrive long after a miss or a failure was acted on
    /// (`stickyAttemptMissed`, `directCandidateFailed`): the record itself
    /// never drops stickiness.
    private func raceDidFinish(
        _ record: DirectRaceRecord,
        generation: Int,
        selection raced: Int,
        dropSerial: Int
    ) {
        guard raced == selection else {
            return
        }
        races.append(record)
        if races.count > Self.raceLimit {
            races.removeFirst(races.count - Self.raceLimit)
        }
        let directLeg = String(describing: record.direct.jsonValue)
        let publicLeg = String(describing: record.publicOrigin.jsonValue)
        let winner = record.winner == .direct ? "direct" : record.winner == .publicOrigin ? "public" : "none"
        let path = record.path?.rawValue ?? "none"
        let sticky = record.sticky
        Self.logger.info(
            """
            Direct connection: direct \(directLeg, privacy: .public) public \(publicLeg, privacy: .public) \
            winner \(winner, privacy: .public) path \(path, privacy: .public) sticky \(sticky, privacy: .public)
            """
        )
        guard generation == pathGeneration else {
            return
        }
        if record.winner == .direct, dropSerial == stickyDropSerial {
            stickyDropped = false
        }
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
    /// `then` runs on the main actor once the listener has closed them (at
    /// once when there is no listener).
    ///
    /// Which relays this event closes, and the closure they make, are
    /// decided here, in the event's own turn: their claims are taken before
    /// anything that runs after it can end them. The transport may close
    /// its streams right after reporting a loss, and a relay whose stream
    /// ended before the close reached it would otherwise look as if it had
    /// ended on its own, and its page would not be told. A closure made in
    /// the foreground is published at once, while the listener closes the
    /// relays.
    private func closeRelays(
        _ backend: DirectRelayBackend?,
        countsAsClosure: Bool,
        loss: (cause: DirectLossRecord.Cause, path: DirectPathKind?, host: String?, rule: DirectPathUse.RetiredRule)? = nil,
        then: (@MainActor () -> Void)? = nil
    ) {
        guard let listener else {
            then?()
            return
        }
        // Only relays committed under a plan made before this event: one
        // committed after it is kept.
        let generation = pathGeneration
        let claimed = listener.lifecycle.claimForEvent(backend: backend, before: generation)
        // Recorded in the same turn as the claim: a background closure is
        // pending before the return can settle, so the first status after
        // the return includes it.
        if countsAsClosure {
            recordClosure(hosts: claimed.filter { $0.value > 0 }.map(\.key))
        }
        // Until the listener has issued this close (cancelled the relays and
        // ended their authority; not until every one has drained), a return
        // is not confirmed: the closure it publishes is for connections
        // that have been told to end.
        closesInFlight += 1
        // A close of direct relays for a path that is gone is recorded, with
        // its cause and the path the relays ran on.
        let cause = loss?.cause
        let ruleAtClose = loss?.rule
        let pathAtClose = loss?.path
        // Whose connections these are and what their path was called, read
        // now: by the time the listener has closed them, a newer link may
        // have been planned on and named differently.
        var useAtClose: (scope: DirectPathUse.Scope, path: DirectPathUse.Path)?
        if let loss, let host = loss.host {
            let planned = plannedPath.flatMap { $0.kind == loss.path ? $0.use : nil }
            useAtClose = (
                scope: useScope(host),
                path: planned ?? DirectPathUse.Path(kind: loss.path, local: nil)
            )
        }
        let epoch = accountEpoch
        Task { [weak self] in
            let result = await listener.closeRelaysTimed(backend: backend, before: generation)
            await MainActor.run {
                self?.closeDidIssue()
                if let cause, let ruleAtClose {
                    self?.recordLoss(
                        result.quietMs, cause: cause, rule: ruleAtClose, path: pathAtClose, use: useAtClose,
                        accountEpoch: epoch
                    )
                }
                then?()
            }
        }
    }

    /// Committed direct relays were closed for a lost path: kept for
    /// diagnosis and logged once. Nothing is recorded when none was open.
    private func recordLoss(
        _ quietMs: [Int],
        cause: DirectLossRecord.Cause,
        rule: DirectPathUse.RetiredRule,
        path: DirectPathKind?,
        use: (scope: DirectPathUse.Scope, path: DirectPathUse.Path)?,
        accountEpoch epoch: Int
    ) {
        guard !quietMs.isEmpty, epoch == accountEpoch else {
            return
        }
        let record = DirectLossRecord(
            cause: cause,
            relays: quietMs.count,
            quietMs: Array(quietMs.prefix(DirectLossRecord.quietLimit)),
            path: path
        )
        if let use {
            pathUse.retired(rule, scope: use.scope, path: use.path)
        }
        losses.append(record)
        if losses.count > Self.raceLimit {
            losses.removeFirst(losses.count - Self.raceLimit)
        }
        let shortest = record.quietMs.first ?? 0
        let kind = path?.rawValue ?? "none"
        let reason = cause.rawValue
        Self.logger.info(
            """
            Direct relays closed (\(reason, privacy: .public)): \(record.relays, privacy: .public) relays, \
            shortest time since a byte \(shortest, privacy: .public) ms, path \(kind, privacy: .public)
            """
        )
    }

    /// A close was issued. With none left, a kept completion is handled
    /// again, from the start: whatever happened meanwhile is decided there.
    private func closeDidIssue() {
        closesInFlight -= 1
        guard closesInFlight == 0, let kept = completionAwaitingCloses else {
            return
        }
        completionAwaitingCloses = nil
        rebindDidComplete(kept)
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

    /// Sends the state to the runtime's documents.
    private func notify() {
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
    private func ensureListener(for host: String) async -> Bool {
        switch listenerState {
        case .ready:
            return true
        case .failed, .closedForBackground:
            // Failed and between attempts, or closed for the background: a
            // host opened now loads without a route, like one whose
            // listener was not ready in time.
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
        let started = ContinuousClock.now
        while listenerState == .binding || listenerState == .rebinding {
            let seconds = needsListener(host) ? timing.strictOpenSeconds : timing.listenerWaitSeconds
            guard !Task.isCancelled, ContinuousClock.now < started.advanced(by: .seconds(seconds)) else { break }
            do { try await Task.sleep(for: .milliseconds(25)) } catch { return false }
        }
        return listenerState == .ready
    }

    private func startListener() -> Task<Bool, Never> {
        listenersStarted += 1
        let firstInProcess = listenersStarted == 1
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
        firstBindStarted = ContinuousClock.now
        // The first bind starts in the foreground: its episode begins here.
        armWatchdog()
        let rebindSeconds = timing.rebindSeconds
        let task = Task { [weak self] () -> Bool in
            guard let binding = try? await listener.bind(port: nil) else {
                // The bind failed, and its lifecycle already says so. No
                // route points at this listener, so it is not kept. (A bind
                // that only ended because the listener was stopped is no
                // longer this controller's listener.)
                await MainActor.run {
                    guard let self, self.listener === listener else {
                        return
                    }
                    if !self.hearFailure(of: listener, "the first bind failed") {
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
                // recorded first wins. Not owned, or failed meanwhile: this
                // listener is not kept either.
                guard owned, listener.lifecycle.confirm(binding.generation) else {
                    listener.lifecycle.reject(binding.generation, DirectListenerFailureCause(.ownership))
                    if !self.hearFailure(of: listener, "the first bind was not confirmed") {
                        self.retireAndStop()
                    }
                    return false
                }
                // Serving implies the foreground: the background retires a
                // first bind still under way before it can be confirmed.
                self.listenerPort = binding.port
                self.listenerGeneration = binding.generation
                self.disarmWatchdog()
                if firstInProcess, let started = self.firstBindStarted {
                    // The process's first listener only: a later one (after
                    // the first failed or was retired) is no cold start's.
                    // A host that loaded while this bind was under way gets
                    // its route late (`lateRoute`).
                    ColdStartTimings.shared.firstBindConfirmed(
                        took: started.duration(to: ContinuousClock.now),
                        late: self.lateRoute != nil
                    )
                }
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
                // that could be used: it was retired when it failed.
                if !ready, self.listener === listener, self.listenerState == .none {
                    self.retireAndStop()
                }
                if ready, self.listener === listener {
                    self.addLateRoute()
                }
            }
        }
        return task
    }

    /// The first bind was confirmed after a host's first load had gone
    /// ahead: the miss goes on record with how long the bind took, and the
    /// host gets its route if its runtime is still the one on show. Its
    /// documents hear `direct` only once one of its connections has reached
    /// the listener (`awaitingFirstConnect`).
    private func addLateRoute() {
        guard let late = lateRoute, lateRouteTask == nil else {
            return
        }
        if let started = firstBindStarted {
            let took = ContinuousClock.now - started
            let milliseconds = took.components.seconds * 1000 + took.components.attoseconds / 1_000_000_000_000_000
            appendToRecord(DirectListenerFailure(
                step: .late,
                code: Int32(clamping: max(milliseconds, 0)),
                backgroundSeconds: awaySeconds,
                at: ContinuousClock.now
            ))
            Self.logger.notice(
                "Direct path: the first bind was confirmed after the first load, \(milliseconds, privacy: .public) ms after it started"
            )
        }
        lateRouteTask = Task { [weak self] in
            guard let self else { return }
            await self.prepareRoute(for: late.runtime, late: true) {
                self.lateRoute?.runtime == late.runtime && self.isCurrentRuntime(late.runtime)
                    && !self.isInBackground && self.websiteDataStore.identifier == late.store
            }
            // Routed by this task, or by an ordinary open of the same host
            // that got there first.
            let routed = self.routeOwner.routes(late.host, store: late.store)
            // Abandoned meanwhile: whoever did that has settled the rest.
            guard let still = self.lateRoute, still.runtime == late.runtime, still.store == late.store else {
                return
            }
            self.lateRoute = nil
            self.lateRouteTask = nil
            if routed {
                self.select()
                self.updatePreference()
            } else {
                // Its runtime is no longer the one on show, or the write
                // was refused: it loaded without a route and gets none.
                self.routeOwner.exclude(late.host, store: late.store)
                if self.preparations == 0, self.routeOwner.allowedHosts.isEmpty, !self.firstRouteWanted {
                    self.retireAndStop()
                }
            }
            self.onAvailabilityChanged()
        }
    }

    /// The host that was waiting for the first bind gets no route on its
    /// store: its runtime went away, or the listener it waited for did.
    private func abandonLateRoute() {
        guard let late = lateRoute else {
            return
        }
        lateRoute = nil
        lateRouteTask?.cancel()
        lateRouteTask = nil
        routeOwner.exclude(late.host, store: late.store)
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
    /// (`retire`), whether or not it had failed: a listener a route was
    /// ever published to is never stopped or replaced, because the route
    /// cannot be taken back and only that listener, on that port, can serve
    /// it.
    private func retireListener() -> DirectConnectListener? {
        guard let listener else {
            return nil
        }
        // A failure recorded before the retirement and not heard yet is on
        // record all the same; nothing else follows from it, because
        // nothing points at this listener.
        let unheard = listener.lifecycle.failure.flatMap { failure in
            heardFailure == failure.generation ? nil : failure.cause
        }
        switch listener.lifecycle.retire() {
        case .retired:
            if let unheard {
                recordFailure(unheard, "a listener no route points at had failed")
            }
            // Whoever waited for this listener's first bind gets no route.
            abandonLateRoute()
            firstBindStarted = nil
            disarmWatchdog()
            endRecovery()
            self.listener = nil
            listenerPort = nil
            listenerGeneration = nil
            listenerStart = nil
            heardFailure = nil
            portLastKnownForeign = false
            rebinds.abandon()
            completionAwaitingCloses = nil
            return listener
        case .published:
            return nil
        }
    }

    // MARK: Failure and recovery

    /// Non-`nil` while a listener that a route points at is not serving, in
    /// the foreground: from the return to the foreground, or from the
    /// failure if it failed while the App was in front, until it serves
    /// again. It includes an ordinary return's one attempt, during which
    /// `lastFailure` is `nil`. Every change of it is announced through
    /// `onAvailabilityChanged`.
    var listenerRecovery: DirectListenerRecovery? {
        if let waiting = firstRouteWait, !isInBackground, isCurrentRuntime(waiting.runtime),
           waiting.store == websiteDataStore.identifier,
           let host = DirectAuthority(instanceURL: waiting.runtime.instance.url)?.host,
           needsListener(host), !routeOwner.routes(host, store: waiting.store) {
            return DirectListenerRecovery(since: waiting.since, lastFailure: waiting.lastFailure,
                                          portMayBeForeign: portLastKnownForeign)
        }
        guard
            let listener,
            let recovery,
            !isInBackground,
            listener.lifecycle.isPublished
        else {
            return nil
        }
        if case .serving = listener.lifecycle.phase {
            return nil
        }
        // A failure the lifecycle recorded and this controller has not
        // heard of yet is not in `lastFailure`; the gate already has it.
        return DirectListenerRecovery(
            since: recovery.since,
            lastFailure: recovery.lastFailure,
            portMayBeForeign: portLastKnownForeign
        )
    }

    /// Starts an attempt to bind and check the listener's port now, unless
    /// one is running: what 「重试」 asks for. Nothing in the background, or
    /// when nothing is recovering. The attempts the recovery makes by
    /// itself go on as before.
    func retryListenerNow() {
        settleRecordedFault()
        startAttemptIfFailed()
        retryFirstRouteIfNeeded()
    }

    private var firstRouteWanted: Bool {
        guard let wait = firstRouteWait, isCurrentRuntime(wait.runtime), wait.store == websiteDataStore.identifier,
              let host = DirectAuthority(instanceURL: wait.runtime.instance.url)?.host else { return false }
        return needsListener(host) && !routeOwner.routes(host, store: wait.store)
    }

    private func cancelFirstRouteWait() {
        firstRouteWake?.cancel()
        firstRouteWake = nil
        firstRouteRetry?.cancel()
        firstRouteRetry = nil
        firstRouteWait = nil
        if preparations == 0, routeOwner.allowedHosts.isEmpty, lateRoute == nil { retireAndStop() }
    }

    private func scheduleFirstRouteRetry() {
        guard firstRouteWanted, !isInBackground, preparations == 0, firstRouteRetry == nil,
              firstRouteWake == nil, let wait = firstRouteWait else { return }
        let delays = timing.retrySeconds
        let delay = delays.isEmpty ? 5 : delays[min(wait.retries, delays.count - 1)]
        firstRouteWait?.retries += 1
        firstRouteWake = Task { [weak self] in
            do { try await Task.sleep(for: .seconds(delay)) } catch { return }
            guard !Task.isCancelled else { return }
            self?.firstRouteWake = nil
            self?.retryFirstRouteIfNeeded()
        }
    }

    private func retryFirstRouteIfNeeded() {
        guard preparations == 0, let waiting = firstRouteWait, isCurrentRuntime(waiting.runtime), !isInBackground,
              waiting.store == websiteDataStore.identifier, firstRouteRetry == nil,
              let host = DirectAuthority(instanceURL: waiting.runtime.instance.url)?.host,
              needsListener(host), !routeOwner.routes(host, store: waiting.store) else { return }
        firstRouteWake?.cancel()
        firstRouteWake = nil
        firstRouteRetry = Task { [weak self] in
            guard !Task.isCancelled, let self else { return }
            await self.prepareRoute(for: waiting.runtime) {
                !Task.isCancelled && self.firstRouteWait?.runtime == waiting.runtime &&
                    self.isCurrentRuntime(waiting.runtime) && !self.isInBackground &&
                    self.websiteDataStore.identifier == waiting.store && self.needsListener(host)
            }
            guard !Task.isCancelled else { return }
            self.firstRouteRetry = nil
            if self.routeOwner.routes(host, store: waiting.store) {
                self.firstRouteWait = nil
                self.select()
                self.updatePreference()
            } else {
                self.scheduleFirstRouteRetry()
            }
            self.onAvailabilityChanged()
        }
    }

    /// The listening socket of `failed` failed, as its lifecycle recorded
    /// where Network reported it.
    func listenerDidFail(_ failed: DirectConnectListener) {
        hearFailure(of: failed, "the listening socket failed")
    }

    /// Whether `listener` is this controller's and its lifecycle is in a
    /// failure; and, the first time for each failure, what follows from it
    /// here (`listenerFailed`). The controller never decides a failure
    /// itself: a stale generation, a deliberate close, or a stop records
    /// none, and then this does nothing and returns `false`.
    @discardableResult
    private func hearFailure(of listener: DirectConnectListener, _ reason: String) -> Bool {
        guard listener === self.listener, let failure = listener.lifecycle.failure else {
            return false
        }
        if heardFailure != failure.generation {
            heardFailure = failure.generation
            listenerFailed(failure.cause, reason)
        }
        return true
    }

    /// Adds `entry` to the record unless it says what the last one does,
    /// and keeps the last few.
    private func appendToRecord(_ entry: DirectListenerFailure) {
        if let last = listenerFailures.last,
           last.step == entry.step,
           last.code == entry.code,
           last.backgroundSeconds == entry.backgroundSeconds
        {
            return
        }
        listenerFailures.append(entry)
        if listenerFailures.count > Self.listenerFailureLimit {
            listenerFailures.removeFirst(listenerFailures.count - Self.listenerFailureLimit)
        }
    }

    /// The one exit for every listener failure, entered once for each, in
    /// the foreground or the background. The lifecycle's `failed` phase has
    /// already ended every admission, plan, grant, and publication, and
    /// revoked what was not granted; this does the rest. Granted relays are
    /// left as the close-and-keep table has them: a failure closes none of
    /// them, and it is not a closure.
    ///
    /// A listener no route points at is stopped: there is nothing for it to
    /// serve, and the next open makes another. One a route points at is
    /// kept, never stopped (it holds those relays, and only it, on its
    /// port, can serve that route): its socket is closed, so that WebKit
    /// fails over from the port, and the port is bound and checked again,
    /// attempt after attempt, for as long as the App is in the foreground.
    private func listenerFailed(_ cause: DirectListenerFailureCause, _ reason: String) {
        let failure = recordFailure(cause, reason)
        disarmWatchdog()
        if completionAwaitingCloses != nil {
            // An attempt that had found the listener and waited only for
            // closes is settled by this failure.
            completionAwaitingCloses = nil
            rebinds.abandon()
        }
        guard let listener else {
            return
        }
        guard listener.lifecycle.isPublished else {
            // Retired with its failure: nothing points at it.
            retireAndStop()
            if isResuming, !isInBackground {
                finishResume()
            }
            return
        }
        if cause.step != .deadline {
            // What this attempt found of the port. One that only ran out of
            // time found nothing.
            portLastKnownForeign = cause.portMayBeForeign
        }
        if !rebinds.inFlight {
            // The socket goes, and the revoked work with it. An attempt
            // under way closes it itself, before it binds.
            Task {
                await listener.closeListeningSocket()
            }
        }
        // The selection stays: deselecting would have the transport close
        // its streams, and a failure closes no relay.
        advanceGeneration()
        updatePreference()
        guard !isInBackground else {
            // Nothing is bound in the background: the return binds again.
            return
        }
        if recovery == nil {
            recovery = Recovery(since: failure.at, lastFailure: failure)
        } else {
            recovery?.lastFailure = failure
        }
        scheduleRetryIfFailed()
        if isResuming {
            // The return's first attempt has ended: its held answers are
            // given now, as the public path, and the gate is as this
            // failure says.
            finishResume()
        } else {
            // A later attempt may find the port merely closed where an
            // earlier one found it in use: what was kept pending is
            // published then, and the pages are told.
            if publishPendingClosures() {
                notify()
            }
            onAvailabilityChanged()
        }
    }

    /// Puts a failure on record and in the log: the step, the number, and
    /// how long the App had been away. The record keeps what differs: an
    /// attempt that fails as the one before it did, in the same stay, adds
    /// nothing to it.
    @discardableResult
    private func recordFailure(
        _ cause: DirectListenerFailureCause,
        _ reason: String
    ) -> DirectListenerFailure {
        let failure = DirectListenerFailure(
            step: cause.step,
            code: cause.code,
            backgroundSeconds: awaySeconds,
            at: ContinuousClock.now
        )
        // The diagnostic ring deduplicates equal failures across waits. Recovery
        // presentation needs this attempt, even if it added no diagnostic row.
        if let wait = firstRouteWait, failure.at >= wait.since, firstRouteWanted {
            firstRouteWait?.lastFailure = failure
        }
        appendToRecord(failure)
        let step = cause.step.rawValue
        let code = cause.code.map(String.init) ?? "none"
        Self.logger.error(
            "Direct path: listener failure: \(reason, privacy: .public); step \(step, privacy: .public), code \(code, privacy: .public)"
        )
        return failure
    }

    /// The recovery is over: the listener serves, was retired, or the App
    /// left the foreground.
    private func endRecovery() {
        recovery = nil
        retriesScheduled = 0
        retryWake?.cancel()
        retryWake = nil
    }

    /// Schedules the next attempt, if the listener is failed in the
    /// foreground, a route points at it, and neither an attempt nor a
    /// wake-up for one is under way.
    private func scheduleRetryIfFailed() {
        guard
            retryWake == nil,
            !rebinds.inFlight,
            !isInBackground,
            let listener,
            listener.lifecycle.isPublished,
            case .failed = listener.lifecycle.phase
        else {
            return
        }
        let delays = timing.retrySeconds
        let delay = delays.isEmpty ? 5 : delays[min(retriesScheduled, delays.count - 1)]
        retriesScheduled += 1
        // On the continuous clock, which keeps counting while the device
        // sleeps.
        retryWake = Task { @MainActor [weak self, weak listener] in
            do {
                try await Task.sleep(for: .seconds(delay), clock: .continuous)
            } catch {
                return
            }
            self?.retryDidWake(listener)
        }
    }

    private func retryDidWake(_ woken: DirectConnectListener?) {
        retryWake = nil
        guard let listener, woken === listener else {
            return
        }
        startAttemptIfFailed()
    }

    /// Binds and checks the port again now, if the listener is failed in
    /// the foreground, a route points at it, and no attempt is running.
    private func startAttemptIfFailed() {
        guard
            !rebinds.inFlight,
            !isInBackground,
            let listener,
            let listenerPort,
            listener.lifecycle.isPublished,
            case .failed = listener.lifecycle.phase
        else {
            return
        }
        retryWake?.cancel()
        retryWake = nil
        startRebind(listener, port: listenerPort)
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
        if hearFailure(of: listener, "the listener did not serve in the foreground in time") {
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
        firstRouteWake?.cancel()
        firstRouteWake = nil
        leftForegroundAt = ContinuousClock.now
        // Nothing is bound or watched while the App is away: this stay's
        // recovery ends here, and the return begins its own. An episode
        // already overdue fails the listener here, before the close below;
        // the background itself is never a failure.
        endRecovery()
        listener?.lifecycle.endEpisode()
        disarmWatchdog()
        settleRecordedFault()
        // The relays are closed and counted before the transport is told to
        // stop, which closes its streams: a relay whose stream ended first
        // would no longer be there to count.
        let pathAtBackground = currentPath
        let hostAtBackground = target?.authority.host
        // The link ends with the background, which is no death of its path,
        // and nothing counts as one until the return.
        endProbationLink(.background)
        probation.background()
        deselect(stoppingTransport: false, keepingDiagnostics: true)
        advanceGeneration()
        // Out of service now, before any close runs, and on purpose: what a
        // bind under way reports after this is not a failure. A serving
        // listener is closed here. An attempt still running is closed by
        // its own completion, which parks it or, if its bind had already
        // failed, records that. A failed listener's socket is closed
        // already; the return binds its port again. A first bind still
        // under way has no route pointing at it: its listener is retired,
        // and a later open in the foreground makes a new one.
        if let listener {
            if !listener.lifecycle.isPublished {
                retireAndStop()
            } else if rebinds.inFlight {
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
        // A return that waited only for closes is parked now, not when they
        // are issued.
        if let kept = completionAwaitingCloses {
            completionAwaitingCloses = nil
            rebindDidComplete(kept)
        }
        closeRelays(
            .direct, countsAsClosure: true,
            loss: (.background, pathAtBackground, hostAtBackground, .background)
        ) { [weak self] in
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
        probation.foreground()
        if let wait = firstRouteWait {
            firstRouteWait = FirstRouteWait(runtime: wait.runtime, store: wait.store)
        }
        retryFirstRouteIfNeeded()
        if let leftForegroundAt {
            awaySeconds = Int(leftForegroundAt.duration(to: ContinuousClock.now).components.seconds)
        }
        // A new stay in the foreground: a new episode, with its own
        // deadline, for a listener that does not serve yet.
        armWatchdog()
        if rebinds.inFlight {
            // A return before the previous return's attempt finished: that
            // attempt settles this resume too. Held answers stay held and
            // pending closures stay pending until the listener is ready, or
            // the attempt has failed.
            isResuming = true
            beginRecovery()
            onAvailabilityChanged()
            return
        }
        // Closed for the background, or failed before it: either way the
        // return binds the same port again, and this stay's first attempt
        // is held like any return's.
        guard
            let listener,
            let listenerPort,
            listenerState == .closedForBackground
                || (listenerState == .failed && listener.lifecycle.isPublished)
        else {
            finishResume()
            return
        }
        isResuming = true
        beginRecovery()
        startRebind(listener, port: listenerPort)
        if recovery != nil {
            onAvailabilityChanged()
        }
    }

    /// This stay's recovery begins with the return, if a route points at
    /// the listener: `listenerRecovery` says so until the listener serves.
    private func beginRecovery() {
        endRecovery()
        guard let listener, listener.lifecycle.isPublished else {
            return
        }
        recovery = Recovery(since: ContinuousClock.now, lastFailure: nil)
    }

    /// Binds `port` again, checks it, and settles the return.
    private func startRebind(_ listener: DirectConnectListener, port listenerPort: UInt16) {
        // Each attempt has its own identity: only the current attempt's
        // completion may settle anything.
        completionAwaitingCloses = nil
        let attempt = rebinds.begin()
        let rebindSeconds = timing.rebindSeconds
        let nonce = listener.ownershipNonce
        Task { [weak self] in
            let ended = await Self.bindAndCheck(
                listener,
                port: listenerPort,
                nonce: nonce,
                within: rebindSeconds
            )
            await MainActor.run {
                self?.rebindDidComplete(
                    DirectRebindCompletion(
                        attempt: attempt,
                        listener: listener,
                        port: listenerPort,
                        generation: ended.generation,
                        failure: ended.failure
                    )
                )
            }
        }
    }

    /// One attempt to have `listener` serve `port` again: its socket is
    /// closed, the port is bound, and the listener there is asked for
    /// `nonce`. Returns how it ended (`nil`: this listener answered) and
    /// the bind's generation, if the bind returned one, so that its caller
    /// confirms or rejects exactly that bind.
    ///
    /// `seconds` bounds the whole attempt, spent in two parts: the close
    /// and the bind end at it, and the ownership check then has what is
    /// left of it as its own window, and ends itself there. No second
    /// deadline runs beside it, so what is recorded says which part
    /// failed: `deadline` when the bound ran out before the check could
    /// begin, `ownership` when the port was bound and this listener did
    /// not answer on it in what was left. Neither says the port is someone
    /// else's: the bind is exclusive, so after it only this listener
    /// accepts there, and the attempt's failure closes its socket.
    nonisolated static func bindAndCheck(
        _ listener: DirectConnectListener,
        port: UInt16,
        nonce: String,
        within seconds: TimeInterval
    ) async -> (failure: DirectListenerFailureCause?, generation: Int?) {
        // Recorded as soon as the bind returns, also when the bound then
        // ends the attempt.
        let bound = DirectBindingBox()
        let started = ContinuousClock.now
        let failure: DirectListenerFailureCause?
        do {
            let binding = try await directDeadline(seconds) {
                // The background's close, or the close after a failure, may
                // not have run yet.
                await listener.closeListeningSocket()
                let binding = try await listener.bind(port: port)
                bound.set(binding.generation)
                return binding
            }
            let spent = started.duration(to: ContinuousClock.now)
            let left = seconds
                - Double(spent.components.seconds)
                - Double(spent.components.attoseconds) / 1e18
            if left > 0 {
                let owned = await DirectConnectListener.ownershipCheck(
                    port: binding.port,
                    nonce: nonce,
                    within: left
                )
                failure = owned ? nil : DirectListenerFailureCause(.ownership)
            } else {
                failure = DirectListenerFailureCause(.deadline)
            }
        } catch DirectConnectError.timedOut {
            failure = DirectListenerFailureCause(.deadline)
        } catch {
            // The bind threw. Its lifecycle recorded why, where Network said
            // so; this is only for a bind that left no record.
            failure = DirectListenerFailureCause(.bind, error)
        }
        return (failure, bound.value)
    }

    private func rebindDidComplete(_ completion: DirectRebindCompletion) {
        let attempt = completion.attempt
        let listener = completion.listener
        let listenerPort = completion.port
        let generation = completion.generation
        let ready = completion.failure == nil
        // Only the current attempt settles anything. A stale one (replaced
        // by a newer attempt, or its listener retired) changes no state
        // here; the socket it bound, if any, is the listener's, which the
        // newer attempt closes before binding, and its generation can no
        // longer be confirmed or rejected.
        guard self.listener === listener, rebinds.current == attempt else {
            return
        }
        // A bind that already failed (its lifecycle recorded it) is a
        // failed attempt, in the foreground or the background: failure wins
        // over parking, and over waiting for closes. The attempt is over
        // before the failure is heard, so the next one can be scheduled.
        if listener.lifecycle.failure != nil {
            attemptFailed(attempt, of: listener)
            return
        }
        // Success waits until every relay close decided so far has been
        // issued: the closure the return publishes is then for connections
        // already told to end. The attempt stays current and the episode
        // open, so its watchdog still bounds the wait; the last close
        // handles this completion again (`closeDidIssue`), and a background
        // handles it at once.
        if ready, !isInBackground, closesInFlight > 0 {
            completionAwaitingCloses = completion
            return
        }
        guard rebinds.end(attempt) else {
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
            portLastKnownForeign = false
            disarmWatchdog()
            endRecovery()
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
        // lifecycle's lock, for what the attempt found. Nothing is left
        // binding or verifying with no attempt behind it.
        listener.lifecycle.failUnconfirmed(completion.failure ?? DirectListenerFailureCause(.ownership))
        if hearFailure(of: listener, "the port could not be re-bound and checked") {
            scheduleRetryIfFailed()
            return
        }
        switch listener.lifecycle.phase {
        case .closing, .closed:
            // A background closed it in between: the return binds again.
            startRebind(listener, port: listenerPort)
        default:
            // Serving (nothing to do but settle) or stopped (no listener).
            // Neither is held.
            finishResume()
        }
    }

    /// The current attempt ended with the listener failed: the attempt is
    /// over, the failure is heard if it was not, and the next attempt is
    /// scheduled.
    private func attemptFailed(_ attempt: Int, of listener: DirectConnectListener) {
        guard rebinds.end(attempt) else {
            return
        }
        completionAwaitingCloses = nil
        hearFailure(of: listener, "the port could not be re-bound and checked")
        scheduleRetryIfFailed()
    }

    /// Publishes the closures the background caused, if a page that acts on
    /// them can reach anything: the listener serves, or its port is closed
    /// or refusing, so that WebKit fails the page's new connections over to
    /// its normal path. While something else may hold the port they stay
    /// pending: a page told to reconnect now would send that connection to
    /// the port's holder, and a terminal does not try a second time. They
    /// are published by the attempt that finds the port ours, or closed.
    /// `true` when this call published any.
    @discardableResult
    private func publishPendingClosures() -> Bool {
        guard !pendingClosureHosts.isEmpty else {
            return false
        }
        if let listener, listener.lifecycle.isPublished, listenerGate(listener) == .fault {
            return false
        }
        for host in pendingClosureHosts {
            closures[host, default: 0] += 1
        }
        pendingClosureHosts.removeAll()
        sequence += 1
        return true
    }

    /// Publishes the closures the background caused (unless the port may
    /// be someone else's), answers the held status requests, sends the
    /// state once more, and selects again.
    private func finishResume() {
        isResuming = false
        publishPendingClosures()
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
            entry.reply(currentStatus(for: entry.origin))
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

/// What one re-bind attempt ended with.
struct DirectRebindCompletion: Sendable {
    let attempt: Int
    let listener: DirectConnectListener
    let port: UInt16
    let generation: Int?
    /// Why the attempt did not find this listener on the port; `nil` when
    /// it did.
    let failure: DirectListenerFailureCause?
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

/// Every WKWebView the App makes that may load an instance, held weakly,
/// so a hand-over can tell whether another live document of the same data
/// store has a host loaded (`direct.handover`). A WebView is listed from
/// creation until it is deallocated, whether or not it is on screen: one
/// that still exists may still hold connections.
@MainActor
final class DirectWebViewRegistry {
    static let shared = DirectWebViewRegistry()

    /// What a listed WebView is, for the record of a hand-over it refused.
    enum Kind: Sendable {
        /// A runtime's own page.
        case runtime
        /// A window a page of the runtime opened.
        case child
        /// A voice take's hidden WebView.
        case voice

        var other: DirectOtherDocument {
            switch self {
            case .runtime: return .runtime
            case .child: return .child
            case .voice: return .voice
            }
        }
    }

    private final class Entry {
        weak var webView: WKWebView?
        let kind: Kind

        init(_ webView: WKWebView, _ kind: Kind) {
            self.webView = webView
            self.kind = kind
        }
    }

    private var entries: [Entry] = []

    func register(_ webView: WKWebView, as kind: Kind) {
        entries.removeAll { $0.webView == nil }
        guard !entries.contains(where: { $0.webView === webView }) else {
            return
        }
        entries.append(Entry(webView, kind))
    }

    /// What else of `webView`'s data store has `host` loaded or loading:
    /// `sole` when nothing has. Of several, one that has the host loaded is
    /// named before one with no address yet, and among those a voice take
    /// before a child window before a runtime's page. `unknown` when the
    /// caller is not listed.
    func otherDocument(than webView: WKWebView, host: String) -> DirectOtherDocument {
        entries.removeAll { $0.webView == nil }
        guard entries.contains(where: { $0.webView === webView }) else {
            return .unknown
        }
        let store = webView.configuration.websiteDataStore
        var loaded: [Kind] = []
        var unloaded = false
        for entry in entries {
            guard let other = entry.webView, other !== webView else {
                continue
            }
            // Two objects can name one persistent store.
            let otherStore = other.configuration.websiteDataStore
            let sameStore = otherStore === store
                || (store.identifier != nil && otherStore.identifier == store.identifier)
            guard sameStore else {
                continue
            }
            guard let url = other.url else {
                unloaded = true
                continue
            }
            if url.host?.lowercased() == host.lowercased() {
                loaded.append(entry.kind)
            }
        }
        for kind in [Kind.voice, .child, .runtime] where loaded.contains(kind) {
            return kind.other
        }
        return unloaded ? .unloaded : .sole
    }
}
