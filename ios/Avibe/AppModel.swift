import SwiftUI
import WebKit

enum AppRoute: Equatable {
    case loading
    case onboarding(MobileOnboardingStartScreen)
    case browser(MobileRuntimeContext)
}

@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var route: AppRoute = .loading

    let websiteDataStore: AccountWebsiteDataStore

    private let defaults: UserDefaults
    private let controlPlane: MobileControlPlaneConfiguration
    private let transportFactory: MobileSessionCoordinator.TransportFactory
    private weak var runtimeController: WebViewController?
    private var pendingRuntimeReleaseBarriers: [
        WebsiteDataStoreReleaseBarrier
    ] = []

    private lazy var sessionCoordinator = MobileSessionCoordinator(
        defaults: defaults,
        controlPlane: controlPlane,
        websiteDataStore: websiteDataStore,
        transportFactory: transportFactory,
        onOpenRuntime: { [weak self] runtime in
            self?.openRuntime(runtime)
        },
        onPrepareAccountTransition: { [weak self] in
            self?.prepareForAccountTransition() ?? []
        }
    )

    var mobileSessionCoordinator: MobileSessionCoordinator {
        sessionCoordinator
    }

    var runtimeWebsiteDataStore: WKWebsiteDataStore {
        websiteDataStore.dataStore
    }

    init(
        defaults: UserDefaults = .standard,
        controlPlane: MobileControlPlaneConfiguration = MobileControlPlaneConfiguration(),
        websiteDataStore: AccountWebsiteDataStore? = nil,
        transportFactory: @escaping MobileSessionCoordinator.TransportFactory = {
            MobileAuthTransport(controlPlaneURL: $0, websiteDataStore: $1)
        }
    ) {
        self.defaults = defaults
        self.controlPlane = controlPlane
        self.websiteDataStore =
            websiteDataStore ?? AccountWebsiteDataStore(defaults: defaults)
        self.transportFactory = transportFactory

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

    func showAccount() {
        stopRuntime()
        route = .onboarding(.account)
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
        stopRuntime()
        route = .browser(runtime)
    }

    private func prepareForAccountTransition() -> [
        WebsiteDataStoreReleaseBarrier
    ] {
        stopRuntime()
        if case .browser = route {
            route = .onboarding(.instances)
        }
        let barriers = pendingRuntimeReleaseBarriers
        pendingRuntimeReleaseBarriers.removeAll()
        return barriers
    }
}
