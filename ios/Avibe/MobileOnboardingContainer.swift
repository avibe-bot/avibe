import SwiftUI

enum MobileOnboardingStartScreen: String, Equatable {
    case account
    case instances
    /// The instances sheet over a live runtime. The sheet's Close button and
    /// native dismissal return to that runtime without reloading it.
    case sheet
    /// Variant B's named-instance entry; the bundled page omits Account.
    case sheetInstances = "sheet-instances"
    /// Variant B's Home avatar: Account is the panel root, without Back.
    case sheetAccount = "sheet-account"
    /// Account over a live runtime: Back calls `app.returnToInstance`.
    case runtimeAccount = "runtime-account"

    var isSheet: Bool {
        self == .sheet || self == .sheetInstances || self == .sheetAccount
    }
}

struct MobileOnboardingContainer: UIViewControllerRepresentable {
    let coordinator: MobileSessionCoordinator
    let startScreen: MobileOnboardingStartScreen
    let returnToRuntime: () -> Bool
    /// The instance page's scheme for a sheet over it; `nil` follows the system.
    var pageColorScheme: PageColorScheme? = nil

    func makeUIViewController(
        context: Context
    ) -> MobileOnboardingViewController {
        let controller = MobileOnboardingViewController(
            coordinator: coordinator,
            startScreen: startScreen,
            returnToRuntime: returnToRuntime,
            dismissSurface: returnToRuntime
        )
        controller.pageColorScheme = pageColorScheme
        return controller
    }

    func updateUIViewController(
        _ uiViewController: MobileOnboardingViewController,
        context: Context
    ) {
        uiViewController.pageColorScheme = pageColorScheme
    }
}
