import SwiftUI

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
                        .allowsHitTesting(!appModel.route.coversRuntime)
                        .accessibilityHidden(appModel.route.coversRuntime)
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
                if let attempt = appModel.runtimeOpenAttempt {
                    RuntimeOpenProgressOverlay(
                        attempt: attempt,
                        cancel: {
                            appModel.cancelRuntimeOpen(attemptID: attempt.id)
                        }
                    )
                } else {
                    ProgressView()
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
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
                returnToRuntime: appModel.returnToRuntime
            )
            .ignoresSafeArea(.container, edges: .all)
            .presentationDetents([.large])
            .presentationDragIndicator(.visible)
        }
        .background(Color(uiColor: .systemBackground))
    }
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
                        instanceName: appModel.selectedInstanceName,
                        directStatus: appModel.directStatusProvider,
                        directLoadGate: appModel.directLoadGateProvider(for: runtime),
                        pushOpenedAck: appModel.acknowledgePushOpened,
                        setBadgeCount: appModel.setBadgeCount,
                        reconcileNotifications: appModel.reconcileNotifications,
                        accountFallbackVisibilityChanged: {
                            showsAccountFallback = $0
                        },
                        pageColorSchemeChanged: {
                            pageColorScheme = $0
                        },
                        runtimeNavigationChanged: { event in
                            appModel.handleRuntimeNavigation(
                                event,
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
                        RuntimeOpenProgressOverlay(
                            attempt: attempt,
                            cancel: {
                                appModel.cancelRuntimeOpen(
                                    attemptID: attempt.id
                                )
                            }
                        )
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

private struct RuntimeOpenProgressOverlay: View {
    let attempt: RuntimeOpenAttempt
    let cancel: () -> Void

    var body: some View {
        VStack(spacing: 14) {
            ProgressView()
                .controlSize(.large)
            Text(
                runtimeOpenPhaseText(
                    attempt.phase,
                    instanceName: attempt.runtime.instance.name
                )
            )
                .font(.headline)
                .multilineTextAlignment(.center)
            if attempt.isTakingLong {
                Text("runtime.open.long_wait")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            Button("common.cancel", action: cancel)
                .buttonStyle(.bordered)
        }
        .padding(22)
        .frame(maxWidth: 320)
        .background(
            .regularMaterial,
            in: RoundedRectangle(cornerRadius: 16, style: .continuous)
        )
        .shadow(color: .black.opacity(0.12), radius: 18, y: 8)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.black.opacity(0.08))
        .accessibilityIdentifier("runtime-open-progress")
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
    case awaitingUser
    case loadFailure
    case directFault

    init(attemptPhase: RuntimeOpenPhase?, directFault: Bool) {
        if directFault {
            self = .directFault
            return
        }
        switch attemptPhase {
        case .preparingRoute, .connecting, .authenticating, .loadingPage:
            self = .loading
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
