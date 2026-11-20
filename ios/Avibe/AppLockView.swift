import SwiftUI
import UIKit

/// The window App Lock draws in, above every other window of the scene, alerts included
/// (ui/design: app-lock/lock/*). It never becomes key, so code that presents on the key
/// window (VoiceCaptureAlert, VoiceCaptureBanner) keeps presenting under it, to be seen
/// after unlocking.
final class AppLockWindow: UIWindow {
    init(scene: UIWindowScene, controller: AppLockController) {
        super.init(windowScene: scene)
        windowLevel = .alert + 1
        let host = UIHostingController(rootView: AppLockView(controller: controller))
        host.view.backgroundColor = AppLockPalette.background
        host.view.accessibilityViewIsModal = true
        rootViewController = host
        isHidden = true
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { nil }

    override var canBecomeKey: Bool { false }
}

/// The Web UI's tokens, as native chrome draws them (ui/src/index.css).
enum AppLockPalette {
    static let background = UIColor { $0.userInterfaceStyle == .dark
        ? UIColor(red: 0x08 / 255, green: 0x08 / 255, blue: 0x12 / 255, alpha: 1)
        : UIColor(red: 0xF4 / 255, green: 0xF6 / 255, blue: 0xFB / 255, alpha: 1) }
    static let foreground = UIColor { $0.userInterfaceStyle == .dark
        ? UIColor(red: 0xF5 / 255, green: 0xF1 / 255, blue: 0xE8 / 255, alpha: 1)
        : UIColor(red: 0x0A / 255, green: 0x0A / 255, blue: 0x14 / 255, alpha: 1) }
    static let muted = UIColor { $0.userInterfaceStyle == .dark
        ? UIColor(red: 0x9B / 255, green: 0xA3 / 255, blue: 0xB8 / 255, alpha: 1)
        : UIColor(red: 0x5C / 255, green: 0x60 / 255, blue: 0x79 / 255, alpha: 1) }
    static let destructive = UIColor { $0.userInterfaceStyle == .dark
        ? UIColor(red: 0xFF / 255, green: 0x6B / 255, blue: 0x6B / 255, alpha: 1)
        : UIColor(red: 0xE1 / 255, green: 0x1D / 255, blue: 0x48 / 255, alpha: 1) }
}

/// What the lock screen shows for one state: the button's words and symbol, and the line under it.
struct AppLockScreenCopy: Equatable {
    enum Line: Equatable { case none, failed, muted(String.LocalizationValue) }

    let action: String.LocalizationValue
    let symbol: String
    let line: Line

    static func make(capability: AppLockCapability, failed: Bool) -> AppLockScreenCopy {
        let biometric = capability.biometryState == .usable
            && (capability.biometry == .faceID || capability.biometry == .touchID)
        let action: String.LocalizationValue = !biometric ? "appLock.lock.iphone.unlockPasscode"
            : capability.biometry == .faceID ? "appLock.lock.iphone.unlockFaceID" : "appLock.lock.unlockTouchID"
        let symbol = !biometric ? "key" : capability.biometry == .faceID ? "faceid" : "touchid"
        let line: Line
        if failed {
            line = .failed
        } else {
            switch (capability.biometryState, capability.biometry) {
            case (.lockout, .faceID): line = .muted("appLock.lock.iphone.faceIDLockout")
            case (.lockout, .touchID): line = .muted("appLock.lock.iphone.touchIDLockout")
            case (.notEnrolled, .faceID): line = .muted("appLock.lock.iphone.faceIDNotEnrolled")
            case (.denied, .faceID): line = .muted("appLock.lock.iphone.faceIDDenied")
            default: line = .none
            }
        }
        return AppLockScreenCopy(action: action, symbol: symbol, line: line)
    }
}

struct AppLockView: View {
    @ObservedObject var controller: AppLockController

    var body: some View {
        ZStack {
            Color(uiColor: AppLockPalette.background).ignoresSafeArea()
            if controller.machine.locked {
                locked
            } else {
                // The App Switcher snapshot and a glance away: the brand only.
                AppLockLogo()
            }
        }
    }

    private var locked: some View {
        let copy = AppLockScreenCopy.make(
            capability: controller.capability,
            failed: controller.machine.lastAttemptFailed
        )
        let verifying = controller.machine.verifying
        return VStack(spacing: 20) {
            AppLockLogo()
            Text("appLock.lock.lockedTitle")
                .font(.system(size: 22, weight: .semibold))
                .foregroundStyle(Color(uiColor: AppLockPalette.foreground))
                .multilineTextAlignment(.center)
            Button {
                controller.unlock()
            } label: {
                HStack(spacing: 8) {
                    if verifying {
                        ProgressView().tint(AvibeStyle.onAccent)
                        Text("appLock.lock.verifying")
                    } else {
                        Image(systemName: copy.symbol)
                        Text(String(localized: copy.action))
                    }
                }
                .font(.system(size: 17, weight: .bold))
                .frame(maxWidth: .infinity, minHeight: 48)
                .foregroundStyle(AvibeStyle.onAccent)
                .background(AvibeStyle.accent, in: Capsule())
                // The Web UI's disabled state: the whole button at half strength.
                .opacity(verifying ? 0.5 : 1)
            }
            .buttonStyle(.plain)
            .disabled(verifying)
            Group {
                switch copy.line {
                case .none: Text(verbatim: " ").accessibilityHidden(true)
                case .failed:
                    Text("appLock.lock.verificationFailed")
                        .foregroundStyle(Color(uiColor: AppLockPalette.destructive))
                case .muted(let key):
                    Text(String(localized: key))
                        .foregroundStyle(Color(uiColor: AppLockPalette.muted))
                }
            }
            .font(.system(size: 13))
            .multilineTextAlignment(.center)
            .frame(minHeight: 20)
        }
        .frame(maxWidth: 340)
        .padding(.horizontal, 32)
    }
}

private struct AppLockLogo: View {
    var body: some View {
        Image("AppLockLogo")
            .resizable()
            .interpolation(.high)
            .frame(width: 64, height: 64)
            .accessibilityLabel(Text(verbatim: "Avibe"))
    }
}
