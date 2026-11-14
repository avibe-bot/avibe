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
    private var hasStarted = false
    // These controls exist only in the simulator fixture app, never Avibe.
    private let captureState = ProcessInfo.processInfo.environment["AVIBE_FIXTURE_CAPTURE_STATE"]
    private lazy var coordinator: MobileSessionCoordinator = {
        let defaults = UserDefaults(suiteName: "avibe.ios.sheet-fixture")!
        defaults.removePersistentDomain(forName: "avibe.ios.sheet-fixture")
        let store = AccountWebsiteDataStore(defaults: defaults)
        let transport = FixtureMobileAuthTransport(
            snapshot: captureState == nil ? Self.snapshot : captureSnapshot,
            failRefresh: captureState == "stale",
            didFetch: { [weak self] count in
                self?.sheetController?.view.accessibilityValue = "fetch-\(count)"
            }
        )
        return MobileSessionCoordinator(
            defaults: defaults,
            websiteDataStore: store,
            transportFactory: { _, _ in
                transport
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

    private var captureSnapshot: MobileSessionSnapshot {
        MobileSessionSnapshot(
            account: MobileAccount(id: "capture-account", email: "alex@example.invalid"),
            instances: [
                captureInstance("current", name: "Studio", available: true),
                captureInstance("lab", name: "Lab", available: true),
                captureInstance("offline", name: "Home", available: false),
            ]
        )
    }

    private func captureInstance(_ id: String, name: String, available: Bool) -> MobileInstance {
        MobileInstance(
            id: id, name: name, host: "\(id).example.invalid",
            // A genuine native selection rejection produces the row failure;
            // no replacement bridge or fabricated DOM/UI is used.
            url: URL(string: captureState == "open-failed" && id == "lab"
                ? "ftp://lab.example.invalid" : "https://\(id).example.invalid")!,
            relationship: "owner", ownerEmail: nil, available: available
        )
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        view.accessibilityIdentifier = "fixture-root"
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        guard !hasStarted else { return }
        hasStarted = true
        if let captureState {
            precondition(["default", "offline-open", "open-failed", "stale", "account", "first-run"].contains(captureState))
            overrideUserInterfaceStyle = ProcessInfo.processInfo.environment["AVIBE_FIXTURE_CAPTURE_THEME"] == "dark" ? .dark : .light
            Task { @MainActor in
                do {
                    coordinator.activateAccountSurface()
                    _ = try await coordinator.bootstrap()
                    if captureState != "first-run" {
                        _ = try coordinator.selectInstance(id: "current")
                    }
                    presentBundledPage(realBundle: true)
                } catch {
                    preconditionFailure("Capture fixture bootstrap failed: \(error)")
                }
            }
        } else {
            presentBundledPage(realBundle: false)
        }
    }

    private func presentBundledPage(realBundle: Bool) {
        let resourceURL = Bundle.main.url(
            forResource: "mobile",
            withExtension: "html",
            subdirectory: realBundle ? "MobileApp" : "FixtureMobile"
        )!
        let controller = MobileOnboardingViewController(
            coordinator: coordinator,
            startScreen: captureState == "first-run" ? .instances : .sheet,
            resourceURL: resourceURL,
            returnToRuntime: { [weak self] in
                self?.dismissFixtureSheet() ?? false
            },
            dismissSurface: { [weak self] in
                self?.dismissFixtureSheet() ?? false
            }
        )
        if realBundle {
            controller.view.accessibilityIdentifier = "real-bundle-capture-surface"
        }
        controller.modalPresentationStyle = .pageSheet
        sheetController = controller
        controller.overrideUserInterfaceStyle = overrideUserInterfaceStyle
        if captureState == "first-run" {
            addChild(controller)
            controller.view.frame = view.bounds
            controller.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            view.addSubview(controller.view)
            controller.didMove(toParent: self)
        } else {
            present(controller, animated: false)
        }
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

// Data-only transport shapes keep the fixture independent of the production
// push coordinator, notification center, and entitlements. Their field shapes
// match NativePushCoordinator.swift; the fixture never registers for push.
struct PushManagementRequest {
    let method: String
    let path: String
    let headers: [String: String]
    let body: Data?
}

struct PushManagementResponse {
    let statusCode: Int
    let body: [String: Any]
}

@MainActor
private final class FixtureMobileAuthTransport: MobileAuthTransporting {
    private let session: MobileSessionSnapshot
    private let failRefresh: Bool
    private let didFetch: (Int) -> Void
    private var fetches = 0

    init(
        snapshot: MobileSessionSnapshot,
        failRefresh: Bool = false,
        didFetch: @escaping (Int) -> Void = { _ in }
    ) {
        session = snapshot
        self.failRefresh = failRefresh
        self.didFetch = didFetch
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
        fetches += 1
        didFetch(fetches)
        if failRefresh && fetches > 1 { throw MobileAccountError.network }
        return .signedIn(session)
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
