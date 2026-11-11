import SwiftUI
import WebKit

enum AppRoute: Equatable {
    case loading
    case onboarding(MobileOnboardingStartScreen)
    case browser(MobileRuntimeContext)
    /// The bundled account page covers the runtime it was opened from. That
    /// runtime keeps its document until an instance switch or an account
    /// transition releases it.
    case accountOverBrowser(MobileRuntimeContext)

    /// The runtime the root keeps alive, shown or covered by Account.
    var runtime: MobileRuntimeContext? {
        switch self {
        case .browser(let runtime), .accountOverBrowser(let runtime):
            return runtime
        case .loading, .onboarding:
            return nil
        }
    }

    /// The bundled page the root shows, above any covered runtime.
    var onboardingStartScreen: MobileOnboardingStartScreen? {
        switch self {
        case .onboarding(let startScreen):
            return startScreen
        case .accountOverBrowser:
            return .runtimeAccount
        case .loading, .browser:
            return nil
        }
    }

    var coversRuntime: Bool {
        if case .accountOverBrowser = self {
            return true
        }
        return false
    }
}

private struct PendingPushOpened {
    let id: String
    let accountID: String
    let instanceOrigin: String
    let url: String
    let runtimeIdentity: Int
    let createdAt: Date
}

@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var route: AppRoute = .loading
    /// Advances each time a runtime opens, so the root builds a new runtime
    /// controller even when the same instance is selected again.
    @Published private(set) var runtimeIdentity = 0

    let websiteDataStore: AccountWebsiteDataStore

    private let defaults: UserDefaults
    private let controlPlane: MobileControlPlaneConfiguration
    private let pushKeychain: PushKeychainStoring
    private let pushGenerationStore: PushGenerationStoring
    private let pushInstallationMarker: PushInstallationMarkerStoring
    private let pushNotificationCenter: PushNotificationCentering?
    private let pushOpenedNow: () -> Date
    private let pushOpenedLifetime: TimeInterval
    /// The App icon badge: the selected instance's Inbox total, which its Web
    /// UI sets through `badge.set` (and a push sets while the App is away).
    private let nativeBadge: NativeBadge
    private weak var runtimeController: WebViewController?
    private var pendingRuntimeReleaseBarriers: [
        WebsiteDataStoreReleaseBarrier
    ] = []
    private var hasCompletedBootstrap = false
    private var pendingPushTap: PushNotificationTap?
    private var pendingPushOpened: PendingPushOpened?
    private var pendingPushOpenedExpiryTask: Task<Void, Never>?

    private lazy var sessionCoordinator = MobileSessionCoordinator(
        defaults: defaults,
        controlPlane: controlPlane,
        websiteDataStore: websiteDataStore,
        onOpenRuntime: { [weak self] runtime in
            self?.openRuntime(runtime)
        },
        onPrepareAccountTransition: { [weak self] in
            self?.prepareForAccountTransition() ?? []
        },
        onPreparePushLogout: { [weak self] in
            // Signing out or another account: the badge counted this
            // account's Inbox. (A session refresh of the same account keeps it.)
            self?.nativeBadge.clear()
            await self?.nativePushCoordinator.prepareForLogout()
        }
    )

    private lazy var nativePushCoordinator = NativePushCoordinator(
        keychain: pushKeychain,
        generationStore: pushGenerationStore,
        managementRequest: { [weak self] request in
            guard let self else {
                throw NativePushError.unavailable
            }
            return try await self.mobileSessionCoordinator.pushManagementRequest(
                request
            )
        },
        accountProvider: { [weak self] in
            self?.mobileSessionCoordinator.currentAccount
        },
        installationMarker: pushInstallationMarker,
        notificationCenter: pushNotificationCenter,
        onTap: { [weak self] tap in
            self?.handlePushTap(tap)
        },
        onTapRecovery: { [weak self] in
            self?.showInstancePicker()
        }
    )

    var mobileSessionCoordinator: MobileSessionCoordinator {
        sessionCoordinator
    }

    var pushCoordinator: NativePushCoordinator {
        nativePushCoordinator
    }

    var runtimeWebsiteDataStore: WKWebsiteDataStore {
        websiteDataStore.dataStore
    }

    init(
        defaults: UserDefaults = .standard,
        controlPlane: MobileControlPlaneConfiguration =
            MobileControlPlaneConfiguration(),
        pushKeychain: PushKeychainStoring = PushKeychainStore(),
        pushGenerationStore: PushGenerationStoring =
            SharedPushGenerationStore(),
        pushInstallationMarker: PushInstallationMarkerStoring =
            UserDefaultsPushInstallationMarkerStore(),
        pushNotificationCenter: PushNotificationCentering? = nil,
        pushOpenedNow: @escaping () -> Date = Date.init,
        pushOpenedLifetime: TimeInterval = 120,
        badgeCenter: AppBadgeCentering? = nil
    ) {
        self.defaults = defaults
        self.controlPlane = controlPlane
        self.pushKeychain = pushKeychain
        self.pushGenerationStore = pushGenerationStore
        self.pushInstallationMarker = pushInstallationMarker
        self.pushNotificationCenter = pushNotificationCenter
        self.pushOpenedNow = pushOpenedNow
        self.pushOpenedLifetime = pushOpenedLifetime
        nativeBadge = NativeBadge(center: badgeCenter ?? SystemAppBadgeCenter())
        websiteDataStore = AccountWebsiteDataStore(defaults: defaults)
        _ = nativePushCoordinator

        Task { [weak self] in
            await self?.bootstrap()
        }
    }

    func registerRuntimeController(
        _ controller: WebViewController,
        for runtime: MobileRuntimeContext
    ) {
        guard
            route == .browser(runtime),
            sessionCoordinator.isCurrentRuntime(runtime)
        else {
            recordRuntimeReleaseBarrier(
                controller.invalidateForAccountTransition()
            )
            if route == .browser(runtime) {
                route = .onboarding(.instances)
            }
            return
        }
        runtimeController = controller
    }

    func recordRuntimeReleaseBarrier(
        _ barrier: WebsiteDataStoreReleaseBarrier
    ) {
        pendingRuntimeReleaseBarriers.removeAll(where: \.isReleased)
        guard !barrier.isReleased else {
            return
        }
        pendingRuntimeReleaseBarriers.append(barrier)
    }

    /// Covers the live runtime with the bundled account page, keeping its
    /// document, history, and scroll position for `returnToRuntime()`.
    func showAccount() {
        switch route {
        case .accountOverBrowser:
            return
        case .browser(let runtime)
            where runtimeController != nil
                && sessionCoordinator.isCurrentRuntime(runtime):
            runtimeController?.prepareForAccountCover()
            route = .accountOverBrowser(runtime)
        default:
            stopRuntime()
            route = .onboarding(.account)
        }
    }

    /// Shows the runtime the account page covers again, unchanged. Returns
    /// `false` once a switch or an account transition has released it.
    func returnToRuntime() -> Bool {
        guard
            case .accountOverBrowser(let runtime) = route,
            runtimeController != nil,
            sessionCoordinator.isCurrentRuntime(runtime)
        else {
            return false
        }
        sessionCoordinator.deactivateAccountSurface()
        route = .browser(runtime)
        return true
    }

    func showInstancePicker() {
        clearPendingPushOpened()
        stopRuntime()
        route = .onboarding(.instances)
    }

    private func bootstrap() async {
        await websiteDataStore.sweepOrphans()
        sessionCoordinator.activateAccountSurface()
        do {
            let state = try await sessionCoordinator.bootstrap()
            if let runtime = sessionCoordinator.restorableRuntime(from: state) {
                sessionCoordinator.deactivateAccountSurface()
                openRuntime(runtime)
            } else {
                route = .onboarding(.instances)
            }
        } catch {
            route = .onboarding(.instances)
        }
        hasCompletedBootstrap = true
        if let pendingPushTap {
            self.pendingPushTap = nil
            handlePushTap(pendingPushTap)
        }
    }

    private func stopRuntime() {
        if let runtimeController {
            recordRuntimeReleaseBarrier(
                runtimeController.invalidateForAccountTransition()
            )
        }
        runtimeController = nil
    }

    private func openRuntime(_ runtime: MobileRuntimeContext) {
        guard sessionCoordinator.isCurrentRuntime(runtime) else {
            return
        }
        if let pendingPushOpened,
           pendingPushOpened.instanceOrigin
                != WebOrigin(url: runtime.instance.url)?.serialized
        {
            clearPendingPushOpened()
        }
        nativePushCoordinator.resumeAfterAccountActivation()
        stopRuntime()
        runtimeIdentity += 1
        route = .browser(runtime)
    }

    func didFinishRuntimeLoading() {
        guard
            let pendingPushOpened,
            let runtimeController
        else {
            return
        }
        guard pendingPushOpened.createdAt.addingTimeInterval(
            pushOpenedLifetime
        ) > pushOpenedNow(),
              pendingPushOpened.runtimeIdentity == runtimeIdentity,
              sessionCoordinator.currentAccount?.id == pendingPushOpened.accountID,
              sessionCoordinator.currentInstances.contains(where: {
                  $0.available
                      && WebOrigin(url: $0.url)?.serialized
                          == pendingPushOpened.instanceOrigin
              })
        else {
            clearPendingPushOpened()
            return
        }
        runtimeController.deliverPushOpened(
            id: pendingPushOpened.id,
            path: pendingPushOpened.url
        )
    }

    @discardableResult
    func acknowledgePushOpened(id: String, origin: WebOrigin) -> Bool {
        guard
            let pendingPushOpened,
            pendingPushOpened.id == id,
            pendingPushOpened.instanceOrigin == origin.serialized,
            pendingPushOpened.runtimeIdentity == runtimeIdentity,
            pendingPushOpened.createdAt.addingTimeInterval(
                pushOpenedLifetime
            ) > pushOpenedNow(),
            sessionCoordinator.currentAccount?.id == pendingPushOpened.accountID,
            let runtime = route.runtime,
            WebOrigin(url: runtime.instance.url)?.serialized == origin.serialized
        else {
            if pendingPushOpened?.id == id {
                clearPendingPushOpened()
            }
            return false
        }
        clearPendingPushOpened()
        return true
    }

    /// `badge.set` from a runtime page: only the selected instance's page
    /// sets the badge, the rule `push.*` follows.
    @discardableResult
    func setBadgeCount(_ count: Int, origin: WebOrigin) -> Bool {
        guard
            let runtime = route.runtime,
            sessionCoordinator.isCurrentRuntime(runtime),
            WebOrigin(url: runtime.instance.url)?.serialized == origin.serialized
        else {
            return false
        }
        nativeBadge.set(count)
        return true
    }

    private func handlePushTap(_ tap: PushNotificationTap) {
        guard hasCompletedBootstrap else {
            pendingPushTap = tap
            return
        }
        guard
            let account = sessionCoordinator.currentAccount,
            account.id == tap.accountID,
            let instance = sessionCoordinator.currentInstances.first(where: {
                $0.available
                    && WebOrigin(url: $0.url)?.serialized == tap.instanceOrigin
            }),
            Self.isSafeInstancePath(tap.url)
        else {
            clearPendingPushOpened()
            showInstancePicker()
            return
        }
        let pending = PendingPushOpened(
            id: UUID().uuidString,
            accountID: tap.accountID,
            instanceOrigin: tap.instanceOrigin,
            url: tap.url,
            runtimeIdentity: runtimeIdentity + 1,
            createdAt: pushOpenedNow()
        )
        pendingPushOpened = pending
        pendingPushOpenedExpiryTask?.cancel()
        let lifetime = pushOpenedLifetime
        pendingPushOpenedExpiryTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(lifetime))
            guard !Task.isCancelled else {
                return
            }
            self?.expirePendingPushOpened(id: pending.id)
        }
        do {
            _ = try sessionCoordinator.selectInstance(id: instance.id)
        } catch {
            clearPendingPushOpened()
            showInstancePicker()
        }
    }

    private func expirePendingPushOpened(id: String) {
        guard pendingPushOpened?.id == id else {
            return
        }
        clearPendingPushOpened()
    }

    private func clearPendingPushOpened() {
        pendingPushOpenedExpiryTask?.cancel()
        pendingPushOpenedExpiryTask = nil
        pendingPushOpened = nil
    }

    private static func isSafeInstancePath(_ value: String) -> Bool {
        guard
            !value.isEmpty,
            value.first == "/",
            !value.contains("\\"),
            !value.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains),
            let components = URLComponents(string: value),
            components.scheme == nil,
            components.host == nil,
            components.user == nil,
            components.password == nil
        else {
            return false
        }
        return true
    }

    private func prepareForAccountTransition() -> [
        WebsiteDataStoreReleaseBarrier
    ] {
        clearPendingPushOpened()
        stopRuntime()
        switch route {
        case .browser:
            route = .onboarding(.instances)
        case .accountOverBrowser:
            // The account page stays where it is; only the runtime goes.
            route = .onboarding(.account)
        case .loading, .onboarding:
            break
        }
        let barriers = pendingRuntimeReleaseBarriers
        pendingRuntimeReleaseBarriers.removeAll()
        return barriers
    }
}
