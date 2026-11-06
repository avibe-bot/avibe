import SwiftUI

enum MobileOnboardingStartScreen: String, Equatable {
    case account
    case instances
}

struct MobileOnboardingContainer: UIViewControllerRepresentable {
    let coordinator: MobileSessionCoordinator
    let startScreen: MobileOnboardingStartScreen

    func makeUIViewController(
        context: Context
    ) -> MobileOnboardingViewController {
        MobileOnboardingViewController(
            coordinator: coordinator,
            startScreen: startScreen
        )
    }

    func updateUIViewController(
        _ uiViewController: MobileOnboardingViewController,
        context: Context
    ) {}
}
