import SwiftUI

struct RootView: View {
    @EnvironmentObject private var appModel: AppModel

    var body: some View {
        Group {
            switch appModel.route {
            case .loading:
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .onboarding(let startScreen):
                MobileOnboardingContainer(
                    coordinator: appModel.mobileSessionCoordinator,
                    startScreen: startScreen
                )
                .ignoresSafeArea(.container, edges: .all)
            case .browser(let runtime):
                BrowserScreen(runtime: runtime)
            }
        }
        .background(Color(uiColor: .systemBackground))
    }
}

private struct BrowserScreen: View {
    @EnvironmentObject private var appModel: AppModel
    let runtime: MobileRuntimeContext

    @State private var reloadID = UUID()
    @State private var isLoading = true
    @State private var failed = false

    var body: some View {
        let instance = runtime.instance
        if let instanceOrigin = WebOrigin(url: instance.url) {
            ZStack(alignment: .topTrailing) {
                WebViewContainer(
                    instanceOrigin: instanceOrigin,
                    websiteDataStore: appModel.runtimeWebsiteDataStore,
                    showAccount: appModel.showAccount,
                    didCreateController: {
                        appModel.registerRuntimeController($0, for: runtime)
                    },
                    didStartLoading: {
                        isLoading = true
                        failed = false
                    },
                    didFinishLoading: {
                        isLoading = false
                        failed = false
                    },
                    didFailLoading: {
                        isLoading = false
                        failed = true
                    }
                )
                .id(reloadID)
                .ignoresSafeArea(.keyboard)

                if isLoading && !failed {
                    ProgressView("browser.loading")
                        .padding(14)
                        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8))
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }

                if failed {
                    ContentUnavailableView {
                        Label("browser.error.title", systemImage: "wifi.exclamationmark")
                    } description: {
                        Text("browser.error.body")
                    } actions: {
                        Button("browser.error.retry") {
                            reloadID = UUID()
                        }
                        .buttonStyle(.borderedProminent)

                        Button("browser.change_instance") {
                            appModel.showInstancePicker()
                        }
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(Color(uiColor: .systemBackground))
                }

                if !failed {
                    Menu {
                        Button("browser.account", systemImage: "person.crop.circle") {
                            appModel.showAccount()
                        }
                        Button(
                            "browser.change_instance",
                            systemImage: "arrow.triangle.2.circlepath"
                        ) {
                            appModel.showInstancePicker()
                        }
                    } label: {
                        Image(systemName: "ellipsis.circle.fill")
                            .font(.title2)
                            .symbolRenderingMode(.hierarchical)
                            .padding(8)
                            .background(.regularMaterial, in: Circle())
                    }
                    .accessibilityLabel(Text("browser.menu.accessibility"))
                    .padding(10)
                }
            }
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
