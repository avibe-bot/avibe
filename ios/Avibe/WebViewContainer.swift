import SwiftUI
import WebKit

struct WebViewContainer: UIViewControllerRepresentable {
    final class Coordinator {
        let didReleaseWebViews: (WebsiteDataStoreReleaseBarrier) -> Void

        init(
            didReleaseWebViews: @escaping (
                WebsiteDataStoreReleaseBarrier
            ) -> Void
        ) {
            self.didReleaseWebViews = didReleaseWebViews
        }
    }

    let instanceOrigin: WebOrigin
    let websiteDataStore: WKWebsiteDataStore
    let showAccount: () -> Void
    let accountFallbackVisibilityChanged: (Bool) -> Void
    let didCreateController: (WebViewController) -> Void
    let didReleaseWebViews: (WebsiteDataStoreReleaseBarrier) -> Void
    let didStartLoading: () -> Void
    let didFinishLoading: () -> Void
    let didFailLoading: () -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(didReleaseWebViews: didReleaseWebViews)
    }

    func makeUIViewController(context: Context) -> WebViewController {
        let controller = WebViewController(
            instanceOrigin: instanceOrigin,
            websiteDataStore: websiteDataStore,
            showAccount: showAccount,
            accountFallbackVisibilityChanged:
                accountFallbackVisibilityChanged,
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
        coordinator: Coordinator
    ) {
        coordinator.didReleaseWebViews(
            uiViewController.invalidateForAccountTransition()
        )
    }
}
