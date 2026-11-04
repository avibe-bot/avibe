import SwiftUI

struct RootView: View {
    @EnvironmentObject private var appModel: AppModel

    var body: some View {
        Group {
            if let instanceOrigin = appModel.instanceOrigin {
                BrowserScreen(instanceOrigin: instanceOrigin)
            } else {
                InstanceSetupView()
            }
        }
        .background(Color(uiColor: .systemBackground))
    }
}

private struct InstanceSetupView: View {
    @EnvironmentObject private var appModel: AppModel
    @State private var instanceURL = ""
    @State private var showsValidationError = false

    var body: some View {
        VStack(spacing: 24) {
            Spacer()

            Image(systemName: "waveform.circle.fill")
                .font(.system(size: 64))
                .foregroundStyle(.tint)
                .accessibilityHidden(true)

            VStack(spacing: 8) {
                Text("setup.title")
                    .font(.largeTitle.bold())
                Text("setup.subtitle")
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
            }

            VStack(alignment: .leading, spacing: 10) {
                TextField("setup.url.placeholder", text: $instanceURL)
                    .textContentType(.URL)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .submitLabel(.go)
                    .onSubmit(connect)
                    .padding(14)
                    .background(.secondary.opacity(0.1), in: RoundedRectangle(cornerRadius: 8))

                if showsValidationError {
                    Text("setup.invalid")
                        .font(.footnote)
                        .foregroundStyle(.red)
                }

                Button(action: connect) {
                    Text("setup.connect")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
            }
            .frame(maxWidth: 480)

            Spacer()
        }
        .padding(24)
    }

    private func connect() {
        showsValidationError = !appModel.configureInstance(from: instanceURL)
    }
}

private struct BrowserScreen: View {
    @EnvironmentObject private var appModel: AppModel
    let instanceOrigin: WebOrigin

    @State private var reloadID = UUID()
    @State private var isLoading = true
    @State private var failed = false

    var body: some View {
        ZStack(alignment: .topTrailing) {
            WebViewContainer(
                instanceOrigin: instanceOrigin,
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
                        appModel.forgetInstance()
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color(uiColor: .systemBackground))
            }

            if !failed {
                Menu {
                    Button("browser.change_instance", systemImage: "arrow.triangle.2.circlepath") {
                        appModel.forgetInstance()
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
    }
}
