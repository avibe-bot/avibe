import SwiftUI
import UIKit

@MainActor
struct SystemScheduleAccessibilityAnnouncer {
    static let live = SystemScheduleAccessibilityAnnouncer {
        UIAccessibility.post(notification: $0, argument: nil)
    }

    let post: (UIAccessibility.Notification) -> Void

    func didPresent(focusTitle: () -> Void) {
        focusTitle()
        post(.screenChanged)
    }

    func didDismiss() {
        post(.screenChanged)
    }
}

struct RootView: View {
    @EnvironmentObject private var appModel: AppModel

    // Each layer keeps its own position, so Account can cover a runtime and
    // leave it untouched, and an account transition can release that runtime
    // without reloading the account page above it.
    var body: some View {
        ZStack(alignment: .top) {
            if let runtime = appModel.route.runtime {
                if appModel.presentsDirectFaultOnly(runtime) {
                    // Opened under the listener fault: no WebView is made
                    // for a route to a port the App may not hold.
                    DirectFaultMessage()
                        .id(appModel.runtimeIdentity)
                } else {
                    BrowserScreen(runtime: runtime)
                        .id(appModel.runtimeIdentity)
                        .allowsHitTesting(
                            !appModel.route.coversRuntime
                                && appModel.systemScheduleConfirmation == nil
                        )
                        .accessibilityHidden(
                            appModel.route.coversRuntime
                                || appModel.systemScheduleConfirmation != nil
                        )
                }
            }
            if let startScreen = appModel.route.onboardingStartScreen {
                MobileOnboardingContainer(
                    coordinator: appModel.mobileSessionCoordinator,
                    startScreen: startScreen,
                    returnToRuntime: appModel.returnToRuntime
                )
                .ignoresSafeArea(.container, edges: .all)
            }
            if appModel.route == .loading {
                // Never a blocking spinner: the page's skeleton, and while an
                // open is under way the thin progress line over it.
                if appModel.runtimeOpenAttempt != nil
                    || appModel.mobileSessionCoordinator.hasStoredSelection
                {
                    RuntimeSkeleton()
                }
                if let attempt = appModel.runtimeOpenAttempt {
                    RuntimeOpenProgress(attempt: attempt) {
                        appModel.cancelRuntimeOpen(attemptID: attempt.id)
                    }
                } else {
                    // The session is being restored: the line, with no stage.
                    RuntimeProgressLine(label: Text("runtime.loading"))
                }
            }
            if appModel.route == .onboarding(.instances),
               let notice = appModel.runtimeOpenNotice
            {
                // The page's current block is the one way to open the
                // instance again; the notice only explains. At the bottom it
                // leaves the page's title, account button and current block
                // uncovered.
                RuntimeOpenNoticeBanner(
                    notice: notice,
                    dismiss: appModel.dismissRuntimeOpenNotice
                )
                .padding(.horizontal, 16)
                .padding(.bottom, 12)
                .frame(
                    maxWidth: .infinity,
                    maxHeight: .infinity,
                    alignment: .bottom
                )
            }
            if let confirmation = appModel.systemScheduleConfirmation {
                SystemScheduleConfirmationOverlay(
                    confirmation: confirmation,
                    confirm: {
                        appModel.confirmSystemSchedule(id: confirmation.id)
                    },
                    cancel: {
                        appModel.denySystemSchedule(id: confirmation.id)
                    }
                )
            }
        }
        .sheet(
            isPresented: Binding(
                get: {
                    if case .accountOverBrowser = appModel.route {
                        return true
                    }
                    return false
                },
                set: { presented in
                    guard !presented else { return }
                    _ = appModel.returnToRuntime()
                }
            ),
            onDismiss: {
                _ = appModel.returnToRuntime()
            }
        ) {
            MobileOnboardingContainer(
                coordinator: appModel.mobileSessionCoordinator,
                startScreen: appModel.accountSheetStartScreen,
                returnToRuntime: appModel.returnToRuntime,
                // The sheet takes the instance page's appearance (the system's
                // until the page reports one), so it does not flash or turn
                // light over a dark page.
                pageColorScheme: appModel.runtimePageColorScheme
            )
            .ignoresSafeArea(.container, edges: .all)
            .presentationDetents([.large])
            .presentationDragIndicator(.visible)
        }
        .background(Color(uiColor: .systemBackground))
    }
}

private struct SystemScheduleConfirmationOverlay: View {
    let confirmation: SystemScheduleConfirmation
    let confirm: () -> Void
    let cancel: () -> Void

    @AccessibilityFocusState private var titleIsFocused: Bool
    private let accessibilityAnnouncer =
        SystemScheduleAccessibilityAnnouncer.live

    private var kindKey: String {
        confirmation.request.kind == .alarm
            ? "system_schedule.kind.alarm"
            : "system_schedule.kind.reminder"
    }

    private var titleKey: String {
        confirmation.request.kind == .alarm
            ? "system_schedule.confirm.alarm.title"
            : "system_schedule.confirm.reminder.title"
    }

    private var disclosureKey: String {
        confirmation.request.kind == .alarm
            ? "system_schedule.confirm.alarm.disclosure"
            : "system_schedule.confirm.reminder.disclosure"
    }

    private var actionKey: String {
        confirmation.request.kind == .alarm
            ? "system_schedule.confirm.alarm.action"
            : "system_schedule.confirm.reminder.action"
    }

    var body: some View {
        ZStack {
            Color.black.opacity(0.28)
                .ignoresSafeArea()

            GeometryReader { geometry in
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        HStack(spacing: 12) {
                            Image(
                                systemName: confirmation.request.kind == .alarm
                                    ? "alarm.fill"
                                    : "checklist"
                            )
                            .font(.title2)
                            .foregroundStyle(.tint)
                            Text(LocalizedStringKey(titleKey))
                                .font(.title3.weight(.semibold))
                                .accessibilityFocused($titleIsFocused)
                        }

                        Text(confirmation.request.title)
                            .font(.headline)
                            .frame(maxWidth: .infinity, alignment: .leading)

                        VStack(spacing: 10) {
                            SystemScheduleDetailRow(
                                label: "system_schedule.confirm.type",
                                value: NSLocalizedString(kindKey, comment: "")
                            )
                            SystemScheduleDetailRow(
                                label: "system_schedule.confirm.when",
                                value: systemScheduleDateTimeText(
                                    confirmation.request.fireAt,
                                    timeZoneIdentifier:
                                        confirmation.request.timeZoneIdentifier
                                )
                            )
                            SystemScheduleDetailRow(
                                label: "system_schedule.confirm.time_zone",
                                value: confirmation.request.timeZoneIdentifier
                            )
                            SystemScheduleDetailRow(
                                label: "system_schedule.confirm.instance",
                                value: confirmation.instanceName
                            )
                            SystemScheduleDetailRow(
                                label: "system_schedule.confirm.session",
                                value: confirmation.request.sessionID,
                                monospaced: true
                            )
                        }

                        Text(LocalizedStringKey(disclosureKey))
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)

                        if confirmation.isSubmitting {
                            HStack(spacing: 10) {
                                ProgressView()
                                Text("system_schedule.confirm.submitting")
                                    .font(.subheadline.weight(.medium))
                            }
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 6)
                            .accessibilityIdentifier(
                                "system-schedule-submitting"
                            )
                        } else {
                            HStack(spacing: 12) {
                                Button("common.cancel", action: cancel)
                                    .buttonStyle(.bordered)
                                    .frame(maxWidth: .infinity)
                                    .accessibilityIdentifier(
                                        "system-schedule-cancel"
                                    )
                                Button(action: confirm) {
                                    Text(LocalizedStringKey(actionKey))
                                        .frame(maxWidth: .infinity)
                                }
                                .buttonStyle(.borderedProminent)
                                .foregroundStyle(AvibeStyle.onAccent)
                                .accessibilityIdentifier(
                                    "system-schedule-confirm"
                                )
                            }
                            .controlSize(.large)
                        }
                    }
                    .padding(22)
                }
                .scrollBounceBehavior(.basedOnSize)
                .frame(
                    maxWidth: 380,
                    maxHeight: max(0, geometry.size.height - 40)
                )
                .background(
                    Color(uiColor: .systemBackground),
                    in: RoundedRectangle(
                        cornerRadius: 20,
                        style: .continuous
                    )
                )
                .shadow(color: .black.opacity(0.22), radius: 28, y: 14)
                .position(
                    x: geometry.size.width / 2,
                    y: geometry.size.height / 2
                )
            }
            .padding(.horizontal, 20)
        }
        .accessibilityIdentifier("system-schedule-confirmation")
        .accessibilityAddTraits(.isModal)
        .onAppear {
            DispatchQueue.main.async {
                accessibilityAnnouncer.didPresent {
                    titleIsFocused = true
                }
            }
        }
        .onDisappear {
            accessibilityAnnouncer.didDismiss()
        }
    }
}

private struct SystemScheduleDetailRow: View {
    let label: LocalizedStringKey
    let value: String
    var monospaced = false

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(label)
                .foregroundStyle(.secondary)
            Spacer(minLength: 12)
            Text(value)
                .font(monospaced ? .caption.monospaced() : .subheadline)
                .multilineTextAlignment(.trailing)
                .lineLimit(3)
        }
    }
}

private func systemScheduleDateTimeText(
    _ date: Date,
    timeZoneIdentifier: String
) -> String {
    let formatter = DateFormatter()
    formatter.locale = .current
    formatter.timeZone = TimeZone(identifier: timeZoneIdentifier)
    formatter.dateStyle = .full
    formatter.timeStyle = .medium
    return formatter.string(from: date)
}

private struct BrowserScreen: View {
    @EnvironmentObject private var appModel: AppModel
    @Environment(\.colorScheme) private var colorScheme
    let runtime: MobileRuntimeContext

    @State private var showsAccountFallback = false
    @State private var pageColorScheme: PageColorScheme?

    /// The scheme of what the status bar sits over: the page's own once it
    /// is known, otherwise the system's. The WebView itself always keeps
    /// the system appearance.
    private var chromeColorScheme: ColorScheme {
        switch pageColorScheme {
        case .light:
            return .light
        case .dark:
            return .dark
        case nil:
            return colorScheme
        }
    }

    var body: some View {
        let instance = runtime.instance
        let attempt = appModel.runtimeOpenAttempt.flatMap {
            $0.runtime == runtime ? $0 : nil
        }
        let attemptID = attempt?.id ?? appModel.runtimeIdentity
        let failed = attempt?.phase == .failed
        let overlay = BrowserOverlay(
            attemptPhase: attempt?.phase,
            directFault: appModel.directFaultCovers(runtime)
        )
        if let instanceOrigin = WebOrigin(url: instance.url) {
            VStack(spacing: 0) {
                if showsAccountFallback && !failed {
                    LegacyRuntimeToolbar(
                        instanceName: instance.name,
                        accountEmail: runtime.account.email,
                        showAccount: appModel.showAccount,
                        showInstancePicker: appModel.showInstancePicker
                    )
                    .environment(\.colorScheme, chromeColorScheme)
                }
                if overlay == .awaitingUser, let attempt {
                    RuntimeOpenAwaitingUserBanner(
                        instanceName: instance.name,
                        cancel: {
                            appModel.cancelRuntimeOpen(
                                attemptID: attempt.id
                            )
                        }
                    )
                    .padding(.horizontal, 16)
                    .padding(.vertical, 10)
                    .background(Color(uiColor: .systemBackground))
                }

                ZStack(alignment: .top) {
                    WebViewContainer(
                        instanceOrigin: instanceOrigin,
                        websiteDataStore: appModel.runtimeWebsiteDataStore,
                        pushCoordinator: appModel.pushCoordinator,
                        showAccount: appModel.showAccount,
                        showAccountRoot: appModel.showAccountRoot,
                        showInstances: appModel.showInstances,
                        showSignIn: appModel.showSignIn,
                        instanceName: appModel.selectedInstanceName,
                        instanceID: { instance.id },
                        deviceID: { appModel.systemScheduleDeviceID },
                        directStatus: appModel.directStatusProvider,
                        directHandover: appModel.directHandoverProvider,
                        liveActivity: appModel.handleLiveActivity,
                        healthConnect: appModel.connectHealth,
                        directLoadGate: appModel.directLoadGateProvider(for: runtime),
                        pushOpenedAck: appModel.acknowledgePushOpened,
                        setBadgeCount: appModel.setBadgeCount,
                        reconcileNotifications: appModel.reconcileNotifications,
                        systemScheduleAdmission: appModel.captureSystemScheduleAdmission,
                        systemScheduleCapabilities: appModel.systemScheduleCapabilities,
                        systemScheduleRequest: appModel.requestSystemSchedule,
                        systemScheduleCancel: appModel.cancelSystemSchedule,
                        accountFallbackVisibilityChanged: {
                            showsAccountFallback = $0
                        },
                        pageColorSchemeChanged: {
                            pageColorScheme = $0
                            appModel.runtimePageColorScheme = $0
                        },
                        runtimeNavigationChanged: { event in
                            appModel.handleRuntimeNavigation(
                                event,
                                attemptID: attemptID,
                                runtime: runtime
                            )
                        },
                        pageCache: appModel.runtimePageCacheAccess(for: runtime),
                        cachedPageConnected: {
                            appModel.runtimeCachedPageConnected(
                                attemptID: attemptID,
                                runtime: runtime
                            )
                        },
                        cachedPageKeepsShell: {
                            appModel.runtimeCachedPageKeepsShell(
                                attemptID: attemptID,
                                runtime: runtime
                            )
                        },
                        didCreateController: {
                            appModel.registerRuntimeController(
                                $0,
                                for: runtime,
                                attemptID: attemptID
                            )
                        },
                        didReleaseWebViews: {
                            appModel.recordRuntimeReleaseBarrier($0)
                        },
                        didStartLoading: {},
                        didFinishLoading: {},
                        didFailLoading: {}
                    )
                    .ignoresSafeArea(.keyboard)
                    // The controller decides where the page ends; the Web
                    // UI reserves the status bar and the Home indicator
                    // strip itself. Below the legacy toolbar the top edge
                    // is not the screen's, so the page starts there.
                    .ignoresSafeArea(.container, edges: [.top, .bottom])

                    if overlay == .loading, let attempt {
                        // Until the page's document commits the WebView is
                        // blank: its skeleton stands in. The progress line
                        // stays until the document finishes and blocks
                        // nothing.
                        if attempt.phase != .loadingPage {
                            RuntimeSkeleton()
                        }
                        RuntimeOpenProgress(attempt: attempt) {
                            appModel.cancelRuntimeOpen(attemptID: attempt.id)
                        }
                    }

                    if overlay == .showingCachedLive, let attempt {
                        // The last page is on screen and live to touch: only
                        // the line, and past the long-wait mark one row
                        // saying it is the last content.
                        CachedRuntimeOpenProgress(attempt: attempt)
                    }

                    if overlay == .cachedUnreachable, let attempt {
                        RuntimeCachedPageTimeoutBanner(
                            instanceName: attempt.runtime.instance.name,
                            retry: { appModel.retryRuntimeCachedPage() }
                        )
                        .id(attempt.id)
                    }

                    if overlay == .cachedOffline, let attempt {
                        // avibe.bot lists the instance offline; its last
                        // page stays, and connects by itself when it is back.
                        RuntimeCachedPageOfflineBanner(
                            retry: { appModel.retryOfflineCachedPage() },
                            switchInstance: { appModel.showInstances() }
                        )
                        .id(attempt.id)
                    }

                    if overlay == .loadFailure {
                        ContentUnavailableView {
                            Label(
                                String(
                                    format: NSLocalizedString(
                                        "browser.error.title.named",
                                        comment: ""
                                    ),
                                    instance.name
                                ),
                                systemImage: "wifi.exclamationmark"
                            )
                        } description: {
                            Text("browser.error.body")
                        } actions: {
                            // Retry is offered only while the shell may
                            // reload: on WebKit's normal path, or with a
                            // serving listener. While the listener is being
                            // bound again after the background it is not
                            // shown; it returns when the return settles.
                            if appModel.mayReload(runtime) {
                                Button("browser.error.retry") {
                                    showsAccountFallback = false
                                    guard let attempt else {
                                        return
                                    }
                                    _ = appModel.retryRuntimeOpen(
                                        attemptID: attempt.id
                                    )
                                }
                                .buttonStyle(.borderedProminent)
                                .foregroundStyle(AvibeStyle.onAccent)
                            }

                            Button("browser.other_instances") {
                                appModel.showInstancePicker()
                            }
                        }
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .background(
                            Color(uiColor: .systemBackground)
                                .ignoresSafeArea(.container, edges: .top)
                        )
                    }

                    // Terminal for this process: no Retry or reload, which
                    // would not reach a working listener. Reopening does.
                    if overlay == .directFault {
                        DirectFaultMessage()
                    }
                }
            }
            .background(
                runtimeHeaderBackground(for: chromeColorScheme)
                    .ignoresSafeArea(.container, edges: .top)
            )
        } else {
            ContentUnavailableView {
                Label(
                    String(
                        format: NSLocalizedString(
                            "browser.error.title.named",
                            comment: ""
                        ),
                        instance.name
                    ),
                    systemImage: "exclamationmark.triangle"
                )
            } description: {
                Text("browser.error.body")
            } actions: {
                Button("browser.other_instances") {
                    appModel.showInstancePicker()
                }
            }
        }
    }
}

/// Avibe's colours in native chrome: the mint accent is the Web UI's
/// `--primary` (#10b981 light, #5bffa0 dark), as the Live Activity uses;
/// text on an accent fill is `--primary-foreground` (white light, #080812
/// dark). White on #10b981 is the owner's accepted brand pair (2.54:1,
/// ui/src/index.css); #080812 on #5bffa0 is 15:1.
enum AvibeStyle {
    static let accent = Color("AccentColor")
    static let onAccent = Color(uiColor: UIColor { traits in
        traits.userInterfaceStyle == .dark
            ? UIColor(red: 0x08 / 255, green: 0x08 / 255, blue: 0x12 / 255, alpha: 1)
            : .white
    })
    /// The Workbench's top bar (instance name, account), below the status
    /// bar: native chrome over a page stays below it.
    static let topBarHeight: CGFloat = 44
    /// The Workbench's bottom tab bar, above the Home indicator.
    static let tabBarHeight: CGFloat = 56
    /// What a page keeps at its bottom edge: the tab bar, or a chat's
    /// composer, whichever is taller. Native banners sit above it.
    static let bottomChromeHeight: CGFloat = 64
    /// The open's progress line, at the top edge below the status bar.
    static let progressLineHeight: CGFloat = 2.5
}

/// How an open that is under way shows itself at first: a thin line at the
/// top edge, below the status bar. No text and no button, and it takes no
/// touches; VoiceOver hears the stage once.
struct RuntimeOpenProgressLine: View {
    let attempt: RuntimeOpenAttempt

    var body: some View {
        let label = runtimeOpenPhaseText(
            attempt.phase,
            instanceName: attempt.runtime.instance.name
        )
        RuntimeProgressLine(label: Text(label))
            .onAppear {
                // Once per attempt, however many screens show its line.
                if RuntimeOpenAnnouncements.lastAnnounced != attempt.id {
                    RuntimeOpenAnnouncements.lastAnnounced = attempt.id
                    AccessibilityNotification.Announcement(label).post()
                }
            }
            .accessibilityIdentifier("runtime-open-progress")
    }
}

@MainActor
enum RuntimeOpenAnnouncements {
    static var lastAnnounced: Int?
}

/// The thin progress line itself: Avibe green at the top edge, below the
/// status bar. It takes no touches.
struct RuntimeProgressLine: View {
    let label: Text
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var sweeping = false

    var body: some View {
        GeometryReader { geometry in
            let segment = geometry.size.width * 0.3
            ZStack(alignment: .leading) {
                Rectangle().fill(AvibeStyle.accent.opacity(0.18))
                if reduceMotion {
                    Rectangle().fill(AvibeStyle.accent.opacity(0.6))
                } else {
                    Capsule()
                        .fill(AvibeStyle.accent)
                        .frame(width: segment)
                        .offset(x: sweeping ? geometry.size.width : -segment)
                }
            }
        }
        .frame(height: AvibeStyle.progressLineHeight)
        .clipped()
        .frame(maxHeight: .infinity, alignment: .top)
        .allowsHitTesting(false)
        .onAppear {
            withAnimation(.linear(duration: 1.4).repeatForever(autoreverses: false)) {
                sweeping = true
            }
        }
        .accessibilityElement()
        .accessibilityLabel(label)
    }
}

/// An open showing the instance's last page from the cache, uncovered: the
/// line, and past the long-wait mark one row over the content (dismissable).
struct CachedRuntimeOpenProgress: View {
    let attempt: RuntimeOpenAttempt
    @State private var dismissedAttempt: Int?

    var body: some View {
        ZStack {
            RuntimeOpenProgressLine(attempt: attempt)
            if attempt.isTakingLong, dismissedAttempt != attempt.id {
                RuntimeOpenSlowBanner(
                    showsLastContent: true,
                    dismiss: { dismissedAttempt = attempt.id }
                )
                .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.2), value: attempt.isTakingLong)
    }
}

/// avibe.bot lists the instance offline while its last page is shown: one
/// row where the long-wait row sits, in its style, with Retry and Switch
/// instance. It stays until the instance answers (the open is then done).
struct RuntimeCachedPageOfflineBanner: View {
    let retry: () -> Void
    let switchInstance: () -> Void

    var body: some View {
        // One line when the copy and both buttons fit; otherwise the copy
        // in full, and the buttons below it on the right (stacked when even
        // they do not fit side by side, at accessibility sizes).
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .center, spacing: 10) {
                message
                    .frame(maxWidth: .infinity, alignment: .leading)
                buttons
            }
            VStack(alignment: .leading, spacing: 10) {
                message
                    .frame(maxWidth: .infinity, alignment: .leading)
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 10) {
                        Spacer(minLength: 0)
                        buttons
                    }
                    VStack(alignment: .trailing, spacing: 8) {
                        buttons
                    }
                    .frame(maxWidth: .infinity, alignment: .trailing)
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(
            .regularMaterial,
            in: RoundedRectangle(cornerRadius: 14, style: .continuous)
        )
        .shadow(color: .black.opacity(0.12), radius: 14, y: 6)
        .padding(.horizontal, 16)
        .padding(.bottom, AvibeStyle.bottomChromeHeight + 8)
        .frame(maxHeight: .infinity, alignment: .bottom)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("runtime-cached-offline")
        .onAppear {
            AccessibilityNotification.Announcement(
                NSLocalizedString("runtime.open.offline.body", comment: "")
            ).post()
        }
    }

    private var message: some View {
        Text("runtime.open.offline.body")
            .font(.subheadline.weight(.semibold))
            .fixedSize(horizontal: false, vertical: true)
    }

    @ViewBuilder private var buttons: some View {
        Button("runtime.open.offline.retry", action: retry)
            .buttonStyle(.bordered)
            .controlSize(.small)
            .tint(AvibeStyle.accent)
        Button("runtime.open.offline.switch", action: switchInstance)
            .buttonStyle(.bordered)
            .controlSize(.small)
            .tint(AvibeStyle.accent)
    }
}

/// The open of a cached page ran out of time: the content stays, and one
/// row says the instance is not reachable now, with Retry.
struct RuntimeCachedPageTimeoutBanner: View {
    let instanceName: String
    let retry: () -> Void
    @State private var dismissed = false
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        if !dismissed {
            content
        }
    }

    private var content: some View {
        HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(
                    String(
                        format: NSLocalizedString("runtime.open.cached_timeout.title.named", comment: ""),
                        instanceName
                    )
                )
                .font(.subheadline.weight(.semibold))
                Text("runtime.open.cached_timeout.body")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .lineLimit(dynamicTypeSize.isAccessibilitySize ? 3 : 1)
            .frame(maxWidth: .infinity, alignment: .leading)
            Button("runtime.open.cached_timeout.retry", action: retry)
                .buttonStyle(.bordered)
                .controlSize(.small)
                .tint(AvibeStyle.accent)
            Button(action: { dismissed = true }) {
                Image(systemName: "xmark")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            .accessibilityLabel(Text("runtime.open.timeout.dismiss"))
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(
            .regularMaterial,
            in: RoundedRectangle(cornerRadius: 14, style: .continuous)
        )
        .shadow(color: .black.opacity(0.12), radius: 14, y: 6)
        .padding(.horizontal, 16)
        .padding(.bottom, AvibeStyle.bottomChromeHeight + 8)
        .frame(maxHeight: .infinity, alignment: .bottom)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("runtime-cached-timeout")
        .onAppear {
            AccessibilityNotification.Announcement(
                String(
                    format: NSLocalizedString("runtime.open.cached_timeout.title.named", comment: ""),
                    instanceName
                )
            ).post()
        }
    }
}

/// The open's progress while no page content shows: the line, and past the
/// long-wait mark the banner with Cancel.
struct RuntimeOpenProgress: View {
    let attempt: RuntimeOpenAttempt
    let cancel: () -> Void

    var body: some View {
        ZStack {
            RuntimeOpenProgressLine(attempt: attempt)
            if attempt.offersCancel {
                RuntimeOpenSlowBanner(
                    showsLastContent: false,
                    cancel: cancel
                )
                .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.2), value: attempt.offersCancel)
        .onChange(of: attempt.offersCancel) { _, offers in
            // Cancel is the way out now: say so.
            if offers {
                AccessibilityNotification.Announcement(
                    String(localized: "runtime.open.slow")
                ).post()
            }
        }
    }
}

/// An open that has run past the long-wait mark says so in one row above
/// the page's tab bar or composer. Over the last page's content it only
/// says that content is shown; with nothing to show it offers Cancel, back
/// to the instances.
struct RuntimeOpenSlowBanner: View {
    let showsLastContent: Bool
    var cancel: (() -> Void)?
    var dismiss: (() -> Void)?
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        // No spinner: the progress line at the top already shows the open is
        // under way, and the instance's name is on the page.
        HStack(alignment: .center, spacing: 10) {
            Text(
                showsLastContent
                    ? "runtime.open.slow.last_content"
                    : "runtime.open.slow"
            )
            .font(.subheadline.weight(.semibold))
            .lineLimit(dynamicTypeSize.isAccessibilitySize ? 3 : 1)
            .frame(maxWidth: .infinity, alignment: .leading)
            if let cancel {
                Button("common.cancel", action: cancel)
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .tint(AvibeStyle.accent)
            }
            if let dismiss {
                Button(action: dismiss) {
                    Image(systemName: "xmark")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .accessibilityLabel(Text("runtime.open.timeout.dismiss"))
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .background(
            .regularMaterial,
            in: RoundedRectangle(cornerRadius: 14, style: .continuous)
        )
        .shadow(color: .black.opacity(0.12), radius: 14, y: 6)
        .padding(.horizontal, 16)
        .padding(.bottom, AvibeStyle.bottomChromeHeight + 8)
        .frame(maxHeight: .infinity, alignment: .bottom)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("runtime-open-slow")
    }
}

/// The page's shape before its first paint, with no data: a header, a few
/// list rows, and the tab bar. Touches stop at it.
struct RuntimeSkeleton: View {
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Circle().frame(width: 32, height: 32)
                RoundedRectangle(cornerRadius: 6).frame(width: 44, height: 14)
                Spacer()
                Circle().frame(width: 28, height: 28)
            }
            .padding(.horizontal, 20)
            .frame(height: AvibeStyle.topBarHeight)
            .padding(.bottom, 16)
            ForEach(0..<7, id: \.self) { index in
                HStack(alignment: .top, spacing: 12) {
                    Circle().frame(width: 40, height: 40)
                    VStack(alignment: .leading, spacing: 8) {
                        RoundedRectangle(cornerRadius: 5)
                            .frame(width: index.isMultiple(of: 2) ? 150 : 110, height: 12)
                        RoundedRectangle(cornerRadius: 5)
                            .frame(maxWidth: index.isMultiple(of: 3) ? 220 : 260)
                            .frame(height: 10)
                    }
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 12)
            }
            Spacer(minLength: 0)
            HStack {
                ForEach(0..<4, id: \.self) { _ in
                    Spacer()
                    RoundedRectangle(cornerRadius: 6).frame(width: 28, height: 28)
                    Spacer()
                }
            }
            .padding(.vertical, 14)
            .padding(.bottom, 20)
        }
        .foregroundStyle(Color(uiColor: .tertiarySystemFill))
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(uiColor: .systemBackground).ignoresSafeArea())
        // It covers a page that may already be painting underneath (a
        // sign-in redirect, a reload): taps stop here, as the old overlay's.
        .contentShape(Rectangle())
        .onTapGesture {}
        .accessibilityHidden(true)
        .accessibilityIdentifier("runtime-skeleton")
    }
}

private struct RuntimeOpenAwaitingUserBanner: View {
    let instanceName: String
    let cancel: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "person.crop.circle.badge.clock")
                .font(.title2)
                .foregroundStyle(.tint)
            VStack(alignment: .leading, spacing: 2) {
                Text(
                    String(
                        format: NSLocalizedString(
                            "runtime.open.awaiting_user.title.named",
                            comment: ""
                        ),
                        instanceName
                    )
                )
                .font(.subheadline.weight(.semibold))
                Text("runtime.open.awaiting_user.body")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Button("common.cancel", action: cancel)
                .buttonStyle(.bordered)
                .controlSize(.small)
        }
        .padding(14)
        .background(
            .regularMaterial,
            in: RoundedRectangle(cornerRadius: 14, style: .continuous)
        )
        .shadow(color: .black.opacity(0.12), radius: 14, y: 6)
        .accessibilityIdentifier("runtime-open-awaiting-user")
    }
}

private struct RuntimeOpenNoticeBanner: View {
    let notice: RuntimeOpenNotice
    let dismiss: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "clock.badge.exclamationmark")
                .font(.title2)
                .foregroundStyle(.orange)
            VStack(alignment: .leading, spacing: 3) {
                Text(
                    String(
                        format: NSLocalizedString(
                            "runtime.open.timeout.title.named",
                            comment: ""
                        ),
                        notice.instanceName
                    )
                )
                .font(.subheadline.weight(.semibold))
                Text(
                    String(
                        format: NSLocalizedString(
                            "runtime.open.timeout.last_stage",
                            comment: ""
                        ),
                        runtimeOpenPhaseLabel(notice.lastPhase)
                    )
                )
                .font(.caption)
                .foregroundStyle(.secondary)
                // The page's current block, above the notice, reopens it.
                Text(
                    String(
                        format: NSLocalizedString(
                            "runtime.open.timeout.reopen_hint.named",
                            comment: ""
                        ),
                        notice.instanceName
                    )
                )
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Button(action: dismiss) {
                Image(systemName: "xmark")
            }
            .buttonStyle(.plain)
            .accessibilityLabel(Text("runtime.open.timeout.dismiss"))
        }
        .padding(14)
        .background(
            .regularMaterial,
            in: RoundedRectangle(cornerRadius: 14, style: .continuous)
        )
        .shadow(color: .black.opacity(0.12), radius: 14, y: 6)
        .accessibilityIdentifier("runtime-open-timeout-notice")
    }
}

private func runtimeOpenPhaseText(
    _ phase: RuntimeOpenPhase,
    instanceName: String
) -> String {
    let key: String
    switch phase {
    case .preparingRoute:
        key = "runtime.open.phase.preparing_route.named"
    case .connecting:
        key = "runtime.open.phase.connecting.named"
    case .authenticating:
        key = "runtime.open.phase.authenticating.named"
    case .loadingPage:
        key = "runtime.open.phase.loading_page.named"
    case .showingCached, .showingCachedLive, .cachedUnreachable, .cachedOffline:
        key = "runtime.open.phase.connecting.named"
    case .awaitingUser:
        key = "runtime.open.awaiting_user.title.named"
    case .documentLoaded:
        key = "runtime.open.phase.document_loaded.named"
    case .failed:
        key = "runtime.open.phase.failed.named"
    }
    return String(
        format: NSLocalizedString(key, comment: ""),
        instanceName
    )
}

private func runtimeOpenPhaseLabel(_ phase: RuntimeOpenPhase) -> String {
    let key: String
    switch phase {
    case .preparingRoute:
        key = "runtime.open.stage.preparing_route"
    case .connecting:
        key = "runtime.open.stage.connecting"
    case .authenticating:
        key = "runtime.open.stage.authenticating"
    case .loadingPage:
        key = "runtime.open.stage.loading_page"
    case .showingCached, .showingCachedLive, .cachedUnreachable, .cachedOffline:
        key = "runtime.open.stage.connecting"
    case .awaitingUser:
        key = "runtime.open.stage.awaiting_user"
    case .documentLoaded:
        key = "runtime.open.stage.document_loaded"
    case .failed:
        key = "runtime.open.stage.failed"
    }
    return NSLocalizedString(key, comment: "")
}

/// The listener fault's message: reopen the App. It has no Retry and no other
/// action, because nothing in this process reaches a working listener.
private struct DirectFaultMessage: View {
    var body: some View {
        ContentUnavailableView {
            Label(
                "browser.direct_fault.title",
                systemImage: "wifi.exclamationmark"
            )
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(
            Color(uiColor: .systemBackground)
                .ignoresSafeArea(.container, edges: .top)
        )
        .contentShape(Rectangle())
        .accessibilityIdentifier("direct-listener-fault")
    }
}

/// What covers the runtime's WebView. The listener fault comes first: it is
/// terminal for the process, so neither the spinner nor the load-failure
/// view, whose Retry would not reach a working listener, is shown with it.
enum BrowserOverlay: Equatable {
    case none
    case loading
    case showingCachedLive
    case cachedUnreachable
    case cachedOffline
    case awaitingUser
    case loadFailure
    case directFault

    init(attemptPhase: RuntimeOpenPhase?, directFault: Bool) {
        if directFault {
            self = .directFault
            return
        }
        switch attemptPhase {
        case .preparingRoute, .connecting, .authenticating, .loadingPage,
             .showingCached:
            // A cached page stays covered until its instance answers,
            // unless its shell waits out an unreachable instance.
            self = .loading
        case .showingCachedLive:
            self = .showingCachedLive
        case .cachedUnreachable:
            self = .cachedUnreachable
        case .cachedOffline:
            self = .cachedOffline
        case .awaitingUser:
            self = .awaitingUser
        case .failed:
            self = .loadFailure
        case .documentLoaded, nil:
            self = .none
        }
    }

    init(isLoading: Bool, failed: Bool, directFault: Bool) {
        if directFault {
            self = .directFault
        } else if failed {
            self = .loadFailure
        } else if isLoading {
            self = .loading
        } else {
            self = .none
        }
    }
}

struct LegacyRuntimeToolbar: View {
    @Environment(\.colorScheme) private var colorScheme

    let instanceName: String
    let accountEmail: String?
    let showAccount: () -> Void
    let showInstancePicker: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Text(instanceName)
                .font(.subheadline.weight(.semibold))
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)

            Menu {
                Button(
                    "browser.account",
                    systemImage: "person.crop.circle",
                    action: showAccount
                )
                Button(
                    "browser.change_instance",
                    systemImage: "arrow.triangle.2.circlepath",
                    action: showInstancePicker
                )
            } label: {
                Text(runtimeAccountInitial(for: accountEmail))
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(runtimeAccountAccent(for: colorScheme))
                    .frame(width: 32, height: 32)
                    .background(
                        runtimeAccountAccent(for: colorScheme).opacity(0.08),
                        in: Circle()
                    )
                    .overlay {
                        Circle()
                            .stroke(
                                runtimeAccountAccent(for: colorScheme)
                                    .opacity(0.35),
                                lineWidth: 1
                            )
                    }
            }
            .accessibilityLabel(Text("browser.menu.accessibility"))
        }
        .padding(.horizontal, 16)
        .frame(height: 56)
        .background(runtimeHeaderBackground(for: colorScheme))
        .overlay(alignment: .bottom) {
            Rectangle()
                .fill(runtimeHeaderBorder(for: colorScheme))
                .frame(height: 1)
        }
        .accessibilityIdentifier("legacy-runtime-toolbar")
    }
}

func runtimeAccountInitial(for email: String?) -> String {
    guard let email else {
        return "A"
    }
    let localPart = email.split(separator: "@", maxSplits: 1).first
    let trimmed = localPart?.trimmingCharacters(
        in: .whitespacesAndNewlines
    )
    return trimmed?.first.map { String($0).uppercased() } ?? "A"
}

private func runtimeHeaderBackground(
    for colorScheme: ColorScheme
) -> Color {
    colorScheme == .dark
        ? Color(red: 8 / 255, green: 8 / 255, blue: 18 / 255)
        : Color(red: 244 / 255, green: 246 / 255, blue: 251 / 255)
}

private func runtimeHeaderBorder(
    for colorScheme: ColorScheme
) -> Color {
    colorScheme == .dark
        ? Color.white.opacity(0.08)
        : Color(red: 8 / 255, green: 8 / 255, blue: 18 / 255)
            .opacity(0.08)
}

private func runtimeAccountAccent(
    for colorScheme: ColorScheme
) -> Color {
    colorScheme == .dark
        ? Color(red: 63 / 255, green: 224 / 255, blue: 229 / 255)
        : Color(red: 8 / 255, green: 145 / 255, blue: 178 / 255)
}
