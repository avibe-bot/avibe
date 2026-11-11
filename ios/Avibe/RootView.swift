import SwiftUI

struct RootView: View {
    @EnvironmentObject private var appModel: AppModel

    // Each layer keeps its own position, so Account can cover a runtime and
    // leave it untouched, and an account transition can release that runtime
    // without reloading the account page above it.
    var body: some View {
        ZStack {
            if let runtime = appModel.route.runtime {
                BrowserScreen(runtime: runtime)
                    .id(appModel.runtimeIdentity)
                    .allowsHitTesting(!appModel.route.coversRuntime)
                    .accessibilityHidden(appModel.route.coversRuntime)
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
                        pushOpenedAck: appModel.acknowledgePushOpened,
                        setBadgeCount: appModel.setBadgeCount,
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

                    if isLoading && !failed {
                        ProgressView("browser.loading")
                            .padding(14)
                            .background(
                                .regularMaterial,
                                in: RoundedRectangle(cornerRadius: 8)
                            )
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }

                    if failed {
                        ContentUnavailableView {
                            Label(
                                "browser.error.title",
                                systemImage: "wifi.exclamationmark"
                            )
                        } description: {
                            Text("browser.error.body")
                        } actions: {
                            Button("browser.error.retry") {
                                showsAccountFallback = false
                                reloadID = UUID()
                            }
                            .buttonStyle(.borderedProminent)

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
