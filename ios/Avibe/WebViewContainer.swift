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
    let showAccountRoot: () -> Void
    let showInstances: () -> Void
    let showSignIn: () -> Void
    let instanceName: () -> String?
    let liveActivity: LiveActivityAction
    let healthConnect: HealthConnectAction
    let instanceID: () -> String?
    let deviceID: () -> String?
    let deviceDescription: () -> [String: Any]?
    let healthBindingTicket: @MainActor () async -> String?
    let directStatus: DirectStatusProvider?
    let directHandover: DirectHandoverProvider?
    let directPreference: DirectPreferenceProvider?
    let directLoadGate: DirectLoadGateProvider?
    let directInstanceLoad: DirectInstanceLoadNote?
    let pushOpenedAck: (String, WebOrigin) -> Bool
    let setBadgeCount: (Int, WebOrigin) -> Bool
    let reconcileNotifications: (NotificationReconcile, WebOrigin) -> Bool
    let systemScheduleAdmission: (WebOrigin) -> SystemScheduleAdmission?
    let systemScheduleCapabilities: (WebOrigin) -> [String: Any]?
    let systemScheduleRequest: (
        SystemScheduleRequest,
        SystemScheduleAdmission
    ) async -> [String: Any]?
    let systemScheduleCancel: (
        SystemScheduleCancelRequest,
        SystemScheduleAdmission
    ) async -> [String: Any]?
    let accountFallbackVisibilityChanged: (Bool) -> Void
    let pageColorSchemeChanged: (PageColorScheme?) -> Void
    let runtimeNavigationChanged: (RuntimeNavigationEvent) -> Void
    let pageCache: RuntimePageCacheAccess?
    /// A same-origin URL the first load goes to instead of the root (a
    /// reopen where the page was); asked once, at that load.
    let startURL: () -> URL?
    let cachedPageConnected: () -> Void
    let cachedPageKeepsShell: () -> Void
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
        showAccountRoot: @escaping () -> Void = {},
        showInstances: @escaping () -> Void = {},
        showSignIn: @escaping () -> Void = {},
        instanceName: @escaping () -> String? = { nil },
        instanceID: @escaping () -> String? = { nil },
        deviceID: @escaping () -> String? = { nil },
        deviceDescription: @escaping () -> [String: Any]? = { nil },
        healthBindingTicket: @escaping @MainActor () async -> String? = { nil },
        directStatus: DirectStatusProvider? = nil,
        directHandover: DirectHandoverProvider? = nil,
        directPreference: DirectPreferenceProvider? = nil,
        liveActivity: @escaping LiveActivityAction = { _, _ in throw NativeBridgeError.unsupported },
        healthConnect: @escaping HealthConnectAction = { _, _, _, _ in "failed" },
        directLoadGate: DirectLoadGateProvider? = nil,
        directInstanceLoad: DirectInstanceLoadNote? = nil,
        pushOpenedAck: @escaping (String, WebOrigin) -> Bool = { _, _ in false },
        setBadgeCount: @escaping (Int, WebOrigin) -> Bool = { _, _ in false },
        reconcileNotifications: @escaping (NotificationReconcile, WebOrigin) -> Bool = { _, _ in false },
        systemScheduleAdmission: @escaping (WebOrigin) -> SystemScheduleAdmission? = { _ in nil },
        systemScheduleCapabilities: @escaping (
            WebOrigin
        ) -> [String: Any]? = { _ in nil },
        systemScheduleRequest: @escaping (
            SystemScheduleRequest,
            SystemScheduleAdmission
        ) async -> [String: Any]? = { _, _ in nil },
        systemScheduleCancel: @escaping (
            SystemScheduleCancelRequest,
            SystemScheduleAdmission
        ) async -> [String: Any]? = { _, _ in nil },
        accountFallbackVisibilityChanged: @escaping (Bool) -> Void,
        pageColorSchemeChanged: @escaping (PageColorScheme?) -> Void,
        runtimeNavigationChanged: @escaping (RuntimeNavigationEvent) -> Void = { _ in },
        pageCache: RuntimePageCacheAccess? = nil,
        startURL: @escaping () -> URL? = { nil },
        cachedPageConnected: @escaping () -> Void = {},
        cachedPageKeepsShell: @escaping () -> Void = {},
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
        self.showAccountRoot = showAccountRoot
        self.showInstances = showInstances
        self.showSignIn = showSignIn
        self.instanceName = instanceName
        self.instanceID = instanceID
        self.deviceID = deviceID
        self.deviceDescription = deviceDescription
        self.healthBindingTicket = healthBindingTicket
        self.directStatus = directStatus
        self.directHandover = directHandover
        self.directPreference = directPreference
        self.liveActivity = liveActivity
        self.healthConnect = healthConnect
        self.directLoadGate = directLoadGate
        self.directInstanceLoad = directInstanceLoad
        self.pushOpenedAck = pushOpenedAck
        self.setBadgeCount = setBadgeCount
        self.reconcileNotifications = reconcileNotifications
        self.systemScheduleAdmission = systemScheduleAdmission
        self.systemScheduleCapabilities = systemScheduleCapabilities
        self.systemScheduleRequest = systemScheduleRequest
        self.systemScheduleCancel = systemScheduleCancel
        self.accountFallbackVisibilityChanged =
            accountFallbackVisibilityChanged
        self.pageColorSchemeChanged = pageColorSchemeChanged
        self.runtimeNavigationChanged = runtimeNavigationChanged
        self.pageCache = pageCache
        self.startURL = startURL
        self.cachedPageConnected = cachedPageConnected
        self.cachedPageKeepsShell = cachedPageKeepsShell
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
            showAccountRoot: showAccountRoot,
            showInstances: showInstances,
            showSignIn: showSignIn,
            instanceName: instanceName,
            instanceID: instanceID,
            deviceID: deviceID,
            deviceDescription: deviceDescription,
            healthBindingTicket: healthBindingTicket,
            directStatus: directStatus,
            directHandover: directHandover,
            directPreference: directPreference,
            liveActivity: liveActivity,
            healthConnect: healthConnect,
            directLoadGate: directLoadGate,
            directInstanceLoad: directInstanceLoad,
            pushOpenedAck: pushOpenedAck,
            setBadgeCount: setBadgeCount,
            reconcileNotifications: reconcileNotifications,
            systemScheduleAdmission: systemScheduleAdmission,
            systemScheduleCapabilities: systemScheduleCapabilities,
            systemScheduleRequest: systemScheduleRequest,
            systemScheduleCancel: systemScheduleCancel,
            accountFallbackVisibilityChanged:
                accountFallbackVisibilityChanged,
            pageColorSchemeChanged: pageColorSchemeChanged,
            runtimeNavigationChanged: runtimeNavigationChanged,
            pageCache: pageCache,
            startURL: startURL,
            cachedPageConnected: cachedPageConnected,
            cachedPageKeepsShell: cachedPageKeepsShell,
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
