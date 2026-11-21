import SwiftUI
import UIKit
import UserNotifications

extension Notification.Name {
    static let avibePushDidRegister = Notification.Name(
        "avibe.push.didRegister"
    )
    static let avibePushDidFailToRegister = Notification.Name(
        "avibe.push.didFailToRegister"
    )
    static let avibePushNotificationTapped = Notification.Name(
        "avibe.push.notificationTapped"
    )
}

final class AvibeAppDelegate: NSObject, UIApplicationDelegate,
    UNUserNotificationCenterDelegate
{
    static var isRunningTests: Bool {
        ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
            || ProcessInfo.processInfo.environment["XCTestBundlePath"] != nil
            || NSClassFromString("XCTestCase") != nil
    }
    @MainActor static let healthSync = HealthSyncCoordinator()

    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        guard identifier == HealthBackgroundUploader.identifier,
              let uploader = Self.healthSync.backgroundUploader else { completionHandler(); return }
        uploader.systemCompletion = completionHandler
        uploader.reconnect()
        Task { @MainActor in await Self.healthSync.resume(foreground: Self.applicationIsForeground) }
    }

    /// Read when a launch or background-session resume actually runs, as
    /// AppModel.bootstrap does; a constant would overwrite a foreground state
    /// that an earlier resume already applied.
    @MainActor private static var applicationIsForeground: Bool {
        UIApplication.shared.applicationState != .background
    }

    /// The tag the instance gives the test notification sent from Settings
    /// (`/api/web-push/test`); the notification service extension carries a
    /// push's tag into `threadIdentifier`.
    static let testNotificationThread = "web-push-test"

    /// What a notification that arrives while the App is in front shows.
    ///
    /// Only the test notification is shown, as a banner, in the list, and
    /// with its sound: the user sent it from Settings to see one, and could
    /// not otherwise. Every other notification stays silent in front, as iOS
    /// does by default: the Inbox and the badge in the App already show it,
    /// and a banner would cover the conversation being read. A notification
    /// the extension could not decrypt has no tag, so it stays silent too.
    ///
    /// The badge is never set from here. In front, the Web UI sets it from the
    /// live Inbox total through `badge.set` whenever its unread map changes; a
    /// push's count was computed when it was sent, so applying it could put
    /// an older number back (a message read in the App before its push
    /// lands), to stay until the Inbox next changes.
    static func foregroundPresentationOptions(
        for content: UNNotificationContent
    ) -> UNNotificationPresentationOptions {
        switch content.threadIdentifier {
        case testNotificationThread: [.banner, .list, .sound]
        // A failed voice start, posted while Avibe came forward to say why:
        // keep it in Notification Center so its line can be sent back.
        case VoiceCaptureFailureNotification.thread: [.list]
        default: []
        }
    }

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [
            UIApplication.LaunchOptionsKey: Any
        ]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        // Before any scene draws: a locked App opens behind its lock (docs/plans/app-lock.md).
        if !Self.isRunningTests {
            MainActor.assumeIsolated { AppLockController.shared.start() }
        }
        VoiceCaptureLaunch.markLaunched()
        // A Live Activity left by a process that is gone (killed while a
        // take ran) would otherwise show a timer no one can stop.
        Task { @MainActor in
            ShowShareCoordinator.shared.cleanAtLaunch()
            await VoiceCaptureController.shared.endLeftoversAtLaunch()
            await VoiceCaptureController.shared.restoreOutbox()
            if !Self.isRunningTests { await Self.healthSync.resume(foreground: Self.applicationIsForeground) }
        }
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        NotificationCenter.default.post(
            name: .avibePushDidRegister,
            object: nil,
            userInfo: ["deviceToken": deviceToken]
        )
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        NotificationCenter.default.post(
            name: .avibePushDidFailToRegister,
            object: nil
        )
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (
            UNNotificationPresentationOptions
        ) -> Void
    ) {
        completionHandler(
            Self.foregroundPresentationOptions(for: notification.request.content)
        )
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        // A voice-capture notice only opens Avibe; it is not an instance push.
        guard response.notification.request.content.threadIdentifier
            != VoiceCaptureFailureNotification.thread
        else {
            completionHandler()
            return
        }
        NotificationCenter.default.post(
            name: .avibePushNotificationTapped,
            object: nil,
            userInfo: ["payload": response.notification.request.content.userInfo]
        )
        completionHandler()
    }
}

@main
struct AvibeApp: App {
    @UIApplicationDelegateAdaptor(AvibeAppDelegate.self)
    private var appDelegate
    @StateObject private var appModel: AppModel
    /// The source is retained for the lifetime of the App because the
    /// transport's fetch closure intentionally captures it weakly.
    private let directRendezvousSource: DirectRendezvousWebViewSource

    init() {
        let source = DirectRendezvousWebViewSource()
        let transport = DirectIrohTransport(
            engine: DirectIrohBinding(),
            fetchRendezvous: source.fetch
        )
        let model = AppModel(
            directTransport: transport,
            healthSync: AvibeAppDelegate.isRunningTests ? nil : AvibeAppDelegate.healthSync
        )
        source.appModel = model
        directRendezvousSource = source
        _appModel = StateObject(wrappedValue: model)
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .modifier(HealthConsentHost(health: AvibeAppDelegate.healthSync))
                .environmentObject(appModel)
                .onOpenURL { url in
                    // The hidden diagnostics switch (VoiceCaptureEndSwitch).
                    VoiceCaptureEndSwitch.handle(url)
                    // And the hold's "start at touch-down" variant (NativeHoldEarlySwitch).
                    NativeHoldEarlySwitch.handle(url)
                    appModel.openLiveActivity(url)
                }
                .task {
                    // App Shortcuts (AppShortcuts.swift) reach the model here.
                    let appModel = appModel
                    InstancePathRequests.shared.attach { [weak appModel] path in
                        appModel?.openSelectedInstance(path: path)
                    }
                }
        }
    }
}
