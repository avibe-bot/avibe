import SafariServices
import UIKit
import WebKit

final class WebViewController: UIViewController {
    private struct PopupRecord {
        let webView: WKWebView
        let navigationController: UINavigationController
        let parentIdentifier: ObjectIdentifier
    }

    private struct DownloadContext {
        let sourceOrigin: WebOrigin?
        let trustedMainFrame: Bool
    }

    private final class PendingJavaScriptDialog {
        let webViewIdentifier: ObjectIdentifier
        weak var alert: UIAlertController?
        let cancel: () -> Void

        init(
            webViewIdentifier: ObjectIdentifier,
            alert: UIAlertController,
            cancel: @escaping () -> Void
        ) {
            self.webViewIdentifier = webViewIdentifier
            self.alert = alert
            self.cancel = cancel
        }
    }

    private let instanceOrigin: WebOrigin
    private let navigationPolicy: NavigationPolicy
    private let didStartLoading: () -> Void
    private let didFinishLoading: () -> Void
    private let didFailLoading: () -> Void
    private var popupRecords: [ObjectIdentifier: PopupRecord] = [:]
    private var downloadContexts: [ObjectIdentifier: DownloadContext] = [:]
    private var downloadDestinations: [ObjectIdentifier: URL] = [:]
    private var cancelledDownloads: Set<ObjectIdentifier> = []
    private var pendingDownloadedFiles: [URL] = []
    private var isPresentingDownloadedFile = false
    private var pendingDialogs: [UUID: PendingJavaScriptDialog] = [:]
    private var dialogIdentifiersByWebView: [ObjectIdentifier: Set<UUID>] = [:]

    private lazy var webView: WKWebView = {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.allowsInlineMediaPlayback = true

        let webView = WKWebView(frame: .zero, configuration: configuration)
        configure(webView)
        webView.translatesAutoresizingMaskIntoConstraints = false
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

        loadConfiguredInstance()
    }

    override func willMove(toParent parent: UIViewController?) {
        if parent == nil {
            cancelAllPendingDialogs()
            discardAllPopups(animated: false)
            discardPendingDownloadedFiles()
        }
        super.willMove(toParent: parent)
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
        cancelAllPendingDialogs(dismissAlerts: false)
        for record in popupRecords.values {
            record.webView.stopLoading()
            record.webView.navigationDelegate = nil
            record.webView.uiDelegate = nil
        }
        for url in pendingDownloadedFiles {
            removeDownloadedFile(at: url)
        }
    }

    @objc
    private func resetOuterScrollPosition() {
        webView.scrollView.setContentOffset(.zero, animated: false)
    }

    private func configure(_ webView: WKWebView) {
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.scrollView.contentInsetAdjustmentBehavior = .never
    }

    private func loadConfiguredInstance() {
        webView.load(URLRequest(url: instanceOrigin.url))
    }

    private func presentExternalURL(_ url: URL) {
        let host = topViewController(from: self)
        guard host.viewIfLoaded?.window != nil else {
            return
        }
        host.present(SFSafariViewController(url: url), animated: true)
    }

    private func openExternalApplication(_ url: URL) {
        UIApplication.shared.open(url)
    }

    private func presentGoogleSignInMessage() {
        let alert = UIAlertController(
            title: String(localized: "google.title"),
            message: String(localized: "google.body"),
            preferredStyle: .alert
        )
        alert.addAction(
            UIAlertAction(title: String(localized: "common.ok"), style: .default) { [weak self] _ in
                self?.presentNextDownloadedFileAfterDismissal()
            }
        )
        let host = topViewController(from: self)
        guard
            host.viewIfLoaded?.window != nil,
            host.presentedViewController == nil
        else {
            return
        }
        host.present(alert, animated: true)
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
                finishPrimaryLoadingIfNeeded(webView)
                presentGoogleSignInMessage()
            }
            return .cancel
        case .external:
            if isMainFrame {
                finishPrimaryLoadingIfNeeded(webView)
                presentExternalURL(url)
            }
            return .cancel
        case .externalApplication:
            if mainFrameIsTrusted(in: webView) {
                finishPrimaryLoadingIfNeeded(webView)
                openExternalApplication(url)
            }
            return .cancel
        case .blocked:
            if isMainFrame {
                finishPrimaryLoadingIfNeeded(webView)
            }
            return .cancel
        }
    }

    private func finishPrimaryLoadingIfNeeded(_ webView: WKWebView) {
        if webView === self.webView {
            didFinishLoading()
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

    private func webOrigin(for origin: WKSecurityOrigin) -> WebOrigin? {
        WebOrigin(
            scheme: origin.protocol,
            host: origin.host,
            port: origin.port == 0 ? nil : origin.port
        )
    }

    private func mainFrameIsTrusted(in webView: WKWebView) -> Bool {
        guard let url = webView.url else {
            return false
        }
        return WebOrigin(url: url, requiresHTTPS: false) == instanceOrigin
    }

    private func popupPresentationContext(
        for sourceWebView: WKWebView
    ) -> (presenter: UIViewController, parentIdentifier: ObjectIdentifier)? {
        var parentIdentifier = ObjectIdentifier(sourceWebView)
        var presenter: UIViewController

        if sourceWebView === webView {
            presenter = self
        } else if let record = popupRecords[parentIdentifier],
                  let visibleController = record.navigationController.visibleViewController {
            presenter = visibleController
        } else {
            return nil
        }

        while let presentedController = presenter.presentedViewController {
            guard
                let record = popupRecords.values.first(where: {
                    $0.navigationController === presentedController
                }),
                let visibleController = record.navigationController.visibleViewController
            else {
                return nil
            }
            parentIdentifier = ObjectIdentifier(record.webView)
            presenter = visibleController
        }

        guard presenter.viewIfLoaded?.window != nil else {
            return nil
        }
        return (presenter, parentIdentifier)
    }

    private func presentChildWebView(
        using configuration: WKWebViewConfiguration,
        from sourceWebView: WKWebView
    ) -> WKWebView? {
        guard let presentation = popupPresentationContext(for: sourceWebView) else {
            return nil
        }

        let childWebView = WKWebView(frame: .zero, configuration: configuration)
        configure(childWebView)

        let popupController = PopupWebViewController(
            webView: childWebView
        ) { [weak self, weak childWebView] in
            guard let self, let childWebView else {
                return
            }
            self.closePopup(childWebView)
        }
        let navigationController = UINavigationController(rootViewController: popupController)
        navigationController.modalPresentationStyle = .formSheet
        navigationController.isModalInPresentation = true

        let identifier = ObjectIdentifier(childWebView)
        popupRecords[identifier] = PopupRecord(
            webView: childWebView,
            navigationController: navigationController,
            parentIdentifier: presentation.parentIdentifier
        )
        presentation.presenter.present(navigationController, animated: true)
        return childWebView
    }

    private func popupSubtree(startingAt rootIdentifier: ObjectIdentifier) -> [ObjectIdentifier] {
        var result = [rootIdentifier]
        var index = 0
        while index < result.count {
            let parentIdentifier = result[index]
            result.append(
                contentsOf: popupRecords.compactMap { identifier, record in
                    record.parentIdentifier == parentIdentifier ? identifier : nil
                }
            )
            index += 1
        }
        return result
    }

    private func closePopup(_ popupWebView: WKWebView) {
        let identifier = ObjectIdentifier(popupWebView)
        guard let rootRecord = popupRecords[identifier] else {
            return
        }

        for descendantIdentifier in popupSubtree(startingAt: identifier).reversed() {
            guard let record = popupRecords.removeValue(forKey: descendantIdentifier) else {
                continue
            }
            cancelPendingDialogs(for: descendantIdentifier)
            record.webView.stopLoading()
            record.webView.navigationDelegate = nil
            record.webView.uiDelegate = nil
        }
        rootRecord.navigationController.dismiss(animated: true)
    }

    private func discardAllPopups(animated: Bool) {
        let mainIdentifier = ObjectIdentifier(webView)
        let rootControllers = popupRecords.values.compactMap { record in
            record.parentIdentifier == mainIdentifier ? record.navigationController : nil
        }
        for controller in rootControllers {
            controller.dismiss(animated: animated)
        }
        for (identifier, record) in popupRecords {
            cancelPendingDialogs(for: identifier)
            record.webView.stopLoading()
            record.webView.navigationDelegate = nil
            record.webView.uiDelegate = nil
        }
        popupRecords.removeAll()
    }

    private func downloadContext(
        for webView: WKWebView,
        sourceOrigin: WebOrigin?
    ) -> DownloadContext {
        DownloadContext(
            sourceOrigin: sourceOrigin,
            trustedMainFrame: mainFrameIsTrusted(in: webView)
        )
    }

    private func registerDownload(_ download: WKDownload, context: DownloadContext) {
        downloadContexts[ObjectIdentifier(download)] = context
        download.delegate = self
    }

    private func beginDownload(
        _ request: URLRequest,
        in webView: WKWebView,
        sourceOrigin: WebOrigin?
    ) {
        let context = downloadContext(for: webView, sourceOrigin: sourceOrigin)
        webView.startDownload(using: request) { [weak self] download in
            self?.registerDownload(download, context: context)
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
        let illegalCharacters = CharacterSet(charactersIn: "/\\:")
            .union(.controlCharacters)
        let sanitizedFilename = suggestedFilename
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .unicodeScalars
            .map { illegalCharacters.contains($0) ? "-" : String($0) }
            .joined()
            .trimmingCharacters(in: CharacterSet(charactersIn: ". "))
        let filename = sanitizedFilename.isEmpty
            ? "Download"
            : String(sanitizedFilename.prefix(180))
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

    private func enqueueDownloadedFile(at url: URL) {
        pendingDownloadedFiles.append(url)
        presentNextDownloadedFile()
    }

    private func presentNextDownloadedFile() {
        guard
            !isPresentingDownloadedFile,
            let url = pendingDownloadedFiles.first
        else {
            return
        }

        let host = topViewController(from: self)
        guard
            host.viewIfLoaded?.window != nil,
            !(host is UIAlertController),
            !(host is UIActivityViewController)
        else {
            return
        }

        let activityController = UIActivityViewController(
            activityItems: [url],
            applicationActivities: nil
        )
        activityController.completionWithItemsHandler = { [weak self] _, _, _, _ in
            guard let self else {
                return
            }
            self.removeDownloadedFile(at: url)
            if self.pendingDownloadedFiles.first == url {
                self.pendingDownloadedFiles.removeFirst()
            } else {
                self.pendingDownloadedFiles.removeAll { $0 == url }
            }
            self.isPresentingDownloadedFile = false
            self.presentNextDownloadedFile()
        }

        if let popover = activityController.popoverPresentationController {
            popover.sourceView = host.view
            popover.sourceRect = CGRect(
                x: host.view.bounds.midX,
                y: host.view.bounds.midY,
                width: 1,
                height: 1
            )
        }
        isPresentingDownloadedFile = true
        host.present(activityController, animated: true)
    }

    private func presentNextDownloadedFileAfterDismissal() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { [weak self] in
            self?.presentNextDownloadedFile()
        }
    }

    private func discardPendingDownloadedFiles() {
        for url in pendingDownloadedFiles {
            removeDownloadedFile(at: url)
        }
        pendingDownloadedFiles.removeAll()
        isPresentingDownloadedFile = false
    }

    private func presentDownloadFailure() {
        let host = topViewController(from: self)
        guard
            host.viewIfLoaded?.window != nil,
            host.presentedViewController == nil,
            !(host is UIAlertController),
            !(host is UIActivityViewController)
        else {
            return
        }
        let alert = UIAlertController(
            title: String(localized: "download.failed.title"),
            message: String(localized: "download.failed.body"),
            preferredStyle: .alert
        )
        alert.addAction(
            UIAlertAction(title: String(localized: "common.ok"), style: .default) { [weak self] _ in
                self?.presentNextDownloadedFileAfterDismissal()
            }
        )
        host.present(alert, animated: true)
    }

    private func dialogTitle(key: String.LocalizationValue, frame: WKFrameInfo) -> String {
        let hostName = frame.request.url?.host ?? instanceOrigin.host
        return String(
            format: String(localized: key),
            locale: .current,
            hostName
        )
    }

    private func dialogPresenter(for webView: WKWebView) -> UIViewController? {
        let presenter: UIViewController
        if webView === self.webView {
            presenter = self
        } else if let record = popupRecords[ObjectIdentifier(webView)],
                  let visibleController = record.navigationController.visibleViewController {
            presenter = visibleController
        } else {
            return nil
        }

        guard
            presenter.viewIfLoaded?.window != nil,
            presenter.presentedViewController == nil
        else {
            return nil
        }
        return presenter
    }

    private func registerDialog(
        for webView: WKWebView,
        alert: UIAlertController,
        cancel: @escaping () -> Void
    ) -> UUID {
        let identifier = UUID()
        let webViewIdentifier = ObjectIdentifier(webView)
        pendingDialogs[identifier] = PendingJavaScriptDialog(
            webViewIdentifier: webViewIdentifier,
            alert: alert,
            cancel: cancel
        )
        dialogIdentifiersByWebView[webViewIdentifier, default: []].insert(identifier)
        return identifier
    }

    private func takeDialog(_ identifier: UUID) -> PendingJavaScriptDialog? {
        guard let dialog = pendingDialogs.removeValue(forKey: identifier) else {
            return nil
        }
        dialogIdentifiersByWebView[dialog.webViewIdentifier]?.remove(identifier)
        if dialogIdentifiersByWebView[dialog.webViewIdentifier]?.isEmpty == true {
            dialogIdentifiersByWebView.removeValue(forKey: dialog.webViewIdentifier)
        }
        return dialog
    }

    private func resolveDialog(_ identifier: UUID, completion: () -> Void) {
        guard takeDialog(identifier) != nil else {
            return
        }
        completion()
        presentNextDownloadedFileAfterDismissal()
    }

    private func cancelPendingDialogs(
        for webViewIdentifier: ObjectIdentifier,
        dismissAlerts: Bool = true
    ) {
        let identifiers = dialogIdentifiersByWebView[webViewIdentifier] ?? []
        for identifier in identifiers {
            guard let dialog = takeDialog(identifier) else {
                continue
            }
            if dismissAlerts {
                dialog.alert?.dismiss(animated: false)
            }
            dialog.cancel()
        }
        presentNextDownloadedFileAfterDismissal()
    }

    private func cancelPendingDialogs(
        for webView: WKWebView,
        dismissAlerts: Bool = true
    ) {
        cancelPendingDialogs(
            for: ObjectIdentifier(webView),
            dismissAlerts: dismissAlerts
        )
    }

    private func cancelAllPendingDialogs(dismissAlerts: Bool = true) {
        for webViewIdentifier in Array(dialogIdentifiersByWebView.keys) {
            cancelPendingDialogs(
                for: webViewIdentifier,
                dismissAlerts: dismissAlerts
            )
        }
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

        if navigationAction.shouldPerformDownload {
            let sourceOrigin = webOrigin(for: navigationAction.sourceFrame.securityOrigin)
            let context = downloadContext(for: webView, sourceOrigin: sourceOrigin)
            switch navigationPolicy.downloadDisposition(
                for: url,
                sourceOrigin: sourceOrigin,
                trustedMainFrame: context.trustedMainFrame
            ) {
            case .download:
                decisionHandler(.download)
            case .externalBrowser:
                decisionHandler(.cancel)
                presentExternalURL(url)
            case .blocked:
                decisionHandler(.cancel)
            }
            return
        }

        if navigationAction.targetFrame == nil {
            decisionHandler(.allow)
            return
        }

        decisionHandler(
            navigationDecision(
                for: url,
                in: webView,
                isMainFrame: navigationAction.targetFrame?.isMainFrame == true
            )
        )
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationResponse: WKNavigationResponse,
        decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void
    ) {
        guard
            isAttachment(navigationResponse.response) || !navigationResponse.canShowMIMEType
        else {
            decisionHandler(.allow)
            return
        }
        guard let url = navigationResponse.response.url else {
            decisionHandler(.cancel)
            return
        }

        switch navigationPolicy.downloadDisposition(
            for: url,
            sourceOrigin: nil,
            trustedMainFrame: mainFrameIsTrusted(in: webView)
        ) {
        case .download:
            decisionHandler(.download)
        case .externalBrowser:
            decisionHandler(.cancel)
            presentExternalURL(url)
        case .blocked:
            decisionHandler(.cancel)
        }
    }

    func webView(
        _ webView: WKWebView,
        navigationAction: WKNavigationAction,
        didBecome download: WKDownload
    ) {
        registerDownload(
            download,
            context: downloadContext(
                for: webView,
                sourceOrigin: webOrigin(for: navigationAction.sourceFrame.securityOrigin)
            )
        )
    }

    func webView(
        _ webView: WKWebView,
        navigationResponse: WKNavigationResponse,
        didBecome download: WKDownload
    ) {
        registerDownload(
            download,
            context: downloadContext(for: webView, sourceOrigin: nil)
        )
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation?) {
        cancelPendingDialogs(for: webView)
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
        cancelPendingDialogs(for: webView)
        if webView === self.webView {
            discardAllPopups(animated: false)
            loadConfiguredInstance()
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
        runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping () -> Void
    ) {
        let alert = UIAlertController(
            title: dialogTitle(key: "browser.alert.title", frame: frame),
            message: message,
            preferredStyle: .alert
        )
        let identifier = registerDialog(
            for: webView,
            alert: alert,
            cancel: completionHandler
        )
        alert.addAction(
            UIAlertAction(title: String(localized: "common.ok"), style: .default) { [weak self] _ in
                self?.resolveDialog(identifier, completion: completionHandler)
            }
        )

        guard let presenter = dialogPresenter(for: webView) else {
            resolveDialog(identifier, completion: completionHandler)
            return
        }
        presenter.present(alert, animated: true)
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (Bool) -> Void
    ) {
        let alert = UIAlertController(
            title: dialogTitle(key: "browser.confirm.title", frame: frame),
            message: message,
            preferredStyle: .alert
        )
        let identifier = registerDialog(
            for: webView,
            alert: alert,
            cancel: { completionHandler(false) }
        )
        alert.addAction(
            UIAlertAction(title: String(localized: "common.cancel"), style: .cancel) { [weak self] _ in
                self?.resolveDialog(identifier) {
                    completionHandler(false)
                }
            }
        )
        alert.addAction(
            UIAlertAction(title: String(localized: "common.ok"), style: .default) { [weak self] _ in
                self?.resolveDialog(identifier) {
                    completionHandler(true)
                }
            }
        )

        guard let presenter = dialogPresenter(for: webView) else {
            resolveDialog(identifier) {
                completionHandler(false)
            }
            return
        }
        presenter.present(alert, animated: true)
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptTextInputPanelWithPrompt prompt: String,
        defaultText: String?,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (String?) -> Void
    ) {
        let alert = UIAlertController(
            title: dialogTitle(key: "browser.prompt.title", frame: frame),
            message: prompt,
            preferredStyle: .alert
        )
        alert.addTextField { textField in
            textField.text = defaultText
        }
        let identifier = registerDialog(
            for: webView,
            alert: alert,
            cancel: { completionHandler(nil) }
        )
        alert.addAction(
            UIAlertAction(title: String(localized: "common.cancel"), style: .cancel) { [weak self] _ in
                self?.resolveDialog(identifier) {
                    completionHandler(nil)
                }
            }
        )
        alert.addAction(
            UIAlertAction(title: String(localized: "common.ok"), style: .default) { [weak self, weak alert] _ in
                let text = alert?.textFields?.first?.text ?? ""
                self?.resolveDialog(identifier) {
                    completionHandler(text)
                }
            }
        )

        guard let presenter = dialogPresenter(for: webView) else {
            resolveDialog(identifier) {
                completionHandler(nil)
            }
            return
        }
        presenter.present(alert, animated: true)
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

        let sourceOrigin = webOrigin(for: navigationAction.sourceFrame.securityOrigin)
        let trustedMainFrame = mainFrameIsTrusted(in: webView)
        if navigationAction.shouldPerformDownload {
            switch navigationPolicy.downloadDisposition(
                for: url,
                sourceOrigin: sourceOrigin,
                trustedMainFrame: trustedMainFrame
            ) {
            case .download:
                beginDownload(
                    navigationAction.request,
                    in: webView,
                    sourceOrigin: sourceOrigin
                )
            case .externalBrowser:
                presentExternalURL(url)
            case .blocked:
                break
            }
            return nil
        }

        switch navigationPolicy.newWindowDisposition(
            for: url,
            trustedMainFrame: trustedMainFrame
        ) {
        case .childWebView:
            return presentChildWebView(using: configuration, from: webView)
        case .externalBrowser:
            presentExternalURL(url)
        case .externalApplication:
            openExternalApplication(url)
        case .googleAuthentication:
            presentGoogleSignInMessage()
        case .blocked:
            break
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
            downloadContexts.removeValue(forKey: ObjectIdentifier(download))
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
        let identifier = ObjectIdentifier(download)
        guard
            let context = downloadContexts[identifier],
            let url = request.url
        else {
            cancelledDownloads.insert(identifier)
            decisionHandler(.cancel)
            return
        }

        switch navigationPolicy.downloadDisposition(
            for: url,
            sourceOrigin: context.sourceOrigin,
            trustedMainFrame: context.trustedMainFrame
        ) {
        case .download:
            decisionHandler(.allow)
        case .externalBrowser:
            cancelledDownloads.insert(identifier)
            decisionHandler(.cancel)
            presentExternalURL(url)
        case .blocked:
            cancelledDownloads.insert(identifier)
            decisionHandler(.cancel)
        }
    }

    func downloadDidFinish(_ download: WKDownload) {
        let identifier = ObjectIdentifier(download)
        downloadContexts.removeValue(forKey: identifier)
        cancelledDownloads.remove(identifier)
        guard let destination = downloadDestinations.removeValue(forKey: identifier) else {
            return
        }
        enqueueDownloadedFile(at: destination)
    }

    func download(
        _ download: WKDownload,
        didFailWithError error: Error,
        resumeData: Data?
    ) {
        let identifier = ObjectIdentifier(download)
        downloadContexts.removeValue(forKey: identifier)
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
    private var didRequestClose = false

    init(webView: WKWebView, onClose: @escaping () -> Void) {
        self.webView = webView
        self.onClose = onClose
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        if isBeingDismissed || navigationController?.isBeingDismissed == true {
            requestClose()
        }
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
                self?.requestClose()
            }
        )
    }

    private func requestClose() {
        guard !didRequestClose else {
            return
        }
        didRequestClose = true
        onClose()
    }
}
