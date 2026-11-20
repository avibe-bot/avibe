import SwiftUI

struct HealthConsentView: View {
    let request: HealthConsentRequest
    let answer: (Bool) -> Void

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    LabeledContent(String(localized: "health.sync_to"), value: request.target.name)
                    Text(request.target.origin)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                    Label(String(localized: "health.metrics"), systemImage: "heart")
                }
                Section {
                    Text(String(localized: "health.explanation"))
                    Text(String(localized: "health.ai_disclosure"))
                }
                Section {
                    Text(String(localized: "health.stop"))
                    Text(String(localized: "health.single_instance"))
                        .foregroundStyle(.secondary)
                }
                Section {
                    Button(String(localized: "health.allow")) { answer(true) }
                        .accessibilityIdentifier("health.allow")
                    Button(String(localized: "health.not_now"), role: .cancel) { answer(false) }
                        .accessibilityIdentifier("health.not_now")
                }
            }
            .navigationTitle(String(localized: "health.title"))
            .navigationBarTitleDisplayMode(.inline)
        }
        .interactiveDismissDisabled()
    }
}

struct HealthConsentHost: ViewModifier {
    @ObservedObject var health: HealthSyncCoordinator

    func body(content: Content) -> some View {
        content
            .sheet(item: Binding(get: { health.consent }, set: { if $0 == nil { health.answerConsent(false) } })) { request in
                HealthConsentView(request: request, answer: health.answerConsent)
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
