import SwiftUI

struct WebViewContainer: UIViewControllerRepresentable {
    let instanceOrigin: WebOrigin
    let didStartLoading: () -> Void
    let didFinishLoading: () -> Void
    let didFailLoading: () -> Void

    func makeUIViewController(context: Context) -> WebViewController {
        WebViewController(
            instanceOrigin: instanceOrigin,
            didStartLoading: didStartLoading,
            didFinishLoading: didFinishLoading,
            didFailLoading: didFailLoading
        )
    }

    func updateUIViewController(_ uiViewController: WebViewController, context: Context) {}
}
