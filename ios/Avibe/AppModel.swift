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

@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var route: AppRoute = .loading
    /// Advances each time a runtime opens, so the root builds a new runtime
    /// controller even when the same instance is selected again.
    @Published private(set) var runtimeIdentity = 0

    let websiteDataStore: AccountWebsiteDataStore

    private let defaults: UserDefaults
    private let controlPlane: MobileControlPlaneConfiguration
    private weak var runtimeController: WebViewController?
    private var pendingRuntimeReleaseBarriers: [
        WebsiteDataStoreReleaseBarrier
    ] = []
    private var pendingPushOpenedURL: String?

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
            await self?.nativePushCoordinator.prepareForLogout()
        }
    )

    private lazy var nativePushCoordinator = NativePushCoordinator(
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
        controlPlane: MobileControlPlaneConfiguration = MobileControlPlaneConfiguration()
    ) {
        self.defaults = defaults
        self.controlPlane = controlPlane
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
        nativePushCoordinator.resumeAfterAccountActivation()
        stopRuntime()
        runtimeIdentity += 1
        route = .browser(runtime)
    }

    func didFinishRuntimeLoading() {
        guard
            let pendingPushOpenedURL,
            let runtimeController
        else {
            return
        }
        self.pendingPushOpenedURL = nil
        runtimeController.deliverPushOpened(path: pendingPushOpenedURL)
    }

    private func handlePushTap(_ tap: PushNotificationTap) {
        guard
            let account = sessionCoordinator.currentAccount,
            account.id == tap.accountID,
            let instance = sessionCoordinator.currentInstances.first(where: {
                $0.available
                    && WebOrigin(url: $0.url)?.serialized == tap.instanceOrigin
            }),
            Self.isSafeInstancePath(tap.url)
        else {
            pendingPushOpenedURL = nil
            showInstancePicker()
            return
        }
        pendingPushOpenedURL = tap.url
        do {
            _ = try sessionCoordinator.selectInstance(id: instance.id)
        } catch {
            pendingPushOpenedURL = nil
            showInstancePicker()
        }
    }

    private static func isSafeInstancePath(_ value: String) -> Bool {
        guard
            !value.isEmpty,
            value.first == "/",
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
