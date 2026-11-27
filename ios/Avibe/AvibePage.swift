import UIKit
import WebKit

/// The avibe.bot pages the bundled account page may show inside the App
/// (`app.openPage`). The set is closed: the page names one of these, never a
/// URL, and each has one fixed address on the control plane.
enum AvibePage: String, CaseIterable {
    /// Deleting the account, with the App's sign-in (App Review 5.1.1(v)).
    case accountDeletion
    case privacyPolicy

    private var path: String {
        switch self {
        case .accountDeletion:
            return "/account/delete"
        case .privacyPolicy:
            return "/privacy"
        }
    }

    /// Where the deletion page goes once the account is gone (agreed with
    /// avibe-bot-backend): the App closes the page instead of loading it.
    static let accountDeletionDonePath = "/account/delete/done"

    /// Whether the page needs the account's sign-in: it then shares the
    /// account's WebKit store. Other pages get a store of their own.
    var usesAccountSignIn: Bool {
        self == .accountDeletion
    }

    func url(controlPlane: URL) -> URL {
        var components = URLComponents()
        components.scheme = controlPlane.scheme
        components.host = controlPlane.host
        components.port = controlPlane.port
        components.path = path
        return components.url!
    }
}

enum AvibePageNavigation: Equatable {
    case allow
    /// The deletion page reported the account deleted.
    case finished
    /// Somewhere other than the control plane: the system opens it.
    case openOutside
    case cancel
}

/// Navigation inside an `AvibePage`. The main frame stays on the control
/// plane's origin; other web addresses and application schemes go to the
/// system. Subframes load web content but never launch anything.
struct AvibePagePolicy {
    let page: AvibePage
    let origin: WebOrigin

    func mainFrameNavigation(to url: URL) -> AvibePageNavigation {
        guard let scheme = url.scheme?.lowercased() else {
            return .cancel
        }
        switch scheme {
        case "about", "blob", "data":
            return .allow
        case "http", "https":
            guard WebOrigin(url: url, requiresHTTPS: false) == origin else {
                return .openOutside
            }
            if
                page == .accountDeletion,
                URLComponents(url: url, resolvingAgainstBaseURL: false)?
                    .percentEncodedPath == AvibePage.accountDeletionDonePath
            {
                return .finished
            }
            return .allow
        case "javascript", "file":
            return .cancel
        default:
            return .openOutside
        }
    }

    func subframeNavigation(to url: URL) -> AvibePageNavigation {
        switch url.scheme?.lowercased() {
        case "http", "https", "about", "blob", "data":
            return .allow
        default:
            return .cancel
        }
    }

    /// `window.open` and `target="_blank"`: web addresses open in the
    /// system browser; the page itself never gets a second window.
    func newWindow(to url: URL) -> AvibePageNavigation {
        switch url.scheme?.lowercased() {
        case "http", "https", "mailto", "tel":
            return .openOutside
        default:
            return .cancel
        }
    }
}

/// One `AvibePage` in a sheet with Done. It reports how it closed exactly
/// once: `true` when the deletion page reported the account deleted, `false`
/// for Done, a swipe down, or the presenter going away. The pages use no
/// JavaScript dialogs, so it implements none: WebKit answers them itself.
final class AvibePageViewController: UIViewController, WKNavigationDelegate,
    WKUIDelegate, UIAdaptivePresentationControllerDelegate
{
    let webView: WKWebView
    private let policy: AvibePagePolicy
    private let pageURL: URL
    private var onFinish: ((Bool) -> Void)?
    private var hostObservation: NSKeyValueObservation?
    /// Set when the page closes itself; any other dismissal is `false`.
    private var closingResult: Bool?

    init(
        page: AvibePage,
        controlPlane: URL,
        websiteDataStore: WKWebsiteDataStore,
        userAgentSuffix: String = NativeShellMetadata().userAgentSuffix,
        onFinish: @escaping (Bool) -> Void
    ) {
        let url = page.url(controlPlane: controlPlane)
        pageURL = url
        policy = AvibePagePolicy(
            page: page,
            origin: WebOrigin(url: url, requiresHTTPS: false)!
        )
        self.onFinish = onFinish
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = websiteDataStore
        configuration.applicationNameForUserAgent = userAgentSuffix
        webView = WKWebView(frame: .zero, configuration: configuration)
        super.init(nibName: nil, bundle: nil)
        // Not registered with `DirectWebViewRegistry`: this WebView only ever
        // holds its one avibe.bot page (`AvibePagePolicy` opens anything else
        // outside the App), so it is never another document of an instance
        // for a direct hand-over.
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.translatesAutoresizingMaskIntoConstraints = false
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    deinit {
        // A presentation that never appeared still answers its caller.
        onFinish?(false)
    }

    /// The navigation controller this page is presented in.
    func makePresentation() -> UINavigationController {
        let navigationController = UINavigationController(rootViewController: self)
        navigationController.modalPresentationStyle = .pageSheet
        navigationController.presentationController?.delegate = self
        return navigationController
    }

    /// UIKit refused the presentation (the presenter left its window
    /// meanwhile): answer now instead of never.
    func finishUnlessPresented() {
        if (navigationController ?? self).presentingViewController == nil {
            finish(completed: false)
        }
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        view.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            webView.leadingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.safeAreaLayoutGuide.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        navigationItem.leftBarButtonItem = UIBarButtonItem(
            title: String(localized: "common.done"),
            style: .done,
            target: self,
            action: #selector(done)
        )
        hostObservation = webView.observe(\.url, options: [.initial, .new]) { [weak self] webView, _ in
            self?.navigationItem.title = webView.url?.host
        }
        webView.load(URLRequest(url: pageURL))
    }

    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        // Dismissed by anything else, including the presenter going away.
        if isBeingDismissed || navigationController?.isBeingDismissed == true {
            finish(completed: closingResult ?? false)
        }
    }

    @objc
    private func done() {
        close(completed: false)
    }

    func presentationControllerDidDismiss(
        _ presentationController: UIPresentationController
    ) {
        finish(completed: false)
    }

    private func close(completed: Bool) {
        webView.stopLoading()
        closingResult = completed
        let presented = navigationController ?? self
        if presented.presentingViewController != nil {
            // `viewDidDisappear` reports the result once the sheet is gone.
            presented.dismiss(animated: true)
        } else {
            finish(completed: completed)
        }
    }

    private func finish(completed: Bool) {
        guard let onFinish else {
            return
        }
        self.onFinish = nil
        onFinish(completed)
    }

    private func openOutside(_ url: URL) {
        UIApplication.shared.open(url)
    }

    // MARK: WKNavigationDelegate

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        let decision = navigationAction.targetFrame?.isMainFrame == false
            ? policy.subframeNavigation(to: url)
            : policy.mainFrameNavigation(to: url)
        switch decision {
        case .allow:
            decisionHandler(.allow)
        case .finished:
            decisionHandler(.cancel)
            close(completed: true)
        case .openOutside:
            decisionHandler(.cancel)
            openOutside(url)
        case .cancel:
            decisionHandler(.cancel)
        }
    }

    // MARK: WKUIDelegate

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if
            let url = navigationAction.request.url,
            policy.newWindow(to: url) == .openOutside
        {
            openOutside(url)
        }
        return nil
    }
}
