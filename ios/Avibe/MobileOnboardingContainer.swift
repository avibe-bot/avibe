import SwiftUI

enum MobileOnboardingStartScreen: String, Equatable {
    case account
    case instances
    /// The instances sheet over a live runtime. The sheet's Close button and
    /// native dismissal return to that runtime without reloading it.
    case sheet
    /// Variant B's named-instance entry; the bundled page omits Account.
    case sheetInstances = "sheet-instances"
    /// Account over a live runtime: Back calls `app.returnToInstance`.
    case runtimeAccount = "runtime-account"

    var isSheet: Bool {
        self == .sheet || self == .sheetInstances
    }
}

struct MobileOnboardingContainer: UIViewControllerRepresentable {
    let coordinator: MobileSessionCoordinator
    let startScreen: MobileOnboardingStartScreen
    let returnToRuntime: () -> Bool

    func makeUIViewController(
        context: Context
    ) -> MobileOnboardingViewController {
        MobileOnboardingViewController(
            coordinator: coordinator,
            startScreen: startScreen,
            returnToRuntime: returnToRuntime,
            dismissSurface: returnToRuntime
        )
    }

    func updateUIViewController(
        _ uiViewController: MobileOnboardingViewController,
        context: Context
    ) {}
}
