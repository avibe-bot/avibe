import SwiftUI

enum MobileOnboardingStartScreen: String, Equatable {
    case account
    case instances
    /// Account over a live runtime: Back calls `app.returnToInstance`.
    case runtimeAccount = "runtime-account"
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
            returnToRuntime: returnToRuntime
        )
    }

    func updateUIViewController(
        _ uiViewController: MobileOnboardingViewController,
        context: Context
    ) {}
}
