import SwiftUI
import WebKit

struct WebViewContainer: UIViewControllerRepresentable {
    let instanceOrigin: WebOrigin
    let websiteDataStore: WKWebsiteDataStore
    let showAccount: () -> Void
    let didCreateController: (WebViewController) -> Void
    let didStartLoading: () -> Void
    let didFinishLoading: () -> Void
    let didFailLoading: () -> Void

    func makeUIViewController(context: Context) -> WebViewController {
        let controller = WebViewController(
            instanceOrigin: instanceOrigin,
            websiteDataStore: websiteDataStore,
            showAccount: showAccount,
            didStartLoading: didStartLoading,
            didFinishLoading: didFinishLoading,
            didFailLoading: didFailLoading
        )
        didCreateController(controller)
        return controller
    }

    func updateUIViewController(
        _ uiViewController: WebViewController,
        context: Context
    ) {}

    static func dismantleUIViewController(
        _ uiViewController: WebViewController,
        coordinator: Void
    ) {
        uiViewController.invalidateForAccountTransition()
    }
}
