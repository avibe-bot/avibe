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
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [
            UIApplication.LaunchOptionsKey: Any
        ]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
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

    /// A notification that arrives while the App is in front shows as it
    /// would in the background, as a banner, in the list, and with its sound,
    /// the way a PWA's service worker shows every push. Without this, iOS
    /// silences it, and the test notification sent from the App's own
    /// Settings could never be seen. The badge is left alone here: in front,
    /// the Web UI sets it from the live Inbox total through `badge.set`, and
    /// the count a push carries, computed when it was sent, could only put an
    /// older one back.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (
            UNNotificationPresentationOptions
        ) -> Void
    ) {
        completionHandler([.banner, .list, .sound])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
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
    @StateObject private var appModel = AppModel()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(appModel)
        }
    }
}
