import SafariServices
import UIKit
import WebKit

final class WebViewController: UIViewController {
    private let instanceOrigin: WebOrigin
    private let navigationPolicy: NavigationPolicy
    private let didStartLoading: () -> Void
    private let didFinishLoading: () -> Void
    private let didFailLoading: () -> Void
    private var popupControllers: [ObjectIdentifier: UIViewController] = [:]
    private var downloadDestinations: [ObjectIdentifier: URL] = [:]
    private var cancelledDownloads: Set<ObjectIdentifier> = []

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
        topViewController(from: self).present(
            SFSafariViewController(url: url),
            animated: true
        )
    }

    private func presentGoogleSignInMessage() {
        let alert = UIAlertController(
            title: String(localized: "google.title"),
            message: String(localized: "google.body"),
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: String(localized: "common.ok"), style: .default))
        topViewController(from: self).present(alert, animated: true)
    }

    private func navigationContext(for webView: WKWebView, isMainFrame: Bool) -> NavigationContext {
        if !isMainFrame {
            return .embeddedFrame
        }
        return webView === self.webView ? .primaryMainFrame : .popupMainFrame
    }

    private func navigationDecision(
        for url: URL,
        in webView: WKWebView,
        isMainFrame: Bool
    ) -> WKNavigationActionPolicy {
        let context = navigationContext(for: webView, isMainFrame: isMainFrame)
        switch navigationPolicy.disposition(for: url, context: context) {
        case .allow:
            return .allow
        case .googleAuthentication:
            if isMainFrame {
                if webView === self.webView {
                    didFinishLoading()
                }
                presentGoogleSignInMessage()
            }
            return .cancel
        case .external:
            if isMainFrame {
                if webView === self.webView {
                    didFinishLoading()
                }
                presentExternalURL(url)
            }
            return .cancel
        case .blocked:
            if isMainFrame && webView === self.webView {
                didFinishLoading()
            }
            return .cancel
        }
    }

    private func reportLoadFailure(_ error: Error, in webView: WKWebView) {
        let error = error as NSError
        guard error.domain != NSURLErrorDomain || error.code != NSURLErrorCancelled else {
            return
        }
        if webView === self.webView {
            didFailLoading()
        }
    }

    private func isTrustedPopupRequest(_ navigationAction: WKNavigationAction) -> Bool {
        navigationAction.sourceFrame.isMainFrame
            && webOrigin(for: navigationAction.sourceFrame.securityOrigin) == instanceOrigin
    }

    private func webOrigin(for origin: WKSecurityOrigin) -> WebOrigin? {
        WebOrigin(
            scheme: origin.protocol,
            host: origin.host,
            port: origin.port == 0 ? nil : origin.port
        )
    }

    private func presentPopup(using configuration: WKWebViewConfiguration) -> WKWebView? {
        guard presentedViewController == nil else {
            return nil
        }

        let popupWebView = WKWebView(frame: .zero, configuration: configuration)
        popupWebView.navigationDelegate = self
        popupWebView.uiDelegate = self
        popupWebView.scrollView.contentInsetAdjustmentBehavior = .never

        let popupController = PopupWebViewController(webView: popupWebView) { [weak self, weak popupWebView] in
            guard let self, let popupWebView else {
                return
            }
            popupWebView.evaluateJavaScript("window.close()") { [weak self, weak popupWebView] _, _ in
                guard let self, let popupWebView else {
                    return
                }
                self.closePopup(popupWebView)
            }
        }
        let navigationController = UINavigationController(rootViewController: popupController)
        navigationController.modalPresentationStyle = .formSheet
        navigationController.isModalInPresentation = true
        popupControllers[ObjectIdentifier(popupWebView)] = navigationController
        present(navigationController, animated: true)
        return popupWebView
    }

    private func closePopup(_ popupWebView: WKWebView) {
        let identifier = ObjectIdentifier(popupWebView)
        guard let controller = popupControllers.removeValue(forKey: identifier) else {
            return
        }
        popupWebView.stopLoading()
        popupWebView.navigationDelegate = nil
        popupWebView.uiDelegate = nil
        controller.dismiss(animated: true)
    }

    private func beginDownload(_ request: URLRequest, in webView: WKWebView) {
        guard let url = request.url, navigationPolicy.permitsDownload(from: url) else {
            return
        }
        webView.startDownload(using: request) { [weak self] download in
            download.delegate = self
        }
    }

    private func isAttachment(_ response: URLResponse) -> Bool {
        guard let response = response as? HTTPURLResponse else {
            return false
        }
        return response.value(forHTTPHeaderField: "Content-Disposition")?
            .lowercased()
            .contains("attachment") == true
    }

    private func downloadDestination(for suggestedFilename: String) throws -> URL {
        let trimmedFilename = suggestedFilename.trimmingCharacters(in: .whitespacesAndNewlines)
        let safeFilename = trimmedFilename
            .replacingOccurrences(of: "/", with: "-")
            .replacingOccurrences(of: "\\", with: "-")
        let filename = safeFilename.isEmpty ? "Download" : safeFilename
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("Avibe-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true
        )
        return directory.appendingPathComponent(filename, isDirectory: false)
    }

    private func removeDownloadedFile(at url: URL) {
        try? FileManager.default.removeItem(at: url.deletingLastPathComponent())
    }

    private func topViewController(from controller: UIViewController) -> UIViewController {
        if let presented = controller.presentedViewController {
            return topViewController(from: presented)
        }
        if let navigationController = controller as? UINavigationController,
           let visible = navigationController.visibleViewController {
            return topViewController(from: visible)
        }
        return controller
    }

    private func presentDownloadedFile(at url: URL) {
        let activityController = UIActivityViewController(
            activityItems: [url],
            applicationActivities: nil
        )
        activityController.completionWithItemsHandler = { [weak self] _, _, _, _ in
            self?.removeDownloadedFile(at: url)
        }

        let host = topViewController(from: self)
        if let popover = activityController.popoverPresentationController {
            popover.sourceView = host.view
            popover.sourceRect = CGRect(
                x: host.view.bounds.midX,
                y: host.view.bounds.midY,
                width: 1,
                height: 1
            )
        }
        host.present(activityController, animated: true)
    }

    private func presentDownloadFailure() {
        let host = topViewController(from: self)
        guard host.presentedViewController == nil else {
            return
        }
        let alert = UIAlertController(
            title: String(localized: "download.failed.title"),
            message: String(localized: "download.failed.body"),
            preferredStyle: .alert
        )
        alert.addAction(UIAlertAction(title: String(localized: "common.ok"), style: .default))
        host.present(alert, animated: true)
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
        if navigationAction.shouldPerformDownload && navigationPolicy.permitsDownload(from: url) {
            decisionHandler(.download)
            return
        }
        decisionHandler(
            navigationDecision(
                for: url,
                in: webView,
                isMainFrame: isMainFrame
            )
        )
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationResponse: WKNavigationResponse,
        decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void
    ) {
        guard
            navigationResponse.isForMainFrame,
            let url = navigationResponse.response.url,
            navigationPolicy.permitsDownload(from: url),
            isAttachment(navigationResponse.response) || !navigationResponse.canShowMIMEType
        else {
            decisionHandler(.allow)
            return
        }
        decisionHandler(.download)
    }

    func webView(
        _ webView: WKWebView,
        navigationAction: WKNavigationAction,
        didBecome download: WKDownload
    ) {
        download.delegate = self
    }

    func webView(
        _ webView: WKWebView,
        navigationResponse: WKNavigationResponse,
        didBecome download: WKDownload
    ) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation?) {
        if webView === self.webView {
            didStartLoading()
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation?) {
        if webView === self.webView {
            didFinishLoading()
        }
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation?,
        withError error: Error
    ) {
        reportLoadFailure(error, in: webView)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation?, withError error: Error) {
        reportLoadFailure(error, in: webView)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        if webView === self.webView {
            didFailLoading()
        } else {
            closePopup(webView)
        }
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
        let allowed = frame.isMainFrame
            && type == .microphone
            && webOrigin(for: origin) == instanceOrigin
        decisionHandler(allowed ? .grant : .deny)
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (Bool) -> Void
    ) {
        let hostName = frame.request.url?.host ?? instanceOrigin.host
        let title = String(
            format: String(localized: "browser.confirm.title"),
            locale: .current,
            hostName
        )
        let alert = UIAlertController(
            title: title,
            message: message,
            preferredStyle: .alert
        )
        alert.addAction(
            UIAlertAction(title: String(localized: "common.cancel"), style: .cancel) { _ in
                completionHandler(false)
            }
        )
        alert.addAction(
            UIAlertAction(title: String(localized: "common.ok"), style: .default) { _ in
                completionHandler(true)
            }
        )

        let host = topViewController(from: self)
        guard host.presentedViewController == nil else {
            completionHandler(false)
            return
        }
        host.present(alert, animated: true)
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
        if navigationAction.shouldPerformDownload && navigationPolicy.permitsDownload(from: url) {
            beginDownload(navigationAction.request, in: webView)
            return nil
        }
        if navigationPolicy.isPopupBootstrap(url) && isTrustedPopupRequest(navigationAction) {
            return presentPopup(using: configuration)
        }
        if navigationDecision(for: url, in: webView, isMainFrame: true) == .allow {
            webView.load(navigationAction.request)
        }
        return nil
    }

    func webViewDidClose(_ webView: WKWebView) {
        closePopup(webView)
    }
}

extension WebViewController: WKDownloadDelegate {
    func download(
        _ download: WKDownload,
        decideDestinationUsing response: URLResponse,
        suggestedFilename: String,
        completionHandler: @escaping (URL?) -> Void
    ) {
        do {
            let destination = try downloadDestination(for: suggestedFilename)
            downloadDestinations[ObjectIdentifier(download)] = destination
            completionHandler(destination)
        } catch {
            completionHandler(nil)
            presentDownloadFailure()
        }
    }

    func download(
        _ download: WKDownload,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        decisionHandler: @escaping (WKDownload.RedirectPolicy) -> Void
    ) {
        guard let url = request.url else {
            decisionHandler(.cancel)
            return
        }

        switch navigationPolicy.destination(for: url) {
        case .instance:
            decisionHandler(.allow)
        case .authentication:
            cancelledDownloads.insert(ObjectIdentifier(download))
            decisionHandler(.cancel)
            webView.load(request)
        case .googleAuthentication:
            cancelledDownloads.insert(ObjectIdentifier(download))
            decisionHandler(.cancel)
            presentGoogleSignInMessage()
        case .protectedVault, .external:
            cancelledDownloads.insert(ObjectIdentifier(download))
            decisionHandler(.cancel)
            presentExternalURL(url)
        case .blocked:
            cancelledDownloads.insert(ObjectIdentifier(download))
            decisionHandler(.cancel)
        }
    }

    func downloadDidFinish(_ download: WKDownload) {
        let identifier = ObjectIdentifier(download)
        guard let destination = downloadDestinations.removeValue(forKey: identifier) else {
            return
        }
        cancelledDownloads.remove(identifier)
        presentDownloadedFile(at: destination)
    }

    func download(
        _ download: WKDownload,
        didFailWithError error: Error,
        resumeData: Data?
    ) {
        let identifier = ObjectIdentifier(download)
        if let destination = downloadDestinations.removeValue(forKey: identifier) {
            removeDownloadedFile(at: destination)
        }
        if cancelledDownloads.remove(identifier) != nil {
            return
        }
        let error = error as NSError
        if error.domain == NSURLErrorDomain && error.code == NSURLErrorCancelled {
            return
        }
        presentDownloadFailure()
    }
}

private final class PopupWebViewController: UIViewController {
    private let webView: WKWebView
    private let onClose: () -> Void

    init(webView: WKWebView, onClose: @escaping () -> Void) {
        self.webView = webView
        self.onClose = onClose
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        webView.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        navigationItem.leftBarButtonItem = UIBarButtonItem(
            systemItem: .close,
            primaryAction: UIAction { [weak self] _ in
                self?.onClose()
            }
        )
    }
}
