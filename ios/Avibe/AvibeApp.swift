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
        !content.threadIdentifier.isEmpty
            ? [.banner, .list, .sound]
            : []
    }

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
