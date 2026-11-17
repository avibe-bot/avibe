import AppIntents
import Foundation

/// The App's App Shortcuts: what Siri, Spotlight, the Shortcuts app, and the
/// Action button (Settings > Action Button > Shortcut) offer for Avibe. An
/// App declares exactly one provider; every App Shortcut belongs here.
struct AvibeAppShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: OpenMainBotIntent(),
            phrases: ["Open Main Bot in \(.applicationName)"],
            shortTitle: "Open Main Bot",
            systemImageName: "bubble.left.and.bubble.right"
        )
        if #available(iOS 18.0, *) {
            AppShortcut(
                intent: SpeakToMainBotIntent(),
                phrases: ["Talk to Main Bot in \(.applicationName)"],
                shortTitle: "Talk to Main Bot",
                systemImageName: "waveform"
            )
        }
    }
}

/// Opens Avibe in the main Bot's conversation on the selected instance.
///
/// It runs in the App's own process with the App in front, and only hands
/// a path to the `AppModel`, which opens it the way a notification tap
/// opens a chat. Signed out, with no instance selected, or with the selected
/// instance unavailable, the App shows the screen it would show anyway.
struct OpenMainBotIntent: AppIntent {
    /// The main Bot's Session id is fixed (`ses_main`, docs/plans/bots.md
    /// D9), and the Web UI opens it at this path. Only the instance Owner has
    /// Bots: anyone else reading it gets a 404, and `from=notification` makes
    /// the Web UI answer that with the Inbox rather than an error, as it does
    /// for a notification whose chat is gone.
    static let path = "/chat/ses_main?from=notification"

    static let title: LocalizedStringResource = "Open Main Bot"

    /// iOS 17 through 25. iOS 26 reads `supportedModes` instead.
    static let openAppWhenRun = true

    @available(iOS 26.0, *)
    static var supportedModes: IntentModes {
        .foreground(.immediate)
    }

    @MainActor
    func perform() async throws -> some IntentResult {
        InstancePathRequests.shared.open(path: Self.path)
        return .result()
    }
}

/// Carries an App Shortcut's request to the `AppModel`. On a cold launch
/// the intent can run before the scene has made the `AppModel`, so the
/// latest request waits here until the App attaches its handler.
@MainActor
final class InstancePathRequests {
    static let shared = InstancePathRequests()

    private var handler: (@MainActor (String) -> Void)?
    private var pendingPath: String?

    func open(path: String) {
        if let handler {
            handler(path)
        } else {
            pendingPath = path
        }
    }

    /// Replaces any earlier handler and delivers a request that waited.
    func attach(_ handler: @escaping @MainActor (String) -> Void) {
        self.handler = handler
        if let pendingPath {
            self.pendingPath = nil
            handler(pendingPath)
        }
    }
}
