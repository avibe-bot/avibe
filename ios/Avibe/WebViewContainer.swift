import SwiftUI
import WebKit

struct WebViewContainer: UIViewControllerRepresentable {
    final class Coordinator {
        let didReleaseWebViews: (WebsiteDataStoreReleaseBarrier) -> Void

        init(
            didReleaseWebViews: @escaping (
                WebsiteDataStoreReleaseBarrier
            ) -> Void
        ) {
            self.didReleaseWebViews = didReleaseWebViews
        }
    }

    let instanceOrigin: WebOrigin
    let websiteDataStore: WKWebsiteDataStore
    let pushCoordinator: NativePushCoordinator?
    let showAccount: () -> Void
    let showInstances: () -> Void
    let instanceName: () -> String?
    let directStatus: DirectStatusProvider?
    let directLoadGate: DirectLoadGateProvider?
    let pushOpenedAck: (String, WebOrigin) -> Bool
    let setBadgeCount: (Int, WebOrigin) -> Bool
    let reconcileNotifications: (NotificationReconcile, WebOrigin) -> Bool
    let accountFallbackVisibilityChanged: (Bool) -> Void
    let pageColorSchemeChanged: (PageColorScheme?) -> Void
    let didCreateController: (WebViewController) -> Void
    let didReleaseWebViews: (WebsiteDataStoreReleaseBarrier) -> Void
    let didStartLoading: () -> Void
    let didFinishLoading: () -> Void
    let didFailLoading: () -> Void

    init(
        instanceOrigin: WebOrigin,
        websiteDataStore: WKWebsiteDataStore,
        pushCoordinator: NativePushCoordinator? = nil,
        showAccount: @escaping () -> Void,
        showInstances: @escaping () -> Void = {},
        instanceName: @escaping () -> String? = { nil },
        directStatus: DirectStatusProvider? = nil,
        directLoadGate: DirectLoadGateProvider? = nil,
        pushOpenedAck: @escaping (String, WebOrigin) -> Bool = { _, _ in false },
        setBadgeCount: @escaping (Int, WebOrigin) -> Bool = { _, _ in false },
        reconcileNotifications: @escaping (NotificationReconcile, WebOrigin) -> Bool = { _, _ in false },
        accountFallbackVisibilityChanged: @escaping (Bool) -> Void,
        pageColorSchemeChanged: @escaping (PageColorScheme?) -> Void,
        didCreateController: @escaping (WebViewController) -> Void,
        didReleaseWebViews: @escaping (WebsiteDataStoreReleaseBarrier) -> Void,
        didStartLoading: @escaping () -> Void,
        didFinishLoading: @escaping () -> Void,
        didFailLoading: @escaping () -> Void
    ) {
        self.instanceOrigin = instanceOrigin
        self.websiteDataStore = websiteDataStore
        self.pushCoordinator = pushCoordinator
        self.showAccount = showAccount
        self.showInstances = showInstances
        self.instanceName = instanceName
        self.directStatus = directStatus
        self.directLoadGate = directLoadGate
        self.pushOpenedAck = pushOpenedAck
        self.setBadgeCount = setBadgeCount
        self.reconcileNotifications = reconcileNotifications
        self.accountFallbackVisibilityChanged =
            accountFallbackVisibilityChanged
        self.pageColorSchemeChanged = pageColorSchemeChanged
        self.didCreateController = didCreateController
        self.didReleaseWebViews = didReleaseWebViews
        self.didStartLoading = didStartLoading
        self.didFinishLoading = didFinishLoading
        self.didFailLoading = didFailLoading
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(didReleaseWebViews: didReleaseWebViews)
    }

    func makeUIViewController(context: Context) -> WebViewController {
        let controller = WebViewController(
            instanceOrigin: instanceOrigin,
            websiteDataStore: websiteDataStore,
            pushCoordinator: pushCoordinator,
            showAccount: showAccount,
            showInstances: showInstances,
            instanceName: instanceName,
            directStatus: directStatus,
            directLoadGate: directLoadGate,
            pushOpenedAck: pushOpenedAck,
            setBadgeCount: setBadgeCount,
            reconcileNotifications: reconcileNotifications,
            accountFallbackVisibilityChanged:
                accountFallbackVisibilityChanged,
            pageColorSchemeChanged: pageColorSchemeChanged,
            didStartLoading: didStartLoading,
            didFinishLoading: didFinishLoading,
            didFailLoading: didFailLoading
        )
        didCreateController(controller)
        return controller
    }

    func updateUIViewController(
        _ uiViewController: WebViewController,
        context: Context
    ) {}

    static func dismantleUIViewController(
        _ uiViewController: WebViewController,
        coordinator: Coordinator
    ) {
        coordinator.didReleaseWebViews(
            uiViewController.invalidateForAccountTransition()
        )
    }
}
