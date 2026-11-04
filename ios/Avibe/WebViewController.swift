import SafariServices
import UIKit
import WebKit

final class WebViewController: UIViewController {
    private let instanceOrigin: WebOrigin
    private let navigationPolicy: NavigationPolicy
    private let didStartLoading: () -> Void
    private let didFinishLoading: () -> Void
    private let didFailLoading: () -> Void

    private lazy var webView: WKWebView = {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.allowsInlineMediaPlayback = true

        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.translatesAutoresizingMaskIntoConstraints = false
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.scrollView.isScrollEnabled = false
        webView.scrollView.bounces = false
        return webView
    }()

    init(
        instanceOrigin: WebOrigin,
        didStartLoading: @escaping () -> Void,
        didFinishLoading: @escaping () -> Void,
        didFailLoading: @escaping () -> Void
    ) {
        self.instanceOrigin = instanceOrigin
        navigationPolicy = NavigationPolicy(instanceOrigins: [instanceOrigin])
        self.didStartLoading = didStartLoading
        self.didFinishLoading = didFinishLoading
        self.didFailLoading = didFailLoading
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        view.addSubview(webView)

        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            webView.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: view.keyboardLayoutGuide.topAnchor),
        ])

        NotificationCenter.default.addObserver(
            self,
            selector: #selector(resetOuterScrollPosition),
            name: UIResponder.keyboardWillChangeFrameNotification,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(resetOuterScrollPosition),
            name: UIResponder.keyboardWillHideNotification,
            object: nil
        )

        webView.load(URLRequest(url: instanceOrigin.url))
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
    }

    @objc
    private func resetOuterScrollPosition() {
        webView.scrollView.setContentOffset(.zero, animated: false)
    }

    private func presentExternalURL(_ url: URL) {
        guard presentedViewController == nil else {
            return
        }
        present(SFSafariViewController(url: url), animated: true)
    }

    private func presentGoogleSignInMessage() {
        guard presentedViewController == nil else {
            return
        }
        let alert = UIAlertController(
            title: String(localized: "google.title"),
            message: String(localized: "google.body"),
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: String(localized: "common.ok"), style: .default))
        present(alert, animated: true)
    }

    private func navigationDecision(for url: URL, isMainFrame: Bool) -> WKNavigationActionPolicy {
        switch navigationPolicy.destination(for: url) {
        case .instance, .authentication:
            return .allow
        case .googleAuthentication:
            if isMainFrame {
                didFinishLoading()
                presentGoogleSignInMessage()
            }
            return .cancel
        case .external:
            if isMainFrame {
                didFinishLoading()
                presentExternalURL(url)
            }
            return .cancel
        case .blocked:
            if isMainFrame {
                didFinishLoading()
            }
            return .cancel
        }
    }

    private func reportLoadFailure(_ error: Error) {
        let error = error as NSError
        guard error.domain != NSURLErrorDomain || error.code != NSURLErrorCancelled else {
            return
        }
        didFailLoading()
    }
}

extension WebViewController: WKNavigationDelegate {
    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        let isMainFrame = navigationAction.targetFrame?.isMainFrame ?? true
        decisionHandler(navigationDecision(for: url, isMainFrame: isMainFrame))
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation?) {
        didStartLoading()
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation?) {
        didFinishLoading()
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation?,
        withError error: Error
    ) {
        reportLoadFailure(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation?, withError error: Error) {
        reportLoadFailure(error)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        didFailLoading()
    }
}

extension WebViewController: WKUIDelegate {
    func webView(
        _ webView: WKWebView,
        requestMediaCapturePermissionFor origin: WKSecurityOrigin,
        initiatedByFrame frame: WKFrameInfo,
        type: WKMediaCaptureType,
        decisionHandler: @escaping (WKPermissionDecision) -> Void
    ) {
        let requestedOrigin = WebOrigin(
            scheme: origin.protocol,
            host: origin.host,
            port: origin.port == 0 ? nil : origin.port
        )
        let allowed = frame.isMainFrame
            && type == .microphone
            && requestedOrigin == instanceOrigin
        decisionHandler(allowed ? .grant : .deny)
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        guard let url = navigationAction.request.url else {
            return nil
        }
        if navigationDecision(for: url, isMainFrame: true) == .allow {
            webView.load(navigationAction.request)
        }
        return nil
    }
}
