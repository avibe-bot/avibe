import SwiftUI

struct RootView: View {
    @EnvironmentObject private var appModel: AppModel

    // Each layer keeps its own position, so Account can cover a runtime and
    // leave it untouched, and an account transition can release that runtime
    // without reloading the account page above it.
    var body: some View {
        ZStack {
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
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(Color(uiColor: .systemBackground))
    }
}

private struct BrowserScreen: View {
    @EnvironmentObject private var appModel: AppModel
    @Environment(\.colorScheme) private var colorScheme
    let runtime: MobileRuntimeContext

    @State private var reloadID = UUID()
    @State private var isLoading = true
    @State private var failed = false
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

                ZStack {
                    WebViewContainer(
                        instanceOrigin: instanceOrigin,
                        websiteDataStore: appModel.runtimeWebsiteDataStore,
                        pushCoordinator: appModel.pushCoordinator,
                        showAccount: appModel.showAccount,
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
                        didCreateController: {
                            appModel.registerRuntimeController($0, for: runtime)
                        },
                        didReleaseWebViews: {
                            appModel.recordRuntimeReleaseBarrier($0)
                        },
                        didStartLoading: {
                            isLoading = true
                            failed = false
                        },
                        didFinishLoading: {
                            isLoading = false
                            failed = false
                            appModel.didFinishRuntimeLoading()
                        },
                        didFailLoading: {
                            isLoading = false
                            failed = true
                            showsAccountFallback = false
                        }
                    )
                    .id(reloadID)
                    .ignoresSafeArea(.keyboard)
                    // The controller decides where the page ends; the Web
                    // UI reserves the status bar and the Home indicator
                    // strip itself. Below the legacy toolbar the top edge
                    // is not the screen's, so the page starts there.
                    .ignoresSafeArea(.container, edges: [.top, .bottom])

                    let overlay = BrowserOverlay(
                        isLoading: isLoading,
                        failed: failed,
                        directFault: appModel.directFaultCovers(runtime)
                    )

                    if overlay == .loading {
                        ProgressView("browser.loading")
                            .padding(14)
                            .background(
                                .regularMaterial,
                                in: RoundedRectangle(cornerRadius: 8)
                            )
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }

                    if overlay == .loadFailure {
                        ContentUnavailableView {
                            Label(
                                "browser.error.title",
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
                                    // Decided again at the tap, for the
                                    // reload itself.
                                    guard appModel.authorizeReload(of: runtime) else {
                                        return
                                    }
                                    showsAccountFallback = false
                                    reloadID = UUID()
                                }
                                .buttonStyle(.borderedProminent)
                            }

                            Button("browser.change_instance") {
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
                Label("browser.error.title", systemImage: "exclamationmark.triangle")
            } description: {
                Text("browser.error.body")
            } actions: {
                Button("browser.change_instance") {
                    appModel.showInstancePicker()
                }
            }
        }
    }
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
    case loadFailure
    case directFault

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
