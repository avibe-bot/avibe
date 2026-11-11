import Foundation
import Network
import UIKit
import WebKit

/// Which path carries the selected instance's origin, as the bridge reports
/// it (`direct.status()` and `direct.transportChanged`).
struct DirectPathStatus: Equatable {
    enum Transport: String, Equatable {
        /// Cloudflare Tunnel: WebKit resolves the public name itself.
        case tunnel
        /// The loopback CONNECT listener and a direct transport stream.
        case direct
    }

    let transport: Transport
    /// Grows with every change while the App process lives.
    let sequence: Int

    /// Before any change: the tunnel.
    static let initial = DirectPathStatus(transport: .tunnel, sequence: 0)

    var jsonObject: [String: Any] {
        [
            "transport": transport.rawValue,
            "sequence": sequence,
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

/// The only writer of `proxyConfigurations` on an account data store. The
/// runtime WebViews and the hidden transports share that store, so a route
/// set here applies to all of them, and nothing else may set one.
@MainActor
final class DirectRouteOwner {
    /// How many times this process has written `proxyConfigurations`. Every
    /// write goes through `write(_:to:)` below, the only assignment in the
    /// App, so a build that never installs a route leaves this at zero, and
    /// a test can read that.
    private(set) static var writeCount = 0

    private let websiteDataStore: AccountWebsiteDataStore
    private weak var routedStore: WKWebsiteDataStore?
    private(set) var installedTag: DirectRouteTag?

    init(websiteDataStore: AccountWebsiteDataStore) {
        self.websiteDataStore = websiteDataStore
    }

    /// Routes `tag`'s host, and only that host, to the loopback listener on
    /// `proxyPort`. Refuses a tag made for a store that is no longer the
    /// account's current one.
    func install(_ tag: DirectRouteTag, proxyPort: NWEndpoint.Port) -> Bool {
        guard tag.target.storeIdentifier == websiteDataStore.identifier else {
            return false
        }
        remove()
        var route = ProxyConfiguration(
            httpCONNECTProxy: .hostPort(host: "127.0.0.1", port: proxyPort)
        )
        // A suffix match: the listener refuses the subdomains it also gets.
        route.matchDomains = [tag.target.authority.host]
        route.allowFailover = false
        let store = websiteDataStore.dataStore
        write([route], to: store)
        routedStore = store
        installedTag = tag
        return true
    }

    /// Removes the route from the store it was installed on. Without one,
    /// this touches nothing.
    func remove() {
        guard installedTag != nil else {
            return
        }
        if let routedStore {
            write([], to: routedStore)
        }
        routedStore = nil
        installedTag = nil
    }

    private func write(
        _ configurations: [ProxyConfiguration],
        to store: WKWebsiteDataStore
    ) {
        store.proxyConfigurations = configurations
        Self.writeCount += 1
    }
}

/// Decides when the selected instance's origin travels the direct path. The
/// state is `tunnel` unless a transport reported a usable path for the
/// current tags, the listener is bound, and the route is installed; any
/// failure returns to `tunnel` at once.
///
/// Without a transport it creates no listener, writes no route, and reports
/// no change. It still exists from launch: it reads the application state
/// once, sets the store's `onWillRotate` hook, and observes the App
/// entering the background and the foreground.
@MainActor
final class DirectPathController {
    private struct Activation {
        let id: Int
        let tag: DirectRouteTag
        let listener: DirectConnectListener
        var isInstalled = false
    }

    private let websiteDataStore: AccountWebsiteDataStore
    private let routeOwner: DirectRouteOwner
    private let transport: DirectTransporting?
    private let isCurrentRuntime: (MobileRuntimeContext) -> Bool
    private let limits: DirectConnectLimits
    private let onStatusChanged: (DirectPathStatus) -> Void
    private let notificationCenter: NotificationCenter
    private var lifecycleObservers: [NSObjectProtocol] = []

    private var runtime: MobileRuntimeContext?
    private var target: DirectTarget?
    private var selection = 0
    private var acceptedGeneration: String?
    private var replacedGenerations: Set<String> = []
    private var activation: Activation?
    private var activationCount = 0
    private var isInBackground: Bool
    private var current = DirectPathStatus.initial

    init(
        websiteDataStore: AccountWebsiteDataStore,
        transport: DirectTransporting?,
        isCurrentRuntime: @escaping (MobileRuntimeContext) -> Bool,
        limits: DirectConnectLimits = .standard,
        notificationCenter: NotificationCenter = .default,
        isInBackground: Bool? = nil,
        onStatusChanged: @escaping (DirectPathStatus) -> Void
    ) {
        self.websiteDataStore = websiteDataStore
        routeOwner = DirectRouteOwner(websiteDataStore: websiteDataStore)
        self.transport = transport
        self.isCurrentRuntime = isCurrentRuntime
        self.limits = limits
        self.notificationCenter = notificationCenter
        // The App can be launched into the background.
        self.isInBackground = isInBackground
            ?? (UIApplication.shared.applicationState == .background)
        self.onStatusChanged = onStatusChanged

        // The route leaves with the store it was installed on.
        websiteDataStore.onWillRotate = { [weak self] in
            self?.runtimeDidStop()
        }
        // A listening socket does not survive suspension reliably, so the
        // background always returns to the tunnel.
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
    }

    /// The path that carries `origin` now. Only the selected instance's
    /// origin can be on the direct path.
    func status(for origin: WebOrigin) -> DirectPathStatus {
        guard
            let activation,
            activation.isInstalled,
            WebOrigin(url: activation.tag.target.runtime.instance.url) == origin
        else {
            return DirectPathStatus(
                transport: .tunnel,
                sequence: current.sequence
            )
        }
        return current
    }

    /// A runtime opened. It starts on the tunnel. A lease that is not the
    /// account's current one selects nothing.
    func runtimeDidOpen(_ runtime: MobileRuntimeContext) {
        runtimeDidStop()
        guard isCurrentRuntime(runtime) else {
            return
        }
        self.runtime = runtime
        select()
    }

    /// The runtime stopped: an instance switch, the instance picker, an
    /// account transition, or the data store rotating.
    func runtimeDidStop() {
        runtime = nil
        deselect()
    }

    private func applicationDidEnterBackground() {
        isInBackground = true
        deselect()
    }

    private func applicationWillEnterForeground() {
        isInBackground = false
        select()
    }

    /// Starts a selection for the open runtime. Everything a transport
    /// reported for an earlier selection is void.
    private func select() {
        guard
            let transport,
            let runtime,
            target == nil,
            !isInBackground,
            let authority = DirectAuthority(instanceURL: runtime.instance.url)
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
        returnToTunnel()
        guard target != nil else {
            return
        }
        target = nil
        acceptedGeneration = nil
        replacedGenerations.removeAll()
        transport?.deselect()
    }

    private func receive(_ report: DirectPathReport, for target: DirectTarget) {
        // A report for a selection that has ended.
        guard target == self.target else {
            return
        }
        switch report {
        case .lost:
            returnToTunnel()
        case .usable(let generation):
            // Generations never repeat, so one that a newer answer replaced
            // can only be a late result.
            guard !replacedGenerations.contains(generation) else {
                return
            }
            if let acceptedGeneration, acceptedGeneration != generation {
                replacedGenerations.insert(acceptedGeneration)
            }
            acceptedGeneration = generation
            let tag = DirectRouteTag(
                target: target,
                rendezvousGeneration: generation
            )
            guard activation?.tag != tag else {
                return
            }
            activate(tag)
        }
    }

    /// Binds a listener for `tag`, then installs the route if `tag` is still
    /// current when the listener is ready.
    private func activate(_ tag: DirectRouteTag) {
        returnToTunnel()
        guard let transport else {
            return
        }
        activationCount += 1
        let id = activationCount
        let listener = DirectConnectListener(
            authority: tag.target.authority,
            limits: limits,
            openStream: {
                try await transport.openStream(for: tag)
            },
            onFailure: { [weak self] _ in
                Task { @MainActor in
                    self?.activationDidFail(id)
                }
            }
        )
        activation = Activation(id: id, tag: tag, listener: listener)
        Task { [weak self] in
            do {
                let port = try await listener.start()
                guard let self else {
                    await listener.stop()
                    return
                }
                self.listenerDidStart(on: port, activation: id)
            } catch {
                self?.activationDidFail(id)
            }
        }
    }

    private func listenerDidStart(
        on port: NWEndpoint.Port,
        activation id: Int
    ) {
        // An activation that was abandoned has already stopped its listener.
        guard var activation, activation.id == id else {
            return
        }
        guard
            admits(activation.tag),
            routeOwner.install(activation.tag, proxyPort: port)
        else {
            // The lease also reads as not current while the account
            // coordinator runs a request of its own, with no transition.
            // Either way the App stays on the tunnel, and the transport
            // decides when to report the path again.
            activationDidFail(id)
            return
        }
        activation.isInstalled = true
        self.activation = activation
        setTransport(.direct)
    }

    /// Whether every tag of `tag` is still current. The lease check is the
    /// one the App's route and WebView registration already use.
    private func admits(_ tag: DirectRouteTag) -> Bool {
        tag.target == target
            && tag.rendezvousGeneration == acceptedGeneration
            && !isInBackground
            && isCurrentRuntime(tag.target.runtime)
    }

    /// The listener could not be bound, the route could not be installed, or
    /// a connection could not be served.
    private func activationDidFail(_ id: Int) {
        guard let activation, activation.id == id else {
            return
        }
        returnToTunnel()
        transport?.pathDidFail(for: activation.tag)
    }

    /// Removes the route first, so every connection WebKit opens from here
    /// on uses the tunnel, then closes the listener and its relays.
    private func returnToTunnel() {
        guard let activation else {
            return
        }
        self.activation = nil
        routeOwner.remove()
        Task {
            await activation.listener.stop()
        }
        setTransport(.tunnel)
    }

    private func setTransport(_ transport: DirectPathStatus.Transport) {
        guard current.transport != transport else {
            return
        }
        current = DirectPathStatus(
            transport: transport,
            sequence: current.sequence + 1
        )
        onStatusChanged(current)
    }
}
