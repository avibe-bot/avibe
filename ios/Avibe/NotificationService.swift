import UserNotifications

final class NotificationService: UNNotificationServiceExtension {
    private let keychain: PushKeychainStoring
    private let generationStore: PushGenerationStoring
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
        generationStore: PushGenerationStoring
    ) {
        self.keychain = keychain
        self.generationStore = generationStore
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
            generationStore: generationStore
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
