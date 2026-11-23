import SwiftUI

struct HealthConsentView: View {
    let request: HealthConsentRequest
    let answer: (Bool) -> Void
    @State private var showsPrivacyDetails = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    LabeledContent(String(localized: "health.sync_to"), value: request.target.name)
                    Text(request.target.origin)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                    Label {
                        Text(String(localized: "health.metrics"))
                    } icon: {
                        Image(systemName: "heart")
                            .foregroundStyle(AvibeStyle.accent)
                    }
                }
                Section {
                    Text(String.localizedStringWithFormat(String(localized: "health.summary"), request.target.name))
                }
                Section {
                    Text(String(localized: "health.access_summary"))
                }
            }
            .safeAreaInset(edge: .bottom, spacing: 0) {
                VStack(spacing: 8) {
                    Button { answer(true) } label: {
                        Text(String(localized: "health.allow"))
                            .font(.body.weight(.semibold))
                            .frame(maxWidth: .infinity, minHeight: 48)
                            .foregroundStyle(AvibeStyle.onAccent)
                            .background(AvibeStyle.accent, in: RoundedRectangle(cornerRadius: 12))
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("health.allow")
                    Button(String(localized: "health.not_now"), role: .cancel) { answer(false) }
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .buttonStyle(.plain)
                        .foregroundStyle(.secondary)
                        .accessibilityIdentifier("health.not_now")
                }
                .padding(.horizontal, 20)
                .padding(.top, 12)
                .background(.bar)
            }
            .navigationTitle(String(localized: "health.title"))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showsPrivacyDetails = true } label: {
                        Image(systemName: "info.circle")
                    }
                    .accessibilityLabel(String(localized: "health.privacy_details"))
                    .accessibilityIdentifier("health.privacy_details")
                }
            }
            .sheet(isPresented: $showsPrivacyDetails) {
                NavigationStack {
                    Form {
                        Section {
                            LabeledContent(String(localized: "health.sync_to"), value: request.target.name)
                            Text(request.target.origin)
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                        }
                        Section {
                            Text(String(localized: "health.explanation"))
                            Text(String(localized: "health.ai_disclosure"))
                        }
                        Section {
                            Text(String(localized: "health.stop"))
                            Text(String(localized: "health.single_instance"))
                        }
                    }
                    .navigationTitle(String(localized: "health.privacy_details"))
                    .navigationBarTitleDisplayMode(.inline)
                    .toolbar {
                        ToolbarItem(placement: .confirmationAction) {
                            Button(String(localized: "health.ok")) { showsPrivacyDetails = false }
                        }
                    }
                }
            }
        }
        .interactiveDismissDisabled()
    }
}

struct HealthConsentHost: ViewModifier {
    @ObservedObject var health: HealthSyncCoordinator
    @State private var presentedConsentID: UUID?

    func body(content: Content) -> some View {
        content
            .sheet(item: Binding(get: { health.consent }, set: {
                if $0 == nil, health.consent != nil { health.answerConsent(false) }
            }), onDismiss: {
                if let id = presentedConsentID { health.consentDismissed(id: id) }
                presentedConsentID = nil
            }) { request in
                HealthConsentView(request: request, answer: { health.answerConsent($0, afterDismissal: true) })
                    .onAppear { presentedConsentID = request.id }
            }
            .alert(String(localized: "health.title"), isPresented: Binding(
                get: { health.notice != nil }, set: { if !$0 { health.dismissNotice() } }
            )) {
                Button(String(localized: "health.ok")) { health.dismissNotice() }
            } message: {
                Text(LocalizedStringKey(health.notice ?? "health.connection_failed"))
            }
    }
}
