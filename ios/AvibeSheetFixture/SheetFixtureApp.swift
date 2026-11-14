import UIKit
import WebKit

@main
final class SheetFixtureAppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [
            UIApplication.LaunchOptionsKey: Any
        ]? = nil
    ) -> Bool {
        let window = UIWindow(frame: UIScreen.main.bounds)
        window.rootViewController = SheetFixtureRootViewController()
        window.makeKeyAndVisible()
        self.window = window
        return true
    }
}

@MainActor
private final class SheetFixtureRootViewController: UIViewController {
    private var sheetController: MobileOnboardingViewController?
    private lazy var coordinator: MobileSessionCoordinator = {
        let defaults = UserDefaults(suiteName: "avibe.ios.sheet-fixture")!
        let store = AccountWebsiteDataStore(defaults: defaults)
        return MobileSessionCoordinator(
            defaults: defaults,
            websiteDataStore: store,
            transportFactory: { _, _ in
                FixtureMobileAuthTransport(snapshot: Self.snapshot)
            },
            onOpenRuntime: { _ in },
            onPrepareAccountTransition: { [] }
        )
    }()

    private static let snapshot = MobileSessionSnapshot(
        account: MobileAccount(id: "fixture-account", email: "fixture@example.com"),
        instances: (1...32).map { index in
            MobileInstance(
                id: "fixture-\(index)",
                name: "Fixture instance \(index)",
                host: "instance-\(index).avibe.bot",
                url: URL(string: "https://instance-\(index).avibe.bot")!,
                relationship: index.isMultiple(of: 3) ? "shared" : "owner",
                ownerEmail: index.isMultiple(of: 3) ? "owner@example.com" : nil,
                available: !index.isMultiple(of: 11)
            )
        }
    )

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        view.accessibilityIdentifier = "fixture-root"
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        guard sheetController == nil else { return }
        let resourceURL = Bundle.main.url(
            forResource: "mobile",
            withExtension: "html",
            subdirectory: "FixtureMobile"
        )!
        let controller = MobileOnboardingViewController(
            coordinator: coordinator,
            startScreen: .sheet,
            resourceURL: resourceURL,
            returnToRuntime: { [weak self] in
                self?.dismissFixtureSheet() ?? false
            },
            dismissSurface: { [weak self] in
                self?.dismissFixtureSheet() ?? false
            }
        )
        controller.modalPresentationStyle = .pageSheet
        sheetController = controller
        present(controller, animated: false)
    }

    @discardableResult
    private func dismissFixtureSheet() -> Bool {
        guard let controller = sheetController else { return false }
        controller.dismiss(animated: true) { [weak self] in
            self?.sheetController = nil
        }
        return true
    }
}

@MainActor
private final class FixtureMobileAuthTransport: MobileAuthTransporting {
    private let session: MobileSessionSnapshot

    init(snapshot: MobileSessionSnapshot) {
        session = snapshot
    }

    func requestOTP(email: String) async throws -> Int {
        throw MobileAccountError.invalidRequest
    }

    func verifyOTP(
        email: String,
        code: String
    ) async throws -> MobileSessionDiscovery {
        throw MobileAccountError.invalidRequest
    }

    func signInWithPassword(
        email: String,
        password: String
    ) async throws -> MobilePasswordSignInDiscovery {
        throw MobileAccountError.invalidRequest
    }

    func fetchSession() async throws -> MobileSessionDiscovery? {
        .signedIn(session)
    }

    func passkeyChallenge() async throws -> MobilePasskeyChallenge {
        throw MobileAccountError.passkeyUnavailable
    }

    func verifySecondFactor(
        _ proof: MobileSecondFactorProof
    ) async throws -> MobileSessionDiscovery {
        throw MobileAccountError.invalidRequest
    }

    func logout() async throws {}

    func pushManagementRequest(
        _ request: PushManagementRequest
    ) async throws -> PushManagementResponse {
        PushManagementResponse(statusCode: 200, body: [:])
    }

    func cancelAll() async {}

    func invalidate() -> WebsiteDataStoreReleaseBarrier? {
        nil
    }
}
