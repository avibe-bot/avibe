import SwiftUI
import UIKit
import XCTest
@testable import Avibe

/// The App's own native screens, captured in light and dark for the SDK
/// comparison (`ios-sdk-comparison`): each is hosted the way the App hosts
/// it (a `UIHostingController` in a full-screen window), so UIKit's layout
/// (safe areas, layout margins, presentation) is part of the picture. Fixture
/// data only. Runs only when the comparison asks for it
/// (`TEST_RUNNER_AVIBE_NATIVE_CAPTURES=1`); `ios-shell` skips it.
@MainActor
final class NativeScreenCaptureTests: XCTestCase {
    override func setUp() async throws {
        try XCTSkipUnless(
            ProcessInfo.processInfo.environment["AVIBE_NATIVE_CAPTURES"] == "1",
            "Only the SDK comparison captures native screens."
        )
    }

    func testCaptureNativeScreens() async throws {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let previous = scene.windows.first(where: \.isKeyWindow)
        let window = UIWindow(windowScene: scene)
        window.frame = scene.coordinateSpace.bounds
        window.makeKeyAndVisible()
        defer {
            window.rootViewController?.dismiss(animated: false)
            window.isHidden = true
            window.rootViewController = nil
            previous?.makeKeyAndVisible()
        }
        for theme in ["light", "dark"] {
            window.overrideUserInterfaceStyle = theme == "dark" ? .dark : .light
            for (name, view) in screens() {
                window.rootViewController = UIHostingController(rootView: view)
                try await settle()
                try await attach(window, name: "native-\(name)-\(theme)")
            }
            for (name, present) in alerts() {
                let root = UIHostingController(rootView: AnyView(Color(uiColor: .systemBackground).ignoresSafeArea()))
                window.rootViewController = root
                try await settle()
                present()
                try await waitForPresentation(on: root)
                try await attach(window, name: "native-alert-\(name)-\(theme)")
                root.dismiss(animated: false)
            }
        }
    }

    // MARK: The screens

    private func screens() -> [(String, AnyView)] {
        var shown: [(String, AnyView)] = []
        for (name, failed) in [("app-lock", false), ("app-lock-failed", true)] {
            let lock = lockController(failed: failed)
            shown.append((name, AnyView(AppLockView(controller: lock))))
        }
        shown.append(("offline-row", AnyView(onPage(RuntimeCachedPageOfflineBanner(retry: {}, switchInstance: {})))))
        shown.append(("slow-open", AnyView(onPage(RuntimeOpenSlowBanner(showsLastContent: false, cancel: {})))))
        shown.append(("slow-open-last-content", AnyView(onPage(RuntimeOpenSlowBanner(showsLastContent: true, dismiss: {})))))
        shown.append(("skeleton", AnyView(RuntimeSkeleton())))
        shown.append(("health-consent", AnyView(HealthConsentView(
            request: HealthConsentRequest(target: HealthConnectionTarget(
                accountID: "account-1", instanceID: "instance-1",
                name: "Fixture", origin: "https://instance-one.example"
            )),
            answer: { _ in }
        ))))
        for (name, state) in voiceStates() {
            shown.append(("voice-lock-screen-\(name)", AnyView(onLockScreen(VoiceCaptureLockScreenView(state: state)))))
        }
        return shown
    }

    private func alerts() -> [(String, () -> Void)] {
        [
            ("needs-app", { VoiceCaptureAlert.present(.signIn) }),
            ("unsent", {
                VoiceCaptureAlert.presentUnsent(
                    count: 2, oldest: Date().addingTimeInterval(-3 * 60 * 60), answer: { _ in }
                )
            }),
        ]
    }

    private func voiceStates() -> [(String, VoiceCaptureAttributes.ContentState)] {
        let start = Date(timeIntervalSince1970: 1_800_000_000)
        func state(_ phase: VoiceCaptureAttributes.Phase, _ reason: VoiceCaptureAttributes.Reason? = nil)
            -> VoiceCaptureAttributes.ContentState {
            var state = VoiceCaptureAttributes.ContentState(phase: phase, startedAt: start)
            state.endedAt = phase == .listening ? nil : start.addingTimeInterval(7)
            state.reason = reason
            return state
        }
        var sent = state(.sent)
        // Fixture words only.
        sent.heard = "This is a fixture sentence for the comparison."
        return [
            ("listening", state(.listening)),
            ("sending", state(.sending)),
            ("sent", sent),
            ("will-send", state(.failed, .willSend)),
            ("update-required", state(.failed, .updateAvibe)),
            ("needs-app-sign-in", state(.needsApp, .signIn)),
            ("needs-app-confirm", state(.needsApp, .confirmPending)),
        ]
    }

    private func lockController(failed: Bool) -> AppLockController {
        let suite = "NativeScreenCapture.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        addTeardownBlock { defaults.removePersistentDomain(forName: suite) }
        let store = AppLockStore(defaults: defaults)
        store.save(.immediately)
        let authenticator = CaptureAuthenticator(failing: failed)
        let lock = AppLockController(
            store: store, authenticator: authenticator, clock: { 0 }, promptAllowed: { false }
        )
        if failed {
            lock.unlock()
        }
        return lock
    }

    /// A banner where the App shows it: over the page, inside the safe area.
    private func onPage(_ banner: some View) -> some View {
        ZStack(alignment: .top) {
            Color(uiColor: .systemBackground).ignoresSafeArea()
            banner.padding(.horizontal, 12)
        }
    }

    /// A Lock Screen presentation's width and background, as the system
    /// draws it (fixture only; the real one is the system's).
    private func onLockScreen(_ view: some View) -> some View {
        ZStack {
            Color.black.opacity(0.85).ignoresSafeArea()
            view
                .padding(16)
                .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 24))
                .padding(.horizontal, 12)
        }
    }

    // MARK: Capture

    private func settle() async throws {
        try await Task.sleep(for: .milliseconds(400))
    }

    private func waitForPresentation(on root: UIViewController) async throws {
        for _ in 0..<40 where !(root.presentedViewController is UIAlertController) {
            try await Task.sleep(for: .milliseconds(50))
        }
        XCTAssertTrue(root.presentedViewController is UIAlertController)
        // The presentation's animation.
        try await Task.sleep(for: .milliseconds(600))
    }

    /// The window once its picture has stopped changing: two renders in a
    /// row with the same pixels (a material or a presentation still
    /// settling would otherwise be caught mid-way, differently on each
    /// run). A screen that keeps moving (a running timer) is taken as it is
    /// after a few tries.
    private func attach(_ window: UIWindow, name: String) async throws {
        func render() -> UIImage {
            UIGraphicsImageRenderer(bounds: window.bounds).image { _ in
                window.drawHierarchy(in: window.bounds, afterScreenUpdates: true)
            }
        }
        var image = render()
        for _ in 0..<12 {
            try await Task.sleep(for: .milliseconds(200))
            let next = render()
            let settled = next.pngData() == image.pngData()
            image = next
            if settled { break }
        }
        let attachment = XCTAttachment(image: image)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}

private final class CaptureAuthenticator: AppLockAuthenticating {
    private let failing: Bool
    init(failing: Bool) { self.failing = failing }
    func capability() -> AppLockCapability {
        AppLockCapability(available: true, biometry: .faceID, biometryState: .usable)
    }
    func authenticate(reason: String) async -> AppLockAuthOutcome {
        failing ? .failed : .cancelled
    }
}
