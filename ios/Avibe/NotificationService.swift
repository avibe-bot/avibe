import Foundation
import UserNotifications

final class NotificationService: UNNotificationServiceExtension {
    private let keychain: PushKeychainStoring
    private let generationStore: PushGenerationStoring
    private let now: () -> Date
    private var contentHandler: ((UNNotificationContent) -> Void)?
    private var bestAttemptContent: UNMutableNotificationContent?

    override convenience init() {
        self.init(
            keychain: PushKeychainStore(),
            generationStore: SharedPushGenerationStore()
        )
    }

    init(
        keychain: PushKeychainStoring,
        generationStore: PushGenerationStoring,
        now: @escaping () -> Date = Date.init
    ) {
        self.keychain = keychain
        self.generationStore = generationStore
        self.now = now
        super.init()
    }

    override func didReceive(
        _ request: UNNotificationRequest,
        withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void
    ) {
        self.contentHandler = contentHandler
        let content = (request.content.mutableCopy() as? UNMutableNotificationContent)
            ?? UNMutableNotificationContent()
        bestAttemptContent = content

        let processed = PushNotificationProcessor(
            keychain: keychain,
            generationStore: generationStore,
            now: now
        ).process(content)
        contentHandler(processed)
        self.contentHandler = nil
    }

    override func serviceExtensionTimeWillExpire() {
        guard let bestAttemptContent else {
            return
        }
        contentHandler?(bestAttemptContent)
        contentHandler = nil
    }
}
